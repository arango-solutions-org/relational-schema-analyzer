"""Snowflake source connector (Phase 6, slices 1 + 3).

Slice 1 (``get_schema``) reads ``INFORMATION_SCHEMA`` and the
account-level views for declared foreign keys. Slice 3 (P6.3 + P6.4)
adds :meth:`SnowflakeConnector.open_session` returning a
:class:`SnowflakeSession` that implements the
:class:`r2g.connectors.session.SourceSession` Protocol: consistent-snapshot
transaction, cursor-based streaming, and a
``SELECT * FROM … ORDER BY NULL`` CSV export path.

Snowflake does not expose a user-facing ``REPEATABLE READ`` isolation
level, but any multi-statement transaction (``BEGIN`` … ``COMMIT``)
reads a consistent snapshot for its lifetime. The session opens exactly
one such transaction and reuses it across every count / stream / dump
call.

Connection string format
------------------------

R2G stores a single ``connection_string`` per source. For Snowflake we
accept the Snowflake SQLAlchemy URL shape (which is also the format
published by Snowflake themselves)::

    snowflake://<user>:<password>@<account>/<database>[/<schema>]
        ?warehouse=<wh>&role=<role>[&authenticator=<auth>]

- ``account`` is the Snowflake account identifier (e.g.
  ``xy12345.us-east-1``); the host portion of the URL.
- ``database`` is required — it identifies the ``INFORMATION_SCHEMA`` we
  introspect.
- ``schema`` is optional inside the URL. If absent the value passed as
  ``schema_name`` to the constructor wins (default ``PUBLIC``).
- Query parameters propagate straight through to
  ``snowflake.connector.connect``.
- Sign-in is exactly one of: a password in the URL, ``private_key_file``
  (key-pair, optionally with ``private_key_file_pwd``), or an
  ``authenticator`` with no password (e.g. ``externalbrowser`` for SSO).

Example::

    snowflake://svc_r2g:xxx@xy12345.us-east-1/ANALYTICS/CORE
        ?warehouse=ETL_WH&role=R2G_READER
    snowflake://alice@xy12345.us-east-1/ANALYTICS?authenticator=externalbrowser

Missing ``snowflake-connector-python``
--------------------------------------

The package is an *optional* dependency (``r2g-arango[snowflake]``). We never
import it at module import time; the first attempt to introspect a
Snowflake source raises :class:`ImportError` with a pip-install hint so
the UI / MCP server can surface a clean message.
"""

from __future__ import annotations

import csv
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, unquote, urlparse

from ..bitemporal import CATALOG
from ..bitemporal import to_iso as _iso
from ..log import get_logger
from ..redact import connection_error, secrets_in
from ..types import Column, ForeignKey, Schema, SourceProvenance, Table
from .base import mark_one_to_one, quote_identifier, unique_columns

logger = get_logger(__name__)


def _load_snowflake_connector() -> Any:
    """Import ``snowflake.connector`` lazily with a helpful error.

    Centralising the import means both :class:`SnowflakeConnector` and
    :class:`SnowflakeSession` surface the same message when the
    optional extra is not installed.
    """
    try:
        import snowflake.connector as _sf
    except ImportError as err:
        raise ImportError(
            "Snowflake support requires snowflake-connector-python. "
            "Install with: pip install 'relational-schema-analyzer[snowflake]'"
        ) from err
    return _sf


class SnowflakeConnector:
    """Snowflake source connector (introspection only)."""

    def __init__(self, connection_string: str, schema_name: str = "PUBLIC") -> None:
        self.connection_string = connection_string
        self.schema_name = schema_name.upper() if schema_name else "PUBLIC"
        self._connect_params = _parse_snowflake_url(connection_string)
        url_schema = self._connect_params.pop("_url_schema", None)
        if url_schema and (schema_name in (None, "", "public", "PUBLIC")):
            self.schema_name = url_schema.upper()
        self._database: str = self._connect_params.get("database", "")
        if not self._database:
            raise ValueError(
                "Snowflake connection string must include a database "
                "(snowflake://user:pass@account/<DATABASE>[/<SCHEMA>])"
            )
        self._database = self._database.upper()

    def open_session(self) -> "SnowflakeSession":
        """Open a Snowflake read session with consistent-snapshot semantics."""
        return SnowflakeSession(
            self.connection_string,
            database=self._database,
            schema_name=self.schema_name,
            connect_params=dict(self._connect_params),
        )

    def get_schema(self) -> Schema:
        """Introspect the Snowflake schema and return a populated :class:`Schema`."""
        snowflake = _load_snowflake_connector()

        connect_kwargs = dict(self._connect_params)
        connect_kwargs.setdefault("schema", self.schema_name)

        logger.info(
            "snowflake_connect",
            account=connect_kwargs.get("account"),
            database=self._database,
            schema=self.schema_name,
            warehouse=connect_kwargs.get("warehouse"),
            role=connect_kwargs.get("role"),
        )

        try:
            conn = snowflake.connect(**connect_kwargs)
        except Exception as err:
            raise connection_error(
                "Failed to connect to Snowflake", err, secrets_in(connect_kwargs, self.connection_string)
            ) from None

        try:
            return self._introspect(conn)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _introspect(self, conn: Any) -> Schema:
        schema = Schema(source=self._provenance(conn))
        cur = conn.cursor()
        try:
            cur.execute(
                f"""
                SELECT TABLE_NAME, TABLE_TYPE, COMMENT
                FROM {self._database}.INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')
                ORDER BY TABLE_NAME
                """,
                (self.schema_name,),
            )
            table_rows = cur.fetchall()
        finally:
            cur.close()

        valid_times = self._valid_times(conn)

        for name, table_type, comment in table_rows:
            table = self._process_table(
                conn, name, is_view=(table_type == "VIEW"), comment=comment
            )
            if name in valid_times:
                table.valid_from = valid_times[name]
                table.valid_time_source = CATALOG
            schema.tables[name] = table

        return schema

    def _valid_times(self, conn: Any) -> dict[str, str]:
        """``LAST_ALTERED`` per table, for bitemporal valid time (addendum §3).

        Issued separately from the table sweep, and best-effort, so a catalog that will not
        answer costs a date rather than the whole introspection.

        **This value is not trustworthy on its own.** Snowflake moves ``LAST_ALTERED`` on
        DML as well as DDL, so a table that was merely inserted into looks freshly altered.
        It is recorded as ``catalog`` and then gated by the fingerprint rule in
        ``bitemporal.stamp_bitemporal`` — which discards it when the structure did not
        actually change. Capturing it here and judging it there keeps the connector honest
        about what the catalog said and the resolver responsible for what to believe.
        """
        out: dict[str, str] = {}
        cur = conn.cursor()
        try:
            cur.execute(
                f"""
                SELECT TABLE_NAME, COALESCE(LAST_ALTERED, CREATED)
                FROM {self._database}.INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = %s
                """,
                (self.schema_name,),
            )
            for name, altered in cur.fetchall():
                # `_iso` returns None for placeholder dates (epoch zero), so guard on its
                # result rather than on the raw value — otherwise a placeholder would be
                # recorded as a `catalog` source with no date behind it.
                stamp = _iso(altered)
                if stamp:
                    out[name] = stamp
        except Exception as err:  # noqa: BLE001 - valid time is best-effort
            logger.warning("snowflake_valid_time_failed", error=str(err))
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass
        return out

    def _provenance(self, conn: Any) -> SourceProvenance:
        version: Optional[str] = None
        cur = conn.cursor()
        try:
            cur.execute("SELECT CURRENT_VERSION()")
            row = cur.fetchone()
            if row and row[0]:
                version = str(row[0])
        except Exception:  # noqa: BLE001 - provenance is best-effort
            version = None
        finally:
            cur.close()
        return SourceProvenance(
            dialect="snowflake",
            server_version=version,
            database=self._database,
            namespace=self.schema_name,
        )

    def _process_table(
        self, conn: Any, table_name: str, *, is_view: bool = False, comment: Any = None
    ) -> Table:
        columns = self._fetch_columns(conn, table_name)
        pks = self._fetch_primary_key(conn, table_name)
        fks = self._fetch_foreign_keys(conn, table_name)
        uniques = self._fetch_unique_constraints(conn, table_name)

        pk_set = set(pks)
        single_unique_cols = unique_columns(pks, uniques)
        for col in columns:
            col.is_primary_key = col.name in pk_set
            col.is_unique = col.name in single_unique_cols

        mark_one_to_one(fks, pks, uniques)

        return Table(
            name=table_name,
            columns=columns,
            primary_key=pks,
            foreign_keys=fks,
            is_view=is_view,
            comment=(str(comment) if comment else None),
            schema_name=self.schema_name,
            unique_constraints=[list(u) for u in uniques],
        )

    def _fetch_columns(self, conn: Any, table_name: str) -> list[Column]:
        cur = conn.cursor()
        try:
            cur.execute(
                f"""
                SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE,
                       COLUMN_DEFAULT, ORDINAL_POSITION, COMMENT
                FROM {self._database}.INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                (self.schema_name, table_name),
            )
            rows = cur.fetchall()
        finally:
            cur.close()
        columns: list[Column] = []
        for name, data_type, is_nullable, default, ordinal, comment in rows:
            columns.append(
                Column(
                    name=name,
                    data_type=(data_type or "").lower(),
                    is_nullable=(is_nullable == "YES"),
                    is_primary_key=False,
                    default=(str(default) if default is not None else None),
                    comment=(str(comment) if comment else None),
                    ordinal=(_safe_int(ordinal) - 1 if ordinal is not None else None),
                )
            )
        return columns

    def _qualified_name(self, table_name: str) -> str:
        """``"DB"."SCHEMA"."TABLE"`` for a ``SHOW … IN TABLE`` statement."""
        return ".".join(quote_identifier(n) for n in (self._database, self.schema_name, table_name))

    def _fetch_unique_constraints(self, conn: Any, table_name: str) -> list[list[str]]:
        """Return unique-key column sets via ``SHOW UNIQUE KEYS`` (best-effort).

        Snowflake unique constraints are declarative (not enforced). Grouped by
        constraint name and ordered by key sequence.
        """
        cur = conn.cursor()
        try:
            cur.execute(
                f"SHOW UNIQUE KEYS IN TABLE {self._qualified_name(table_name)}"
            )
            rows = cur.fetchall()
            columns = [d[0] for d in (cur.description or [])]
        except Exception:  # noqa: BLE001 - unique keys are optional
            return []
        finally:
            cur.close()
        if not rows:
            return []
        col_ix = {c.lower(): i for i, c in enumerate(columns)}
        name_ix = col_ix.get("constraint_name")
        col_name_ix = col_ix.get("column_name")
        seq_ix = col_ix.get("key_sequence")
        if col_name_ix is None:
            return []
        grouped: OrderedDict[str, list[tuple[int, str]]] = OrderedDict()
        for i, row in enumerate(rows):
            cname = row[name_ix] if name_ix is not None else str(i)
            seq = _safe_int(row[seq_ix]) if seq_ix is not None else i
            grouped.setdefault(cname, []).append((seq, row[col_name_ix]))
        return [[c for _, c in sorted(pairs)] for pairs in grouped.values()]

    def _fetch_primary_key(self, conn: Any, table_name: str) -> list[str]:
        """Return primary-key column names, ordered by key position.

        Snowflake exposes declared PKs via ``SHOW PRIMARY KEYS``. We use
        that rather than ``INFORMATION_SCHEMA`` because Snowflake's
        ``TABLE_CONSTRAINTS`` coverage has changed across releases.
        """
        cur = conn.cursor()
        try:
            cur.execute(
                f"SHOW PRIMARY KEYS IN TABLE {self._qualified_name(table_name)}"
            )
            rows = cur.fetchall()
            columns = [d[0] for d in (cur.description or [])]
        finally:
            cur.close()
        if not rows:
            return []
        col_ix = {c.lower(): i for i, c in enumerate(columns)}
        name_ix = col_ix.get("column_name")
        seq_ix = col_ix.get("key_sequence")
        if name_ix is None:
            return []
        if seq_ix is not None:
            ordered = sorted(rows, key=lambda r: _safe_int(r[seq_ix]))
        else:
            ordered = rows
        return [row[name_ix] for row in ordered]

    def _fetch_foreign_keys(self, conn: Any, table_name: str) -> list[ForeignKey]:
        """Return declared foreign keys via ``SHOW IMPORTED KEYS``.

        Reminder for callers: Snowflake *does not enforce* FK
        constraints (PRD P6.6). They are declarative only, and many
        Snowflake schemas have no FK metadata at all — in which case
        this returns an empty list. FK inference based on naming /
        value overlap is tracked separately as P6.6 and is out of scope
        for this slice.
        """
        cur = conn.cursor()
        try:
            cur.execute(
                f"SHOW IMPORTED KEYS IN TABLE {self._qualified_name(table_name)}"
            )
            rows = cur.fetchall()
            columns = [d[0] for d in (cur.description or [])]
        finally:
            cur.close()

        if not rows:
            return []

        col_ix = {c.lower(): i for i, c in enumerate(columns)}
        local_ix = col_ix.get("fk_column_name") or col_ix.get("pk_column_name_fk")
        foreign_table_ix = col_ix.get("pk_table_name")
        foreign_col_ix = col_ix.get("pk_column_name")
        constraint_ix = col_ix.get("fk_name")
        key_seq_ix = col_ix.get("key_sequence")
        if local_ix is None or foreign_table_ix is None or foreign_col_ix is None:
            logger.warning(
                "snowflake_fk_columns_unexpected",
                table=table_name,
                columns=columns,
            )
            return []

        def _ident(value: Any) -> Optional[str]:
            """Accept a plausible identifier only (guards against malformed rows,
            e.g. some emulators misalign columns and yield an int here)."""
            if not isinstance(value, str):
                return None
            v = value.strip()
            return v or None

        grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for row in rows:
            local = _ident(row[local_ix])
            foreign_table = _ident(row[foreign_table_ix])
            foreign_col = _ident(row[foreign_col_ix])
            if local is None or foreign_table is None or foreign_col is None:
                # Malformed / non-FK row (e.g. an emulator echoing the PK): skip.
                continue
            cname = _ident(row[constraint_ix]) if constraint_ix is not None else None
            key = cname or foreign_table
            bucket = grouped.setdefault(
                key,
                {"foreign_table": foreign_table, "constraint_name": cname, "_pairs": []},
            )
            seq = _safe_int(row[key_seq_ix]) if key_seq_ix is not None else len(bucket["_pairs"])
            bucket["_pairs"].append((seq, local, foreign_col))

        fks: list[ForeignKey] = []
        for bucket in grouped.values():
            pairs = sorted(bucket["_pairs"], key=lambda p: p[0])
            fks.append(
                ForeignKey(
                    columns=[p[1] for p in pairs],
                    foreign_table=bucket["foreign_table"],
                    foreign_columns=[p[2] for p in pairs],
                    constraint_name=bucket["constraint_name"],
                )
            )
        return fks


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _parse_snowflake_url(url: str) -> dict[str, Any]:
    """Parse a Snowflake SQLAlchemy-style URL into connector kwargs.

    Returns the kwargs that ``snowflake.connector.connect`` accepts,
    with one extra private key ``_url_schema`` if the URL path supplied
    a schema (which the connector wrapper consumes before handing the
    dict to the Snowflake driver).
    """

    if not url or "://" not in url:
        raise ValueError(
            "Snowflake connection string must look like "
            "snowflake://user:pass@account/DATABASE[/SCHEMA]?warehouse=...&role=..."
        )
    parsed = urlparse(url)
    if parsed.scheme.lower() != "snowflake":
        raise ValueError(
            f"Expected a snowflake:// connection string, got scheme '{parsed.scheme}'"
        )

    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    account = parsed.hostname or ""
    if not user or not account:
        raise ValueError(
            "Snowflake connection string must include user and account: "
            "snowflake://user:pass@account/DATABASE"
        )

    path_parts = [p for p in (parsed.path or "").split("/") if p]
    if not path_parts:
        raise ValueError(
            "Snowflake connection string is missing a database path component"
        )
    database = path_parts[0]
    url_schema = path_parts[1] if len(path_parts) > 1 else None

    query = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items() if v}

    # Key-pair authentication (ported from r2g, where it backs the Snowflake
    # Customer 360 demo). Exactly one method must be configured: a URL carrying
    # both a password and a key would otherwise authenticate with whichever the
    # driver prefers, silently.
    private_key_file = query.get("private_key_file", "")
    private_key_file_pwd = query.get("private_key_file_pwd", "")
    if password and private_key_file:
        raise ValueError(
            "Configure exactly one Snowflake authentication method: password or private_key_file"
        )
    if private_key_file_pwd and not private_key_file:
        raise ValueError("private_key_file_pwd requires private_key_file")
    # An ``authenticator`` (externalbrowser SSO, OAuth, ...) is a sign-in method
    # of its own and carries no password or key; only a URL with no method at
    # all is refused here.
    if not password and not private_key_file and not query.get("authenticator"):
        raise ValueError(
            "Snowflake authentication requires a password, private_key_file or authenticator"
        )
    if private_key_file and not Path(private_key_file).is_file():
        raise ValueError("private_key_file must name an existing file")

    kwargs: dict[str, Any] = {
        "user": user,
        "account": account,
        "database": database,
    }
    if private_key_file:
        kwargs["private_key_file"] = private_key_file
        if private_key_file_pwd:
            kwargs["private_key_file_pwd"] = private_key_file_pwd
    elif password:
        kwargs["password"] = password
    for key in ("warehouse", "role", "authenticator", "application"):
        if key in query:
            kwargs[key] = query[key]
    if private_key_file:
        # A caller-supplied authenticator cannot weaken key-pair semantics.
        kwargs["authenticator"] = "SNOWFLAKE_JWT"
    if parsed.port:
        kwargs["port"] = parsed.port
    if url_schema:
        kwargs["_url_schema"] = url_schema
    return kwargs


class SnowflakeSession:
    """Bulk-read session for :class:`SnowflakeConnector`.

    Opens a single ``snowflake.connector`` connection, starts a
    transaction (``BEGIN``) so that every count / stream / dump during
    the session sees the same committed snapshot, and commits on
    :meth:`close`. A rollback is issued instead if the caller passes
    ``abort=True`` — kept as a hook even though nothing today calls it.

    Snowflake identifiers are case-sensitive when quoted; we always
    quote both the schema and table names with double-quotes and pass
    the *exact* identifier stored on the :class:`Schema` (which in
    practice is upper-case because that's what ``INFORMATION_SCHEMA``
    stores).
    """

    def __init__(
        self,
        connection_string: str,
        *,
        database: str,
        schema_name: str,
        connect_params: dict[str, Any],
    ) -> None:
        self.connection_string = connection_string
        self._database = database
        self.schema_name = schema_name
        self._connect_params = dict(connect_params)
        self._connect_params.setdefault("schema", schema_name)
        self._conn: Any = None
        self._tx_open = False

    @property
    def connection(self) -> Any:
        if self._conn is None:
            snowflake = _load_snowflake_connector()
            try:
                self._conn = snowflake.connect(**self._connect_params)
            except Exception as err:
                raise connection_error(
                    "Failed to connect to Snowflake", err, secrets_in(self._connect_params, self.connection_string)
                ) from None
            try:
                cur = self._conn.cursor()
                try:
                    cur.execute("BEGIN")
                finally:
                    cur.close()
                self._tx_open = True
            except Exception as err:  # noqa: BLE001
                logger.warning("snowflake_begin_failed", error=str(err))
        return self._conn

    def close(self, *, abort: bool = False) -> None:
        if self._conn is None:
            return
        try:
            if self._tx_open:
                cur = self._conn.cursor()
                try:
                    cur.execute("ROLLBACK" if abort else "COMMIT")
                except Exception as err:  # noqa: BLE001
                    logger.warning(
                        "snowflake_commit_failed",
                        error=str(err),
                        aborted=abort,
                    )
                finally:
                    cur.close()
            self._tx_open = False
        finally:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def __enter__(self) -> "SnowflakeSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _qualified(self, table: str) -> str:
        return ".".join(quote_identifier(n) for n in (self._database, self.schema_name, table))

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
                    f"SELECT COUNT(*) FROM {q} WHERE {quote_identifier(since_column)} >= %s",  # noqa: S608
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
        """Stream rows via Snowflake's cursor in ``batch_size`` chunks.

        We use ``cursor.fetchmany(batch_size)`` rather than iterating
        the cursor directly — the Snowflake driver's ``__iter__``
        fetches one row at a time which is an order of magnitude slower
        on wide tables.
        """
        q = self._qualified(table)
        conn = self.connection
        cur = conn.cursor()
        try:
            if since_column and since_value is not None:
                cur.execute(
                    f"SELECT * FROM {q} WHERE {quote_identifier(since_column)} >= %s",  # noqa: S608
                    (since_value,),
                )
            else:
                cur.execute(f"SELECT * FROM {q}")  # noqa: S608
            columns = [d[0] for d in (cur.description or [])]
            fetch = max(1, batch_size)
            while True:
                rows = cur.fetchmany(fetch)
                if not rows:
                    break
                for row in rows:
                    yield dict(zip(columns, row))
        finally:
            cur.close()

    def dump_table_to_csv(
        self,
        table: str,
        out_path: Path,
        *,
        header: bool = True,
    ) -> int:
        """Export *table* as CSV through the Snowflake cursor.

        A future iteration could use ``COPY INTO @<stage>`` for larger
        tables, but that requires the user to have provisioned an
        accessible stage. Cursor streaming is portable, needs no
        Snowflake-side provisioning, and works for any table the user
        can ``SELECT`` from.
        """
        q = self._qualified(table)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connection
        cur = conn.cursor()
        total = 0
        try:
            cur.execute(f"SELECT * FROM {q}")  # noqa: S608
            columns = [d[0] for d in (cur.description or [])]
            with out_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f, lineterminator="\n")
                if header:
                    writer.writerow(columns)
                while True:
                    rows = cur.fetchmany(10_000)
                    if not rows:
                        break
                    for row in rows:
                        writer.writerow(
                            ["" if v is None else v for v in row]
                        )
                        total += 1
        finally:
            cur.close()
        return total
