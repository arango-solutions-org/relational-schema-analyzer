"""Microsoft SQL Server source connector.

Provides:

- :class:`SQLServerConnector` — schema introspection over ``INFORMATION_SCHEMA``
  (tables, columns, primary keys) plus ``sys.foreign_keys`` /
  ``sys.foreign_key_columns`` for declared foreign keys (the clean catalog path
  for composite FKs and their referenced columns).
- :class:`SQLServerSession` — bulk-read session implementing the
  :class:`r2g.connectors.session.SourceSession` Protocol: batched cursor
  streaming and a cursor-based CSV export. Both the streaming pipeline and
  ``source dump`` consume SQL Server through this interface, exactly like the
  other relational connectors.

Schema vs database
------------------

Like PostgreSQL (and unlike MySQL), SQL Server has schemas *inside* a database.
The database lives in the connection string; ``schema_name`` selects the
namespace and defaults to ``dbo``. ``--pg-schema`` (the ``schema_name``
constructor argument) overrides it; the historical ``public`` default is
treated as "use ``dbo``".

Read consistency
----------------

The session reads under SQL Server's default isolation (``READ COMMITTED``).
A true cross-table snapshot would require ``SNAPSHOT`` isolation, which in turn
requires ``ALLOW_SNAPSHOT_ISOLATION`` to be enabled on the database; forcing it
when it is off fails mid-read, so r2g does not force it. Enable snapshot
isolation server-side if you need a point-in-time view across tables.

Connection string format
------------------------

::

    mssql://<user>:<password>@<host>[:<port>]/<database>
    sqlserver://<user>:<password>@<host>[:<port>]/<database>

Default port is 1433.

Missing ``pymssql``
-------------------

``pymssql`` is an optional dependency (``r2g-arango[sqlserver]``). It is never
imported at module-import time; the first introspection / read raises
:class:`ImportError` with a pip-install hint so the UI / MCP server can surface
a clean message.
"""

from __future__ import annotations

import csv
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, unquote, urlparse

from ..bitemporal import CATALOG
from ..bitemporal import to_iso as _iso
from ..check_enum import enrich_check_constraint
from ..log import get_logger
from ..types import CheckConstraint, Column, Schema, SourceProvenance, Table
from .base import group_foreign_keys, mark_one_to_one, unique_columns

logger = get_logger(__name__)

_DEFAULT_SCHEMA_SENTINELS = frozenset({None, "", "public", "PUBLIC"})

# SQL Server `bit` is a boolean (0/1), but the shared type map treats `bit` as a
# PostgreSQL bit-string (→ string). Translate it connector-side so the stored
# data_type carries the correct SQL Server semantics.
_MSSQL_TYPE_OVERRIDES: dict[str, str] = {"bit": "boolean"}


def _load_pymssql() -> Any:
    """Import ``pymssql`` lazily with a helpful error."""
    try:
        import pymssql
    except ImportError as err:
        raise ImportError(
            "SQL Server support requires pymssql. "
            "Install with: pip install 'relational-schema-analyzer[sqlserver]'"
        ) from err
    return pymssql


def _quote_ident(name: str) -> str:
    """Bracket-quote a SQL Server identifier, escaping embedded ``]``."""
    return "[" + name.replace("]", "]]") + "]"


def _parse_mssql_url(url: str) -> dict[str, Any]:
    """Parse an ``mssql://`` / ``sqlserver://`` URL into ``pymssql.connect`` kwargs.

    Returns ``server`` / ``port`` / ``user`` / ``password`` / ``database``.
    A ``+driver`` suffix on the scheme (e.g. ``mssql+pymssql``) is tolerated.
    Raises :class:`ValueError` for a malformed URL.
    """
    if not url or "://" not in url:
        raise ValueError(
            "SQL Server connection string must look like "
            "mssql://user:pass@host[:port]/database"
        )
    parsed = urlparse(url)
    scheme = parsed.scheme.lower().split("+", 1)[0]
    if scheme not in ("mssql", "sqlserver"):
        raise ValueError(
            f"Expected an mssql:// or sqlserver:// connection string, got scheme '{parsed.scheme}'"
        )

    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    host = parsed.hostname or ""
    if not user or not host:
        raise ValueError(
            "SQL Server connection string must include user and host: "
            "mssql://user:pass@host/database"
        )

    database = (parsed.path or "").lstrip("/").split("/")[0]
    if not database:
        raise ValueError(
            "SQL Server connection string is missing a database path component: "
            "mssql://user:pass@host/<database>"
        )

    # Reserved for future tuning knobs (e.g. tds_version); parsed but unused.
    parse_qs(parsed.query, keep_blank_values=True)

    return {
        "server": host,
        "user": user,
        "password": password,
        "database": database,
        "port": parsed.port or 1433,
    }


class SQLServerConnector:
    """Microsoft SQL Server source connector (introspection + session factory)."""

    def __init__(self, connection_string: str, schema_name: str = "dbo") -> None:
        self.connection_string = connection_string
        self._connect_params = _parse_mssql_url(connection_string)
        self._database = self._connect_params["database"]
        self.schema_name = "dbo" if schema_name in _DEFAULT_SCHEMA_SENTINELS else schema_name

    def _connect(self) -> Any:
        pymssql = _load_pymssql()
        try:
            return pymssql.connect(**self._connect_params)
        except Exception as err:
            raise RuntimeError(f"Failed to connect to SQL Server: {err}") from err

    def open_session(self) -> "SQLServerSession":
        """Open a bulk-read session for streaming / dumps."""
        return SQLServerSession(
            self.connection_string,
            schema_name=self.schema_name,
            connect_params=dict(self._connect_params),
        )

    def get_schema(self) -> Schema:
        """Introspect the SQL Server schema and return a populated :class:`Schema`."""
        logger.info(
            "mssql_connect",
            server=self._connect_params.get("server"),
            port=self._connect_params.get("port"),
            database=self._database,
            schema=self.schema_name,
        )
        conn = self._connect()
        try:
            return self._introspect(conn)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _provenance(self, conn: Any) -> SourceProvenance:
        version: Optional[str] = None
        database: Optional[str] = None
        cur = conn.cursor(as_dict=True)
        try:
            cur.execute(
                "SELECT CAST(SERVERPROPERTY('ProductVersion') AS VARCHAR(64)) AS ver, "
                "DB_NAME() AS db"
            )
            row = cur.fetchone()
            if row:
                version = row.get("ver")
                database = row.get("db")
        except Exception:  # noqa: BLE001 - provenance is best-effort
            pass
        finally:
            cur.close()
        return SourceProvenance(
            dialect="sqlserver",
            server_version=(str(version) if version else None),
            database=database,
            namespace=self.schema_name,
        )

    def _introspect(self, conn: Any) -> Schema:
        schema = Schema(source=self._provenance(conn))
        cur = conn.cursor(as_dict=True)
        try:
            cur.execute(
                """
                SELECT TABLE_NAME, TABLE_TYPE
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')
                ORDER BY TABLE_NAME
                """,
                (self.schema_name,),
            )
            table_rows = cur.fetchall()
        finally:
            cur.close()

        valid_times = self._valid_times(conn)

        for row in table_rows:
            name = row["TABLE_NAME"]
            table = self._process_table(
                conn, name, is_view=(row.get("TABLE_TYPE") == "VIEW")
            )
            if name in valid_times:
                table.valid_from = valid_times[name]
                table.valid_time_source = CATALOG
            schema.tables[name] = table
        return schema

    def _valid_times(self, conn: Any) -> dict[str, str]:
        """``sys.objects.modify_date`` per table, for bitemporal valid time (addendum §3).

        The most trustworthy signal of any source RSA supports: SQL Server moves
        ``modify_date`` on ``ALTER`` and index changes but not on DML, so it means what the
        addendum wants ``valid_from`` to mean. It is still passed through the fingerprint
        rule, which costs nothing when the date is right and protects against the cases
        where an index rebuild moved it without changing the schema RSA models.

        Best-effort: a role without ``sys.objects`` visibility loses the date, not the run.
        """
        out: dict[str, str] = {}
        cur = conn.cursor(as_dict=True)
        try:
            cur.execute(
                """
                SELECT o.name AS TABLE_NAME,
                       COALESCE(o.modify_date, o.create_date) AS CHANGED_AT
                FROM sys.objects o
                JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE s.name = %s AND o.type IN ('U', 'V')
                """,
                (self.schema_name,),
            )
            for row in cur.fetchall() or []:
                stamp = _iso(row.get("CHANGED_AT"))
                if stamp:
                    out[row["TABLE_NAME"]] = stamp
        except Exception as err:  # noqa: BLE001 - valid time is best-effort
            logger.warning("mssql_valid_time_failed", error=str(err))
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass
        return out

    def _process_table(self, conn: Any, table_name: str, *, is_view: bool = False) -> Table:
        cur = conn.cursor(as_dict=True)
        try:
            cur.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, ORDINAL_POSITION
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                (self.schema_name, table_name),
            )
            columns_data = cur.fetchall()

            cur.execute(
                """
                SELECT kcu.COLUMN_NAME
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                  ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                 AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
                WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                  AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
                ORDER BY kcu.ORDINAL_POSITION
                """,
                (self.schema_name, table_name),
            )
            pks = [row["COLUMN_NAME"] for row in cur.fetchall()]

            unique_sets = self._fetch_unique_constraints(cur, table_name, pks)
            checks = self._fetch_check_constraints(
                cur, table_name, [c["COLUMN_NAME"] for c in columns_data]
            )
            col_comments = self._fetch_column_comments(cur, table_name)
            table_comment = self._fetch_table_comment(cur, table_name)

            # sys.* catalog views give referenced table/column + composite
            # ordering directly (INFORMATION_SCHEMA does not expose the
            # referenced columns cleanly on SQL Server).
            cur.execute(
                """
                SELECT fk.name              AS constraint_name,
                       cpar.name            AS column_name,
                       rt.name              AS foreign_table_name,
                       cref.name            AS foreign_column_name
                FROM sys.foreign_keys fk
                JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
                JOIN sys.tables t   ON t.object_id = fk.parent_object_id
                JOIN sys.schemas s  ON s.schema_id = t.schema_id
                JOIN sys.columns cpar ON cpar.object_id = fk.parent_object_id
                                     AND cpar.column_id = fkc.parent_column_id
                JOIN sys.tables rt  ON rt.object_id = fk.referenced_object_id
                JOIN sys.columns cref ON cref.object_id = fk.referenced_object_id
                                     AND cref.column_id = fkc.referenced_column_id
                WHERE s.name = %s AND t.name = %s
                ORDER BY fk.name, fkc.constraint_column_id
                """,
                (self.schema_name, table_name),
            )
            fk_rows = cur.fetchall()
        finally:
            cur.close()

        single_unique = unique_columns(pks, unique_sets)

        columns = []
        for c in columns_data:
            raw_type = (c["DATA_TYPE"] or "").lower()
            name = c["COLUMN_NAME"]
            ordinal = c.get("ORDINAL_POSITION")
            columns.append(
                Column(
                    name=name,
                    data_type=_MSSQL_TYPE_OVERRIDES.get(raw_type, raw_type),
                    is_nullable=(c["IS_NULLABLE"] == "YES"),
                    is_primary_key=(name in pks),
                    is_unique=(name in single_unique),
                    default=(str(c["COLUMN_DEFAULT"]) if c.get("COLUMN_DEFAULT") is not None else None),
                    comment=col_comments.get(name),
                    ordinal=(int(ordinal) - 1 if ordinal is not None else None),
                )
            )

        fks = group_foreign_keys(
            fk_rows,
            constraint="constraint_name",
            column="column_name",
            ref_table="foreign_table_name",
            ref_column="foreign_column_name",
        )
        mark_one_to_one(fks, pks, unique_sets)

        return Table(
            name=table_name,
            columns=columns,
            primary_key=pks,
            foreign_keys=fks,
            is_view=is_view,
            comment=table_comment,
            schema_name=self.schema_name,
            unique_constraints=[list(u) for u in unique_sets],
            check_constraints=checks,
        )

    def _fetch_unique_constraints(
        self, cur: Any, table_name: str, primary_key: list[str] | None = None
    ) -> list[list[str]]:
        cur.execute(
            """
            SELECT tc.CONSTRAINT_NAME, kcu.COLUMN_NAME, kcu.ORDINAL_POSITION
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
              ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
             AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
            WHERE tc.CONSTRAINT_TYPE = 'UNIQUE'
              AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
            ORDER BY tc.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
            """,
            (self.schema_name, table_name),
        )
        grouped: OrderedDict[str, list[str]] = OrderedDict()
        for row in cur.fetchall():
            grouped.setdefault(row["CONSTRAINT_NAME"], []).append(row["COLUMN_NAME"])
        from .base import merge_unique_sets

        return merge_unique_sets(
            list(grouped.values()), self._fetch_unique_indexes(cur, table_name), primary_key or []
        )

    def _fetch_unique_indexes(self, cur: Any, table_name: str) -> list[list[str]]:
        """Key columns of unique indexes that back no constraint.

        ``CREATE UNIQUE INDEX`` enforces uniqueness like a ``UNIQUE`` constraint but
        is not listed in ``INFORMATION_SCHEMA.TABLE_CONSTRAINTS``, so FK inference
        could not target it. Excluded: filtered indexes (``WHERE``: unique only over
        a subset of rows), disabled indexes, hypothetical indexes (left by the
        Database Engine Tuning Advisor; they hold no data and enforce nothing),
        ``INCLUDE`` columns (stored, not key), indexes backing a PK or UNIQUE
        constraint (reported by the constraint query), and any index with a
        computed column in its key -- ``LOWER(email)`` as a computed column is an
        expression key, which PostgreSQL rejects too.
        """
        cur.execute(
            """
            SELECT i.name AS INDEX_NAME, c.name AS COLUMN_NAME, ic.key_ordinal
            FROM sys.indexes i
            JOIN sys.index_columns ic
              ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c
              ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            JOIN sys.tables t ON t.object_id = i.object_id
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE i.is_unique = 1 AND i.is_primary_key = 0
              AND i.is_unique_constraint = 0 AND i.has_filter = 0
              AND i.is_disabled = 0 AND i.is_hypothetical = 0
              AND ic.is_included_column = 0
              AND NOT EXISTS (
                  SELECT 1 FROM sys.index_columns kc
                  JOIN sys.columns cc
                    ON cc.object_id = kc.object_id AND cc.column_id = kc.column_id
                  WHERE kc.object_id = i.object_id AND kc.index_id = i.index_id
                    AND kc.is_included_column = 0 AND cc.is_computed = 1
              )
              AND s.name = %s AND t.name = %s
            ORDER BY i.name, ic.key_ordinal
            """,
            (self.schema_name, table_name),
        )
        grouped: OrderedDict[str, list[str]] = OrderedDict()
        for row in cur.fetchall():
            grouped.setdefault(row["INDEX_NAME"], []).append(row["COLUMN_NAME"])
        return list(grouped.values())

    def _fetch_check_constraints(
        self, cur: Any, table_name: str, column_names: Optional[list[str]] = None
    ) -> list[CheckConstraint]:
        """CHECK constraints, with ``enum_values`` read from enumerating expressions.

        SQL Server rewrites ``[status] IN ('a','b')`` as ``([status]='b' OR [status]='a')``,
        which ``check_enum`` recognises. A disabled constraint (``NOCHECK CONSTRAINT``) is
        not enforced, and an untrusted one (``WITH NOCHECK``) never checked existing rows,
        so neither is read for values; both keep their raw expression as before.
        """
        try:
            cur.execute(
                """
                SELECT cc.name AS name, cc.definition AS definition, col.name AS column_name,
                       cc.is_disabled AS is_disabled, cc.is_not_trusted AS is_not_trusted
                FROM sys.check_constraints cc
                JOIN sys.tables t ON t.object_id = cc.parent_object_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                LEFT JOIN sys.columns col ON col.object_id = cc.parent_object_id
                                         AND col.column_id = cc.parent_column_id
                WHERE s.name = %s AND t.name = %s
                """,
                (self.schema_name, table_name),
            )
        except Exception:  # noqa: BLE001 - best-effort
            return []
        out: list[CheckConstraint] = []
        for r in cur.fetchall():
            cols = [r["column_name"]] if r.get("column_name") else []
            check = CheckConstraint(
                name=r.get("name"), expression=(r.get("definition") or ""), columns=cols
            )
            if column_names is not None and not (r.get("is_disabled") or r.get("is_not_trusted")):
                check = enrich_check_constraint(check, column_names)
            out.append(check)
        return out

    def _fetch_table_comment(self, cur: Any, table_name: str) -> Optional[str]:
        try:
            cur.execute(
                """
                SELECT CAST(ep.value AS NVARCHAR(MAX)) AS comment
                FROM sys.extended_properties ep
                JOIN sys.tables t ON t.object_id = ep.major_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE ep.class = 1 AND ep.minor_id = 0 AND ep.name = 'MS_Description'
                  AND s.name = %s AND t.name = %s
                """,
                (self.schema_name, table_name),
            )
            row = cur.fetchone()
        except Exception:  # noqa: BLE001 - best-effort
            return None
        return row.get("comment") if row else None

    def _fetch_column_comments(self, cur: Any, table_name: str) -> dict[str, str]:
        try:
            cur.execute(
                """
                SELECT c.name AS column_name, CAST(ep.value AS NVARCHAR(MAX)) AS comment
                FROM sys.extended_properties ep
                JOIN sys.columns c ON c.object_id = ep.major_id AND c.column_id = ep.minor_id
                JOIN sys.tables t ON t.object_id = ep.major_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE ep.class = 1 AND ep.minor_id > 0 AND ep.name = 'MS_Description'
                  AND s.name = %s AND t.name = %s
                """,
                (self.schema_name, table_name),
            )
        except Exception:  # noqa: BLE001 - best-effort
            return {}
        return {r["column_name"]: r["comment"] for r in cur.fetchall() if r.get("comment")}


class SQLServerSession:
    """Bulk-read session for :class:`SQLServerConnector`.

    Holds one connection reused across every count / stream / dump. Reads run
    under the server's default isolation (see the module docstring on snapshot
    isolation). Each instance owns its connection; call :meth:`close` when done.
    Parallel workers each open their own session.
    """

    def __init__(
        self,
        connection_string: str,
        *,
        schema_name: str,
        connect_params: dict[str, Any],
    ) -> None:
        self.connection_string = connection_string
        self.schema_name = schema_name
        self._connect_params = dict(connect_params)
        self._conn: Any = None

    @property
    def connection(self) -> Any:
        if self._conn is None:
            pymssql = _load_pymssql()
            try:
                self._conn = pymssql.connect(**self._connect_params)
            except Exception as err:
                raise RuntimeError(f"Failed to connect to SQL Server: {err}") from err
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def __enter__(self) -> "SQLServerSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _qualified(self, table: str) -> str:
        return f"{_quote_ident(self.schema_name)}.{_quote_ident(table)}"

    def count_rows(
        self,
        table: str,
        *,
        since_column: Optional[str] = None,
        since_value: Optional[str] = None,
    ) -> int:
        q = self._qualified(table)
        conn = self.connection
        cur = conn.cursor()
        try:
            if since_column and since_value is not None:
                cur.execute(
                    f"SELECT COUNT(*) FROM {q} WHERE {_quote_ident(since_column)} >= %s",  # noqa: S608
                    (since_value,),
                )
            else:
                cur.execute(f"SELECT COUNT(*) FROM {q}")  # noqa: S608
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            cur.close()

    def stream_rows(
        self,
        table: str,
        *,
        batch_size: int = 10_000,
        since_column: Optional[str] = None,
        since_value: Optional[str] = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream rows in ``batch_size`` chunks via ``fetchmany``."""
        q = self._qualified(table)
        conn = self.connection
        cur = conn.cursor(as_dict=True)
        try:
            if since_column and since_value is not None:
                cur.execute(
                    f"SELECT * FROM {q} WHERE {_quote_ident(since_column)} >= %s",  # noqa: S608
                    (since_value,),
                )
            else:
                cur.execute(f"SELECT * FROM {q}")  # noqa: S608
            fetch = max(1, batch_size)
            while True:
                rows = cur.fetchmany(fetch)
                if not rows:
                    break
                yield from rows
        finally:
            cur.close()

    def dump_table_to_csv(
        self,
        table: str,
        out_path: Path,
        *,
        header: bool = True,
    ) -> int:
        """Export *table* as CSV through the cursor."""
        q = self._qualified(table)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connection
        cur = conn.cursor()
        total = 0
        try:
            cur.execute(f"SELECT * FROM {q}")  # noqa: S608
            col_names = [d[0] for d in (cur.description or [])]
            with out_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f, lineterminator="\n")
                if header:
                    writer.writerow(col_names)
                while True:
                    rows = cur.fetchmany(10_000)
                    if not rows:
                        break
                    for row in rows:
                        writer.writerow(["" if v is None else v for v in row])
                        total += 1
        finally:
            cur.close()
        return total


__all__ = ["SQLServerConnector", "SQLServerSession"]
