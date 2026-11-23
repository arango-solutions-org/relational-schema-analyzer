"""Type-discriminator detection.

One detector closes two gaps at once (see ``docs/DESIGN-ADDENDUM-taxonomy.md`` §2):

* **single-table inheritance** — one ``accounts`` table with an ``account_type`` column, the
  whole taxonomy encoded in that column's values.
* **the discriminator half of specialization** — a supertype table that carries the common
  columns *and* a type column, alongside subtype tables joined on shared PK. RSA already
  detects the shared-PK edges (:func:`is_shared_pk_fk`); without the discriminator it emits four
  independent ``subClassOf`` assertions rather than one closed specialization.

Two sources, and the first is strictly better:

1. **Declared** — a ``CHECK (col IN (...))`` constraint already parsed into
   ``CheckConstraint.enum_values``. Exact, free, no database access. This is the relational
   equivalent of preferring a declared edge definition over sampled endpoints.
2. **Sampled** — distinct values read from the source. Requires an injected enumerator and is
   therefore opt-in, matching how ``fk_inference`` treats value overlap.

The acceptance gates mirror ``arangodb-schema-analyzer``'s ``type_detection``: a bounded
distinct count, adequate coverage, and label-shaped values. Keeping them aligned matters —
both analyzers feed the same shared taxonomy library, and a column accepted by one and
rejected by the other would make the conceptual model depend on which side ran.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Optional

from .types import ForeignKey, Schema, Table

DiscriminatorSource = Literal["check_constraint", "sampled"]

#: ``enumerator(table, column, limit) -> distinct values, or None if not evaluable``
ValueEnumerator = Callable[[str, str, int], Optional[list[str]]]

_LABEL_VALUE = re.compile(r"^[A-Za-z0-9_\-]+$")

#: Column-name stems that plausibly hold a type tag. Matched as a whole name or as a
#: trailing ``_``-separated word, so ``account_type`` and ``type`` hit while ``typography``
#: and ``prototype_id`` do not.
_NAME_STEMS: tuple[str, ...] = (
    "type",
    "kind",
    "category",
    "class",
    "subtype",
    "variant",
    "discriminator",
    "role",
    "flavor",
    "flavour",
)


@dataclass
class DiscriminatorOptions:
    min_distinct: int = 2
    max_distinct: int = 32
    #: Fraction of rows carrying a non-null value. Only checkable on the sampled path.
    min_coverage: float = 0.5
    max_value_length: int = 64
    sample_limit: int = 64
    name_stems: tuple[str, ...] = _NAME_STEMS


@dataclass
class DiscriminatorCandidate:
    table: str
    column: str
    values: list[str]
    source: DiscriminatorSource
    confidence: float
    evidence: list[str] = field(default_factory=list)

    @property
    def is_declared(self) -> bool:
        return self.source == "check_constraint"


def looks_like_discriminator_name(column: str, stems: tuple[str, ...] = _NAME_STEMS) -> bool:
    """Whole-name or trailing-word match against the known stems."""
    lowered = column.lower()
    if lowered in stems:
        return True
    return any(lowered.endswith(f"_{stem}") for stem in stems)


def _is_structural(table: Table, column_name: str) -> bool:
    """Keys and references are never type tags, whatever they are called."""
    if column_name in table.primary_key:
        return True
    if any(column_name in fk.columns for fk in table.foreign_keys):
        return True
    return any(column_name in cols for cols in table.unique_constraints)


def _values_are_labels(values: list[str], options: DiscriminatorOptions) -> bool:
    """Free-form content is not a type tag."""
    return all(
        isinstance(v, str) and 0 < len(v) <= options.max_value_length and _LABEL_VALUE.match(v)
        for v in values
    )


def _accept(values: list[str], options: DiscriminatorOptions) -> bool:
    distinct = sorted(set(values))
    if not options.min_distinct <= len(distinct) <= options.max_distinct:
        return False
    return _values_are_labels(distinct, options)


def _declared_candidates(
    table: Table, options: DiscriminatorOptions
) -> list[DiscriminatorCandidate]:
    out: list[DiscriminatorCandidate] = []
    for check in table.check_constraints:
        if not check.enum_values or len(check.columns) != 1:
            continue
        column = check.columns[0]
        if _is_structural(table, column):
            continue
        values = sorted({str(v) for v in check.enum_values})
        if not _accept(values, options):
            continue
        # A declaration is evidence in itself: the schema author stated the value set, so no
        # name affinity is required. `status`-style columns that happen to be enumerated are
        # accepted here and filtered downstream by whether a taxonomy actually forms.
        out.append(
            DiscriminatorCandidate(
                table=table.name,
                column=column,
                values=values,
                source="check_constraint",
                confidence=0.90
                if looks_like_discriminator_name(column, options.name_stems)
                else 0.75,
                evidence=[
                    f"CHECK constraint enumerates {len(values)} value(s) for '{column}'",
                ],
            )
        )
    return out


def _sampled_candidates(
    table: Table,
    enumerator: ValueEnumerator,
    options: DiscriminatorOptions,
    already: set[str],
) -> list[DiscriminatorCandidate]:
    out: list[DiscriminatorCandidate] = []
    for column in table.columns:
        if column.name in already or _is_structural(table, column.name) or column.is_unique:
            continue
        # Unlike the declared path, sampling needs a reason to look: probing every column of
        # every table is a cost with no signal behind it.
        if not looks_like_discriminator_name(column.name, options.name_stems):
            continue
        try:
            values = enumerator(table.name, column.name, options.sample_limit)
        except Exception:  # noqa: BLE001 - an enumerator failure is not a schema fact
            continue
        if not values:
            continue
        distinct = sorted({str(v) for v in values if v is not None})
        if not _accept(distinct, options):
            continue
        out.append(
            DiscriminatorCandidate(
                table=table.name,
                column=column.name,
                values=distinct,
                source="sampled",
                confidence=0.60,
                evidence=[f"sampled {len(distinct)} distinct label-shaped value(s)"],
            )
        )
    return out


def detect_discriminators(
    schema: Schema,
    *,
    options: DiscriminatorOptions | None = None,
    enumerator: ValueEnumerator | None = None,
) -> list[DiscriminatorCandidate]:
    """Return discriminator candidates, declared ones first.

    Deterministic and database-free unless an ``enumerator`` is supplied. A column already
    covered by a declared constraint is never re-sampled — the declaration is exact and
    sampling could only weaken it.
    """
    opts = options or DiscriminatorOptions()
    found: list[DiscriminatorCandidate] = []

    for table_name in sorted(schema.tables):
        table = schema.tables[table_name]
        declared = _declared_candidates(table, opts)
        found.extend(declared)
        if enumerator is not None:
            found.extend(_sampled_candidates(table, enumerator, opts, {c.column for c in declared}))

    return sorted(found, key=lambda c: (-c.confidence, c.table, c.column))


def is_shared_pk_fk(table_name: str, table: Table, fk: ForeignKey) -> bool:
    """True when ``fk`` makes ``table`` a subtype: its single-column primary key *is* the FK.

    The one definition of class-table inheritance, used by the baseline, the shared-PK walk
    and specialization detection. A key that references its own table is excluded: that is
    a self-reference, and reading it as inheritance made an entity its own superclass (#54).
    """
    return (
        len(table.primary_key) == 1
        and list(fk.columns) == table.primary_key
        and fk.foreign_table != table_name
    )


def shared_pk_children(schema: Schema) -> dict[str, list[str]]:
    """``{parent table: [child tables]}`` for class-table inheritance.

    A child whose single-column primary key is also a foreign key to the parent
    (:func:`is_shared_pk_fk`). Collecting it here lets the shared library arbitrate between
    it and concept analysis instead of two channels writing competing parents for the same
    entity.
    """
    parents: dict[str, list[str]] = {}
    for table_name in sorted(schema.tables):
        table = schema.tables[table_name]
        for fk in table.foreign_keys:
            if is_shared_pk_fk(table_name, table, fk):
                parents.setdefault(fk.foreign_table, []).append(table_name)
    return {p: sorted(set(c)) for p, c in parents.items()}


def specialization_parents(
    schema: Schema, candidates: list[DiscriminatorCandidate]
) -> dict[str, list[str]]:
    """``{table: [child tables]}`` where a discriminator sits on a shared-PK parent.

    The two signals corroborate — this is the ER specialization pattern rather than a
    discriminator and four unrelated subclass edges — so the shared taxonomy library reports
    them as one result. Detection stays here because it needs the physical schema; the
    decision belongs there (see the addendum §2, item 2).
    """
    discriminated = {c.table for c in candidates}
    return {p: c for p, c in shared_pk_children(schema).items() if p in discriminated}
