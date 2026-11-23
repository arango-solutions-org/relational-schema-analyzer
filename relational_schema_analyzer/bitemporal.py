"""Bitemporal stamping of physical schemas (DESIGN-ADDENDUM-bitemporal).

Every schema version has two clocks. **Transaction time** is when RSA looked; **valid
time** is when the definition became true of the source. Only the second is hard: nothing
downstream can reconstruct when a table was altered, and RSA is the only component that
reads the relational catalogs. So RSA records both — and records nothing else. It stays
stateless, ``schema_diff`` stays pure, and the history lives in the temporal store.

What makes this more than plumbing is that **the catalogs disagree about what "last
altered" means**:

* Snowflake's ``LAST_ALTERED`` moves on DML as well as DDL — an unchanged table that was
  merely inserted into looks freshly altered.
* MySQL's ``CREATE_TIME`` does not move on ``ALTER`` at all, so an altered table looks
  older than it is.
* PostgreSQL has no DDL timestamp whatsoever.

A naive "read the catalog date" rule would therefore be wrong on three of the nine
sources. The fingerprint RSA already computes is what rescues it (§2.3): a catalog date is
believed only when the structure actually changed. Otherwise the previous date is carried
forward, and the result says so.

Two consequences worth stating plainly, because they are the difference between an
auditable record and a plausible-looking one:

* ``valid_time_source`` always accompanies ``valid_from``. A date whose provenance is
  unknown cannot be audited, and "we had no signal so we used the clock" (``observed``) is
  a fact a reader must be able to see rather than infer.
* The schema-level source is the **weakest** among its tables, so a schema is never
  advertised as ``catalog``-dated when one of its tables was only ``observed``.

RSA does not record ``valid_to``: a version is open-ended at observation, and closing it
is the store's job when a successor arrives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .log import get_logger
from .metadata import fingerprint_physical_schema
from .types import PhysicalSchema

logger = get_logger(__name__)

# ── The valid-time source vocabulary (§2.2), strongest first ─────────

#: A DDL-scoped timestamp read from the source's own catalog.
CATALOG = "catalog"
#: An operator-installed DDL event log RSA was pointed at (PostgreSQL, §3.1).
EVENT = "event"
#: File mtime of a file-backed source, or a manifest's own generation stamp.
FILE = "file"
#: No catalog signal, but a prior run carried the same fingerprint — so the definition has
#: been true at least since that run, and its date is carried back.
FINGERPRINT_CONTINUITY = "fingerprint-continuity"
#: No signal at all: ``valid_from`` is the observation time. Recorded explicitly so an
#: auditor can see that it happened rather than having to deduce it.
OBSERVED = "observed"

#: Strongest → weakest. Ordering is what ``weakest_source`` means, and what lets a schema
#: report the honest floor of its tables' provenance.
VALID_TIME_SOURCES: tuple[str, ...] = (
    CATALOG,
    EVENT,
    FILE,
    FINGERPRINT_CONTINUITY,
    OBSERVED,
)

_STRENGTH = {name: rank for rank, name in enumerate(VALID_TIME_SOURCES)}


def weakest_source(sources: list[str]) -> Optional[str]:
    """The weakest of ``sources`` per :data:`VALID_TIME_SOURCES`, or ``None`` if empty.

    Unknown values sort weakest: a source this build does not recognize is not something
    to advertise as strong.
    """
    known = [s for s in sources if s]
    if not known:
        return None
    return max(known, key=lambda s: (_STRENGTH.get(s, len(VALID_TIME_SOURCES)), s))


def utc_now_iso() -> str:
    """Current UTC instant as ISO-8601, the format every temporal field here uses."""
    return datetime.now(timezone.utc).isoformat()


def to_iso(value: Any) -> Optional[str]:
    """Normalize a catalog timestamp to ISO-8601 UTC, or ``None`` if unusable.

    Drivers return wildly different things for the same column — ``datetime`` (aware or
    naive), ``date``, an epoch number, or a preformatted string. Every connector needs the
    same normalization, so it lives here rather than five times over.

    A naive datetime is assumed UTC: catalog timestamps are server-side, and guessing a
    local zone would silently shift dates by hours. Assuming UTC is at least uniform and
    stated.

    Values at or near the Unix epoch are rejected as placeholders (:data:`_PLAUSIBLE_FROM`).
    Drivers and emulators return ``0``/``1970-01-01`` for "no value" often enough that
    passing it through would produce a ``catalog``-sourced ``valid_from`` of 1970 — a
    confidently wrong date, which is worse than the honest ``observed`` the caller gets when
    this returns ``None``. No relational catalog in service predates the floor.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        stamped = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return _if_plausible(stamped.astimezone(timezone.utc))
    if isinstance(value, date):
        return _if_plausible(datetime(value.year, value.month, value.day, tzinfo=timezone.utc))
    if isinstance(value, (int, float)):
        try:
            return _if_plausible(datetime.fromtimestamp(float(value), tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    return text or None


def file_mtime_iso(path: Path) -> Optional[str]:
    """A file's modification time as ISO-8601 UTC, or ``None`` if it cannot be read.

    The valid time of a file-based source (CSV, OSI) that records no time of its own.
    """
    try:
        return to_iso(path.stat().st_mtime)
    except OSError:
        return None


#: Catalog timestamps older than this are treated as placeholders rather than facts.
_PLAUSIBLE_FROM = datetime(1990, 1, 1, tzinfo=timezone.utc)


def _if_plausible(stamped: datetime) -> Optional[str]:
    if stamped < _PLAUSIBLE_FROM:
        logger.debug("implausible_catalog_timestamp_ignored", value=stamped.isoformat())
        return None
    return stamped.isoformat()


# ── Prior run (§5 Q3) ────────────────────────────────────────────────


@dataclass(frozen=True)
class PriorRun:
    """The previous observation of the same source, supplied by the caller.

    RSA is stateless by design, so it cannot look up what it saw last time — the caller
    (r2g, AOE, the fabric's catalog builder) holds that. This is the smallest shape that
    answers the only two questions §2.3 asks: *did the structure change?* and *what did we
    date it at?*

    Mirrors ASA's ``analyze_incremental`` contract rather than inventing a second idiom.
    """

    fingerprint: str
    valid_from: Optional[str] = None
    valid_time_source: Optional[str] = None
    transaction_time: Optional[str] = None

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any]) -> Optional["PriorRun"]:
        """Build a :class:`PriorRun` from a previous bundle's ``metadata`` block.

        Accepts a whole bundle or the ``metadata`` object itself, since callers hold
        whichever is convenient. Returns ``None`` when there is no fingerprint to compare
        against — an unusable prior is the same as no prior, and saying so here keeps the
        check out of every call site.
        """
        if not isinstance(metadata, dict):
            return None
        block = metadata.get("metadata") if "metadata" in metadata else metadata
        if not isinstance(block, dict):
            return None
        fingerprint = block.get("physicalSchemaFingerprint")
        if not fingerprint:
            return None
        valid = block.get("validTime") or {}
        return cls(
            fingerprint=str(fingerprint),
            valid_from=(valid.get("from") if isinstance(valid, dict) else None),
            valid_time_source=block.get("validTimeSource"),
            transaction_time=block.get("transactionTime") or block.get("timestamp"),
        )


# ── Stamping ─────────────────────────────────────────────────────────


def stamp_bitemporal(
    schema: PhysicalSchema,
    *,
    prior: Optional[PriorRun] = None,
    now: Optional[str] = None,
) -> PhysicalSchema:
    """Return a copy of ``schema`` with valid and transaction time resolved.

    Connectors supply the raw per-table signal (``valid_from`` + ``valid_time_source``)
    during introspection; this applies §2.3 on top and derives the schema-level view.

    The input is never mutated — callers routinely keep the raw snapshot to show what the
    source actually said, and an in-place stamp would destroy that comparison. Same rule as
    :func:`~relational_schema_analyzer.overlay.apply_key_overlay`.
    """
    result = schema.model_copy(deep=True)
    transaction_time = now or utc_now_iso()
    result.transaction_time = transaction_time

    # Computed on the *structure*, so it is unaffected by anything stamped below and can be
    # compared against the prior run's.
    fingerprint = fingerprint_physical_schema(result)
    if prior is not None:
        result.predecessor_fingerprint = prior.fingerprint

    unchanged = prior is not None and prior.fingerprint == fingerprint and bool(prior.valid_from)
    if unchanged:
        logger.debug(
            "bitemporal_fingerprint_unchanged",
            fingerprint=fingerprint,
            carried_from=prior.valid_from if prior else None,
        )

    for table in result.tables.values():
        if unchanged and prior is not None:
            # §2.3. The structure is identical to the prior run, so whatever a catalog now
            # reports moved for a non-DDL reason — Snowflake DML, MySQL statistics. Carry
            # the earlier date back rather than believing the fresher, wronger one.
            table.valid_from = prior.valid_from
            table.valid_time_source = FINGERPRINT_CONTINUITY
        elif table.valid_from:
            # A connector-supplied signal, kept with the source it declared.
            table.valid_time_source = table.valid_time_source or CATALOG
        else:
            # No signal anywhere. Say so rather than quietly presenting the clock as fact.
            table.valid_from = transaction_time
            table.valid_time_source = OBSERVED

    stamped = [t for t in result.tables.values() if t.valid_from]
    if stamped:
        result.valid_from = max(t.valid_from for t in stamped if t.valid_from)
        result.valid_time_source = weakest_source(
            [t.valid_time_source or OBSERVED for t in stamped]
        )
    else:
        # A schema with no tables is still an observation, and dating it is more useful
        # than leaving a hole the store has to special-case.
        result.valid_from = transaction_time
        result.valid_time_source = OBSERVED
    return result


def bitemporal_metadata(schema: PhysicalSchema) -> dict[str, Any]:
    """The tool-contract ``metadata`` keys for a stamped schema (§2.4).

    Additive: ``timestamp`` keeps its meaning for existing consumers and
    ``transactionTime`` is its unambiguous alias. Returns ``{}`` for an unstamped schema so
    a caller that never stamped emits exactly the metadata it always did.
    """
    if not schema.transaction_time:
        return {}
    out: dict[str, Any] = {"transactionTime": schema.transaction_time}
    if schema.valid_from:
        out["validTime"] = {"from": schema.valid_from}
    if schema.valid_time_source:
        out["validTimeSource"] = schema.valid_time_source
    if schema.predecessor_fingerprint:
        out["predecessorFingerprint"] = schema.predecessor_fingerprint
    return out


__all__ = [
    "CATALOG",
    "EVENT",
    "FILE",
    "FINGERPRINT_CONTINUITY",
    "OBSERVED",
    "VALID_TIME_SOURCES",
    "PriorRun",
    "to_iso",
    "bitemporal_metadata",
    "stamp_bitemporal",
    "utc_now_iso",
    "weakest_source",
]
