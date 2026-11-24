"""DuckDB source connector (introspection only).

DuckDB is an embeddable, server-less analytical database with a standard
``information_schema``. That makes it a lightweight, always-on engine for testing
the generic catalog-introspection path, and a faithful reference for the same
``table_constraints`` / ``key_column_usage`` / ``referential_constraints`` pattern
used by the other RDBMS connectors.

``connection_string`` is a path to a ``.duckdb`` file. Introspection opens the file
read-only. Comments live in DuckDB's ``duckdb_tables()`` / ``duckdb_columns()``
catalog functions rather than ``information_schema``, so both are consulted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..log import get_logger
from ..redact import connection_error, secrets_in
from ..types import ForeignKey, Schema, SourceProvenance
from .base import build_table, fold_constraints

logger = get_logger(__name__)

# DuckDB's default schema is ``main``; fold the cross-dialect ``public`` default to it.
_DEFAULT_SCHEMA_SENTINELS = frozenset({None, "", "public", "PUBLIC"})


def _load_duckdb() -> Any:
    try:
        import duckdb
    except ImportError as err:
        raise ImportError(
            "DuckDB support requires duckdb. "
            "Install with: pip install 'relational-schema-analyzer[duckdb]'"
        ) from err
    return duckdb


class DuckDbConnector:
    """Introspect a DuckDB database file into a :class:`Schema`."""

    def __init__(self, connection_string: str, schema_name: str = "main") -> None:
        self.connection_string = connection_string
        self.schema_name = (
            "main" if schema_name in _DEFAULT_SCHEMA_SENTINELS else schema_name
        )
        self.database = Path(connection_string).stem or connection_string

    def get_schema(self) -> Schema:
        duckdb = _load_duckdb()
        try:
            conn = duckdb.connect(self.connection_string, read_only=True)
        except Exception as err:
            raise connection_error(
                "Failed to open DuckDB database", err, secrets_in(self.connection_string)
            ) from None
        try:
            return self._introspect(conn)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _query(self, conn: Any, sql: str, params: tuple = ()) -> list[tuple]:
        rel = conn.execute(sql, params) if params else conn.execute(sql)
        return rel.fetchall()

    def _introspect(self, conn: Any) -> Schema:
        schema = Schema(source=self._provenance(conn))

        columns_by_table = self._columns_by_table(conn)
        col_comments = self._column_comments(conn)
        table_comments = self._table_comments(conn)
        pks, uniques, fks = self._constraints(conn)

        table_rows = self._query(
            conn,
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = ? ORDER BY table_name",
            (self.schema_name,),
        )
        for table_name, table_type in table_rows:
            schema.tables[table_name] = build_table(
                name=table_name,
                schema_name=self.schema_name,
                is_view=(str(table_type).upper() == "VIEW"),
                comment=table_comments.get(table_name),
                columns=[
                    {**c, "comment": col_comments.get((table_name, c["name"]))}
                    for c in columns_by_table.get(table_name, [])
                ],
                primary_key=pks.get(table_name, []),
                unique_sets=uniques.get(table_name, []),
                fks=fks.get(table_name, []),
            )
        return schema

    def _provenance(self, conn: Any) -> SourceProvenance:
        version: Optional[str] = None
        try:
            row = self._query(conn, "SELECT version()")
            if row and row[0] and row[0][0]:
                version = str(row[0][0])
        except Exception:  # noqa: BLE001 - provenance is best-effort
            version = None
        return SourceProvenance(
            dialect="duckdb",
            server_version=version,
            database=self.database,
            namespace=self.schema_name,
        )

    def _columns_by_table(self, conn: Any) -> dict[str, list[dict[str, Any]]]:
        rows = self._query(
            conn,
            "SELECT table_name, column_name, data_type, is_nullable, "
            "column_default, ordinal_position "
            "FROM information_schema.columns WHERE table_schema = ? "
            "ORDER BY table_name, ordinal_position",
            (self.schema_name,),
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for table_name, name, data_type, is_nullable, default, ordinal in rows:
            out.setdefault(table_name, []).append(
                {
                    "name": name,
                    "data_type": str(data_type or "").lower(),
                    "is_nullable": (str(is_nullable).upper() == "YES"),
                    "default": (str(default) if default is not None else None),
                    "ordinal": (int(ordinal) - 1 if ordinal is not None else None),
                }
            )
        return out

    def _column_comments(self, conn: Any) -> dict[tuple[str, str], str]:
        out: dict[tuple[str, str], str] = {}
        try:
            rows = self._query(
                conn,
                "SELECT table_name, column_name, comment FROM duckdb_columns() "
                "WHERE schema_name = ?",
                (self.schema_name,),
            )
        except Exception:  # noqa: BLE001 - comments are best-effort
            return out
        for table_name, column_name, comment in rows:
            if comment:
                out[(table_name, column_name)] = str(comment)
        return out

    def _table_comments(self, conn: Any) -> dict[str, str]:
        out: dict[str, str] = {}
        for fn in ("duckdb_tables()", "duckdb_views()"):
            try:
                rows = self._query(
                    conn,
                    f"SELECT table_name, comment FROM {fn} WHERE schema_name = ?",
                    (self.schema_name,),
                )
            except Exception:  # noqa: BLE001
                continue
            for table_name, comment in rows:
                if comment:
                    out[table_name] = str(comment)
        return out

    def _constraints(
        self, conn: Any
    ) -> tuple[dict[str, list[str]], dict[str, list[list[str]]], dict[str, list[ForeignKey]]]:
        """Resolve PK / UNIQUE / FK per table via standard information_schema views."""
        tc = self._query(
            conn,
            "SELECT constraint_name, constraint_type, table_name "
            "FROM information_schema.table_constraints WHERE table_schema = ?",
            (self.schema_name,),
        )
        kcu = self._query(
            conn,
            "SELECT constraint_name, column_name, ordinal_position "
            "FROM information_schema.key_column_usage WHERE table_schema = ? "
            "ORDER BY constraint_name, ordinal_position",
            (self.schema_name,),
        )
        try:
            rc = self._query(
                conn,
                "SELECT constraint_name, unique_constraint_name "
                "FROM information_schema.referential_constraints WHERE constraint_schema = ?",
                (self.schema_name,),
            )
        except Exception:  # noqa: BLE001
            rc = []

        return fold_constraints(tc, kcu, rc)
