"""Concrete implementations of the injected seams — the parts that need a database.

Three detectors delegate their only DB-touching step to a callable so the logic around them
stays paradigm-neutral and testable:

* ``fk_inference.Sampler`` — value overlap (already implemented per connector)
* ``discriminator.ValueEnumerator`` — distinct values of a candidate type column
* ``taxonomy.SpecializationCounter`` — how many parent rows appear in one, several, or no
  subtype tables

The latter two had no implementation at all, which meant that in practice discriminator
detection ran only on declared ``CHECK`` constraints and specialization constraints were
always ``null``. Both are implemented here against the DB-API cursor every connector
already exposes, so they work on any source whose driver speaks it.

SQL is kept to the intersection of the dialects RSA supports — no window functions, no
lateral joins, no ``FILTER``. Identifiers are quoted, never interpolated raw. The two things
that do differ by dialect come from ``source_type``: how an identifier is quoted (MySQL
rejects ``"t"`` without ``ANSI_QUOTES``) and how a row cap is spelled (SQL Server has no
``LIMIT``). Every query reads a bounded number of rows (#56).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional

from .connectors.base import normalize_source_type, quote_identifier
from .quoting import quote_backtick as _backtick_quote
from .quoting import quote_bracket as _bracket_quote

logger = logging.getLogger(__name__)

#: Anything exposing DB-API ``cursor()``, or a callable returning rows for a SQL string.
Executor = Callable[[str, tuple[Any, ...]], Optional[list[tuple[Any, ...]]]]


#: Default number of rows a probe reads, as for the value samplers.
DEFAULT_ROW_LIMIT = 10_000


class _Dialect:
    """How one source quotes identifiers and caps rows."""

    def __init__(self, source_type: str | None) -> None:
        key = normalize_source_type(source_type) if source_type else None
        self.quote_rule: Callable[[str], str] = {
            "mysql": _backtick_quote,
            "databricks": _backtick_quote,
            "sqlserver": _bracket_quote,
        }.get(key or "", quote_identifier)  # ANSI double quotes: Postgres, Snowflake, DuckDB, SQLite
        self.top = key == "sqlserver"  # SELECT TOP (n), not LIMIT n

    def quote(self, identifier: str, *, schema: str | None = None) -> str:
        """Quote an identifier with this dialect's rule, escaping it.

        Table and column names cannot travel as bind parameters, so this is the guard.
        """
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("identifier must be a non-empty string")
        quoted = self.quote_rule(identifier)
        return f"{self.quote_rule(schema)}.{quoted}" if schema else quoted

    def select(self, columns: str, rest: str, limit: int, *, distinct: bool = False) -> str:
        """``SELECT [DISTINCT] columns rest``, returning at most ``limit`` rows."""
        d, n = ("DISTINCT " if distinct else ""), int(limit)
        if self.top:
            return f"SELECT {d}TOP ({n}) {columns} {rest}"
        return f"SELECT {d}{columns} {rest} LIMIT {n}"


def executor_from_connection(connection: Any) -> Executor:
    """Adapt a DB-API connection to the ``Executor`` shape.

    Returns ``None`` on failure rather than raising: a detector that cannot measure must
    degrade to "unmeasured", never to a wrong answer or a failed analysis.
    """

    def execute(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]] | None:
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, params) if params else cursor.execute(sql)
                return list(cursor.fetchall())
            finally:
                cursor.close()
        except Exception as err:  # noqa: BLE001
            logger.warning("sampler query failed: %s", err)
            return None

    return execute


def make_value_enumerator(
    executor: Executor, *, schema: str | None = None, source_type: str | None = None
) -> Callable[[str, str, int], list[str] | None]:
    """Distinct values of a column, for ``discriminator.detect_discriminators``.

    Bounded by ``limit + 1`` so a high-cardinality column is rejected on cardinality without
    reading it — the detector's own gate then drops anything above ``max_distinct``. A failed
    query is "unmeasured": there is no unbounded retry.
    """
    dialect = _Dialect(source_type)

    def enumerate_values(table: str, column: str, limit: int) -> list[str] | None:
        col = dialect.quote(column)
        sql = dialect.select(
            col,
            f"FROM {dialect.quote(table, schema=schema)} WHERE {col} IS NOT NULL",
            int(limit) + 1,
            distinct=True,
        )
        rows = executor(sql, ())
        if rows is None:
            return None
        return [str(r[0]) for r in rows if r and r[0] is not None]

    return enumerate_values


def make_specialization_counter(
    executor: Executor,
    *,
    schema: str | None = None,
    source_type: str | None = None,
    limit: int = DEFAULT_ROW_LIMIT,
) -> Callable[[str, str, list[str]], dict[str, int] | None]:
    """Parent-key membership across subtype tables, for the ER specialization constraints.

    Counts, over a bounded sample of parent rows, how many appear in more than one subtype
    (overlapping specialization) and how many appear in none (partial specialization). The
    shared library turns those into ``disjoint`` / ``complete``; absent this, both stay
    ``null`` — which is correct but uninformative.

    One query, using scalar subqueries rather than joins so the row count cannot be inflated
    by a subtype with duplicate keys. At most ``limit`` parent rows are sampled.
    """
    dialect = _Dialect(source_type)

    def count(parent: str, pk: str, children: list[str]) -> dict[str, int] | None:
        if not children:
            return None
        pk_ref = dialect.quote(pk)
        sample = dialect.select(pk_ref, f"FROM {dialect.quote(parent, schema=schema)}", limit)
        memberships = " + ".join(
            "(SELECT COUNT(*) FROM {child} c WHERE c.{pk} = p.{pk})".format(
                child=dialect.quote(child, schema=schema), pk=pk_ref
            )
            for child in children
        )
        sql = (
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN n > 1 THEN 1 ELSE 0 END) AS in_multiple, "
            "SUM(CASE WHEN n = 0 THEN 1 ELSE 0 END) AS in_none FROM ("
            f"SELECT ({memberships}) AS n FROM ({sample}) p"
            ") s"
        )
        rows = executor(sql, ())
        if not rows or not rows[0]:
            return None
        total, in_multiple, in_none = (rows[0] + (None, None, None))[:3]
        if total is None:
            return None
        return {
            "total": int(total),
            "inMultiple": int(in_multiple or 0),
            "inNone": int(in_none or 0),
        }

    return count
