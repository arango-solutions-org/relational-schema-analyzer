"""Primary-key candidates from data, for sources that declare none.

FK inference targets only declared candidate keys, so a source that declares no
primary keys -- Snowflake by habit, lakes and warehouses by design -- yields no
relationships at all. This module proposes keys from the data. It never applies
them: the result is evidence for a person (or a draft key overlay) to review,
because a wrong identity key creates duplicate records that surface much later.

**Method.** For each table without a declared primary key:

1. *Screen on a sample.* One bounded query gives, per column, rows / non-null /
   distinct over the first ``probe.limit`` rows. A duplicate or a NULL in the
   sample is proof the column is not a key, so this rejection is sound.
2. *Confirm exactly.* Survivors are re-counted over the whole table -- unless the
   sample already covered every row. A candidate is never reported on sample
   evidence alone.
3. *Composite keys.* Only when no single column qualifies: pairs of
   identifier-like columns, bounded by ``max_pair_checks``. A search cut short
   by that bound, or by a declined probe, leaves the table in
   ``not_evaluated`` -- it is never reported as having no key.
4. *Rank.* Uniqueness alone does not make a key -- an email or a name can be
   unique today by accident. Candidates are scored on generic conventions
   (identifier-like names, a name derived from the table, type, position; a
   column named after *another* table counts against it, being the foreign-key
   convention) and every score carries the reasons behind it.

Queries go through a :class:`KeyProbe`, so cost governance is the probe's job;
:class:`~relational_schema_analyzer.fk_inference.SnowflakeValueSampler` is one,
with a query budget and statement timeout. A probe that declines (budget spent,
query failed) leaves the table in ``not_evaluated`` rather than guessed. Tables
that declare a primary key or a UNIQUE key are not profiled: FK inference
already treats a declared key as a candidate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import combinations, islice
from math import comb
from typing import Any, Optional, Protocol, Sequence

from .naming import singularize
from .types import Column, Schema, Table

ColumnStats = tuple[int, int, int]
"""``(rows, non_null, distinct)`` for one column over the probed rows."""


class KeyProbe(Protocol):
    """What the profiler needs from an engine. ``None`` means "declined"."""

    limit: int

    def column_stats(
        self, table: str, columns: Sequence[str], *, sample: bool
    ) -> Optional[dict[str, ColumnStats]]: ...

    def combo_stats(
        self, table: str, columns: Sequence[str], *, sample: bool
    ) -> Optional[ColumnStats]: ...


@dataclass(frozen=True)
class KeyCandidate:
    """A column set that is non-null and unique over the whole table."""

    table: str
    columns: tuple[str, ...]
    score: float
    rows: int
    distinct: int
    reasons: tuple[str, ...]

    def as_evidence(self) -> dict[str, Any]:
        return {
            "columns": list(self.columns),
            "score": round(self.score, 3),
            "rows": self.rows,
            "distinct": self.distinct,
            "reasons": list(self.reasons),
        }


@dataclass
class KeyProfile:
    """Ranked candidates per table (best first), and what could not be decided."""

    candidates: dict[str, list[KeyCandidate]] = field(default_factory=dict)
    not_evaluated: dict[str, str] = field(default_factory=dict)
    declared: list[str] = field(default_factory=list)
    #: Additional candidate keys on tables that already declare a primary key.
    #: Kept apart from ``candidates`` because these are *not* proposed primary keys —
    #: the declared key stays the primary key. These are columns that are also unique,
    #: and therefore legitimate targets for a foreign key, which is the whole reason
    #: they matter: FK inference aims at any candidate key, not only the PK.
    additional: dict[str, list[KeyCandidate]] = field(default_factory=dict)

    def best(self, table: str) -> Optional[KeyCandidate]:
        found = self.candidates.get(table) or []
        return found[0] if found else None


# Types that are never sensible identity keys. Matched on the raw type name
# because Snowflake reports NUMBER without scale, so the category cannot tell
# an integer from an amount.
_UNSUITABLE_CATEGORIES = {"boolean", "json", "array", "binary"}
_FLOAT_TYPE = re.compile(r"^(float|double|real|binary_double|binary_float)", re.I)
# ACCOUNT_ID / account_key / ORDER_NO, or camelCase accountId -- but not PAID or GRID.
_CAMEL_ID = re.compile(r"[a-z0-9]Id$")


def _id_like(name: str) -> bool:
    if _CAMEL_ID.search(name):
        return True
    return bool(re.search(r"_(?:id|key|no|num|number|code)$|^uuid$", name, re.I))


def _table_names(table_name: str) -> tuple[str, str]:
    """The lower-cased table name and its singular, both used to match ``<name>_id``."""
    return table_name.lower(), singularize(table_name).lower()


def _names_a_table_id(name: str, table_names: set[str]) -> bool:
    """``ORDERID`` / ``orderid``: camelCase ``orderId`` after the case was folded.

    Snowflake upper-cases unquoted identifiers and Postgres lower-cases them, so
    :data:`_CAMEL_ID` cannot see the boundary. Accepted only when the prefix names
    a table in the schema -- the check FK inference makes -- so ``PAID`` and
    ``VALID`` stay plain words.
    """
    low = name.lower()
    if len(low) <= 3 or not low.endswith("id") or low.endswith("_id"):
        return False
    prefix = low[:-2]
    return prefix in table_names or singularize(prefix).lower() in table_names


def _identifier(name: str, table_names: set[str]) -> bool:
    return _id_like(name) or _names_a_table_id(name, table_names)


_DESCRIPTIVE = re.compile(r"(name|email|title|description|url|phone|address)", re.I)

#: Below this many rows, "every value is distinct" is weak evidence.
MIN_CONVINCING_ROWS = 10


def _has_name_evidence(name: str, table: Table, table_names: set[str]) -> bool:
    """Does this column's *name* support it being a key, beyond being unique today?

    The gate on additional keys for an already-keyed table. Uniqueness alone is far
    too weak there: on a 20k-row sample ``note``, ``primary_champion`` and
    ``deployment_date`` are all distinct in every row and none of them is a key.
    Declaring those would be worse than declaring nothing, because an overlay is
    taken as a curator's assertion.

    So a name has to carry the claim — ``account_id`` in ``accounts``, or any
    ``_id`` / ``_key`` / ``_code`` / ``_no`` / ``_num`` style identifier. A bare
    ``id`` is excluded: on a table that already declares a primary key, that is
    almost always the declared key itself.
    """
    low = name.lower()
    if low == "id":
        return False
    own = {f"{n}{sep}id" for n in _table_names(table.name) for sep in ("_", "")}
    return low in own or _identifier(name, table_names)


def _suitable(col: Any) -> bool:
    if col.type_category in _UNSUITABLE_CATEGORIES:
        return False
    return not _FLOAT_TYPE.match((col.data_type or "").strip())


def _is_unique_non_null(stats: ColumnStats) -> bool:
    rows, non_null, distinct = stats
    return rows > 0 and non_null == rows and distinct == rows


def _score_single(
    table: Table,
    col: Any,
    position: int,
    rows: int,
    references: dict[str, str],
    table_names: set[str],
) -> tuple[float, list[str]]:
    name = col.name
    low = name.lower()
    # Both the table's own name and its singular: keys are sometimes plural
    # (ACCOUNTS_ID) and sometimes singular (ACCOUNT_ID) on a plural table.
    own = {f"{n}{sep}id" for n in _table_names(table.name) for sep in ("_", "")}
    score, reasons = 0.5, ["non-null and unique over every row"]
    # <other_table>_ID is the foreign-key naming convention FK inference itself
    # relies on. Unique here usually means "one row per referenced thing" (one
    # event per contact in a small table), not that it identifies this table.
    # A name that is also this table's own (ACCOUNT vs ACCOUNTS) is not a reference.
    referenced = references.get(low)
    if referenced is not None and referenced != table.name and low not in own:
        score -= 0.2
        reasons.append(f"named after another table ({referenced}), so likely a reference to it")
    if low in own:
        score += 0.3
        reasons.append(f"named after its table ({table.name} -> {name})")
    elif low == "id":
        score += 0.3
        reasons.append("named ID")
    elif _identifier(name, table_names):
        score += 0.15
        reasons.append("named like an identifier")
    if _DESCRIPTIVE.search(name) and not _identifier(name, table_names):
        score -= 0.15
        reasons.append("looks like a descriptive attribute, which can be unique by accident")
    if position == 0:
        score += 0.05
        reasons.append("first column")
    if col.type_category in ("integer", "uuid"):
        score += 0.05
        reasons.append(f"{col.type_category} type")
    elif col.type_category == "temporal":
        score -= 0.2
        reasons.append("timestamps are often unique by accident")
    if rows < MIN_CONVINCING_ROWS:
        score *= 0.5
        reasons.append(f"only {rows} rows: uniqueness is weak evidence")
    return max(0.0, min(1.0, score)), reasons


def profile_primary_keys(
    schema: Schema,
    probe: KeyProbe,
    *,
    tables: Optional[Sequence[str]] = None,
    max_pair_checks: int = 10,
    find_additional_keys: bool = True,
) -> KeyProfile:
    """Propose primary keys for tables that declare none. Never mutates ``schema``.

    ``find_additional_keys`` also looks for a *second* candidate key on tables that
    already declare one, reported in :attr:`KeyProfile.additional`. Only columns whose
    name supports the claim are considered, and they are never proposed as primary
    keys — see :func:`_profile_additional_keys`.
    """
    profile = KeyProfile()
    names = list(tables) if tables is not None else list(schema.tables)
    # Every table's name and singular, for recognising ORDERID-style identifiers.
    table_names = {n for t in schema.tables for n in _table_names(t)}
    # "<name>_id" / "<name>id" -> base table, for the "references another table"
    # rule. Views are left out: a view over CUSTOMER named CUSTOMERS would
    # otherwise make CUSTOMER's own CUSTOMER_ID look like a reference to the view.
    references: dict[str, str] = {}
    for other, other_table in schema.tables.items():
        if getattr(other_table, "is_view", False):
            continue
        for n in _table_names(other):
            references[f"{n}_id"] = other
            references[f"{n}id"] = other
    # Keyless tables first. Both passes spend the same probe budget, and proposing a
    # primary key for a table that has none is the point of this function; finding an
    # extra candidate key on a table that already has one is the bonus. If the budget
    # runs out it must run out on the bonus.
    keyed = [n for n in names if (t := schema.tables.get(n)) is not None and t.primary_key]
    names = [n for n in names if n not in set(keyed)]
    for tname in names:
        table = schema.tables.get(tname)
        if table is None:
            profile.not_evaluated[tname] = "not in schema"
            continue
        if getattr(table, "is_view", False):
            profile.not_evaluated[tname] = "view: uniqueness of a view is not a key"
            continue
        if table.unique_constraints:
            keys = "; ".join(", ".join(u) for u in table.unique_constraints)
            profile.not_evaluated[tname] = (
                f"declares a UNIQUE key ({keys}), which FK inference already uses as a candidate key"
            )
            continue
        _profile_table(table, probe, profile, max_pair_checks, references, table_names)

    # Second pass: tables that already declare a primary key. A declared surrogate key
    # does not mean there is no *other* unique column — and when children reference the
    # natural business key rather than the surrogate, that other column is the only
    # thing FK inference can aim at. Skipping these tables was why a schema like
    # `accounts(id PK, account_id)` yielded no relationships at all (#20).
    for tname in keyed:
        table = schema.tables[tname]
        profile.declared.append(tname)
        if find_additional_keys and not getattr(table, "is_view", False):
            _profile_additional_keys(table, probe, profile, references, table_names)
    return profile


def _profile_additional_keys(
    table: Table,
    probe: KeyProbe,
    profile: KeyProfile,
    references: dict[str, str],
    table_names: set[str],
) -> None:
    """Unique columns on an already-keyed table, where the *name* supports it too.

    Single columns only. A composite search here would spend real budget hunting
    something FK inference has no particular use for on a table that already has a key.
    """
    covered = {c.lower() for c in table.primary_key}
    for u in table.unique_constraints:
        covered.update(c.lower() for c in u)
    usable = [
        (i, c)
        for i, c in enumerate(table.columns)
        if _suitable(c)
        and c.name.lower() not in covered
        and _has_name_evidence(c.name, table, table_names)
    ]
    if not usable:
        return

    stopped, found, _ = _unique_single_columns(
        table, probe, usable, references, table_names,
        note=(
            "the table already declares a primary key; this is an "
            "additional candidate key, not a replacement",
        ),
    )
    if stopped == "screen":
        profile.not_evaluated.setdefault(
            table.name, "probe declined while looking for additional keys"
        )
    elif stopped == "confirm":
        profile.not_evaluated.setdefault(
            table.name, "probe declined confirming an additional key"
        )
    elif found:
        found.sort(key=lambda k: (-k.score, k.columns))
        profile.additional[table.name] = found


def _unique_single_columns(
    table: Table,
    probe: KeyProbe,
    usable: list[tuple[int, Column]],
    references: dict[str, str],
    table_names: set[str],
    *,
    note: tuple[str, ...] = (),
) -> tuple[Optional[str], list[KeyCandidate], bool]:
    """Screen ``usable`` on a sample, confirm the survivors over the whole table, score them.

    Returns ``(stopped, found, whole_table_seen)``. ``stopped`` says where the search
    ended early, so each caller can record its own "not evaluated" reason: ``"screen"``
    (the sample probe declined), ``"empty"`` (no rows), ``"confirm"`` (the whole-table
    probe declined), or ``None`` when it ran to the end. ``note`` is appended to every
    candidate's reasons.
    """
    # 1. Screen on a sample: a duplicate or NULL here is conclusive.
    sample = probe.column_stats(table.name, [c.name for _, c in usable], sample=True)
    if sample is None:
        return "screen", [], False
    sampled_rows = next(iter(sample.values()))[0] if sample else 0
    if sampled_rows == 0:
        return "empty", [], True
    survivors = [(i, c) for i, c in usable if _is_unique_non_null(sample[c.name])]

    # 2. Confirm over the whole table, unless the sample already was the whole table.
    whole_table_seen = sampled_rows < probe.limit
    exact: dict[str, ColumnStats] = sample
    if survivors and not whole_table_seen:
        confirmed = probe.column_stats(table.name, [c.name for _, c in survivors], sample=False)
        if confirmed is None:
            return "confirm", [], False
        exact = confirmed

    found: list[KeyCandidate] = []
    for position, col in survivors:
        stats = exact[col.name]
        if not _is_unique_non_null(stats):
            continue  # unique in the sample only
        score, reasons = _score_single(table, col, position, stats[0], references, table_names)
        found.append(
            KeyCandidate(table.name, (col.name,), score, stats[0], stats[2], (*reasons, *note))
        )
    return None, found, whole_table_seen


def _profile_table(
    table: Table,
    probe: KeyProbe,
    profile: KeyProfile,
    max_pair_checks: int,
    references: dict[str, str],
    table_names: set[str],
) -> None:
    usable = [(i, c) for i, c in enumerate(table.columns) if _suitable(c)]
    if not usable:
        profile.not_evaluated[table.name] = "no column of a type suitable for a key"
        return

    stopped, found, whole_table_seen = _unique_single_columns(
        table, probe, usable, references, table_names
    )
    if stopped is not None:
        profile.not_evaluated[table.name] = {
            "screen": "probe declined (budget spent or query failed)",
            "empty": "empty table",
            "confirm": "probe declined during confirmation",
        }[stopped]
        return

    # 3. Composite keys, only when no single column qualifies.
    if not found:
        id_like = [c.name for _, c in usable if _identifier(c.name, table_names)]
        total_pairs = comb(len(id_like), 2)
        declined = False
        for pair in islice(combinations(id_like, 2), max_pair_checks):
            screened = probe.combo_stats(table.name, pair, sample=not whole_table_seen)
            if screened is None:
                declined = True
                continue
            if not _is_unique_non_null(screened):
                continue
            stats = screened
            if not whole_table_seen:
                full = probe.combo_stats(table.name, pair, sample=False)
                if full is None:
                    declined = True
                    continue
                if not _is_unique_non_null(full):
                    continue
                stats = full
            reasons = [
                "no single column is a key",
                "pair is non-null and unique over every row",
                "both columns named like identifiers",
            ]
            score = 0.6 if stats[0] >= MIN_CONVINCING_ROWS else 0.3
            found.append(KeyCandidate(table.name, pair, score, stats[0], stats[2], tuple(reasons)))

        # "No key" is a claim that every pair was checked. A declined probe or a
        # search cut short by the bound is not evidence of absence.
        if not found and declined:
            profile.not_evaluated[table.name] = "probe declined during the composite-key search"
            return
        if not found and total_pairs > max_pair_checks:
            profile.not_evaluated[table.name] = (
                f"composite-key search stopped after {max_pair_checks} of {total_pairs} "
                "identifier pairs; raise max_pair_checks to search the rest"
            )
            return

    found.sort(key=lambda k: (-k.score, len(k.columns), k.columns))
    profile.candidates[table.name] = found


# ── Draft key overlay ────────────────────────────────────────────────────────


@dataclass
class DraftOverlay:
    """A reviewable key overlay plus the evidence behind every entry in it.

    ``overlay`` is a valid key overlay (``overlay.load_key_overlay`` format): a
    person edits it -- deletes what is wrong, adds what was missed -- and applies
    it like any hand-written one. Nothing here is applied to a schema.
    """

    overlay: dict[str, Any]
    primary_keys: dict[str, KeyCandidate] = field(default_factory=dict)
    foreign_keys: list[Any] = field(default_factory=list)
    no_key_proposed: dict[str, str] = field(default_factory=dict)


def draft_key_overlay(
    schema: Schema,
    probe: KeyProbe,
    *,
    sampler: Any = None,
    tables: Optional[Sequence[str]] = None,
    min_pk_score: float = 0.3,
    min_fk_confidence: float = 0.6,
    max_pair_checks: int = 10,
) -> DraftOverlay:
    """Propose primary and foreign keys as a draft overlay for human review.

    1. Primary keys: the best :func:`profile_primary_keys` candidate per table,
       if it scores at least ``min_pk_score``. Every other candidate is listed in
       the table's ``description`` so the reviewer sees the alternatives.
    1b. *Additional* candidate keys on tables that already declare a primary key,
       emitted as ``uniqueConstraints``. A surrogate primary key does not mean there
       is no natural business key beside it, and when children reference that key
       instead, it is the only target FK inference has (#20).
    2. Foreign keys: FK inference run against the schema *with those proposed
       keys applied* -- without them a key-less source gives inference no
       targets. When ``sampler`` is given (``probe`` usually doubles as it),
       value overlap adjusts confidence and a zero overlap vetoes the candidate.
    3. The draft is applied to a copy of ``schema`` before it is returned, so a
       draft that would not load is a bug here, never a surprise for the reviewer.
    """
    from .fk_inference import InferenceOptions, infer_foreign_keys
    from .overlay import SUPPORTED_OVERLAY_VERSION, apply_key_overlay

    profile = profile_primary_keys(schema, probe, tables=tables, max_pair_checks=max_pair_checks)
    draft = DraftOverlay(overlay={})
    specs: dict[str, dict[str, Any]] = {}

    for tname in profile.declared:
        draft.no_key_proposed[tname] = "the source already declares a primary key"
    # An already-keyed table can still carry a second candidate key, and when children
    # reference the business key rather than the surrogate, that is the only thing FK
    # inference can aim at (#20). Emitted as uniqueConstraints, never as a primaryKey:
    # the declared key stays the primary key.
    for tname, ranked in profile.additional.items():
        keys = [list(c.columns) for c in ranked if c.score >= min_pk_score]
        if not keys:
            continue
        spec = specs.setdefault(tname, {})
        spec["uniqueConstraints"] = keys
        spec["description"] = "; ".join(
            f"{list(c.columns)} unique over {c.rows} rows ({', '.join(c.reasons)})"
            for c in ranked
            if c.score >= min_pk_score
        )
        draft.no_key_proposed[tname] = (
            "the source already declares a primary key; "
            f"additional candidate key(s) proposed: {keys}"
        )
    draft.no_key_proposed.update(profile.not_evaluated)
    for tname, ranked in profile.candidates.items():
        best = ranked[0] if ranked else None
        if best is None:
            draft.no_key_proposed[tname] = "no column or column pair is non-null and unique"
            continue
        if best.score < min_pk_score:
            draft.no_key_proposed[tname] = (
                f"best candidate {list(best.columns)} scored {best.score:.2f}, "
                f"below {min_pk_score:.2f}"
            )
            continue
        draft.primary_keys[tname] = best
        lines = [
            f"Proposed key {list(best.columns)} (score {best.score:.2f}): "
            + "; ".join(best.reasons)
            + ".",
        ]
        if len(ranked) > 1:
            lines.append(
                "Also unique: "
                + ", ".join(f"{list(k.columns)} ({k.score:.2f})" for k in ranked[1:])
                + "."
            )
        specs[tname] = {"primaryKey": list(best.columns), "description": " ".join(lines)}

    # FK inference needs targets: give it the proposed keys. Both kinds — a proposed
    # primary key for a keyless table, and an additional unique key on a table that
    # already has one. The second is the whole point of #20: when children reference the
    # business key rather than the surrogate, it is the only target inference can use.
    _KEY_FIELDS = ("primaryKey", "uniqueConstraints")
    keyed = apply_key_overlay(
        schema,
        {
            "version": SUPPORTED_OVERLAY_VERSION,
            "tables": {
                t: {k: v for k, v in spec.items() if k in _KEY_FIELDS}
                for t, spec in specs.items()
            },
        },
    )
    options = InferenceOptions(sample_overlap=sampler is not None)
    for fk in infer_foreign_keys(keyed, options=options, sampler=sampler):
        if fk.confidence < min_fk_confidence:
            continue
        table = keyed.tables[fk.table]
        if any(
            {c.lower() for c in d.columns} == {c.lower() for c in fk.columns}
            for d in table.foreign_keys
        ):
            continue  # declared by the source already
        draft.foreign_keys.append(fk)
        specs.setdefault(fk.table, {}).setdefault("foreignKeys", []).append(
            {
                "columns": list(fk.columns),
                "references": {"table": fk.foreign_table, "columns": list(fk.foreign_columns)},
                "comment": f"Inferred (confidence {fk.confidence:.2f}, {fk.method}): "
                + "; ".join(fk.evidence)
                + ".",
            }
        )

    draft.overlay = {
        "version": SUPPORTED_OVERLAY_VERSION,
        "description": (
            "DRAFT key overlay proposed from data by relational-schema-analyzer -- review "
            "before use. Every key here is a proposal: delete what is wrong, add what was "
            "missed. Applied keys are marked enforced=false."
        ),
        "tables": specs,
    }
    apply_key_overlay(schema, draft.overlay)  # must load; raises OverlayError if not
    return draft
