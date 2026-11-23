"""Foreign-key inference for schemas without declared constraints.

Snowflake does not enforce FK constraints and many Snowflake schemas
have none declared at all (PRD P6.6). Even a PostgreSQL dump or a
warehouse-landed table may have lost its referential metadata. This
module provides a *pure-Python* heuristic engine that suggests probable
FKs so the Mapping Studio / CLI can show them as "confirm to accept"
candidates without silently inventing graph topology.

Design
------

The inference engine is deliberately sampler-pluggable and
source-agnostic:

- :func:`infer_foreign_keys` takes a :class:`r2g.types.Schema` and an
  optional ``sampler`` callable. Without a sampler it returns purely
  name-driven suggestions. With a sampler, it augments every candidate
  with a value-overlap score that either boosts or vetoes it.
- The heuristic runs in two passes: single-column candidates first
  (the common ``{prefix}_id`` → ``{prefix}s.id`` case), then a
  composite pass that groups single-column candidates with the same
  local table → foreign table pair and aligns them by column order.
- Declared FKs already present on a table short-circuit the search for
  that exact column set. We never emit a suggestion that duplicates a
  declared constraint.

The engine returns :class:`InferredForeignKey` objects sorted by
``confidence`` descending. The public API is intentionally small —
callers that accept a suggestion can materialize it as a declared
:class:`~relational_schema_analyzer.types.ForeignKey` via
:meth:`InferredForeignKey.to_foreign_key`.

Heuristic details
-----------------

For every non-PK column ``c`` in every table ``T`` we consider these
patterns:

1. ``{prefix}_id`` → foreign table is singular or plural of ``prefix``
   (``user_id`` → ``user`` or ``users``). The foreign column is the
   target table's primary key if it is a single column.
2. ``{prefix}id`` (no underscore, len > 3) → same, with a penalty.
3. ``{prefix}_{pkcol}`` → for tables with a non-``id`` PK name
   (``order_sku`` → ``orders.sku``).
4. Direct PK-name match across tables for non-generic PK names
   (``sku`` in one table and ``sku`` as PK of another; we never match
   bare ``id``).

Each match is filtered by JSON-level type compatibility
(``pg_type_to_json_type`` shared between PG and Snowflake). Confidence
starts at a pattern-specific base and is modulated by:

- ``+0.1`` when both columns use identical data-type strings.
- ``+0.15`` when the sampler reports overlap ≥ 0.9 (strong signal).
- ``+0.05`` when overlap ≥ 0.5.
- ``-0.25`` when overlap is 0 (hard veto unless caller disables).
- ``-0.1`` per non-nullable column that looks like it points at a
  nullable PK (usually a modelling mistake, not a real FK).

Denormalization probes — a consumer-facing API with no caller here
------------------------------------------------------------------

Alongside the value-overlap ``__call__``, every sampler exposes three probes:

- ``distinct_ratio(table, column)`` — distinct values over row count. Low means
  redundant reference data.
- ``group_single_valued(table, determinant_columns, dependent_column)`` — the
  fraction of determinant groups with a single dependent value, i.e. **functional
  dependency strength**. ``zip`` determining ``city``/``state`` is an embedded
  lookup that wants extracting into its own entity.
- ``delimiter_rate(table, column, delimiter)`` — the fraction of sampled values
  containing the delimiter. High means a multi-valued column (``"a,b,c"``) that is
  really a relationship stuffed into a string.

**Nothing in this library calls them, and that is deliberate rather than dead
code.** They are the measurement half of denormalization analysis; the engine that
interprets them — detectors, scored findings, remediation hints — lives in
``r2g``'s ``denorm.py`` and injects a sampler here. Consumers are expected to call
these directly, which is why they are documented rather than made private.

Two things a consumer should know. Each probe returns ``None`` for "could not
evaluate" and never raises, so a failed measurement degrades to *unmeasured* rather
than to a wrong number — treat ``None`` as absence of evidence, not evidence of
absence. And the ``group_single_valued`` probe is the expensive one: it groups
rather than scanning, so bound it with the sampler's ``limit`` and, on a
pay-per-byte engine, a cost ceiling.

The layering here is inverted — the paradigm-neutral analysis sits in the consumer
while the library holds only the instrument — and the reasons for leaving it that
way are recorded in ``docs/DESIGN-ADDENDUM-denormalization.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional, Sequence, TypeVar, cast

from pydantic import BaseModel, Field

from .log import get_logger
from .typemap import pg_type_to_json_type
from .types import Column, ForeignKey, Schema, Table

logger = get_logger(__name__)


# ── Public models ────────────────────────────────────────────────────


InferenceMethod = Literal["name_suffix", "name_no_underscore", "pk_name_match", "composite"]


class InferredForeignKey(BaseModel):
    """A single FK candidate produced by :func:`infer_foreign_keys`."""

    table: str
    columns: list[str]
    foreign_table: str
    foreign_columns: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    method: InferenceMethod
    evidence: list[str] = Field(default_factory=list)

    def to_foreign_key(self, constraint_name: Optional[str] = None) -> ForeignKey:
        """Materialize an accepted suggestion as a :class:`ForeignKey` -- labelled.

        Lets callers fold an accepted candidate back into the physical schema. It
        is saved ``enforced=False`` and, unless a name is given, named
        ``inferred:<table>_<cols>_fk`` (the overlay's naming, so each key has its
        own name). Folding it in "as if it had been declared", as this used to,
        left ``enforced`` at ``True``, which the serializer omits: the key then
        read as a constraint the source guarantees, and :mod:`.baseline` treats a
        table with an enforced FK as authoritative and skips fallback inference
        on it. The relational analogue of the Arango analyzer's edge-definition
        conversion (which stays in ``r2g``).
        """
        from .overlay import INFERRED_CONSTRAINT_PREFIX

        return ForeignKey(
            columns=list(self.columns),
            foreign_table=self.foreign_table,
            foreign_columns=list(self.foreign_columns),
            constraint_name=constraint_name
            or f"{INFERRED_CONSTRAINT_PREFIX}{self.table}_{'_'.join(self.columns)}_fk",
            enforced=False,
        )


# ── Options ─────────────────────────────────────────────────────────


@dataclass
class InferenceOptions:
    """Knobs that shape what the engine considers / returns."""

    min_confidence: float = 0.4
    generic_pk_names: frozenset[str] = field(
        default_factory=lambda: frozenset({"id", "uuid", "pk", "key"})
    )
    max_candidates_per_column: int = 3
    allow_composite: bool = True
    sample_overlap: bool = False
    overlap_veto_on_zero: bool = True


# ── Sampler protocol ────────────────────────────────────────────────


class Overlap(float):
    """An overlap ratio that remembers whether it needed normalized comparison.

    A plain ``float`` subclass on purpose: every existing consumer — comparisons,
    arithmetic, ``float()``, r2g's re-export of :data:`SamplerResult` — keeps working
    unchanged, while :func:`_apply_sampler` can read ``normalized`` to label the
    evidence. Samplers may still return a bare ``float``; it reads as not normalized.
    """

    normalized: bool

    def __new__(cls, value: float, *, normalized: bool = False) -> "Overlap":
        self = super().__new__(cls, value)
        self.normalized = normalized
        return self

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Overlap({float(self)!r}, normalized={self.normalized!r})"


def _normalized(score: Optional[float]) -> bool:
    """Did this result come from a normalized comparison? False for bare floats."""
    return bool(getattr(score, "normalized", False))


#: Strict overlap at or above this is already conclusive, so the normalized comparison
#: is not run. 0.9 is where ``_apply_sampler``'s confidence bump saturates: a higher
#: number could not change the candidate's fate, and normalizing is not free.
#:
#: Measured on live Postgres 16, 1M-row parent, default bound, median of five runs:
#: the strict query alone is 121 ms, strict-plus-normalized is 794 ms. Normalizing
#: means a second full pass over the referenced column — which since issue #10 is read
#: whole — so it roughly doubles the expensive half. Rewriting it as a single join on
#: the normalized key only recovers ~14% (632 ms), because the cost is the
#: normalization itself, not the query shape.
#:
#: So: healthy foreign keys, the overwhelming majority, cost what they did in 0.9.1,
#: and the second pass runs only on the weak and vetoed pairs that issue #12 is about
#: — the ones where it can actually change the answer.
_STRICT_IS_CONCLUSIVE = 0.9


def _conclusive(exact: Optional[float]) -> bool:
    """Is the strict overlap good enough that normalizing cannot change the outcome?"""
    return exact is not None and exact >= _STRICT_IS_CONCLUSIVE


def _pair(row: Any) -> tuple[Optional[float], Optional[float]]:
    """Read ``(exact, normalized)`` from a result row that may carry only one column.

    Every engine's overlap query selects both, but a cursor is not obliged to hand
    back two values — a stub, an older driver, or a dialect that folded the second
    expression away would give one. Treating the missing column as "not measured"
    degrades to the strict comparison instead of raising.
    """
    if not row:
        return None, None
    exact = row[0]
    norm = row[1] if len(row) > 1 else None
    return (
        None if exact is None else float(exact),
        None if norm is None else float(norm),
    )


def _better(exact: Optional[float], norm: Optional[float]) -> Optional[Overlap]:
    """Pick the stronger of the exact and normalized overlaps, recording which won.

    Normalization can only ever merge values, so ``norm`` is almost always ``>= exact``
    — but it also merges the *denominator*, so it is not guaranteed, and the engine
    should never be handed a worse number than the strict comparison produced.
    """
    if exact is None and norm is None:
        return None
    if exact is None:
        # Nothing to compare against, so the result cannot claim it "needed
        # normalizing" — that label exists to tell a reviewer the raw values
        # disagreed, and here the raw values were never measured.
        return Overlap(float(norm), normalized=False)  # type: ignore[arg-type]
    if norm is None:
        return Overlap(float(exact), normalized=False)
    if float(norm) > float(exact):
        return Overlap(float(norm), normalized=True)
    return Overlap(float(exact), normalized=False)


SamplerResult = Optional[float]
"""Sampler callables return an overlap ratio in [0, 1], or ``None`` to
indicate "no data / couldn't evaluate" (the engine then skips the
overlap signal rather than treating it as a veto).

Concretely this is an :class:`Overlap` — a ``float`` carrying one extra flag — but
the declared type stays ``Optional[float]`` because ``Overlap`` *is* a float and
downstream code (including r2g, which re-exports this name) must not have to change.
"""


Sampler = Callable[[str, str, str, str], SamplerResult]
"""``sampler(local_table, local_column, foreign_table, foreign_column)``

Returns the fraction of the **local** column's distinct values that are present in
the **referenced** column: ``|sampled local ∩ foreign| / |sampled local|``.

**The two sides are not symmetric, and this is the contract's load-bearing clause.**
Bounding the local side limits cost and costs only precision — a sample of the child
values is a fair estimate of the whole. Bounding the *referenced* side destroys the
measurement: a valid foreign key into a table larger than the bound is compared
against an arbitrary slice of its parent and scores far below the truth. With
``LIMIT`` and no ``ORDER BY`` that slice is the physically-first rows, so an
append-only parent whose children reference recent rows scores **zero** — and zero is
the hard veto in :func:`_apply_sampler`, which deletes the candidate outright.

So: **sample the local column; test against the whole referenced column.** Five
samplers originally bounded both sides (issue #10) because symmetry looks even-handed;
it is not.

A sampler that cannot evaluate a pair returns ``None`` ("not evaluated"), never ``0.0``
— the engine reads zero as positive evidence of *absence*, which is a different claim.

**Compare twice: strictly, and normalized.** Values that mean the same key routinely
differ in case or padding — ``CUST1`` against ``cust1``, or ``CUST1`` against the
space-padded ``CUST1   `` a ``CHAR(n)`` column becomes when landed in a warehouse.
Strict comparison scores those **zero**, which is the hard veto, so a real foreign key
is not merely unconfirmed but deleted (issue #12). Every sampler therefore measures
both the strict overlap and the overlap after ``lower(trim(...))``, returns whichever
is stronger via :func:`_better`, and the result carries ``normalized=True`` when the
relaxed comparison is what rescued it, so the evidence can say so. Labelled, not
laundered — the same rule the declared-key overlay follows.

This also makes the answer **independent of the server's collation**. MySQL and SQL
Server are commonly deployed case-insensitive, Postgres, DuckDB, Databricks and
Snowflake are not, so before this an identical schema scored differently depending on
where it lived. Normalizing explicitly means the engine decides, not the deployment.

New samplers must be registered in ``tests/test_sampler_contract.py``, which asserts
this behaviour against every sampler the module defines.
"""


# ── Entry point ─────────────────────────────────────────────────────


def infer_foreign_keys(
    schema: Schema,
    *,
    options: Optional[InferenceOptions] = None,
    sampler: Optional[Sampler] = None,
) -> list[InferredForeignKey]:
    """Return ranked FK candidates for ``schema``.

    See module docstring for the heuristic. Results are deduplicated
    (one entry per ``(table, tuple(columns), foreign_table)`` triple),
    sorted by confidence descending, and filtered by
    ``options.min_confidence``.
    """
    opts = options or InferenceOptions()

    key_index = _build_candidate_key_index(schema)
    declared_index = _build_declared_fk_index(schema)

    single: list[InferredForeignKey] = []
    for table_name, table in schema.tables.items():
        pk_set = set(table.primary_key)
        for col in table.columns:
            if col.name in pk_set and len(table.primary_key) == 1:
                # Skip single-column PKs — they are the referenced side,
                # not a FK origin (a table's lone PK very rarely *also*
                # points somewhere else).
                continue
            if _column_is_covered_by_declared_fk(col.name, table_name, declared_index):
                continue
            single.extend(
                _candidates_for_column(
                    schema,
                    table_name,
                    col,
                    key_index,
                    opts,
                )
            )

    # Composite pass: group single-column candidates sharing (table, foreign_table)
    composite: list[InferredForeignKey] = []
    if opts.allow_composite:
        composite = _find_composite_candidates(schema, single, declared_index)

    # Sampler pass: adjust confidence on every candidate we still have.
    all_candidates = single + composite
    if sampler is not None and opts.sample_overlap:
        sampled = [_apply_sampler(c, sampler, opts) for c in all_candidates]
        all_candidates = [c for c in sampled if c is not None]

    # Dedup + rank + filter.
    deduped = _dedupe(all_candidates)
    ranked = sorted(deduped, key=lambda c: c.confidence, reverse=True)
    return [c for c in ranked if c.confidence >= opts.min_confidence]


# ── Internal helpers ────────────────────────────────────────────────


#: Confidence deducted when the proposed target is a UNIQUE column rather than the
#: primary key. Small on purpose: a unique column is a perfectly good referent, so
#: this only breaks ties in favour of the PK when both are plausible — it must not
#: push an otherwise-solid candidate under ``min_confidence``.
_UNIQUE_TARGET_PENALTY = 0.05


def _single_column_candidate_keys(table: Table) -> list[tuple[str, bool]]:
    """Single-column candidate keys of ``table`` as ``(column_name, is_primary_key)``.

    A foreign key references a *candidate key*, not specifically the primary key —
    and warehouse-landed schemas routinely carry a surrogate integer PK alongside
    the natural business key everything actually joins on::

        accounts.id          bigint  PRIMARY KEY
        accounts.account_id  text    UNIQUE      <- what children reference

    Targeting only the PK makes the real referent invisible: the sole candidate
    generated is ``child.account_id -> accounts.id``, the type check correctly
    rejects ``text -> bigint``, and the engine returns nothing on a schema it
    exists to serve.

    Uniqueness is also what supplies *direction*. Containment alone cannot: in a
    schema with three accounts, every table's ``account_id`` is contained in every
    other's, both ways. Unique on one side and non-unique on the other is what makes
    it a many-to-one rather than a coincidence.

    The PK is listed first and wins on collision, so callers ranking by position or
    by ``is_primary_key`` get the stronger referent first. Composite keys are
    excluded here; they are the composite pass's business.
    """
    keys: list[tuple[str, bool]] = []
    seen: set[str] = set()

    if len(table.primary_key) == 1:
        pk = table.primary_key[0]
        keys.append((pk, True))
        seen.add(pk.lower())

    # Declared single-column UNIQUE constraints, then the per-column flag. Both are
    # populated by the enriched connectors (DESIGN §3.1); either alone is enough.
    unique_names = [u[0] for u in table.unique_constraints if len(u) == 1]
    unique_names += [c.name for c in table.columns if c.is_unique]
    for name in unique_names:
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        keys.append((name, False))
    return keys


def _multi_column_candidate_keys(table: Table) -> list[tuple[list[str], bool]]:
    """Multi-column candidate keys of ``table`` as ``(column_names, is_primary_key)``.

    The composite counterpart of :func:`_single_column_candidate_keys`, and it exists
    for the same reason: a composite business key is just as often expressed as a
    UNIQUE constraint beside a surrogate primary key as it is as the primary key
    itself::

        accounts.id                      bigint  PRIMARY KEY
        accounts.(tenant, account_id)             UNIQUE   <- what children reference

    Scanning only ``primary_key`` made that target invisible, exactly as it did in the
    single-column case. The composite PK is listed first and wins on collision.
    """
    keys: list[tuple[list[str], bool]] = []
    seen: set[tuple[str, ...]] = set()

    if len(table.primary_key) >= 2:
        keys.append((list(table.primary_key), True))
        seen.add(tuple(c.lower() for c in table.primary_key))

    for cols in table.unique_constraints:
        if len(cols) < 2:
            continue  # single-column keys belong to the single-column pass
        sig = tuple(c.lower() for c in cols)
        if sig in seen:
            continue
        seen.add(sig)
        keys.append((list(cols), False))
    return keys


def _build_candidate_key_index(schema: Schema) -> dict[str, list[tuple[str, str, bool]]]:
    """Index tables by each single-column candidate key name.

    Returns ``{key_col_name_lower: [(table_name, key_col_name, is_primary_key), ...]}``.
    Generic names (``id``, ``uuid``, …) do enter the index; they are consulted only
    via the ``{prefix}_id`` patterns and never matched bare (see
    :func:`_candidates_for_column`).
    """
    idx: dict[str, list[tuple[str, str, bool]]] = {}
    for table_name, table in schema.tables.items():
        for col_name, is_pk in _single_column_candidate_keys(table):
            idx.setdefault(col_name.lower(), []).append((table_name, col_name, is_pk))
    return idx


def _partition_family(table: Table) -> str:
    """The table this one's rows really belong to: its partition parent, or itself.

    A partition holds part of its parent's rows under its own name, so a reference
    between a parent and its partition — or between two siblings — is not a
    relationship. It is the same table twice.

    Only *within* a family, though. A partition's foreign keys to other tables are
    perfectly real: PostgreSQL records them on the child, which is why the connector's
    ``annotate_partition_metadata`` rolls them up to the parent. Excluding partition
    children outright therefore destroys genuine keys — measured on Pagila, it cut
    recall from 0.974 to 0.513 (#27).

    Pagila is where this surfaced: ``payment`` has seven partitions, and inference
    proposed a composite key from the parent to each child, from each child back, and
    between every pair of siblings. **56 of the 60 wrong proposals across the whole
    corpus came from that one table.**

    Note that a table is trivially in its own family, so the check also rejects a
    self-reference. That is invisible today because every candidate path already
    excludes one — but ``category.parent_id -> category.category_id`` is a real
    pattern, and anyone adding support for it has to relax this too, not only the
    guards that look like they are the ones in the way.
    """
    return (table.partition_of or table.name).lower()


def _same_partition_family(schema: Schema, a: str, b: str) -> bool:
    """Are these two table names the same underlying table, via partitioning?"""
    ta, tb = schema.tables.get(a), schema.tables.get(b)
    if ta is None or tb is None:
        return False
    return _partition_family(ta) == _partition_family(tb)


def _build_declared_fk_index(schema: Schema) -> dict[str, set[tuple[str, ...]]]:
    """Index ``{table_name: {tuple(fk_columns), ...}}`` for existing FKs."""
    idx: dict[str, set[tuple[str, ...]]] = {}
    for table_name, table in schema.tables.items():
        idx[table_name] = {tuple(sorted(fk.columns)) for fk in table.foreign_keys}
    return idx


def _column_is_covered_by_declared_fk(
    col: str, table: str, declared_index: dict[str, set[tuple[str, ...]]]
) -> bool:
    fks = declared_index.get(table, set())
    return any(col in cols for cols in fks)


def _candidates_for_column(
    schema: Schema,
    table_name: str,
    col: Column,
    key_index: dict[str, list[tuple[str, str, bool]]],
    opts: InferenceOptions,
) -> list[InferredForeignKey]:
    """Produce single-column FK candidates that column ``col`` could be."""
    candidates: list[InferredForeignKey] = []
    local_lower = col.name.lower()

    # Pattern 1 / 2: {prefix}_id, {prefix}id, {prefix}_{keycol}
    prefix_matches = _split_prefix(local_lower)
    for prefix, suffix, method, base_conf in prefix_matches:
        for foreign_table, foreign_col, is_pk in _candidate_tables_for_prefix(schema, prefix):
            if foreign_table == table_name:
                continue
            if suffix and suffix != foreign_col.lower():
                # e.g. pattern `{prefix}_{keycol}` wants suffix == the key's name.
                continue
            cand = _make_candidate(
                schema,
                table_name,
                [col.name],
                foreign_table,
                [foreign_col],
                method=method,
                base_confidence=base_conf - (0.0 if is_pk else _UNIQUE_TARGET_PENALTY),
                evidence=[
                    f"name pattern '{col.name}' → {foreign_table}.{foreign_col}"
                    + ("" if is_pk else " (UNIQUE column, not the primary key)")
                ],
            )
            if cand is not None:
                candidates.append(cand)

    # Pattern 4: the column name is itself a candidate key elsewhere (non-generic).
    if local_lower not in opts.generic_pk_names:
        for foreign_table, foreign_col, is_pk in key_index.get(local_lower, []):
            if foreign_table == table_name:
                continue
            cand = _make_candidate(
                schema,
                table_name,
                [col.name],
                foreign_table,
                [foreign_col],
                method="pk_name_match",
                base_confidence=0.55 - (0.0 if is_pk else _UNIQUE_TARGET_PENALTY),
                evidence=[
                    f"column '{col.name}' matches the "
                    f"{'primary key' if is_pk else 'UNIQUE column'} of "
                    f"'{foreign_table}' (non-generic name)"
                ],
            )
            if cand is not None:
                candidates.append(cand)

    # Trim to top-N per column.
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates[: opts.max_candidates_per_column]


def _split_prefix(col_lower: str) -> list[tuple[str, str, InferenceMethod, float]]:
    """Return ``(prefix, required_suffix, method, base_confidence)`` tuples
    a column name could resolve to.

    The ``required_suffix`` is used by pattern 3 to insist on a specific
    PK column name; otherwise it is ``""`` which accepts any single-col
    PK.
    """
    out: list[tuple[str, str, InferenceMethod, float]] = []
    # Pattern 1: ends with _id
    if col_lower.endswith("_id") and len(col_lower) > 3:
        out.append((col_lower[:-3], "", "name_suffix", 0.75))
    # Pattern 3: {prefix}_{suffix} where suffix looks like a PK name
    if "_" in col_lower:
        prefix, _, suffix = col_lower.rpartition("_")
        if prefix and suffix and suffix not in ("", "id") and len(suffix) >= 2:
            out.append((prefix, suffix, "name_suffix", 0.6))
    # Pattern 2: ends with 'id' but no underscore (userid, orderid)
    if (
        col_lower.endswith("id")
        and not col_lower.endswith("_id")
        and len(col_lower) > 3
        and col_lower != "uuid"
    ):
        out.append((col_lower[:-2], "", "name_no_underscore", 0.45))
    return out


def _candidate_tables_for_prefix(
    schema: Schema, prefix: str
) -> list[tuple[str, str, bool]]:
    """Return ``(table_name, key_column, is_primary_key)`` for every single-column
    candidate key of the tables whose name matches ``prefix`` in singular/plural form.

    One table can contribute several entries — a surrogate PK *and* a natural unique
    key are both legitimate referents, and which one a given child column actually
    points at is decided downstream by the type check and the confidence ranking.
    """
    if not prefix:
        return []
    candidates: list[tuple[str, str, bool]] = []
    target_names = {prefix, _pluralize(prefix), _singularize(prefix)}
    for table_name, table in schema.tables.items():
        if table_name.lower() not in target_names:
            continue
        for col_name, is_pk in _single_column_candidate_keys(table):
            candidates.append((table_name, col_name, is_pk))
    return candidates


from .naming import pluralize as _pluralize  # noqa: E402
from .naming import singularize as _singularize  # noqa: E402


def _make_candidate(
    schema: Schema,
    table: str,
    columns: list[str],
    foreign_table: str,
    foreign_columns: list[str],
    *,
    method: InferenceMethod,
    base_confidence: float,
    evidence: list[str],
) -> Optional[InferredForeignKey]:
    """Validate type compatibility and assemble an :class:`InferredForeignKey`.

    Returns ``None`` if the types are incompatible (we never suggest an
    FK from a ``boolean`` column to an ``integer`` PK, for example).
    """
    local_tbl = schema.tables.get(table)
    foreign_tbl = schema.tables.get(foreign_table)
    if local_tbl is None or foreign_tbl is None:
        return None
    if _same_partition_family(schema, table, foreign_table):
        return None  # the same table twice; see _partition_family

    confidence = base_confidence
    details = list(evidence)

    for lcol_name, fcol_name in zip(columns, foreign_columns):
        lcol = _find_col(local_tbl, lcol_name)
        fcol = _find_col(foreign_tbl, fcol_name)
        if lcol is None or fcol is None:
            return None
        if not _types_compatible(lcol, fcol):
            return None
        if lcol.data_type.strip().lower() == fcol.data_type.strip().lower():
            confidence += 0.1
            details.append(f"identical data_type '{lcol.data_type}'")
        if (not lcol.is_nullable) and fcol.is_nullable:
            # Unusual: a non-nullable child pointing at a nullable-PK parent.
            confidence -= 0.1
            details.append(
                f"'{table}.{lcol_name}' is NOT NULL but '{foreign_table}.{fcol_name}' is nullable"
            )

    confidence = max(0.0, min(1.0, confidence))
    return InferredForeignKey(
        table=table,
        columns=list(columns),
        foreign_table=foreign_table,
        foreign_columns=list(foreign_columns),
        confidence=round(confidence, 3),
        method=method,
        evidence=details,
    )


def _find_col(table: Table, name: str) -> Optional[Column]:
    for c in table.columns:
        if c.name == name:
            return c
    return None


_COMPATIBLE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"integer", "float"}),  # numeric join is common across NUMBER/int
    frozenset({"string"}),
    frozenset({"boolean"}),
)


def _types_compatible(a: Column, b: Column) -> bool:
    aj = pg_type_to_json_type(a.data_type)
    bj = pg_type_to_json_type(b.data_type)
    if aj == bj:
        return True
    for group in _COMPATIBLE_GROUPS:
        if aj in group and bj in group:
            return True
    return False


def _find_composite_candidates(
    schema: Schema,
    single_candidates: list[InferredForeignKey],  # kept for future use (e.g. evidence merging)
    declared_index: dict[str, set[tuple[str, ...]]],
) -> list[InferredForeignKey]:
    """Directly scan the schema for composite FK candidates.

    For every multi-column *candidate key* ``(k1, …, kn)`` of every foreign table
    ``F`` — its composite primary key, and each composite UNIQUE constraint — find
    local tables ``L`` that contain every ``ki`` with a type compatible with
    ``F.ki``. Emit a composite suggestion preserving the key's column order.

    Considering UNIQUE constraints and not just the PK is the composite half of the
    same fix applied to single columns: a composite business key is as often a
    UNIQUE beside a surrogate ``id`` as it is the primary key. A UNIQUE target is
    ranked just below an otherwise-identical PK target.

    This is intentionally independent of the single-column pass: the
    child column names don't have to match the parent table's name
    (a common modelling style for junction tables whose name is
    unrelated to its parents, e.g. ``enrollments`` referencing
    ``course_offerings``).
    """
    del single_candidates  # parameter reserved for future evidence merging
    out: list[InferredForeignKey] = []

    targets = [
        (name, tbl, key_names, is_pk)
        for name, tbl in schema.tables.items()
        for key_names, is_pk in _multi_column_candidate_keys(tbl)
    ]
    for foreign_table, foreign, key_names, is_pk in targets:
        maybe_pk_cols = [_find_col(foreign, k) for k in key_names]
        if any(pc is None for pc in maybe_pk_cols):
            continue
        pk_cols = cast("list[Column]", maybe_pk_cols)

        for local_table, local in schema.tables.items():
            if local_table == foreign_table:
                continue
            if _same_partition_family(schema, local_table, foreign_table):
                continue  # a parent and its partition are the same table
            local_pk_set = set(local.primary_key)
            # Require every PK column name to be present in the local
            # table as a non-PK-only column (it may still participate
            # in the local PK — that's fine, e.g. a junction table
            # whose own PK is the composite FK).
            matched: list[Column] = []
            ok = True
            for pc in pk_cols:
                lcol = _find_col(local, pc.name)
                if lcol is None:
                    ok = False
                    break
                if not _types_compatible(lcol, pc):
                    ok = False
                    break
                matched.append(lcol)
            if not ok:
                continue

            # Skip if the local table's single-column PK is exactly one
            # of the pieces (rare but would be a self-inconsistent match).
            if len(local.primary_key) == 1 and local.primary_key[0] in {pc.name for pc in pk_cols}:
                # Only treat as composite if *all* pieces are present,
                # which the loop above already verified.
                pass

            columns = [pc.name for pc in pk_cols]
            foreign_columns = list(key_names)
            if tuple(sorted(columns)) in declared_index.get(local_table, set()):
                continue

            # Base confidence: 0.7, plus +0.05 per component column that
            # is non-nullable on both sides (strong signal).
            base = 0.7
            nn_bonus = 0.05 * sum(
                1
                for lc, pc in zip(matched, pk_cols)
                if (not lc.is_nullable) and (not pc.is_nullable)
            )
            same_type_bonus = 0.05 * sum(
                1
                for lc, pc in zip(matched, pk_cols)
                if lc.data_type.strip().lower() == pc.data_type.strip().lower()
            )
            # Penalize matches where the local table also has a plain
            # "id" column that looks like it already points elsewhere —
            # the composite is still a valid suggestion but less sure.
            confidence = min(1.0, base + nn_bonus + same_type_bonus)
            # Exclude local tables that are obviously unrelated (no
            # shared column names outside the composite pieces) only if
            # confidence ended up low — we keep clean composite matches
            # regardless.
            if local_pk_set == set(columns):
                confidence = min(1.0, confidence + 0.05)
            if not is_pk:
                confidence = max(0.0, confidence - _UNIQUE_TARGET_PENALTY)

            out.append(
                InferredForeignKey(
                    table=local_table,
                    columns=columns,
                    foreign_table=foreign_table,
                    foreign_columns=foreign_columns,
                    confidence=round(confidence, 3),
                    method="composite",
                    evidence=[
                        f"composite match on "
                        f"{'PK' if is_pk else 'UNIQUE constraint'} of "
                        f"'{foreign_table}': ({', '.join(foreign_columns)})"
                    ],
                )
            )
    return out


def _apply_sampler(
    c: InferredForeignKey, sampler: Sampler, opts: InferenceOptions
) -> Optional[InferredForeignKey]:
    """Invoke the sampler for each (local, foreign) column pair and fold
    the result into the candidate's confidence score.

    Per-column overlaps are averaged. A single zero result vetoes the
    whole candidate when ``opts.overlap_veto_on_zero`` is set.
    """
    scores: list[float] = []
    any_zero = False
    any_normalized = False
    for lcol, fcol in zip(c.columns, c.foreign_columns):
        try:
            score = sampler(c.table, lcol, c.foreign_table, fcol)
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "fk_sampler_failed",
                table=c.table,
                column=lcol,
                foreign_table=c.foreign_table,
                error=str(err),
            )
            return c  # keep candidate; sampler noise shouldn't drop it
        if score is None:
            continue
        if score <= 0.0:
            any_zero = True
        if _normalized(score):
            any_normalized = True
        scores.append(score)

    if not scores:
        return c

    avg = sum(scores) / len(scores)
    # Issue #12: say so when the match needed case/whitespace normalization. The
    # candidate is still proposed — it is almost always right — but a reviewer
    # deciding whether to trust it should see that the raw values do not match.
    note = " (matched after normalizing case/whitespace)" if any_normalized else ""
    evidence = list(c.evidence) + [
        f"value overlap avg={avg:.2f} ({len(scores)} cols sampled){note}"
    ]

    if any_zero and opts.overlap_veto_on_zero:
        return None

    bump = 0.0
    if avg >= 0.9:
        bump = 0.15
    elif avg >= 0.5:
        bump = 0.05
    elif avg <= 0.0:
        bump = -0.25

    new_conf = max(0.0, min(1.0, c.confidence + bump))
    return c.model_copy(update={"confidence": round(new_conf, 3), "evidence": evidence})


def _dedupe(candidates: list[InferredForeignKey]) -> list[InferredForeignKey]:
    """Keep the highest-confidence candidate per ``(table, columns, foreign_table)``."""
    best: dict[tuple[str, tuple[str, ...], str], InferredForeignKey] = {}
    for c in candidates:
        key = (c.table, tuple(c.columns), c.foreign_table)
        prior = best.get(key)
        if prior is None or c.confidence > prior.confidence:
            best[key] = c
    return list(best.values())


# ── Concrete PostgreSQL value sampler ───────────────────────────────


class PostgresValueSampler:
    """Sampler that computes FK value-overlap ratios via PostgreSQL.

    One query per (local column, foreign column) pair, and a second one only when
    the first is inconclusive.

    The **local** side is bounded by a ``LIMIT`` CTE (default 10k distinct values);
    the **referenced** side is read whole, because comparing against a sample of the
    parent answers a different question (issue #10). If that strict comparison scores
    below :data:`_STRICT_IS_CONCLUSIVE`, a second query repeats it over
    ``lower(btrim(...))`` on both sides, so a key differing only in case or padding is
    not read as absent (issue #12). This is a *statistical* signal, not a proof — the
    engine treats it as one input alongside the name-based score.

    Usage::

        sampler = PostgresValueSampler(conn_str, schema_name="public", limit=10_000)
        infer_foreign_keys(schema, options=InferenceOptions(sample_overlap=True),
                           sampler=sampler)

    The sampler is resilient: any exception from psycopg gets caught
    and surfaced as ``None`` so the name-based score still wins. Call
    :meth:`close` to release the connection.

    **Cost governance is opt-in**, so existing callers behave exactly as before.
    ``max_queries`` caps the queries one sampler runs (later probes return ``None``
    and ``stats["budget_exhausted"]`` says so); ``statement_timeout_s`` has the
    server cancel any single query that runs longer; ``query_tag`` sets
    ``application_name`` so the queries are findable in ``pg_stat_activity``.
    These mirror :class:`SnowflakeValueSampler`, so a caller such as r2g's
    ``suggest-keys`` can govern either source the same way. A failed connection
    is attempted once per sampler, never once per probe; ``stats["connect_failed"]``
    reports it, so "could not connect" is never mistaken for "no key".
    """

    def __init__(
        self,
        connection_string: str,
        *,
        schema_name: str = "public",
        limit: int = 10_000,
        max_queries: Optional[int] = None,
        statement_timeout_s: Optional[int] = None,
        query_tag: Optional[str] = None,
    ) -> None:
        if max_queries is not None and int(max_queries) < 1:
            raise ValueError("max_queries must be at least 1 (or None for no cap)")
        if statement_timeout_s is not None and int(statement_timeout_s) < 1:
            raise ValueError("statement_timeout_s must be at least 1 second (or None)")
        self.connection_string = connection_string
        self.schema_name = schema_name
        self.limit = max(100, int(limit))
        self.max_queries = None if max_queries is None else int(max_queries)
        self.statement_timeout_s = None if statement_timeout_s is None else int(statement_timeout_s)
        self.query_tag = query_tag
        self._conn = None
        self._session_ready = False
        self._connect_error: Optional[str] = None
        self.queries_run = 0
        self.budget_exhausted = False

    @property
    def stats(self) -> dict[str, Any]:
        """What the governor allowed -- the same keys :class:`SnowflakeValueSampler` reports."""
        return {
            "queries_run": self.queries_run,
            "max_queries": self.max_queries,
            "cache_hits": 0,
            "budget_exhausted": self.budget_exhausted,
            "connect_failed": self._connect_error is not None,
        }

    def _conn_lazy(self):
        if self._conn is None:
            import psycopg

            self._conn = psycopg.connect(self.connection_string)
        if not self._session_ready:
            self._session_ready = True
            settings = []
            if self.statement_timeout_s is not None:
                settings.append(("statement_timeout", f"{self.statement_timeout_s * 1000}"))
            if self.query_tag:
                settings.append(("application_name", self.query_tag[:63]))
            if settings:
                # set_config is parameterised, unlike SET. Committed, because Postgres
                # undoes a session SET when its transaction rolls back -- and the first
                # failed probe rolls back. Without the commit the timeout would vanish
                # silently at exactly the moment it starts to matter.
                with self._conn.cursor() as cur:
                    for name, value in settings:
                        cur.execute("SELECT set_config(%s, %s, false)", (name, value))
                self._conn.commit()
        return self._conn

    def _execute(self, query: str, params: tuple, *, what: str) -> Optional[tuple]:
        """Run one governed query and return its first row, or ``None``.

        ``None`` means "could not evaluate": budget spent, no connection, or the
        query failed (including a statement timeout). Every probe goes through here,
        so the budget counts all of them and a failed connection is tried once.
        """
        if self.max_queries is not None and self.queries_run >= self.max_queries:
            if not self.budget_exhausted:
                self.budget_exhausted = True
                logger.warning(
                    "postgres_sampler_budget_exhausted",
                    max_queries=self.max_queries,
                    hint="remaining probes return None",
                )
            return None
        if self._connect_error is not None:
            return None
        try:
            conn = self._conn_lazy()
        except Exception as err:  # noqa: BLE001
            self._connect_error = str(err)
            logger.warning(
                f"{what}_connect_failed",
                error=self._connect_error,
                hint="remaining probes return None without reconnecting",
            )
            return None
        self.queries_run += 1
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
            return tuple(row) if row else None
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{what}_query_failed", error=str(err))
            try:
                conn.rollback()  # a failed statement poisons the transaction otherwise
            except Exception:  # noqa: BLE001
                pass
            return None

    @staticmethod
    def _qi(name: str) -> str:
        """Quote an identifier, doubling any embedded quote.

        Names come from the catalog, so this is about correctness rather than
        injection: a column legitimately named ``a"b`` produced a syntax error, the
        query was caught, and the probe reported "could not evaluate" — so that
        table's key could never be found and nothing said why. The DuckDB and
        Snowflake samplers already quote this way.
        """
        return '"' + name.replace('"', '""') + '"'

    def _qt(self, table: str) -> str:
        return f"{self._qi(self.schema_name)}.{self._qi(table)}"

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
        # A reconnect after close() must re-apply the timeout and tag.
        self._session_ready = False

    def __enter__(self) -> "PostgresValueSampler":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __call__(
        self,
        local_table: str,
        local_column: str,
        foreign_table: str,
        foreign_column: str,
    ) -> SamplerResult:
        """Return the fraction of distinct local values present in the
        foreign column, or ``None`` if the query failed."""

        # Issue #10: the local side is bounded (an estimate is fair); the referenced
        # side is not. Comparing against a sampled parent is a different question, and
        # a valid FK into a table larger than the bound would score near zero.
        # Issue #12: a key differing only in case or padding must not read as absent.
        # Normalizing means a second full scan of the referenced column, so it runs
        # only when the strict score leaves the outcome open (_STRICT_IS_CONCLUSIVE).
        def q(norm: bool) -> str:
            # The strict pass costs what 0.9.1's did. NULLIF, not GREATEST: an empty
            # local sample is "not evaluated", never a zero overlap, which vetoes (#52).
            lk = f"lower(btrim({self._qi(local_column)}::text))" if norm else self._qi(local_column)
            fk_ = f"lower(btrim({self._qi(foreign_column)}::text))" if norm else self._qi(foreign_column)
            return f"""
                WITH l AS (
                    SELECT DISTINCT {lk} AS v
                    FROM {self._qt(local_table)}
                    WHERE {self._qi(local_column)} IS NOT NULL
                    LIMIT %s
                ),
                f AS (
                    SELECT DISTINCT {fk_} AS v
                    FROM {self._qt(foreign_table)}
                    WHERE {self._qi(foreign_column)} IS NOT NULL
                )
                SELECT
                    COUNT(*) FILTER (WHERE f.v IS NOT NULL)::float
                        / NULLIF(COUNT(*), 0)::float AS overlap
                FROM l
                LEFT JOIN f ON l.v = f.v
            """  # noqa: S608 - identifiers are quoted with " and schema is from catalog

        row = self._execute(q(False), (self.limit,), what="fk_sampler")
        if not row:
            return None
        if row[0] is None:
            return None  # no non-null local value: nothing to compare, and no budget to spend
        exact = float(row[0])
        if _conclusive(exact):
            return Overlap(exact, normalized=False)
        row = self._execute(q(True), (self.limit,), what="fk_sampler")
        norm = None if not row or row[0] is None else float(row[0])
        return _better(exact, norm)

    # ── Denormalization probes (PRD Phase 11) ──────────────────────

    # ── Key discovery (KeyProbe) ───────────────────────────────────
    #
    # What lets `profile_primary_keys` / `draft_key_overlay` propose keys for a
    # source whose catalog declares none. Until this, `SnowflakeValueSampler` was
    # the only sampler implementing them, so the feature built for keyless schemas
    # could not run on Postgres — see issue #18.
    #
    # The shape is deliberately the same as Snowflake's: screen on a sample (any
    # duplicate or NULL rejects a column conclusively, so a bounded read is enough
    # to say *no*), then confirm the survivors over the whole table (only a full
    # count can say *yes*). Both return ``None`` for "could not evaluate" rather
    # than a guess, which is what keeps a table in `not_evaluated` instead of being
    # reported keyless.

    def _row(self, query: str, params: tuple) -> Optional[tuple]:
        """Run a query and return its first row, or ``None`` if it could not run."""
        return self._execute(query, params, what="key_probe")

    def column_stats(
        self, table: str, columns: Sequence[str], *, sample: bool
    ) -> Optional[dict[str, tuple[int, int, int]]]:
        """``{column: (rows, non_null, distinct)}`` for every column, in one query."""
        if not columns:
            return {}
        cols = [self._qi(c) for c in columns]
        src = f'{self._qt(table)}'
        params: tuple = ()
        if sample:
            src = f"(SELECT {', '.join(cols)} FROM {src} LIMIT %s) AS s"
            params = (self.limit,)
        parts = ["COUNT(*)"] + [f"COUNT({c}), COUNT(DISTINCT {c})" for c in cols]
        row = self._row(f"SELECT {', '.join(parts)} FROM {src}", params)  # noqa: S608
        if row is None:
            return None
        total = int(row[0])
        return {
            name: (total, int(row[1 + 2 * i]), int(row[2 + 2 * i]))
            for i, name in enumerate(columns)
        }

    def combo_stats(
        self, table: str, columns: Sequence[str], *, sample: bool
    ) -> Optional[tuple[int, int, int]]:
        """``(rows, rows with every column non-null, distinct non-null tuples)``."""
        if not columns:
            return None
        cols = [self._qi(c) for c in columns]
        joined = ", ".join(cols)
        not_null = " AND ".join(f"{c} IS NOT NULL" for c in cols)
        limit = " LIMIT %s" if sample else ""
        params: tuple = (self.limit,) if sample else ()
        # Every derived table is aliased: Postgres rejects an unaliased subquery in
        # FROM, where Snowflake accepts one.
        row = self._row(
            f'WITH s AS (SELECT {joined} FROM {self._qt(table)}{limit}) '  # noqa: S608
            "SELECT (SELECT COUNT(*) FROM s), "
            f"(SELECT COUNT(*) FROM s WHERE {not_null}), "
            f"(SELECT COUNT(*) FROM (SELECT DISTINCT {joined} FROM s WHERE {not_null}) AS d)",
            params,
        )
        if row is None:
            return None
        return int(row[0]), int(row[1]), int(row[2])

    def _scalar(self, query: str, params: tuple) -> SamplerResult:
        """Run a bounded scalar query, returning its float value or ``None``."""
        row = self._execute(query, params, what="denorm_sampler")
        if not row or row[0] is None:
            return None
        return float(row[0])

    def distinct_ratio(self, table: str, column: str) -> SamplerResult:
        q = f"""
            WITH s AS (
                SELECT {self._qi(column)} AS v
                FROM {self._qt(table)}
                WHERE {self._qi(column)} IS NOT NULL
                LIMIT %s
            )
            SELECT COUNT(DISTINCT v)::float / GREATEST(COUNT(*), 1)::float FROM s
        """  # noqa: S608 - identifiers are quoted; schema is from the catalog
        return self._scalar(q, (self.limit,))

    def group_single_valued(
        self, table: str, determinant_columns: list[str], dependent_column: str
    ) -> SamplerResult:
        det = ", ".join(self._qi(c) for c in determinant_columns)
        not_null = " AND ".join(f"{self._qi(c)} IS NOT NULL" for c in determinant_columns)
        q = f"""
            WITH s AS (
                SELECT {det}, {self._qi(dependent_column)} AS dep
                FROM {self._qt(table)}
                LIMIT %s
            ),
            g AS (
                SELECT {det}, COUNT(DISTINCT dep) AS dcount
                FROM s
                WHERE {not_null}
                GROUP BY {det}
            )
            SELECT COALESCE(AVG(CASE WHEN dcount <= 1 THEN 1.0 ELSE 0.0 END), 0)::float
            FROM g
        """  # noqa: S608 - identifiers are quoted; schema is from the catalog
        return self._scalar(q, (self.limit,))

    def delimiter_rate(self, table: str, column: str, delimiter: str) -> SamplerResult:
        q = f"""
            WITH s AS (
                SELECT {self._qi(column)} AS v
                FROM {self._qt(table)}
                WHERE {self._qi(column)} IS NOT NULL
                LIMIT %s
            )
            SELECT COALESCE(AVG(CASE WHEN strpos(v, %s) > 0 THEN 1.0 ELSE 0.0 END), 0)::float
            FROM s
        """  # noqa: S608 - identifiers are quoted; schema is from the catalog
        return self._scalar(q, (self.limit, delimiter))


# ── Shared SQL sampler plumbing ──────────────────────────────────────


_S = TypeVar("_S", bound="_SqlSampler")


class _SqlSampler:
    """Connection plumbing the SQL value samplers share (#59).

    A subclass supplies ``_conn_lazy`` and its dialect's SQL. :meth:`_row` runs one query
    and returns the first row, or ``None`` when it could not connect or run (logged as
    ``<_log_name>_connect_failed`` / ``_query_failed``): a probe that cannot measure
    answers "not evaluated", never a number. :meth:`_two_pass` is the strict-then-normalized
    overlap decision (#12). Postgres and Snowflake keep their own governed query paths.
    """

    #: Prefix of this sampler's log events.
    _log_name = "sql_sampler"
    _conn: Any = None

    def _conn_lazy(self) -> Any:  # pragma: no cover - every subclass overrides it
        raise NotImplementedError

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def __enter__(self: _S) -> _S:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _fetch_first(self, conn: Any, query: str, params: tuple) -> Optional[tuple]:
        """The first row of ``query``, through a DB-API cursor."""
        cur = conn.cursor()
        try:
            if params:
                cur.execute(query, params)
            else:
                cur.execute(query)
            row = cur.fetchone()
        finally:
            cur.close()
        return tuple(row) if row else None

    def _row(self, query: str, params: tuple) -> Optional[tuple]:
        """Run ``query`` and return its first row, or ``None`` if it could not run."""
        try:
            conn = self._conn_lazy()
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{self._log_name}_connect_failed", error=str(err))
            return None
        try:
            return self._fetch_first(conn, query, params)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{self._log_name}_query_failed", error=str(err))
            return None

    def _scalar(self, query: str, params: tuple) -> SamplerResult:
        row = self._row(query, params)
        if not row or row[0] is None:
            return None
        return float(row[0])

    @staticmethod
    def _two_pass(run: Callable[[bool], Optional[float]]) -> SamplerResult:
        """Strict overlap, then the normalized one only when the strict is inconclusive.

        ``None`` from the strict pass (a failed query, or no non-null local value) means
        the pair was not evaluated, which is not a zero: zero is the veto (#52).
        """
        exact = run(False)
        if exact is None:
            return None
        if _conclusive(exact):
            return Overlap(exact, normalized=False)
        return _better(exact, run(True))


class _DerivedTableSampler(_SqlSampler):
    """The MySQL and Databricks measurements, written once (#59).

    Neither has a ``FILTER (WHERE …)`` aggregate, so the overlap fraction is a
    ``SUM(CASE WHEN …)`` over a ``LEFT JOIN`` of two distinct-valued derived tables; only
    the local side is bounded (#10). The dialects differ in four places, and a subclass
    overrides only those: :meth:`_qt` (table name), :meth:`_as_text` (text cast),
    :meth:`_limit` (row cap and its parameters) and :attr:`_strict_on` (the strict join).
    """

    #: The strict pass's join condition. The normalized pass has folded case already.
    _strict_on = "l.v = f.v"
    schema_name: str
    limit: int

    def _qi(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def _qt(self, table: str) -> str:
        return f"{self._qi(self.schema_name)}.{self._qi(table)}"

    def _as_text(self, expr: str) -> str:
        return f"CAST({expr} AS CHAR)"

    def _limit(self) -> tuple[str, tuple]:
        return "LIMIT %s", (self.limit,)

    def __call__(
        self,
        local_table: str,
        local_column: str,
        foreign_table: str,
        foreign_column: str,
    ) -> SamplerResult:
        """Return the fraction of distinct local values present in the
        foreign column, or ``None`` if the query failed."""
        # Issue #10: the local side is bounded (an estimate is fair); the referenced
        # side is not. Comparing against a sampled parent is a different question, and
        # a valid FK into a table larger than the bound would score near zero.
        # Issue #12: a key differing only in case or padding must not read as absent.
        # The normalized pass is a second scan of the referenced column, so it runs
        # only when the strict score leaves the outcome open.
        lc, fc = self._qi(local_column), self._qi(foreign_column)
        limit, params = self._limit()

        def run(norm: bool) -> Optional[float]:
            lk = f"LOWER(TRIM({self._as_text(lc)}))" if norm else lc
            fk_ = f"LOWER(TRIM({self._as_text(fc)}))" if norm else fc
            on = "l.v = f.v" if norm else self._strict_on
            q = f"""
                SELECT SUM(CASE WHEN f.v IS NOT NULL THEN 1 ELSE 0 END)
                           / GREATEST(COUNT(*), 1) AS overlap
                FROM (
                    SELECT DISTINCT {lk} AS v
                    FROM {self._qt(local_table)}
                    WHERE {lc} IS NOT NULL
                    {limit}
                ) l
                LEFT JOIN (
                    SELECT DISTINCT {fk_} AS v
                    FROM {self._qt(foreign_table)}
                    WHERE {fc} IS NOT NULL
                ) f ON {on}
            """  # noqa: S608 - identifiers are backtick-quoted; the limit is an int we own
            row = self._row(q, params)
            return None if not row or row[0] is None else float(row[0])

        return self._two_pass(run)

    # ── Denormalization probes (PRD Phase 11) ──────────────────────

    def distinct_ratio(self, table: str, column: str) -> SamplerResult:
        limit, params = self._limit()
        q = f"""
            SELECT COUNT(DISTINCT v) / GREATEST(COUNT(*), 1) AS ratio
            FROM (
                SELECT {self._qi(column)} AS v
                FROM {self._qt(table)}
                WHERE {self._qi(column)} IS NOT NULL
                {limit}
            ) s
        """  # noqa: S608 - identifiers are backtick-quoted; the limit is an int we own
        return self._scalar(q, params)

    def group_single_valued(
        self, table: str, determinant_columns: list[str], dependent_column: str
    ) -> SamplerResult:
        limit, params = self._limit()
        det = ", ".join(self._qi(c) for c in determinant_columns)
        not_null = " AND ".join(f"{self._qi(c)} IS NOT NULL" for c in determinant_columns)
        q = f"""
            SELECT AVG(CASE WHEN dcount <= 1 THEN 1.0 ELSE 0.0 END) AS frac
            FROM (
                SELECT COUNT(DISTINCT dep) AS dcount
                FROM (
                    SELECT {det}, {self._qi(dependent_column)} AS dep
                    FROM {self._qt(table)}
                    {limit}
                ) s
                WHERE {not_null}
                GROUP BY {det}
            ) g
        """  # noqa: S608 - identifiers are backtick-quoted; the limit is an int we own
        return self._scalar(q, params)

    def delimiter_rate(self, table: str, column: str, delimiter: str) -> SamplerResult:
        limit, params = self._limit()
        q = f"""
            SELECT AVG(CASE WHEN LOCATE(%s, v) > 0 THEN 1.0 ELSE 0.0 END) AS rate
            FROM (
                SELECT {self._qi(column)} AS v
                FROM {self._qt(table)}
                WHERE {self._qi(column)} IS NOT NULL
                {limit}
            ) s
        """  # noqa: S608 - identifiers are backtick-quoted; the limit is an int we own
        return self._scalar(q, (delimiter, *params))


# ── Concrete MySQL value sampler ────────────────────────────────────


class MySQLValueSampler(_DerivedTableSampler):
    """Sampler that computes FK value-overlap ratios via MySQL / MariaDB.

    The MySQL analog of :class:`PostgresValueSampler`; its SQL is
    :class:`_DerivedTableSampler`'s (default 10k rows on the local side).

    The database to query is taken from the connection string's path
    component; a non-default ``schema_name`` overrides it. The sampler is
    resilient: any driver error is logged and surfaced as ``None`` so the
    name-based score still wins. Call :meth:`close` to release the connection.
    """

    _log_name = "mysql_sampler"
    # BINARY on the strict join so the measurement does not depend on the server's
    # collation, which is often case-insensitive.
    _strict_on = "BINARY l.v = BINARY f.v"

    def __init__(
        self,
        connection_string: str,
        *,
        schema_name: str = "",
        limit: int = 10_000,
    ) -> None:
        from .connectors.mysql import _DEFAULT_SCHEMA_SENTINELS, _parse_mysql_url

        self.connection_string = connection_string
        self.limit = max(100, int(limit))
        self._connect_params = _parse_mysql_url(connection_string)
        if schema_name in _DEFAULT_SCHEMA_SENTINELS:
            self.schema_name = self._connect_params["database"]
        else:
            self.schema_name = schema_name
            self._connect_params["database"] = schema_name
        self._conn = None

    def _conn_lazy(self):
        if self._conn is None:
            from .connectors.mysql import _load_pymysql

            pymysql = _load_pymysql()
            self._conn = pymysql.connect(**self._connect_params)
        return self._conn


# ── Concrete SQL Server value sampler ───────────────────────────────


class SQLServerValueSampler(_SqlSampler):
    """Sampler that computes FK value-overlap ratios via Microsoft SQL Server.

    The SQL Server analog of :class:`MySQLValueSampler`, using T-SQL ``TOP (n)``
    (no ``LIMIT``), bracket-quoted identifiers, and ``CAST(... AS FLOAT)`` for
    the overlap fraction. The schema namespace is taken from ``schema_name``
    (the historical ``public`` default folds to ``dbo``); the database comes
    from the connection string. Driver errors are logged and surfaced as
    ``None`` so the name-based score still wins.
    """

    def __init__(
        self,
        connection_string: str,
        *,
        schema_name: str = "dbo",
        limit: int = 10_000,
    ) -> None:
        from .connectors.mssql import _DEFAULT_SCHEMA_SENTINELS, _parse_mssql_url

        self.connection_string = connection_string
        self.limit = max(100, int(limit))
        self._connect_params = _parse_mssql_url(connection_string)
        self.schema_name = "dbo" if schema_name in _DEFAULT_SCHEMA_SENTINELS else schema_name
        self._conn = None

    _log_name = "sqlserver_sampler"

    def _conn_lazy(self):
        if self._conn is None:
            from .connectors.mssql import _load_pymssql

            pymssql = _load_pymssql()
            self._conn = pymssql.connect(**self._connect_params)
        return self._conn

    def _qi(self, name: str) -> str:
        return "[" + name.replace("]", "]]") + "]"

    def __call__(
        self,
        local_table: str,
        local_column: str,
        foreign_table: str,
        foreign_column: str,
    ) -> SamplerResult:
        """Return the fraction of distinct local values present in the
        foreign column, or ``None`` if the query failed."""
        s = self._qi(self.schema_name)
        # Issue #10: the local side is bounded (an estimate is fair); the referenced
        # side is not. Comparing against a sampled parent is a different question, and
        # a valid FK into a table larger than the bound would score near zero.
        # Issue #12: a key differing only in case or padding must not read as absent.
        # The strict pass forces a binary collation because SQL Server databases are
        # frequently created case-insensitive, and the answer must not depend on that.
        # Note this controls *case* only: SQL Server blank-pads when comparing strings
        # regardless of collation, so a trailing-space difference matches on the strict
        # pass here while it does not on Postgres. The normalized pass is what makes
        # the two engines agree; it runs only when the strict score leaves the outcome
        # open, because it is a second scan of the referenced column.
        def q(norm: bool) -> str:
            lc, fc = self._qi(local_column), self._qi(foreign_column)
            cast = "CAST({} AS NVARCHAR(MAX))"
            lk = f"LOWER(LTRIM(RTRIM({cast.format(lc)})))" if norm else lc
            fk_ = f"LOWER(LTRIM(RTRIM({cast.format(fc)})))" if norm else fc
            # A binary collation on the strict join so the measurement does not depend
            # on the database's collation; the normalized pass has folded case away.
            bin2 = "COLLATE Latin1_General_BIN2"
            on = (
                "l.v = f.v"
                if norm
                else f"{cast.format('l.v')} {bin2} = {cast.format('f.v')} {bin2}"
            )
            return f"""
                SELECT CAST(SUM(CASE WHEN f.v IS NOT NULL THEN 1 ELSE 0 END) AS FLOAT)
                           / NULLIF(COUNT(*), 0) AS overlap
                FROM (
                    SELECT DISTINCT TOP (%s) {lk} AS v
                    FROM {s}.{self._qi(local_table)}
                    WHERE {lc} IS NOT NULL
                ) l
                LEFT JOIN (
                    SELECT DISTINCT {fk_} AS v
                    FROM {s}.{self._qi(foreign_table)}
                    WHERE {fc} IS NOT NULL
                ) f ON {on}
            """  # noqa: S608 - identifiers are bracket-quoted; schema is from the catalog

        def run(norm: bool) -> Optional[float]:
            row = self._row(q(norm), (self.limit,))
            return None if not row or row[0] is None else float(row[0])

        return self._two_pass(run)

    # ── Denormalization probes (PRD Phase 11) ──────────────────────

    def distinct_ratio(self, table: str, column: str) -> SamplerResult:
        s = self._qi(self.schema_name)
        q = f"""
            SELECT CAST(COUNT(DISTINCT v) AS FLOAT) / NULLIF(COUNT(*), 0) AS ratio
            FROM (
                SELECT TOP (%s) {self._qi(column)} AS v
                FROM {s}.{self._qi(table)}
                WHERE {self._qi(column)} IS NOT NULL
            ) l
        """  # noqa: S608 - identifiers are bracket-quoted; schema is from the catalog
        return self._scalar(q, (self.limit,))

    def group_single_valued(
        self, table: str, determinant_columns: list[str], dependent_column: str
    ) -> SamplerResult:
        s = self._qi(self.schema_name)
        det = ", ".join(self._qi(c) for c in determinant_columns)
        not_null = " AND ".join(f"{self._qi(c)} IS NOT NULL" for c in determinant_columns)
        q = f"""
            SELECT AVG(CAST(CASE WHEN dcount <= 1 THEN 1.0 ELSE 0.0 END AS FLOAT)) AS frac
            FROM (
                SELECT COUNT(DISTINCT dep) AS dcount
                FROM (
                    SELECT TOP (%s) {det}, {self._qi(dependent_column)} AS dep
                    FROM {s}.{self._qi(table)}
                ) l
                WHERE {not_null}
                GROUP BY {det}
            ) g
        """  # noqa: S608 - identifiers are bracket-quoted; schema is from the catalog
        return self._scalar(q, (self.limit,))

    def delimiter_rate(self, table: str, column: str, delimiter: str) -> SamplerResult:
        s = self._qi(self.schema_name)
        q = f"""
            SELECT AVG(CAST(CASE WHEN CHARINDEX(%s, v) > 0 THEN 1.0 ELSE 0.0 END AS FLOAT)) AS rate
            FROM (
                SELECT TOP (%s) {self._qi(column)} AS v
                FROM {s}.{self._qi(table)}
                WHERE {self._qi(column)} IS NOT NULL
            ) l
        """  # noqa: S608 - identifiers are bracket-quoted; schema is from the catalog
        return self._scalar(q, (delimiter, self.limit))


# ── Concrete Databricks value sampler ───────────────────────────────


class DatabricksValueSampler(_DerivedTableSampler):
    """Sampler that computes FK value-overlap ratios via Databricks SQL.

    Value overlap matters more on Databricks than anywhere else: Unity Catalog
    never enforces primary/foreign keys, so a declared FK is unvalidated and an
    undeclared one is invisible to the catalog. Sampling is the only evidence
    that two columns actually join.

    The SQL is :class:`_DerivedTableSampler`'s, as for MySQL. Tables are
    addressed with Unity Catalog's three-level ``catalog.schema.table`` name.
    Row limits are inlined rather than bound, because Spark SQL requires a
    constant in ``LIMIT``; they are ints under our control, never user text.

    Any driver error is logged and surfaced as ``None`` so the name-based score
    still wins. Call :meth:`close` to release the connection.
    """

    _log_name = "databricks_sampler"

    def __init__(
        self,
        connection_string: str,
        *,
        schema_name: str = "",
        limit: int = 10_000,
    ) -> None:
        from .connectors.databricks_source import (
            _DEFAULT_SCHEMA_SENTINELS,
            _parse_databricks_url,
        )

        self.connection_string = connection_string
        self.limit = max(100, int(limit))
        parts = _parse_databricks_url(connection_string)
        self._connect_params = {
            "server_hostname": parts["server_hostname"],
            "http_path": parts["http_path"],
            "access_token": parts["access_token"],
        }
        self.catalog = parts["catalog"]
        self.schema_name = (
            parts["schema"] if schema_name in _DEFAULT_SCHEMA_SENTINELS else schema_name
        )
        self._conn = None

    def _conn_lazy(self):
        if self._conn is None:
            from .connectors.databricks_source import _load_databricks

            dbsql = _load_databricks()
            self._conn = dbsql.connect(
                catalog=self.catalog, schema=self.schema_name, **self._connect_params
            )
        return self._conn

    def _qt(self, table: str) -> str:
        """Fully-qualified ``catalog.schema.table``."""
        return f"{self._qi(self.catalog)}.{self._qi(self.schema_name)}.{self._qi(table)}"

    def _as_text(self, expr: str) -> str:
        return f"CAST({expr} AS STRING)"

    def _limit(self) -> tuple[str, tuple]:
        return f"LIMIT {int(self.limit)}", ()


# ── Concrete DuckDB value sampler ───────────────────────────────────


class DuckDbValueSampler(_SqlSampler):
    """Sampler that computes FK value-overlap and denormalization probes via DuckDB.

    DuckDB is this project's always-on engine (see the testing matrix in
    ``docs/IMPLEMENTATION-PLAN.md``): embedded, server-less, and speaking a
    Postgres-shaped dialect. That makes it the one place the sampler *SQL* can be
    executed for real in ordinary CI, with no Docker and no cloud account.

    That matters more than it sounds. Every other sampler is covered only by mock
    cursors primed to return a canned number, which verifies the plumbing and not
    one character of the SQL — and precisely that gap let two of the CSV probes ship
    a ``TypeError`` on their first contact with real data.

    Parameters are bound (DuckDB uses ``?``), identifiers are double-quoted, and any
    driver error is logged and surfaced as ``None`` so a failed measurement degrades
    to "not evaluated" rather than a wrong answer.
    """

    def __init__(
        self,
        connection_string: str,
        *,
        schema_name: str = "main",
        limit: int = 10_000,
    ) -> None:
        from .connectors.duckdb_source import _DEFAULT_SCHEMA_SENTINELS

        self.connection_string = connection_string
        self.schema_name = (
            "main" if schema_name in _DEFAULT_SCHEMA_SENTINELS else schema_name
        )
        self.limit = max(100, int(limit))
        self._conn = None

    def _conn_lazy(self):
        if self._conn is None:
            from .connectors.duckdb_source import _load_duckdb

            duckdb = _load_duckdb()
            self._conn = duckdb.connect(self.connection_string, read_only=True)
        return self._conn

    _log_name = "duckdb_sampler"

    def _qi(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    # ── Dialect hooks: everything below them is shared with SqliteValueSampler ──

    def _as_text(self, expr: str) -> str:
        return f"CAST({expr} AS VARCHAR)"

    def _ratio(self, numerator: str, denominator: str) -> str:
        return f"{numerator}::DOUBLE / GREATEST({denominator}, 1)::DOUBLE"

    def _real(self, expr: str) -> str:
        return f"({expr})::DOUBLE"

    def _contains(self, value: str, needle: str) -> str:
        return f"strpos({value}, {needle}) > 0"

    def _qt(self, table: str) -> str:
        return f"{self._qi(self.schema_name)}.{self._qi(table)}"

    def _fetch_first(self, conn: Any, query: str, params: tuple) -> Optional[tuple]:
        row = conn.execute(query, params).fetchone() if params else conn.execute(query).fetchone()
        return tuple(row) if row else None

    def __call__(
        self,
        local_table: str,
        local_column: str,
        foreign_table: str,
        foreign_column: str,
    ) -> SamplerResult:
        """Fraction of distinct local values present in the foreign column."""
        # Issue #10: the local side is bounded (an estimate is fair); the referenced
        # side is not. Comparing against a sampled parent is a different question, and
        # a valid FK into a table larger than the bound would score near zero.
        # Issue #12: a key differing only in case or padding must not read as absent.
        # The normalized pass rescans the referenced column, so it runs only when the
        # strict score leaves the outcome open (_STRICT_IS_CONCLUSIVE).
        lc, fc = self._qi(local_column), self._qi(foreign_column)

        def run(norm: bool) -> Optional[float]:
            lk = f"lower(trim({self._as_text(lc)}))" if norm else lc
            fk_ = f"lower(trim({self._as_text(fc)}))" if norm else fc
            share = self._ratio("COUNT(*) FILTER (WHERE f.v IS NOT NULL)", "COUNT(*)")
            q = f"""
                WITH l AS (
                    SELECT DISTINCT {lk} AS v
                    FROM {self._qt(local_table)}
                    WHERE {lc} IS NOT NULL
                    LIMIT ?
                ),
                f AS (
                    SELECT DISTINCT {fk_} AS v
                    FROM {self._qt(foreign_table)}
                    WHERE {fc} IS NOT NULL
                )
                SELECT CASE WHEN COUNT(*) = 0 THEN NULL ELSE {share} END
                FROM l LEFT JOIN f ON l.v = f.v
            """  # noqa: S608 - identifiers are quoted; limits are bound ints we own
            row = self._row(q, (self.limit,))
            return None if not row or row[0] is None else float(row[0])

        return self._two_pass(run)

    # ── Denormalization probes ─────────────────────────────────────

    def distinct_ratio(self, table: str, column: str) -> SamplerResult:
        q = f"""
            WITH s AS (
                SELECT {self._qi(column)} AS v
                FROM {self._qt(table)}
                WHERE {self._qi(column)} IS NOT NULL
                LIMIT ?
            )
            SELECT {self._ratio("COUNT(DISTINCT v)", "COUNT(*)")} FROM s
        """  # noqa: S608 - identifiers are quoted; limit is a bound int we own
        return self._scalar(q, (self.limit,))

    def group_single_valued(
        self, table: str, determinant_columns: list[str], dependent_column: str
    ) -> SamplerResult:
        det = ", ".join(self._qi(c) for c in determinant_columns)
        not_null = " AND ".join(f"{self._qi(c)} IS NOT NULL" for c in determinant_columns)
        single = self._real("COALESCE(AVG(CASE WHEN dcount <= 1 THEN 1.0 ELSE 0.0 END), 0)")
        q = f"""
            WITH s AS (
                SELECT {det}, {self._qi(dependent_column)} AS dep
                FROM {self._qt(table)}
                LIMIT ?
            ),
            g AS (
                SELECT {det}, COUNT(DISTINCT dep) AS dcount
                FROM s WHERE {not_null} GROUP BY {det}
            )
            SELECT {single}
            FROM g
        """  # noqa: S608 - identifiers are quoted; limit is a bound int we own
        return self._scalar(q, (self.limit,))

    def delimiter_rate(self, table: str, column: str, delimiter: str) -> SamplerResult:
        has = self._contains("v", "?")
        rate = self._real(f"COALESCE(AVG(CASE WHEN {has} THEN 1.0 ELSE 0.0 END), 0)")
        q = f"""
            WITH s AS (
                SELECT {self._qi(column)} AS v
                FROM {self._qt(table)}
                WHERE {self._qi(column)} IS NOT NULL
                LIMIT ?
            )
            SELECT {rate}
            FROM s
        """  # noqa: S608 - identifiers are quoted; limit is a bound int we own
        return self._scalar(q, (self.limit, delimiter))


class SqliteValueSampler(DuckDbValueSampler):
    """The DuckDB sampler's measurements, in SQLite's dialect, over a read-only file.

    SQLite takes the same bound ``?`` parameters, double-quoted identifiers and
    ``"main"."table"`` names, and supports ``COUNT(*) FILTER (WHERE ...)``; only the
    casts, ``GREATEST`` and ``strpos`` differ, so those are all this class changes.
    A SQLite file has one schema, so ``schema_name`` is always ``main``.
    """

    _log_name = "sqlite_sampler"

    def __init__(
        self,
        connection_string: str,
        *,
        schema_name: str = "main",
        limit: int = 10_000,
    ) -> None:
        super().__init__(connection_string, schema_name="main", limit=limit)

    def _conn_lazy(self):
        if self._conn is None:
            from .connectors.sqlite_source import connect_read_only

            self._conn = connect_read_only(self.connection_string)
        return self._conn

    def _as_text(self, expr: str) -> str:
        return f"CAST({expr} AS TEXT)"

    def _ratio(self, numerator: str, denominator: str) -> str:
        return f"CAST({numerator} AS REAL) / max({denominator}, 1)"

    def _real(self, expr: str) -> str:
        return f"CAST({expr} AS REAL)"

    def _contains(self, value: str, needle: str) -> str:
        return f"instr({value}, {needle}) > 0"


# ── Concrete CSV value sampler ──────────────────────────────────────


class CsvValueSampler:
    """Sampler that computes FK value-overlap ratios across CSV files.

    A CSV source is a directory of files (one per table, filename stem =
    table name). For each ``(local_column, foreign_column)`` pair we read
    just those two columns (bounded by ``limit`` rows) and return the
    fraction of distinct local values that also appear in the foreign
    column — the same statistic :class:`PostgresValueSampler` computes
    with a ``LEFT JOIN``.

    Values are compared as *raw text* (columns are read with type
    inference disabled) so that ``1`` and ``1.0`` — which Polars might
    otherwise type as int on one side and float on the other — still
    match on their textual token, which is what actually joins in the
    file.

    The sampler is resilient: any read failure (missing file, unreadable
    column, Polars error) is logged and surfaced as ``None`` so the
    name-based score still wins. ``close`` is a no-op; the context-manager
    protocol is provided for parity with :class:`PostgresValueSampler`.
    """

    def __init__(
        self,
        connection_string: str,
        *,
        delimiter: str = ",",
        has_header: bool = True,
        limit: int = 10_000,
    ) -> None:
        from pathlib import Path

        self.connection_string = connection_string
        self.delimiter = delimiter
        self.has_header = has_header
        self.limit = max(100, int(limit))
        self.directory = Path(connection_string).expanduser()

    def close(self) -> None:
        return None

    def __enter__(self) -> "CsvValueSampler":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _resolve(self, table: str):
        from .connectors.csv_source import resolve_csv_table_path

        return resolve_csv_table_path(self.directory, table)

    def _distinct_text_values(
        self, table: str, column: str, *, bounded: bool = True
    ) -> Optional[set[str]]:
        """Distinct, non-empty textual values for a column, or ``None`` if unreadable.

        ``bounded`` caps the read at ``limit`` rows. The *referenced* side of an overlap
        comparison must pass ``bounded=False`` (issue #10): a sample of the parent answers
        a different question, and a valid foreign key into a file longer than the bound
        would otherwise score near zero. Reading one column of one CSV in full is the
        price of a correct answer, and for a file-backed source it is a cheap one.
        """
        import polars as pl

        path = self._resolve(table)
        if path is None:
            return None
        try:
            frame = pl.read_csv(
                str(path),
                separator=self.delimiter,
                has_header=self.has_header,
                columns=[column],
                n_rows=self.limit if bounded else None,
                infer_schema_length=0,  # read everything as Utf8 (raw text)
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "csv_fk_sampler_read_failed",
                table=table,
                column=column,
                error=str(err),
            )
            return None
        if not frame.columns:
            return None
        series = frame.get_column(frame.columns[0])
        return {
            v for v in series.to_list() if v is not None and str(v) != ""
        }

    def __call__(
        self,
        local_table: str,
        local_column: str,
        foreign_table: str,
        foreign_column: str,
    ) -> SamplerResult:
        """Return the fraction of distinct local values present in the
        foreign column, or ``None`` if either side could not be read."""
        local_vals = self._distinct_text_values(local_table, local_column)
        if not local_vals:
            return None
        foreign_vals = self._distinct_text_values(foreign_table, foreign_column, bounded=False)
        if foreign_vals is None:
            return None
        # Issue #12: a key differing only in case or padding must not read as absent.
        # Normalizing means another pass over the whole referenced column, which for a
        # file-backed source is already the expensive half, so it runs only when the
        # strict score leaves the outcome open (_STRICT_IS_CONCLUSIVE).
        exact = len(local_vals & foreign_vals) / len(local_vals)
        if _conclusive(exact):
            return Overlap(exact, normalized=False)
        local_norm = {v.strip().lower() for v in local_vals}
        foreign_norm = {v.strip().lower() for v in foreign_vals}
        norm = len(local_norm & foreign_norm) / len(local_norm) if local_norm else None
        return _better(exact, norm)

    # ── Denormalization probes (PRD Phase 11) ──────────────────────

    def _read_columns(self, table: str, columns: list[str]):
        """Read ``columns`` of ``table`` as raw text (type inference off), bounded
        by ``limit``; returns a Polars frame or ``None`` if unreadable."""
        import polars as pl

        path = self._resolve(table)
        if path is None:
            return None
        try:
            return pl.read_csv(
                str(path),
                separator=self.delimiter,
                has_header=self.has_header,
                columns=columns,
                n_rows=self.limit,
                infer_schema_length=0,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "csv_denorm_sampler_read_failed", table=table, columns=columns, error=str(err)
            )
            return None

    def distinct_ratio(self, table: str, column: str) -> SamplerResult:
        import polars as pl

        frame = self._read_columns(table, [column])
        if frame is None or not frame.columns:
            return None
        # Filter on the frame, not the Series: ``DataFrame.filter`` takes
        # expressions while ``Series.filter`` wants a boolean mask, and passing an
        # expression to the latter raises. Reading text with inference off means
        # ``!= ""`` is a valid emptiness test on every column.
        name = frame.columns[0]
        frame = frame.filter(pl.col(name).is_not_null() & (pl.col(name) != ""))
        series = frame.get_column(name)
        total = series.len()
        if total == 0:
            return None
        return float(series.n_unique() / total)

    def group_single_valued(
        self, table: str, determinant_columns: list[str], dependent_column: str
    ) -> SamplerResult:
        import polars as pl

        frame = self._read_columns(table, [*determinant_columns, dependent_column])
        if frame is None or not frame.columns:
            return None
        for d in determinant_columns:
            frame = frame.filter(pl.col(d).is_not_null() & (pl.col(d) != ""))
        if frame.is_empty():
            return None
        grouped = frame.group_by(determinant_columns).agg(
            pl.col(dependent_column).n_unique().alias("dcount")
        )
        if grouped.is_empty():
            return None
        single = grouped.get_column("dcount") <= 1
        return float(single.sum() / single.len())

    def delimiter_rate(self, table: str, column: str, delimiter: str) -> SamplerResult:
        import polars as pl

        frame = self._read_columns(table, [column])
        if frame is None or not frame.columns:
            return None
        name = frame.columns[0]
        # Same as distinct_ratio: filter the frame (expressions) rather than the
        # Series (boolean mask), which raises on an expression.
        frame = frame.filter(pl.col(name).is_not_null() & (pl.col(name) != ""))
        series = frame.get_column(name)
        total = series.len()
        if total == 0:
            return None
        contains = series.str.contains(delimiter, literal=True)
        return float(contains.sum() / total)


# ── Concrete Snowflake value sampler (cost-governed) ──────────────────


class SnowflakeValueSampler:
    """FK value-overlap sampler for Snowflake, with a cost governor.

    Every query consumes warehouse credits, so -- following the BigQuery design
    (``docs/DESIGN-ADDENDUM-bigquery.md`` D3) -- probing is bounded three ways,
    none of them unbounded by default:

    - **Query budget.** At most ``max_queries`` probe queries per sampler. On
      exhaustion every probe returns ``None`` (the protocol's "not evaluated"),
      so inference falls back to names instead of failing or spending more.
    - **Statement timeout.** ``STATEMENT_TIMEOUT_IN_SECONDS`` is set on the
      session, so a probe that turns out expensive is cancelled by Snowflake.
    - **Per-column cache.** The protocol is called once per candidate *pair*;
      each column's bounded distinct set is fetched once and reused, turning
      O(pairs) scans into O(columns).

    Probes carry ``QUERY_TAG`` (default ``rsa-value-sampler``) so their cost is
    findable in ``QUERY_HISTORY``. ``stats`` reports what was spent.

    **Overlap is measured against the whole foreign column**, not a prefix of
    it. The local side is a bounded distinct sample; when the foreign column's
    distinct values fit the bound they are compared client-side, otherwise the
    sampled local values are checked server-side against the full column.
    (Comparing against an arbitrary ``LIMIT``-ed slice of a large foreign table
    scores a valid FK near zero.) Values are compared as text that Snowflake
    renders with ``TO_VARCHAR`` on both sides, so the client-side and server-side
    paths agree for every column type.

    Pass ``connection_string`` (password, key-pair or ``authenticator`` URL) or an
    open ``connection``. A supplied connection is used as-is: its session
    settings are never changed and it is not closed. Every probe carries a
    per-query ``timeout``, so the time limit holds on either kind of connection
    even when session settings cannot be applied. A failed connection is tried
    once per sampler, never once per probe.
    """

    def __init__(
        self,
        connection_string: str | None = None,
        *,
        connection: Any = None,
        schema_name: str = "PUBLIC",
        limit: int = 10_000,
        max_queries: int = 200,
        statement_timeout_s: int = 60,
        query_tag: str = "rsa-value-sampler",
    ) -> None:
        if connection is None and not connection_string:
            raise ValueError("SnowflakeValueSampler needs a connection_string or a connection")
        if int(max_queries) < 1:
            raise ValueError("max_queries must be at least 1: sampling has no unbounded mode")
        if int(statement_timeout_s) < 1:
            raise ValueError("statement_timeout_s must be at least 1 second")
        self.connection_string = connection_string
        self.limit = max(100, int(limit))
        self.max_queries = int(max_queries)
        self.statement_timeout_s = int(statement_timeout_s)
        self.query_tag = query_tag
        self._conn = connection
        self._owns_conn = connection is None
        self._session_ready = False
        self._connect_error: str | None = None
        self._connect_params: dict[str, Any] = {}
        self.schema_name = schema_name.upper() if schema_name else "PUBLIC"
        if connection_string:
            from .connectors.snowflake import _parse_snowflake_url

            self._connect_params = _parse_snowflake_url(connection_string)
            url_schema = self._connect_params.pop("_url_schema", None)
            if url_schema and schema_name in (None, "", "public", "PUBLIC"):
                self.schema_name = url_schema.upper()
        # A column whose fetch failed is cached as None, so it is not re-queried
        # (and re-charged to the budget) for every candidate pair touching it.
        self._distinct_cache: dict[tuple[str, str], tuple[frozenset[str], bool] | None] = {}
        self.queries_run = 0
        self.cache_hits = 0
        self.budget_exhausted = False

    # ── lifecycle ──

    def close(self) -> None:
        if self._conn is not None and self._owns_conn:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        if self._owns_conn:
            self._conn = None
            self._session_ready = False

    def __enter__(self) -> "SnowflakeValueSampler":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def stats(self) -> dict[str, Any]:
        """What the governor allowed: queries run, cache hits, whether it stopped."""
        return {
            "queries_run": self.queries_run,
            "max_queries": self.max_queries,
            "cache_hits": self.cache_hits,
            "budget_exhausted": self.budget_exhausted,
            "connect_failed": self._connect_error is not None,
        }

    def _conn_lazy(self) -> Any:
        if self._conn is None:
            from .connectors.snowflake import _load_snowflake_connector, _safe_connection_error

            params = dict(self._connect_params)
            params.setdefault("schema", self.schema_name)
            snowflake = _load_snowflake_connector()
            try:
                self._conn = snowflake.connect(**params)
            except Exception as err:
                # ``from None``: the driver's original error can echo the password
                # or key path, and a chained __cause__ is printed by any traceback.
                raise RuntimeError(
                    f"Failed to connect to Snowflake: {_safe_connection_error(err, params)}"
                ) from None
        if self._owns_conn and not self._session_ready:
            self._session_ready = True
            # Session settings are metadata statements (no warehouse), so they are
            # not charged to the probe budget. Only applied to a connection the
            # sampler opened: a caller's session is never altered. Best-effort --
            # the per-query ``timeout`` in :meth:`_run` bounds every probe even
            # if these fail -- but a failure is a warning, not a footnote.
            for stmt in (
                f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {self.statement_timeout_s}",
                "ALTER SESSION SET QUERY_TAG = '" + self.query_tag.replace("'", "''") + "'",
            ):
                try:
                    cur = self._conn.cursor()
                    try:
                        cur.execute(stmt)
                    finally:
                        cur.close()
                except Exception as err:  # noqa: BLE001
                    logger.warning("snowflake_sampler_session_setting_skipped", error=str(err))
        return self._conn

    # ── governed execution ──

    def _run(self, sql: str, params: tuple) -> list[tuple] | None:
        """Execute one probe within budget; ``None`` on exhaustion or any failure."""
        if self.queries_run >= self.max_queries:
            if not self.budget_exhausted:
                self.budget_exhausted = True
                logger.warning(
                    "snowflake_sampler_budget_exhausted",
                    max_queries=self.max_queries,
                    hint="remaining probes return None; inference falls back to names",
                )
            return None
        if self._connect_error is not None:
            return None
        try:
            conn = self._conn_lazy()
        except Exception as err:  # noqa: BLE001
            # One attempt per sampler: retrying per probe turns a bad credential
            # into hundreds of failed logins, enough to lock the service user out.
            self._connect_error = str(err)
            logger.warning(
                "fk_sampler_connect_failed",
                error=self._connect_error,
                hint="remaining probes return None without reconnecting",
            )
            return None
        self.queries_run += 1
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, params, timeout=self.statement_timeout_s)
                return list(cur.fetchall())
            finally:
                cur.close()
        except Exception as err:  # noqa: BLE001
            logger.warning("snowflake_sampler_query_failed", error=str(err))
            return None

    @staticmethod
    def _ident(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def _table(self, table: str) -> str:
        return f"{self._ident(self.schema_name)}.{self._ident(table)}"

    def _distinct(self, table: str, column: str) -> tuple[frozenset[str], bool] | None:
        """Bounded distinct non-null values of a column, cached; flag = complete set.

        Values come back as Snowflake's ``TO_VARCHAR`` text -- the same rendering
        the server-side membership test uses -- so timestamps, booleans and large
        floats compare equal on both paths. (Rendering them in Python instead
        gives ``True`` vs ``true`` and ``10000000000000000`` vs ``1e+16``.)
        """
        key = (table, column)
        if key in self._distinct_cache:
            self.cache_hits += 1
            return self._distinct_cache[key]
        col = self._ident(column)
        rows = self._run(
            f"SELECT DISTINCT TO_VARCHAR({col}) FROM {self._table(table)} WHERE {col} IS NOT NULL LIMIT %s",  # noqa: S608 - quoted identifiers from the catalog
            (self.limit + 1,),
        )
        if rows is None:
            if not self.budget_exhausted and self._connect_error is None:
                self._distinct_cache[key] = None  # the query itself failed
            return None
        values = [r[0] for r in rows]
        result = (frozenset(values[: self.limit]), len(values) <= self.limit)
        self._distinct_cache[key] = result
        return result

    # ── sampler protocol ──

    def __call__(
        self,
        local_table: str,
        local_column: str,
        foreign_table: str,
        foreign_column: str,
    ) -> SamplerResult:
        """Fraction of sampled distinct local values present in the foreign column."""
        local = self._distinct(local_table, local_column)
        if local is None or not local[0]:
            return None
        local_values = local[0]
        foreign = self._distinct(foreign_table, foreign_column)
        if foreign is None:
            return None
        foreign_values, complete = foreign
        # Issue #12: a key differing only in case or padding must not read as absent.
        local_norm = {v.strip().lower() for v in local_values}
        if complete:
            exact = len(local_values & foreign_values) / len(local_values)
            if _conclusive(exact):
                return Overlap(exact, normalized=False)
            foreign_norm = {v.strip().lower() for v in foreign_values}
            return _better(
                exact,
                len(local_norm & foreign_norm) / len(local_norm) if local_norm else None,
            )
        # The foreign column has more distinct values than the bound: test the
        # sampled local values against the whole column, server-side.
        #
        # Unlike the other samplers this keeps *both* comparisons in one statement
        # rather than running the normalized one conditionally. Here the scarce
        # resource is the query budget, not wall-clock: splitting it would cost a
        # second query on exactly the weak pairs issue #12 is about, and the budget is
        # a hard cap that decides how many pairs get evaluated at all. One scan
        # computing two conditional counts is the cheaper trade on this path.
        import json

        fcol = self._ident(foreign_column)
        flatten = "(SELECT VALUE::STRING FROM TABLE(FLATTEN(INPUT => PARSE_JSON(%s))))"
        rows = self._run(
            "SELECT "
            f"COUNT(DISTINCT CASE WHEN TO_VARCHAR({fcol}) IN {flatten} "
            f"THEN TO_VARCHAR({fcol}) END), "
            f"COUNT(DISTINCT CASE WHEN LOWER(TRIM(TO_VARCHAR({fcol}))) IN {flatten} "
            f"THEN LOWER(TRIM(TO_VARCHAR({fcol}))) END) "
            f"FROM {self._table(foreign_table)}",  # noqa: S608
            (json.dumps(sorted(local_values)), json.dumps(sorted(local_norm))),
        )
        if not rows:
            return None
        remote_exact = (
            None if rows[0][0] is None else min(1.0, float(rows[0][0]) / len(local_values))
        )
        remote_norm = (
            None
            if rows[0][1] is None or not local_norm
            else min(1.0, float(rows[0][1]) / len(local_norm))
        )
        return _better(remote_exact, remote_norm)

    # ── Denormalization probes (PRD Phase 11) ──

    def _scalar(self, sql: str, params: tuple) -> SamplerResult:
        rows = self._run(sql, params)
        if not rows or rows[0][0] is None:
            return None
        return float(rows[0][0])

    def distinct_ratio(self, table: str, column: str) -> SamplerResult:
        col = self._ident(column)
        return self._scalar(
            f"WITH s AS (SELECT {col} AS v FROM {self._table(table)} "  # noqa: S608
            f"WHERE {col} IS NOT NULL LIMIT %s) "
            "SELECT COUNT(DISTINCT v) / GREATEST(COUNT(*), 1) FROM s",
            (self.limit,),
        )

    def group_single_valued(
        self, table: str, determinant_columns: list[str], dependent_column: str
    ) -> SamplerResult:
        det = ", ".join(self._ident(c) for c in determinant_columns)
        not_null = " AND ".join(f"{self._ident(c)} IS NOT NULL" for c in determinant_columns)
        return self._scalar(
            f"WITH s AS (SELECT {det}, {self._ident(dependent_column)} AS dep "  # noqa: S608
            f"FROM {self._table(table)} LIMIT %s), "
            f"g AS (SELECT {det}, COUNT(DISTINCT dep) AS dcount FROM s WHERE {not_null} GROUP BY {det}) "
            "SELECT COALESCE(AVG(IFF(dcount <= 1, 1.0, 0.0)), 0) FROM g",
            (self.limit,),
        )

    def delimiter_rate(self, table: str, column: str, delimiter: str) -> SamplerResult:
        col = self._ident(column)
        return self._scalar(
            f"WITH s AS (SELECT TO_VARCHAR({col}) AS v FROM {self._table(table)} "  # noqa: S608
            f"WHERE {col} IS NOT NULL LIMIT %s) "
            "SELECT COALESCE(AVG(IFF(CONTAINS(v, %s), 1.0, 0.0)), 0) FROM s",
            (self.limit, delimiter),
        )

    # ── Key profiling probes (key_profiling.KeyProbe) ──

    def column_stats(
        self, table: str, columns: Sequence[str], *, sample: bool
    ) -> dict[str, tuple[int, int, int]] | None:
        """``{column: (rows, non_null, distinct)}`` in one governed query.

        ``sample=True`` reads the first ``limit`` rows -- enough to *reject* a
        column (any duplicate or NULL is conclusive); ``sample=False`` counts the
        whole table, to *confirm* the survivors.
        """
        if not columns:
            return {}
        cols = [self._ident(c) for c in columns]
        source = f"{self._table(table)}"
        params: tuple = ()
        if sample:
            source = f"(SELECT {', '.join(cols)} FROM {self._table(table)} LIMIT %s)"
            params = (self.limit,)
        parts = ["COUNT(*)"] + [f"COUNT({c}), COUNT(DISTINCT {c})" for c in cols]
        rows = self._run(f"SELECT {', '.join(parts)} FROM {source}", params)  # noqa: S608
        if not rows:
            return None
        r = rows[0]
        total = int(r[0])
        return {
            name: (total, int(r[1 + 2 * i]), int(r[2 + 2 * i])) for i, name in enumerate(columns)
        }

    def combo_stats(
        self, table: str, columns: Sequence[str], *, sample: bool
    ) -> tuple[int, int, int] | None:
        """``(rows, rows with every column non-null, distinct non-null tuples)``."""
        cols = [self._ident(c) for c in columns]
        not_null = " AND ".join(f"{c} IS NOT NULL" for c in cols)
        limit = " LIMIT %s" if sample else ""
        params: tuple = (self.limit,) if sample else ()
        rows = self._run(
            f"WITH s AS (SELECT {', '.join(cols)} FROM {self._table(table)}{limit}) "  # noqa: S608
            f"SELECT (SELECT COUNT(*) FROM s), "
            f"(SELECT COUNT(*) FROM s WHERE {not_null}), "
            f"(SELECT COUNT(*) FROM (SELECT DISTINCT {', '.join(cols)} FROM s WHERE {not_null}))",
            params,
        )
        if not rows:
            return None
        return int(rows[0][0]), int(rows[0][1]), int(rows[0][2])


def create_value_sampler(
    source_type: str | None,
    connection_string: str,
    *,
    pg_schema: str = "public",
    source_params: dict | None = None,
    limit: int = 10_000,
    max_queries: int | None = None,
    statement_timeout_s: int | None = None,
    query_tag: str | None = None,
):
    """Build a value-overlap sampler for a source, or ``None`` if unsupported.

    ``max_queries`` / ``statement_timeout_s`` / ``query_tag`` govern the samplers
    that support them -- Postgres and Snowflake, which are also the two that
    implement key profiling (``column_stats`` / ``combo_stats``). Left as ``None``
    they keep each sampler's own default: Postgres ungoverned, as it always was;
    Snowflake governed by its built-in budget and timeout. Other samplers have no
    governor; a caller that needs one should check ``hasattr(sampler, "stats")``.

    PostgreSQL (incl. ``postgres`` / ``pg`` aliases) → :class:`PostgresValueSampler`,
    MySQL / MariaDB → :class:`MySQLValueSampler`, SQL Server →
    :class:`SQLServerValueSampler`, DuckDB → :class:`DuckDbValueSampler`, Databricks → :class:`DatabricksValueSampler`,
    Snowflake → :class:`SnowflakeValueSampler` (cost-governed),
    CSV → :class:`CsvValueSampler`; any other type returns ``None`` (the caller
    should fall back to name-only inference). Connector / import errors are
    allowed to propagate so callers can decide whether to log-and-continue.
    """
    from .connectors.base import (
        expand_env_vars,
        is_mysql,
        is_postgresql,
        is_sqlserver,
        normalize_source_type,
    )

    params = source_params or {}
    # Resolve $VAR credential references the same way create_source_connector does.
    connection_string = expand_env_vars(connection_string)
    governance: dict[str, Any] = {
        k: v
        for k, v in (
            ("max_queries", max_queries),
            ("statement_timeout_s", statement_timeout_s),
            ("query_tag", query_tag),
        )
        if v is not None
    }
    if is_postgresql(source_type):
        return PostgresValueSampler(
            connection_string, schema_name=pg_schema, limit=limit, **governance
        )
    if is_mysql(source_type):
        return MySQLValueSampler(connection_string, schema_name=pg_schema, limit=limit)
    if is_sqlserver(source_type):
        return SQLServerValueSampler(connection_string, schema_name=pg_schema, limit=limit)
    if normalize_source_type(source_type) == "duckdb":
        return DuckDbValueSampler(connection_string, schema_name=pg_schema, limit=limit)
    if normalize_source_type(source_type) == "sqlite":
        return SqliteValueSampler(connection_string, limit=limit)
    if normalize_source_type(source_type) == "databricks":
        return DatabricksValueSampler(connection_string, schema_name=pg_schema, limit=limit)
    if normalize_source_type(source_type) == "snowflake":
        # Governed by default (query budget + statement timeout); see the class.
        return SnowflakeValueSampler(
            connection_string, schema_name=pg_schema, limit=limit, **governance
        )
    if normalize_source_type(source_type) == "csv":
        return CsvValueSampler(
            connection_string,
            delimiter=params.get("delimiter", ","),
            has_header=bool(params.get("has_header", True)),
            limit=limit,
        )
    return None
