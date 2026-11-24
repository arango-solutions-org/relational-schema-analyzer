"""MySQL / MariaDB source connector.

Provides:

- :class:`MySQLConnector` — schema introspection over ``information_schema``.
- :class:`MySQLSession` — bulk-read session implementing the
  :class:`r2g.connectors.session.SourceSession` Protocol: a consistent-snapshot
  transaction, server-side (unbuffered) cursor streaming, and a cursor-based
  CSV export. Both the streaming pipeline and ``source dump`` consume MySQL
  through this interface, exactly like PostgreSQL and Snowflake.

MariaDB is wire- and ``information_schema``-compatible with MySQL, so the same
connector serves both (``mysql://`` and ``mariadb://`` URLs both work).

Consistent snapshot
-------------------

InnoDB's ``REPEATABLE READ`` plus ``START TRANSACTION WITH CONSISTENT
SNAPSHOT`` gives the session a single point-in-time view across every table
read — the MySQL analog of PostgreSQL's ``SET TRANSACTION ISOLATION LEVEL
REPEATABLE READ`` that :class:`~r2g.streaming.pipeline.StreamingPipeline` has
required since day one.

Connection string format
------------------------

::

    mysql://<user>:<password>@<host>[:<port>]/<database>
    mariadb://<user>:<password>@<host>[:<port>]/<database>

MySQL has no schema namespace separate from the database, so the database in
the URL path *is* the introspection namespace. The connector's ``schema_name``
attribute therefore holds the database name. ``--pg-schema`` (passed as the
``schema_name`` constructor argument) overrides which database to introspect
when it names a real, non-default value; the historical ``public`` default is
treated as "use the database from the URL".

Missing ``pymysql``
-------------------

``pymysql`` is an optional dependency (``r2g-arango[mysql]``). It is never
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
from ..check_enum import enrich_check_constraint, parse_mysql_enum_type
from ..log import get_logger
from ..quoting import quote_backtick
from ..redact import connection_error, secrets_in
from ..types import CheckConstraint, Column, Schema, SourceProvenance, Table
from .base import group_foreign_keys, mark_one_to_one, unique_columns

logger = get_logger(__name__)

#: Attached to every MySQL table carrying a `catalog` valid time. `CREATE_TIME` does not
#: move on `ALTER`, so the date is a lower bound, and the fingerprint rule cannot correct
#: an under-date — it only discards dates that are too fresh (addendum §3, open question 1).
MYSQL_VALID_TIME_CAVEAT = (
    "MySQL CREATE_TIME does not advance on ALTER; valid_from is a lower bound for tables "
    "altered after creation."
)

_DEFAULT_SCHEMA_SENTINELS = frozenset({None, "", "public", "PUBLIC"})


def _load_pymysql() -> Any:
    """Import ``pymysql`` lazily with a helpful error.

    Centralising the import means both :class:`MySQLConnector` and
    :class:`MySQLSession` surface the same message when the optional extra is
    not installed.
    """
    try:
        import pymysql
    except ImportError as err:
        raise ImportError(
            "MySQL support requires pymysql. "
            "Install with: pip install 'relational-schema-analyzer[mysql]'"
        ) from err
    return pymysql


_quote_ident = quote_backtick  # ```name```, doubling a backtick


def _parse_mysql_url(url: str) -> dict[str, Any]:
    """Parse a ``mysql://`` / ``mariadb://`` URL into ``pymysql.connect`` kwargs.

    Returns ``host`` / ``port`` / ``user`` / ``password`` / ``database`` plus
    any recognised query parameters (e.g. ``charset``). Raises
    :class:`ValueError` for a malformed URL.
    """
    if not url or "://" not in url:
        raise ValueError(
            "MySQL connection string must look like "
            "mysql://user:pass@host[:port]/database"
        )
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("mysql", "mariadb"):
        raise ValueError(
            f"Expected a mysql:// or mariadb:// connection string, got scheme '{parsed.scheme}'"
        )

    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    host = parsed.hostname or ""
    if not user or not host:
        raise ValueError(
            "MySQL connection string must include user and host: "
            "mysql://user:pass@host/database"
        )

    database = (parsed.path or "").lstrip("/").split("/")[0]
    if not database:
        raise ValueError(
            "MySQL connection string is missing a database path component: "
            "mysql://user:pass@host/<database>"
        )

    query = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items() if v}

    kwargs: dict[str, Any] = {
        "host": host,
        "user": user,
        "password": password,
        "database": database,
        "port": parsed.port or 3306,
        "charset": query.get("charset", "utf8mb4"),
    }
    return kwargs


class MySQLConnector:
    """MySQL / MariaDB source connector (introspection + session factory)."""

    def __init__(self, connection_string: str, schema_name: str = "") -> None:
        self.connection_string = connection_string
        self._connect_params = _parse_mysql_url(connection_string)
        url_database = self._connect_params["database"]
        # The database in the URL is the namespace; an explicit, non-default
        # schema_name overrides which database to introspect.
        if schema_name in _DEFAULT_SCHEMA_SENTINELS:
            self.schema_name = url_database
        else:
            self.schema_name = schema_name
            self._connect_params["database"] = schema_name

    def _connect(self) -> Any:
        pymysql = _load_pymysql()
        try:
            return pymysql.connect(
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
                **self._connect_params,
            )
        except Exception as err:
            raise connection_error(
                "Failed to connect to MySQL", err, secrets_in(self._connect_params, self.connection_string)
            ) from None

    def open_session(self) -> "MySQLSession":
        """Open a consistent-snapshot read session for streaming / dumps."""
        return MySQLSession(
            self.connection_string,
            schema_name=self.schema_name,
            connect_params=dict(self._connect_params),
        )

    def get_schema(self) -> Schema:
        """Introspect the MySQL schema and return a populated :class:`Schema`."""
        logger.info(
            "mysql_connect",
            host=self._connect_params.get("host"),
            port=self._connect_params.get("port"),
            database=self.schema_name,
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
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION() AS v")
                row = cur.fetchone()
                if row:
                    version = row.get("v") if isinstance(row, dict) else row[0]
        except Exception:  # noqa: BLE001 - provenance is best-effort
            version = None
        return SourceProvenance(
            dialect="mysql",
            server_version=(str(version) if version else None),
            database=self.schema_name,
            namespace=self.schema_name,
        )

    def _introspect(self, conn: Any) -> Schema:
        schema = Schema(source=self._provenance(conn))
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME, TABLE_TYPE, TABLE_COMMENT
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')
                ORDER BY TABLE_NAME
                """,
                (self.schema_name,),
            )
            table_rows = cur.fetchall()

        valid_times = self._valid_times(conn)

        for row in table_rows:
            name = row["TABLE_NAME"]
            table = self._process_table(
                conn,
                name,
                is_view=(row.get("TABLE_TYPE") == "VIEW"),
                comment=row.get("TABLE_COMMENT"),
            )
            if name in valid_times:
                table.valid_from = valid_times[name]
                table.valid_time_source = CATALOG
                # The limitation travels with the data rather than living only in a doc,
                # because a consumer reading `catalog` is entitled to know it may be early.
                table.extra.setdefault("validTimeCaveat", MYSQL_VALID_TIME_CAVEAT)
            schema.tables[name] = table
        return schema

    def _valid_times(self, conn: Any) -> dict[str, str]:
        """``CREATE_TIME`` per table, for bitemporal valid time (addendum §3).

        The weakest catalog signal RSA reads, and the one the fingerprint rule cannot
        rescue. MySQL does **not** move ``CREATE_TIME`` on ``ALTER``, so a table altered
        after creation is dated *earlier* than the truth. The §2.3 rule only ever discards
        a date that is too fresh; nothing can detect one that is too old.

        ``UPDATE_TIME`` is not a substitute — it tracks DML, which is the opposite problem,
        and on InnoDB it is frequently NULL besides.

        So the value is recorded as ``catalog`` with an explicit caveat attached to each
        table (``extra.validTimeCaveat``), and consumers that need exact DDL dating on MySQL
        should treat it as a lower bound.
        """
        out: dict[str, str] = {}
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT TABLE_NAME, CREATE_TIME
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s
                    """,
                    (self.schema_name,),
                )
                for row in cur.fetchall() or []:
                    stamp = _iso(row.get("CREATE_TIME"))
                    if stamp:
                        out[row["TABLE_NAME"]] = stamp
        except Exception as err:  # noqa: BLE001 - valid time is best-effort
            logger.warning("mysql_valid_time_failed", error=str(err))
        return out

    def _process_table(
        self, conn: Any, table_name: str, *, is_view: bool = False, comment: Any = None
    ) -> Table:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE,
                       COLUMN_DEFAULT, ORDINAL_POSITION, COLUMN_COMMENT
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                (self.schema_name, table_name),
            )
            columns_data = cur.fetchall()

            cur.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                  AND CONSTRAINT_NAME = 'PRIMARY'
                ORDER BY ORDINAL_POSITION
                """,
                (self.schema_name, table_name),
            )
            pks = [row["COLUMN_NAME"] for row in cur.fetchall()]

            unique_sets = self._fetch_unique_constraints(cur, table_name)

            cur.execute(
                """
                SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME,
                       CONSTRAINT_NAME
                FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                  AND REFERENCED_TABLE_NAME IS NOT NULL
                ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION
                """,
                (self.schema_name, table_name),
            )
            fk_rows = cur.fetchall()

            checks = self._fetch_check_constraints(
                cur, table_name, [c["COLUMN_NAME"] for c in columns_data]
            )
        if not is_view:
            # A view reports its source column's ENUM type too, but declares no constraint.
            checks.extend(_enum_column_checks(columns_data, checks))

        single_unique = unique_columns(pks, unique_sets)

        columns = [
            Column(
                name=c["COLUMN_NAME"],
                data_type=(c["DATA_TYPE"] or "").lower(),
                is_nullable=(c["IS_NULLABLE"] == "YES"),
                is_primary_key=(c["COLUMN_NAME"] in pks),
                is_unique=(c["COLUMN_NAME"] in single_unique),
                default=(str(c["COLUMN_DEFAULT"]) if c.get("COLUMN_DEFAULT") is not None else None),
                comment=(c.get("COLUMN_COMMENT") or None),
                ordinal=(int(c["ORDINAL_POSITION"]) - 1 if c.get("ORDINAL_POSITION") else None),
            )
            for c in columns_data
        ]

        fks = group_foreign_keys(
            fk_rows,
            constraint="CONSTRAINT_NAME",
            column="COLUMN_NAME",
            ref_table="REFERENCED_TABLE_NAME",
            ref_column="REFERENCED_COLUMN_NAME",
        )
        mark_one_to_one(fks, pks, unique_sets)

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

    def _fetch_unique_constraints(self, cur: Any, table_name: str) -> list[list[str]]:
        """UNIQUE keys, including unique indexes (MySQL lists both as constraints).

        A unique index with a *functional* key part -- ``UNIQUE (ext, (lower(note)))``
        -- is excluded: ``KEY_COLUMN_USAGE`` lists only its plain columns, so it
        would otherwise be reported as a key on ``ext`` alone, which is false (only
        the pair with the expression is unique). Functional parts appear in
        ``STATISTICS`` with a NULL ``COLUMN_NAME``; testing for that rather than
        MySQL 8's ``EXPRESSION`` column also works on MariaDB, which lacks it.
        Prefix parts (``email(10)``) stay: distinct prefixes imply distinct values.
        """
        cur.execute(
            """
            SELECT tc.CONSTRAINT_NAME, kcu.COLUMN_NAME, kcu.ORDINAL_POSITION
            FROM information_schema.TABLE_CONSTRAINTS tc
            JOIN information_schema.KEY_COLUMN_USAGE kcu
              ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
              AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
              AND tc.TABLE_NAME = kcu.TABLE_NAME
            WHERE tc.CONSTRAINT_TYPE = 'UNIQUE'
              AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
              AND NOT EXISTS (
                SELECT 1 FROM information_schema.STATISTICS st
                WHERE st.TABLE_SCHEMA = tc.TABLE_SCHEMA
                  AND st.TABLE_NAME = tc.TABLE_NAME
                  AND st.INDEX_NAME = tc.CONSTRAINT_NAME
                  AND st.COLUMN_NAME IS NULL)
            ORDER BY tc.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
            """,
            (self.schema_name, table_name),
        )
        grouped: OrderedDict[str, list[str]] = OrderedDict()
        for row in cur.fetchall():
            grouped.setdefault(row["CONSTRAINT_NAME"], []).append(row["COLUMN_NAME"])
        return list(grouped.values())

    def _fetch_check_constraints(
        self, cur: Any, table_name: str, column_names: Optional[list[str]] = None
    ) -> list[CheckConstraint]:
        """CHECK constraints (MySQL 8.0.16+ / MariaDB 10.2+). Best-effort.

        ``enum_values`` (and the one column, which MySQL does not report) are read from an
        enumerating clause -- ``(`status` in (_utf8mb4'a',_utf8mb4'b'))``. A constraint
        declared ``NOT ENFORCED`` guarantees nothing about the rows, so it keeps its raw
        expression only. MariaDB has no ``ENFORCED`` column; there the query is retried
        without it and every constraint counts as enforced, which on MariaDB they are.
        """
        rows = None
        for with_enforced in (True, False):
            enforced_col = ", tc.ENFORCED AS enforced" if with_enforced else ""
            try:
                cur.execute(
                    f"""
                    SELECT tc.CONSTRAINT_NAME AS name, cc.CHECK_CLAUSE AS definition
                           {enforced_col}
                    FROM information_schema.TABLE_CONSTRAINTS tc
                    JOIN information_schema.CHECK_CONSTRAINTS cc
                      ON tc.CONSTRAINT_SCHEMA = cc.CONSTRAINT_SCHEMA
                      AND tc.CONSTRAINT_NAME = cc.CONSTRAINT_NAME
                    WHERE tc.CONSTRAINT_TYPE = 'CHECK'
                      AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
                    """,  # noqa: S608 - enforced_col is one of two fixed fragments
                    (self.schema_name, table_name),
                )
            except Exception:  # noqa: BLE001 - older servers lack CHECK_CONSTRAINTS
                continue
            rows = cur.fetchall()
            break
        if rows is None:
            return []
        out: list[CheckConstraint] = []
        for r in rows:
            check = CheckConstraint(name=r.get("name"), expression=(r.get("definition") or ""))
            enforced = str(r.get("enforced") or "YES").upper() != "NO"
            if column_names is not None and enforced:
                check = enrich_check_constraint(check, column_names, dialect="mysql")
            out.append(check)
        return out


def _enum_column_checks(
    columns_data: list[dict[str, Any]], checks: list[CheckConstraint]
) -> list[CheckConstraint]:
    """A MySQL ``ENUM`` column declares its value set in its type; report it as one.

    ``DATA_TYPE`` says only ``enum``, so the values were dropped along with the rest of
    ``COLUMN_TYPE``. Each becomes a :class:`CheckConstraint` named ``<column>_enum`` (the
    name the CSV connector gives its enumerations) whose expression is the raw
    ``COLUMN_TYPE``, so the evidence stays visible. A column that a CHECK constraint
    already enumerates is skipped rather than reported twice. ``SET`` is not an
    enumeration -- a row may hold any subset -- and is not read.
    """
    covered = {c.columns[0].casefold() for c in checks if c.enum_values and len(c.columns) == 1}
    out: list[CheckConstraint] = []
    for c in columns_data:
        if (c.get("DATA_TYPE") or "").lower() != "enum":
            continue
        name = c["COLUMN_NAME"]
        column_type = c.get("COLUMN_TYPE") or ""
        values = parse_mysql_enum_type(column_type)
        if not values or name.casefold() in covered:
            continue
        out.append(
            CheckConstraint(
                name=f"{name}_enum", expression=column_type, columns=[name], enum_values=values
            )
        )
    return out


class MySQLSession:
    """Bulk-read session for :class:`MySQLConnector`.

    Holds one ``autocommit=False`` connection running a single
    ``REPEATABLE READ`` + ``START TRANSACTION WITH CONSISTENT SNAPSHOT``
    transaction so every count / stream / dump during the session sees the same
    committed snapshot. Each instance owns its connection; call :meth:`close`
    when done. Parallel workers each open their own session.
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
            pymysql = _load_pymysql()
            params = dict(self._connect_params)
            params["autocommit"] = False
            try:
                self._conn = pymysql.connect(**params)
            except Exception as err:
                raise connection_error(
                    "Failed to connect to MySQL", err, secrets_in(self._connect_params, self.connection_string)
                ) from None
            with self._conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.commit()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def __enter__(self) -> "MySQLSession":
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
        with conn.cursor() as cur:
            if since_column and since_value is not None:
                cur.execute(
                    f"SELECT COUNT(*) FROM {q} WHERE {_quote_ident(since_column)} >= %s",  # noqa: S608
                    (since_value,),
                )
            else:
                cur.execute(f"SELECT COUNT(*) FROM {q}")  # noqa: S608
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
        """Stream rows via an unbuffered (server-side) cursor.

        ``SSDictCursor`` pulls rows from the server incrementally rather than
        buffering the whole result set in the client, so wide / large tables do
        not blow up memory.
        """
        pymysql = _load_pymysql()
        q = self._qualified(table)
        conn = self.connection
        with conn.cursor(pymysql.cursors.SSDictCursor) as cur:
            if since_column and since_value is not None:
                cur.execute(
                    f"SELECT * FROM {q} WHERE {_quote_ident(since_column)} >= %s",  # noqa: S608
                    (since_value,),
                )
            else:
                cur.execute(f"SELECT * FROM {q}")  # noqa: S608
            yield from cur

    def dump_table_to_csv(
        self,
        table: str,
        out_path: Path,
        *,
        header: bool = True,
    ) -> int:
        """Export *table* as CSV via an unbuffered cursor.

        ``SELECT INTO OUTFILE`` would be faster but needs the ``FILE`` privilege
        and writes on the *server*; cursor streaming is portable and works for
        any table the user can ``SELECT``.
        """
        pymysql = _load_pymysql()
        q = self._qualified(table)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connection
        total = 0
        with conn.cursor(pymysql.cursors.SSCursor) as cur:
            cur.execute(f"SELECT * FROM {q}")  # noqa: S608
            col_names = [d[0] for d in (cur.description or [])]
            with out_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f, lineterminator="\n")
                if header:
                    writer.writerow(col_names)
                for row in cur:
                    writer.writerow(["" if v is None else v for v in row])
                    total += 1
        return total


__all__ = ["MySQLConnector", "MySQLSession"]
