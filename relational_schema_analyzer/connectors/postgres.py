"""PostgreSQL source connector.

Provides:

- :class:`PostgresConnector` — schema introspection (the original scope).
- :class:`PostgresSession` — the Phase 6 slice 3 bulk-read session
  (consistent-snapshot transaction, server-side cursor streaming,
  ``COPY TO STDOUT`` CSV export). Both the streaming pipeline and the
  ``source dump`` CLI now consume PG through this interface.

PG has two fast paths we care about:

1. ``SET TRANSACTION ISOLATION LEVEL REPEATABLE READ`` — gives a
   point-in-time snapshot for the session's lifetime across every
   table read, matching what ``StreamingPipeline`` has required since
   day one.
2. ``COPY <table> TO STDOUT WITH CSV HEADER`` — 10-100x faster than
   row-by-row fetches for export. We route ``dump_table_to_csv``
   through it.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row, tuple_row

from ..bitemporal import EVENT
from ..bitemporal import to_iso as _iso
from ..check_enum import enrich_check_constraint
from ..log import get_logger
from ..types import CheckConstraint, Column, ForeignKey, Schema, SourceProvenance, Table
from .base import group_foreign_keys, mark_one_to_one, quote_identifier, unique_columns

logger = get_logger(__name__)

#: Default name of the optional DDL event log RSA reads for PostgreSQL valid time
#: (DESIGN-ADDENDUM-bitemporal §3.1). Operator-installed; RSA only ever reads it.
DEFAULT_DDL_HISTORY_TABLE = "rsa_ddl_history"

_RELATION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)?$")


def _safe_relation(name: str) -> str:
    """Validate a caller-supplied relation name before it is interpolated into SQL.

    The history table is configuration rather than user input, but it is the one identifier
    in this connector that does not come from the catalog — so it is checked, not trusted.
    """
    if not _RELATION_RE.match(name or ""):
        raise ValueError(f"Unsafe DDL history table name: {name!r}")
    return name


def _fk_dedupe_key(fk: ForeignKey) -> tuple:
    return (tuple(fk.columns), fk.foreign_table, tuple(fk.foreign_columns))


def annotate_partition_metadata(
    schema: Schema, partition_rows: list[dict[str, Any]]
) -> None:
    """Tag partitioned parents / children and roll child FKs up to the parent.

    *partition_rows* are rows of ``(table_name, is_partitioned, parent_name)``
    where ``parent_name`` is the *immediate* inheritance parent (or ``None``).
    PostgreSQL stores a partition's FK constraints on the child, not the
    partitioned parent, so to model the parent as one collection we union the
    declared FKs of every descendant onto the root parent (deduplicated). This
    also repairs partitions that are individually missing a constraint, since a
    sibling supplies the same FK definition.

    Pure and mutating: operates on the already-built ``schema`` in place so it
    can be unit-tested without a live database.
    """
    immediate_parent: dict[str, Optional[str]] = {}
    partitioned: set[str] = set()
    for row in partition_rows:
        name = row["table_name"]
        immediate_parent[name] = row.get("parent_name")
        if row.get("is_partitioned"):
            partitioned.add(name)

    def root_parent(name: str) -> Optional[str]:
        """Walk inheritance up to the top-level partitioned table."""
        seen: set[str] = set()
        cur = immediate_parent.get(name)
        root: Optional[str] = None
        while cur and cur not in seen:
            seen.add(cur)
            root = cur
            cur = immediate_parent.get(cur)
        return root

    # Classify every table, then aggregate child FKs onto each root parent.
    rolled_up: dict[str, list[ForeignKey]] = {}
    for name, table in schema.tables.items():
        table.is_partitioned = name in partitioned
        root = root_parent(name)
        # A table is a partition child only if it ultimately descends from a
        # partitioned parent (guards against plain table inheritance).
        if root is not None and root in partitioned:
            table.partition_of = root
            rolled_up.setdefault(root, []).extend(table.foreign_keys)

    for parent_name, child_fks in rolled_up.items():
        parent = schema.tables.get(parent_name)
        if parent is None:
            continue
        seen = {_fk_dedupe_key(fk) for fk in parent.foreign_keys}
        for fk in child_fks:
            key = _fk_dedupe_key(fk)
            if key not in seen:
                seen.add(key)
                parent.foreign_keys.append(fk)


def preview_table_rows(
    connection_string: str,
    schema_name: str,
    table_name: str,
    limit: int,
) -> list[dict]:
    """Return up to ``limit`` rows from ``schema_name.table_name`` as JSON-safe dicts.

    Both identifiers are bound through psycopg's ``sql.Identifier`` so they are
    never string-interpolated into SQL. Callers MUST still validate
    ``table_name`` against a trusted schema snapshot first — this guards against
    quoting bugs, not against previewing an arbitrary table. Shared by the UI
    and MCP preview endpoints.
    """
    from psycopg import sql

    from .base import serialize_rows

    query = sql.SQL("SELECT * FROM {}.{} LIMIT %s").format(
        sql.Identifier(schema_name),
        sql.Identifier(table_name),
    )
    with psycopg.connect(connection_string, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (limit,))
            rows = cur.fetchall()
    return serialize_rows(rows)


class PostgresConnector:
    def __init__(
        self,
        connection_string: str,
        schema_name: str = "public",
        *,
        ddl_history_table: Optional[str] = DEFAULT_DDL_HISTORY_TABLE,
    ) -> None:
        self.connection_string = connection_string
        self.schema_name = schema_name
        # PostgreSQL exposes no DDL timestamp anywhere in its catalogs, so the only honest
        # source of valid time is a DDL event trigger the *operator* installed
        # (DESIGN-ADDENDUM-bitemporal §3.1). RSA reads it if present and never creates it:
        # this is a read-only introspector (DESIGN §1), and a tool that silently installs
        # triggers in a production database is not one.
        #
        # Absent the table, tables simply have no signal and the resolver records
        # `observed` — which is the truthful answer, not a degraded one.
        self.ddl_history_table = ddl_history_table

    def get_schema(self) -> Schema:
        """Connect to PostgreSQL and inspect the schema.

        Returns a Schema object populated with tables, columns, PKs, and FKs.
        """
        schema = Schema()

        try:
            with psycopg.connect(self.connection_string, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    schema.source = self._provenance(cur)
                    # Base tables, partitioned tables, and views (relkind r/p/v/m).
                    cur.execute(
                        """
                        SELECT c.relname AS table_name,
                               c.relkind AS relkind,
                               obj_description(c.oid) AS comment
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = %s
                          AND c.relkind IN ('r', 'p', 'v', 'm')
                        ORDER BY c.relname;
                        """,
                        (self.schema_name,),
                    )
                    tables = cur.fetchall()

                    valid_times = self._valid_times(cur)

                    for t in tables:
                        table_name = t["table_name"]
                        table = self._process_table(
                            cur,
                            table_name,
                            is_view=t["relkind"] in ("v", "m"),
                            comment=t["comment"],
                        )
                        if table_name in valid_times:
                            table.valid_from = valid_times[table_name]
                            table.valid_time_source = EVENT
                        schema.tables[table_name] = table

                    cur.execute(
                        """
                        SELECT
                            c.relname               AS table_name,
                            (c.relkind = 'p')       AS is_partitioned,
                            parent.relname          AS parent_name
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        LEFT JOIN pg_inherits i ON i.inhrelid = c.oid
                        LEFT JOIN pg_class parent ON parent.oid = i.inhparent
                        WHERE n.nspname = %s
                          AND c.relkind IN ('r', 'p');
                        """,
                        (self.schema_name,),
                    )
                    partition_rows = cur.fetchall()

        except Exception as e:
            raise RuntimeError(f"Failed to fetch schema from PostgreSQL: {e}")

        annotate_partition_metadata(schema, partition_rows)
        return schema

    def _valid_times(self, cur: Any) -> dict[str, str]:
        """Latest DDL time per table from the operator-installed event log (§3.1).

        PostgreSQL keeps no DDL timestamp of its own, so this is the only route to a real
        valid time — and it is the *strongest* kind RSA can get, because a
        ``ddl_command_end`` trigger fires on DDL and nothing else. Where Snowflake's
        ``LAST_ALTERED`` has to be second-guessed, an event row is proof.

        Silent by design when the table is absent or unreadable: that is the overwhelmingly
        common case (no operator has installed it), it is not an error, and the resolver
        records ``observed`` instead. RSA needs only ``SELECT`` on this table.

        ``object_identity`` is schema-qualified (``public.orders``), so matching strips the
        schema and any trailing argument list Postgres appends for non-table objects.
        """
        if not self.ddl_history_table:
            return {}
        table = _safe_relation(self.ddl_history_table)
        out: dict[str, str] = {}
        try:
            cur.execute(
                f"""
                SELECT object_identity, MAX(occurred_at) AS changed_at
                FROM {table}
                WHERE schema_name = %s
                GROUP BY object_identity
                """,  # noqa: S608 - relation name validated by _safe_relation
                (self.schema_name,),
            )
            for row in cur.fetchall() or []:
                identity = str(row["object_identity"] or "")
                bare = identity.split("(", 1)[0].strip()
                name = bare.rsplit(".", 1)[-1].strip('"')
                stamp = _iso(row["changed_at"])
                if name and stamp:
                    out[name] = stamp
        except Exception as err:  # noqa: BLE001 - the log is optional, not expected
            logger.debug("postgres_ddl_history_unavailable", error=str(err))
            # A failed read may have aborted the surrounding transaction; recover so the
            # rest of introspection proceeds.
            try:
                cur.connection.rollback()
            except Exception:  # noqa: BLE001
                pass
        return out

    def open_session(self) -> "PostgresSession":
        """Open a REPEATABLE READ read-only session for streaming / dumps."""
        return PostgresSession(self.connection_string, schema_name=self.schema_name)

    def _provenance(self, cur: "psycopg.Cursor[dict[str, Any]]") -> SourceProvenance:
        version: Optional[str] = None
        database: Optional[str] = None
        try:
            cur.execute(
                "SELECT current_database() AS db, current_setting('server_version') AS ver"
            )
            row = cur.fetchone()
            if row:
                database = row.get("db")
                version = row.get("ver")
        except Exception:  # noqa: BLE001 - provenance is best-effort
            pass
        return SourceProvenance(
            dialect="postgresql",
            server_version=version,
            database=database,
            namespace=self.schema_name,
        )

    def _process_table(
        self,
        cur: "psycopg.Cursor[dict[str, Any]]",
        table_name: str,
        *,
        is_view: bool = False,
        comment: Any = None,
    ) -> Table:
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default, ordinal_position
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position;
            """,
            (self.schema_name, table_name),
        )
        columns_data = cur.fetchall()

        # Column comments (pg keeps these in pg_description, not information_schema).
        cur.execute(
            """
            SELECT a.attname AS column_name, col_description(a.attrelid, a.attnum) AS comment
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
              AND a.attnum > 0 AND NOT a.attisdropped;
            """,
            (self.schema_name, table_name),
        )
        col_comments = {
            r["column_name"]: r["comment"] for r in cur.fetchall() if r.get("comment")
        }

        cur.execute(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
              AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = %s
              AND tc.table_name = %s
            ORDER BY kcu.ordinal_position;
            """,
            (self.schema_name, table_name),
        )
        pks = [row["column_name"] for row in cur.fetchall()]

        unique_sets = self._fetch_unique_constraints(cur, table_name, pks)
        single_unique = unique_columns(pks, unique_sets)

        columns = []
        for c in columns_data:
            name = c["column_name"]
            ordinal = c.get("ordinal_position")
            columns.append(
                Column(
                    name=name,
                    data_type=c["data_type"],
                    is_nullable=(c["is_nullable"] == "YES"),
                    is_primary_key=(name in pks),
                    is_unique=(name in single_unique),
                    default=c.get("column_default"),
                    comment=col_comments.get(name),
                    ordinal=(int(ordinal) - 1 if ordinal is not None else None),
                )
            )

        # pg_catalog gives correct positional pairing for composite FKs
        cur.execute(
            """
            SELECT
                a.attname  AS column_name,
                cf.relname AS foreign_table_name,
                af.attname AS foreign_column_name,
                c.conname  AS constraint_name
            FROM pg_constraint c
            JOIN pg_class cr ON c.conrelid = cr.oid
            JOIN pg_namespace nr ON cr.relnamespace = nr.oid
            JOIN pg_class cf ON c.confrelid = cf.oid
            CROSS JOIN LATERAL unnest(c.conkey, c.confkey)
                WITH ORDINALITY AS u(local_col, ref_col, ord)
            JOIN pg_attribute a  ON a.attrelid = c.conrelid  AND a.attnum = u.local_col
            JOIN pg_attribute af ON af.attrelid = c.confrelid AND af.attnum = u.ref_col
            WHERE c.contype = 'f'
              AND nr.nspname = %s
              AND cr.relname = %s
            ORDER BY c.conname, u.ord;
            """,
            (self.schema_name, table_name),
        )
        fks_data = cur.fetchall()

        fks = group_foreign_keys(
            fks_data,
            constraint="constraint_name",
            column="column_name",
            ref_table="foreign_table_name",
            ref_column="foreign_column_name",
        )
        mark_one_to_one(fks, pks, unique_sets)

        # Value lists are read from the expression; a ``NOT VALID`` constraint, whose
        # existing rows were never checked, is left without them (see ``check_enum``).
        column_names = [c.name for c in columns]
        checks = [
            enrich_check_constraint(check, column_names)
            for check in self._fetch_check_constraints(cur, table_name)
        ]

        return Table(
            name=table_name,
            columns=columns,
            primary_key=pks,
            foreign_keys=fks,
            is_view=is_view,
            comment=(str(comment) if comment else None),
            schema_name=self.schema_name,
            unique_constraints=[list(u) for u in unique_sets],
            check_constraints=checks,
        )

    def _fetch_unique_constraints(
        self,
        cur: "psycopg.Cursor[dict[str, Any]]",
        table_name: str,
        primary_key: list[str] | None = None,
    ) -> list[list[str]]:
        cur.execute(
            """
            SELECT tc.constraint_name, kcu.column_name, kcu.ordinal_position
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
              AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'UNIQUE'
              AND tc.table_schema = %s
              AND tc.table_name = %s
            ORDER BY tc.constraint_name, kcu.ordinal_position;
            """,
            (self.schema_name, table_name),
        )
        grouped: OrderedDict[str, list[str]] = OrderedDict()
        for row in cur.fetchall():
            grouped.setdefault(row["constraint_name"], []).append(row["column_name"])
        from .base import merge_unique_sets

        return merge_unique_sets(
            list(grouped.values()), self._fetch_unique_indexes(cur, table_name), primary_key or []
        )

    def _fetch_unique_indexes(
        self, cur: "psycopg.Cursor[dict[str, Any]]", table_name: str
    ) -> list[list[str]]:
        """Key columns of unique indexes that back no constraint.

        ``CREATE UNIQUE INDEX`` guarantees uniqueness exactly as a ``UNIQUE``
        constraint does, but it is not a constraint, so it never appears in
        ``information_schema.table_constraints`` -- and FK inference, which targets
        only declared candidate keys, could not see it. pagila's
        ``store.manager_staff_id`` is one.

        Only indexes that make a column set a key qualify. Excluded:
        partial indexes (``WHERE``: unique only over a subset of rows), expression
        indexes (``lower(email)``: the key is a function, not the column), invalid
        indexes (a failed ``CREATE INDEX CONCURRENTLY``), ``INCLUDE`` columns
        (stored, not part of the key), and any index already backing a PK, UNIQUE
        or exclusion constraint (reported by the constraint query).

        Only those three constraint types are matched on ``conindid``: a FOREIGN
        KEY's ``conindid`` is the *referenced* table's unique index, so matching
        every constraint would drop exactly the indexes foreign keys point at.
        ``indnkeyatts`` (key columns, excluding ``INCLUDE``) exists from
        PostgreSQL 11; older servers have no ``INCLUDE``, so every column is a key.
        """
        info = getattr(getattr(cur, "connection", None), "info", None)
        version = getattr(info, "server_version", 0) or 0
        key_atts = "i.indnkeyatts" if not version or version >= 110000 else "i.indnatts"
        cur.execute(
            f"""
            SELECT ix.relname AS index_name, a.attname AS column_name, k.ord
            FROM pg_index i
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_class ix ON ix.oid = i.indexrelid
            CROSS JOIN LATERAL unnest((i.indkey::int2[])[0:{key_atts} - 1])
                WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
            WHERE i.indisunique AND NOT i.indisprimary AND i.indisvalid
              AND i.indpred IS NULL AND i.indexprs IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM pg_constraint c
                  WHERE c.conindid = i.indexrelid AND c.contype IN ('p', 'u', 'x')
              )
              AND n.nspname = %s AND t.relname = %s
            ORDER BY ix.relname, k.ord;
            """,  # noqa: S608 - key_atts is one of two fixed column names
            (self.schema_name, table_name),
        )
        grouped: OrderedDict[str, list[str]] = OrderedDict()
        for row in cur.fetchall():
            grouped.setdefault(row["index_name"], []).append(row["column_name"])
        return list(grouped.values())

    def _fetch_check_constraints(
        self, cur: "psycopg.Cursor[dict[str, Any]]", table_name: str
    ) -> list[CheckConstraint]:
        cur.execute(
            """
            SELECT c.conname AS name,
                   pg_get_constraintdef(c.oid) AS definition,
                   array_agg(a.attname ORDER BY u.ord) AS columns
            FROM pg_constraint c
            JOIN pg_class cr ON c.conrelid = cr.oid
            JOIN pg_namespace n ON cr.relnamespace = n.oid
            LEFT JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord) ON true
            LEFT JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = u.attnum
            WHERE c.contype = 'c' AND n.nspname = %s AND cr.relname = %s
            GROUP BY c.conname, c.oid
            ORDER BY c.conname;
            """,
            (self.schema_name, table_name),
        )
        checks: list[CheckConstraint] = []
        for row in cur.fetchall():
            cols = [c for c in (row.get("columns") or []) if c]
            checks.append(
                CheckConstraint(
                    name=row.get("name"),
                    expression=row.get("definition") or "",
                    columns=cols,
                )
            )
        return checks


class PostgresSession:
    """Bulk-read session for :class:`PostgresConnector`.

    Holds a single autocommit=False connection with ``REPEATABLE READ``
    isolation for consistent snapshots across every table read during
    a pipeline pass. Each instance owns its connection; call
    :meth:`close` when done.
    """

    def __init__(self, connection_string: str, *, schema_name: str = "public") -> None:
        self.connection_string = connection_string
        self.schema_name = schema_name
        self._conn: Optional["psycopg.Connection[dict[str, Any]]"] = None

    @property
    def connection(self) -> "psycopg.Connection[dict[str, Any]]":
        if self._conn is None:
            self._conn = psycopg.connect(
                self.connection_string,
                row_factory=dict_row,
                autocommit=False,
            )
            self._conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def __enter__(self) -> "PostgresSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def count_rows(
        self,
        table: str,
        *,
        since_column: Optional[str] = None,
        since_value: Optional[str] = None,
    ) -> int:
        qualified = f"{quote_identifier(self.schema_name)}.{quote_identifier(table)}"
        conn = self.connection
        with conn.cursor(row_factory=tuple_row) as cur:
            if since_column and since_value is not None:
                cur.execute(
                    f'SELECT count(*) FROM {qualified} '  # noqa: S608
                    f"WHERE {quote_identifier(since_column)} >= %s",
                    (since_value,),
                )
            else:
                cur.execute(f"SELECT count(*) FROM {qualified}")  # noqa: S608
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def stream_rows(
        self,
        table: str,
        *,
        batch_size: int = 10_000,
        since_column: Optional[str] = None,
        since_value: Optional[str] = None,
    ) -> Iterator[dict[str, Any]]:
        qualified = f"{quote_identifier(self.schema_name)}.{quote_identifier(table)}"
        cursor_name = f"r2g_{table}"
        conn = self.connection
        with conn.cursor(name=cursor_name, row_factory=dict_row) as cur:
            cur.itersize = max(1, batch_size)
            if since_column and since_value is not None:
                cur.execute(
                    f'SELECT * FROM {qualified} '  # noqa: S608
                    f"WHERE {quote_identifier(since_column)} >= %s",
                    (since_value,),
                )
            else:
                cur.execute(f"SELECT * FROM {qualified}")  # noqa: S608
            yield from cur

    def dump_table_to_csv(
        self,
        table: str,
        out_path: Path,
        *,
        header: bool = True,
    ) -> int:
        """Export *table* via ``COPY TO STDOUT WITH CSV``.

        Uses the server-side fast path rather than Python-level row
        fetching. Returns the number of data rows written (i.e.
        excluding the header).
        """
        qualified = f"{quote_identifier(self.schema_name)}.{quote_identifier(table)}"
        header_clause = "CSV HEADER" if header else "CSV"
        copy_sql = f"COPY {qualified} TO STDOUT WITH {header_clause}"  # noqa: S608
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connection
        with out_path.open("wb") as f:
            with conn.cursor() as cur:
                with cur.copy(copy_sql) as copy:
                    for chunk in copy:
                        f.write(chunk)
        with out_path.open("rb") as f:
            total = sum(1 for _ in f)
        return max(0, total - (1 if header else 0))


__all__ = ["PostgresConnector", "PostgresSession"]
