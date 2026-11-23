"""Databricks (Unity Catalog) source connector — introspection only.

Databricks Unity Catalog exposes the standard ANSI ``information_schema``
(``tables`` / ``columns`` with a ``comment`` column, ``table_constraints`` /
``key_column_usage`` / ``referential_constraints`` for GA primary/foreign/unique
keys), so this is the same catalog-introspection pattern as the DuckDB / Postgres
connectors — with Databricks' three-level ``catalog.schema.table`` namespace.

Unlike an RDBMS, Unity Catalog **does not enforce** primary/foreign/unique keys —
they are informational metadata that enables query optimization, and nothing
validates the referenced rows exist. Every FK is therefore emitted with
``enforced=False`` so the baseline treats it as a hint rather than proof (and
still runs name-based inference for the columns UC never declared). This is a
dialect-level fact, not something to read per constraint: UC's
``information_schema.table_constraints.ENFORCED`` is documented as always ``'NO'``
("reserved for future use"), so it carries no signal. CHECK constraints *are*
enforced in Databricks and would be trustworthy — they are not read yet.

Connection string (SQLAlchemy-ish; token as the password, http_path as the path)::

    databricks://:<access_token>@<server_hostname>/sql/1.0/warehouses/<id>?catalog=main&schema=default

There is no in-process emulator for Databricks (the driver speaks to a live SQL
warehouse), so the assembly is covered by mock-cursor tests; a live workspace is
opt-in via ``RSA_DATABRICKS_DSN``.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from ..bitemporal import CATALOG
from ..bitemporal import to_iso as _iso
from ..log import get_logger
from ..types import ForeignKey, Schema, SourceProvenance
from .base import build_table, fold_constraints

logger = get_logger(__name__)

_DEFAULT_SCHEMA_SENTINELS = frozenset({None, "", "public", "PUBLIC", "default"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _load_databricks() -> Any:
    try:
        from databricks import sql as dbsql
    except ImportError as err:
        raise ImportError(
            "Databricks support requires databricks-sql-connector. "
            "Install with: pip install 'relational-schema-analyzer[databricks]'"
        ) from err
    return dbsql


def _safe_identifier(name: str, kind: str) -> str:
    if not _IDENTIFIER_RE.match(name or ""):
        raise ValueError(f"Unsafe Databricks {kind} name: {name!r}")
    return name


def _parse_databricks_url(url: str) -> dict[str, Any]:
    """Parse ``databricks://:<token>@<host>/<http_path>?catalog=&schema=`` into parts."""
    if not url or not url.startswith("databricks://"):
        raise ValueError(
            "Databricks connection string must look like "
            "databricks://:<token>@<host>/sql/1.0/warehouses/<id>?catalog=..&schema=.."
        )
    parsed = urlparse(url)
    host = parsed.hostname
    http_path = parsed.path or ""
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    token = unquote(parsed.password or "") or query.get("access_token", "")
    http_path = query.get("http_path", http_path)
    if not host or not http_path or not token:
        raise ValueError(
            "Databricks connection string needs host, http_path, and an access token"
        )
    return {
        "server_hostname": host,
        "http_path": http_path,
        "access_token": token,
        "catalog": query.get("catalog", "main"),
        "schema": query.get("schema", "default"),
    }


class DatabricksConnector:
    """Introspect a Databricks Unity Catalog schema into a :class:`Schema`."""

    def __init__(self, connection_string: str, schema_name: str = "default") -> None:
        self.connection_string = connection_string
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

    def get_schema(self) -> Schema:
        dbsql = _load_databricks()
        try:
            conn = dbsql.connect(
                catalog=self.catalog, schema=self.schema_name, **self._connect_params
            )
        except Exception as err:
            raise RuntimeError(f"Failed to connect to Databricks: {err}") from err
        try:
            cur = conn.cursor()
            try:
                return self._introspect(cur)
            finally:
                cur.close()
        finally:
            conn.close()

    def _ident(self) -> str:
        return _safe_identifier(self.catalog, "catalog")

    def _rows(self, cur: Any, sql: str) -> list[tuple]:
        cur.execute(sql)
        return cur.fetchall()

    def _introspect(self, cur: Any) -> Schema:
        catalog = self._ident()
        schema = _safe_identifier(self.schema_name, "schema")
        info = f"{catalog}.information_schema"

        result = Schema(source=self._provenance(cur))

        columns_by_table = self._columns_by_table(cur, info, schema)
        pks, uniques, fks = self._constraints(cur, info, schema)
        valid_times = self._valid_times(cur, info, schema)

        for name, table_type, comment in self._rows(
            cur,
            f"SELECT table_name, table_type, comment FROM {info}.tables "
            f"WHERE table_schema = '{schema}' ORDER BY table_name",
        ):
            # Unity Catalog's table_type vocabulary is VIEW, FOREIGN, MANAGED,
            # STREAMING_TABLE, MATERIALIZED_VIEW, EXTERNAL, MANAGED_SHALLOW_CLONE
            # and EXTERNAL_SHALLOW_CLONE — note it never emits ANSI's "BASE
            # TABLE". Only the two view kinds are query-defined; FOREIGN
            # (federated) and the clones are ordinary tables.
            table = build_table(
                name=name,
                schema_name=self.schema_name,
                is_view=(str(table_type).upper() in ("VIEW", "MATERIALIZED_VIEW")),
                comment=(str(comment) if comment else None),
                columns=columns_by_table.get(name, []),
                primary_key=pks.get(name, []),
                unique_sets=uniques.get(name, []),
                fks=fks.get(name, []),
            )
            if name in valid_times:
                table.valid_from = valid_times[name]
                table.valid_time_source = CATALOG
            result.tables[name] = table
        return result

    def _valid_times(self, cur: Any, info: str, schema: str) -> dict[str, str]:
        """``last_altered`` per table, for bitemporal valid time (addendum §3).

        Databricks documents this as DDL-scoped, so it is the signal the addendum wants.
        It is still routed through the §2.3 fingerprint rule defensively: "documented as"
        and "observed to be" are different claims, and the rule costs nothing when the
        catalog is telling the truth.

        Delta's ``DESCRIBE HISTORY`` would give per-operation dating and could distinguish
        DDL from DML directly, but it needs privileges beyond the read-only role RSA
        connects as — deliberately deferred (addendum §5, open question 2).
        """
        out: dict[str, str] = {}
        try:
            rows = self._rows(
                cur,
                "SELECT table_name, COALESCE(last_altered, created) "
                f"FROM {info}.tables WHERE table_schema = '{schema}'",
            )
            for name, altered in rows:
                stamp = _iso(altered)
                if stamp:
                    out[name] = stamp
        except Exception as err:  # noqa: BLE001 - valid time is best-effort
            logger.warning("databricks_valid_time_failed", error=str(err))
        return out

    def _provenance(self, cur: Any) -> SourceProvenance:
        version: Optional[str] = None
        try:
            rows = self._rows(cur, "SELECT current_version() AS v")
            if rows and rows[0] and rows[0][0]:
                version = str(rows[0][0])
        except Exception:  # noqa: BLE001 - provenance is best-effort
            version = None
        return SourceProvenance(
            dialect="databricks",
            server_version=version,
            database=self.catalog,
            namespace=self.schema_name,
        )

    def _columns_by_table(
        self, cur: Any, info: str, schema: str
    ) -> dict[str, list[dict[str, Any]]]:
        # ``full_data_type`` is "the data type as specified in the column
        # definition"; ``data_type`` is only "the simple data type name of the
        # column, or STRUCT, or ARRAY". Selecting the latter silently drops
        # precision and scale (``decimal(10,2)`` → ``decimal``) and every element
        # / field type (``array<string>`` → ``array``), so prefer the full form.
        # The type map normalizes both to the same category, so this only ever
        # adds detail. Older catalogs that predate the column fall back.
        select = (
            "SELECT table_name, column_name, {type_expr}, is_nullable, column_default, "
            f"ordinal_position, comment FROM {info}.columns "
            f"WHERE table_schema = '{schema}' ORDER BY table_name, ordinal_position"
        )
        try:
            rows = self._rows(cur, select.format(type_expr="full_data_type"))
        except Exception:  # noqa: BLE001 - fall back when only data_type exists
            rows = self._rows(cur, select.format(type_expr="data_type"))
        out: dict[str, list[dict[str, Any]]] = {}
        for table_name, name, data_type, is_nullable, default, ordinal, comment in rows:
            out.setdefault(table_name, []).append(
                {
                    "name": name,
                    "data_type": str(data_type or "").lower(),
                    "is_nullable": (str(is_nullable).upper() == "YES"),
                    "default": (str(default) if default is not None else None),
                    "ordinal": (int(ordinal) - 1 if ordinal is not None else None),
                    "comment": (str(comment) if comment else None),
                }
            )
        return out

    def _constraints(
        self, cur: Any, info: str, schema: str
    ) -> tuple[dict[str, list[str]], dict[str, list[list[str]]], dict[str, list[ForeignKey]]]:
        tc = self._rows(
            cur,
            "SELECT constraint_name, constraint_type, table_name "
            f"FROM {info}.table_constraints WHERE table_schema = '{schema}'",
        )
        kcu = self._rows(
            cur,
            "SELECT constraint_name, column_name, ordinal_position "
            f"FROM {info}.key_column_usage WHERE table_schema = '{schema}' "
            "ORDER BY constraint_name, ordinal_position",
        )
        try:
            rc = self._rows(
                cur,
                "SELECT constraint_name, unique_constraint_name "
                f"FROM {info}.referential_constraints WHERE constraint_schema = '{schema}'",
            )
        except Exception:  # noqa: BLE001
            rc = []

        return fold_constraints(tc, kcu, rc, enforced=False)
