"""Measure inference against schemas whose real answer is already declared (#27).

Every ranking decision runs through constants chosen by judgement — 17 of them, plus
four thresholds. They work, and nobody can say what any of them is worth, because it
has never been measured. Six releases in one week each changed what evidence reaches
the scorer; each was verified against its own case and none against the whole.

The trick that makes measuring cheap: a database that *declares* its keys is already
labelled. Strip the declarations, run inference over what is left, and compare what
comes back with what was removed. Chinook, Northwind and Pagila give 55 tables, 63
foreign keys and 47 primary keys for the price of loading three SQL dumps.

This module is the harness only. It asserts nothing about what good looks like — the
first job is to state where we are, not to defend it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Union

from relational_schema_analyzer.fk_inference import InferenceOptions, infer_foreign_keys
from relational_schema_analyzer.types import PhysicalSchema

#: A foreign key, normalized for comparison: case folded, column order preserved.
#: Order matters — ``(a, b) -> (x, y)`` is a different key from ``(b, a) -> (x, y)``.
Ref = tuple[str, tuple[str, ...], str, tuple[str, ...]]

#: A primary key: ``(table, columns)``. A different shape from :data:`Ref`, which is
#: why :class:`Score` is generic over what it holds rather than claiming everything is
#: a ``Ref`` and silencing the type checker where it is not.
Key = tuple[str, tuple[str, ...]]

Item = Union[Ref, Key]


def _ref(table: str, cols: Iterable[str], ftable: str, fcols: Iterable[str]) -> Ref:
    return (
        table.lower(),
        tuple(c.lower() for c in cols),
        ftable.lower(),
        tuple(c.lower() for c in fcols),
    )


def declared_refs(schema: PhysicalSchema) -> set[Ref]:
    """The foreign keys the database itself declares — the ground truth."""
    return {
        _ref(name, fk.columns, fk.foreign_table, fk.foreign_columns)
        for name, table in schema.tables.items()
        for fk in table.foreign_keys
    }


def without_foreign_keys(schema: PhysicalSchema) -> PhysicalSchema:
    """A copy with every foreign key removed, primary and unique keys intact.

    The realistic shape of the problem: a warehouse that landed its tables without
    constraints but kept the keys. Removing the primary keys as well is a different
    and harder question — see :func:`without_any_keys`.
    """
    stripped = schema.model_copy(deep=True)
    for table in stripped.tables.values():
        table.foreign_keys = []
    return stripped


def without_any_keys(schema: PhysicalSchema) -> PhysicalSchema:
    """A copy with foreign, primary and unique keys all removed.

    What a constraint-free source actually looks like. Inference alone can do nothing
    here — there are no targets — so this is the case key profiling exists for.
    """
    stripped = without_foreign_keys(schema)
    for table in stripped.tables.values():
        table.primary_key = []
        table.unique_constraints = []
    return stripped


@dataclass
class Score:
    """What inference got right, wrong, and missed, against a known answer."""

    corpus: str
    expected: int
    proposed: int
    correct: int
    missed: set[Item] = field(default_factory=set)
    spurious: set[Item] = field(default_factory=set)
    #: Answers the engine **declined** to give — an empty table, a spent probe
    #: budget, a query that failed. Counted apart from ``missed`` and excluded from
    #: ``recall``'s denominator, because "I could not check" is not "there is
    #: nothing there". That distinction is load-bearing everywhere else in this
    #: codebase, and scoring it as a miss punishes the engine for being honest.
    #: Measured: Pagila loaded without its data reported recall 0.000 across 22
    #: tables the profiler had correctly reported as "empty table".
    declined: dict[str, str] = field(default_factory=dict)

    @property
    def precision(self) -> Optional[float]:
        """Of what it proposed, how much was real — or ``None`` if it proposed nothing.

        Undefined, not perfect. Returning 1.0 for an empty proposal would score a
        change that breaks inference entirely as ``precision=1.000, recall=0.000``:
        the instrument built to catch regressions reporting the largest possible one
        as half-right. An undefined ratio has to read as undefined.
        """
        return self.correct / self.proposed if self.proposed else None

    @property
    def evaluated(self) -> int:
        """Known answers the engine actually attempted — the honest denominator."""
        return self.expected - len(self.declined)

    @property
    def recall(self) -> Optional[float]:
        """Of what it attempted, how much it found — ``None`` if it attempted nothing.

        A schema that declares nothing has no answer to measure against, and RSA's
        target domain is full of them: scoring one 1.000 while the engine proposes
        keys that are in no truth set is the most flattering possible lie.

        Declined answers are out of the denominator. An empty table is not a key the
        engine failed to find; it is a question it correctly refused to guess at.
        """
        return self.correct / self.evaluated if self.evaluated > 0 else None

    @property
    def f1(self) -> Optional[float]:
        """``None`` unless both precision and recall are defined and not both zero."""
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    @staticmethod
    def _fmt(value: Optional[float]) -> str:
        return "  n/a" if value is None else f"{value:.3f}"

    def line(self) -> str:
        declined = f" declined={len(self.declined):3}" if self.declined else ""
        return (
            f"{self.corpus:12} expected={self.expected:3} proposed={self.proposed:3} "
            f"correct={self.correct:3}{declined}  precision={self._fmt(self.precision)} "
            f"recall={self._fmt(self.recall)} f1={self._fmt(self.f1)}"
        )


def score_fk_inference(
    schema: PhysicalSchema,
    *,
    corpus: str,
    options: Optional[InferenceOptions] = None,
    sampler: Any = None,
) -> Score:
    """Strip the declared foreign keys, re-infer them, and score the result."""
    truth = declared_refs(schema)
    candidates = infer_foreign_keys(
        without_foreign_keys(schema), options=options, sampler=sampler
    )
    found = {
        _ref(c.table, c.columns, c.foreign_table, c.foreign_columns) for c in candidates
    }
    correct = truth & found
    return Score(
        corpus=corpus,
        expected=len(truth),
        proposed=len(found),
        correct=len(correct),
        missed=truth - found,
        spurious=found - truth,
    )


def score_primary_keys(schema: PhysicalSchema, profile: Any, *, corpus: str) -> Score:
    """Score proposed primary keys against the ones the database declares.

    ``profile`` is a :class:`~relational_schema_analyzer.key_profiling.KeyProfile` built
    over a key-stripped copy. Only tables that actually declared a primary key count:
    a table without one has no answer to be right or wrong about.
    """
    truth = {
        (name.lower(), tuple(c.lower() for c in t.primary_key))
        for name, t in schema.tables.items()
        if t.primary_key
    }
    proposed = set()
    for tname in profile.candidates:
        best = profile.best(tname)
        if best is not None:
            proposed.add((tname.lower(), tuple(c.lower() for c in best.columns)))
    correct = truth & proposed
    # A table the profiler declined to judge is not a table it got wrong.
    declared = {name.lower() for name, t in schema.tables.items() if t.primary_key}
    declined = {
        tname.lower(): reason
        for tname, reason in getattr(profile, "not_evaluated", {}).items()
        if tname.lower() in declared
    }
    return Score(
        corpus=corpus,
        expected=len(truth),
        proposed=len(proposed),
        correct=len(correct),
        missed={t for t in truth if t not in correct and t[0] not in declined},
        spurious={p for p in proposed if p not in correct},
        declined=declined,
    )


def total(scores: Iterable[Score], *, corpus: str = "ALL") -> Score:
    """Pool several corpora into one score, so a small corpus cannot dominate."""
    scores = list(scores)
    return Score(
        corpus=corpus,
        expected=sum(s.expected for s in scores),
        proposed=sum(s.proposed for s in scores),
        correct=sum(s.correct for s in scores),
        missed=set().union(*(s.missed for s in scores)) if scores else set(),
        spurious=set().union(*(s.spurious for s in scores)) if scores else set(),
        declined={k: v for s in scores for k, v in s.declined.items()},
    )
