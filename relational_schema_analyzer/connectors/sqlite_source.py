"""SQLite source connector (introspection only).

SQLite ships with Python, so this connector needs no optional dependency. It reads
the catalog through SQLite's table-valued pragma functions (``pragma_table_xinfo``,
``pragma_index_list``, ``pragma_index_info``, ``pragma_foreign_key_list``), which
take the table name as a bound parameter, so no identifier is ever spliced into SQL.

``connection_string`` is a path to the database file; a ``sqlite:///`` URL prefix
is accepted too. The file is opened read-only.

What SQLite does not record, this connector cannot report:

* **Comments.** SQLite has none; every ``comment`` is ``None``.
* **Foreign-key names.** The pragma exposes none, so each key is named
  ``fk_<table>_<n>`` after its position in the table's key list.

Foreign keys are resolved case-insensitively, as SQLite itself resolves names: a
``REFERENCES Users(ID)`` against a table declared as ``users(id)`` is the same key.
A reference to a table or column that does not exist is skipped and logged, since
SQLite accepts such a declaration and simply never enforces it. A reference with no
column list points at the target's primary key, as in SQLite.

A unique index counts as a uniqueness guarantee when it covers whole columns and is
not partial; a partial unique index guarantees nothing about the rows it excludes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from ..log import get_logger
from ..types import ForeignKey, Schema, SourceProvenance
from .base import build_table

logger = get_logger(__name__)

# A SQLite file has one schema, ``main``; fold the cross-dialect defaults to it.
_DEFAULT_SCHEMA_SENTINELS = frozenset({None, "", "main", "public", "PUBLIC"})
_URL_PREFIXES = ("sqlite:///", "sqlite://")


def sqlite_path(connection_string: str) -> str:
    """The file path in a connection string, without any ``sqlite://`` prefix."""
    for prefix in _URL_PREFIXES:
        if connection_string.startswith(prefix):
            return connection_string[len(prefix) :]
    return connection_string


def connect_read_only(connection_string: str) -> sqlite3.Connection:
    """Open a SQLite file read-only; a missing file is an error, never a new database."""
    path = Path(sqlite_path(connection_string))
    if not path.is_file():
        raise RuntimeError(f"No SQLite database file at {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    # Text that is not valid UTF-8 is common in real files; decode it rather than fail.
    conn.text_factory = lambda b: b.decode(errors="replace")
    return conn


class SqliteConnector:
    """Introspect a SQLite database file into a :class:`Schema`."""

    def __init__(self, connection_string: str, schema_name: str = "main") -> None:
        if schema_name not in _DEFAULT_SCHEMA_SENTINELS:
            raise ValueError(
                f"A SQLite file has one schema, 'main'; got schema_name={schema_name!r}."
            )
        self.connection_string = connection_string
        self.schema_name = "main"
        self.database = Path(sqlite_path(connection_string)).stem or connection_string

    def get_schema(self) -> Schema:
        try:
            conn = connect_read_only(self.connection_string)
        except (RuntimeError, sqlite3.Error) as err:
            raise RuntimeError(f"Failed to open SQLite database: {err}") from err
        try:
            return self._introspect(conn)
        except sqlite3.DatabaseError as err:
            # SQLite opens lazily: a file that is not a database fails on first read.
            raise RuntimeError(f"Failed to read SQLite database: {err}") from err
        finally:
            conn.close()

    def _query(self, conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
        return conn.execute(sql, params).fetchall()

    def _introspect(self, conn: sqlite3.Connection) -> Schema:
        schema = Schema(source=self._provenance())
        rows = self._query(
            conn,
            "SELECT name, type FROM sqlite_master "
            # `_` is a LIKE wildcard: unescaped, this also hid a user table `sqlitedata`.
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
            "ORDER BY name",
        )
        columns: dict[str, list[dict[str, Any]]] = {}
        for name, _ in rows:
            try:
                columns[name] = self._columns(conn, name)
            except sqlite3.Error as err:
                # A virtual table whose module is not loaded (SpatiaLite, say) cannot be
                # read; skip that one table rather than fail the whole file.
                logger.warning("sqlite_table_unreadable", table=name, error=str(err))
        rows = [(name, kind) for name, kind in rows if name in columns]
        names = [name for name, _ in rows]
        by_lower = {name.lower(): name for name in names}
        pks = {name: self._primary_key(columns[name]) for name in names}
        for name, kind in rows:
            schema.tables[name] = build_table(
                name=name,
                schema_name=self.schema_name,
                is_view=(kind == "view"),
                columns=columns[name],
                primary_key=pks[name],
                unique_sets=self._unique_sets(conn, name) if kind == "table" else [],
                fks=(
                    self._foreign_keys(conn, name, by_lower, columns, pks)
                    if kind == "table"
                    else []
                ),
            )
        return schema

    def _provenance(self) -> SourceProvenance:
        return SourceProvenance(
            dialect="sqlite",
            server_version=sqlite3.sqlite_version,
            database=self.database,
            namespace=self.schema_name,
        )

    def _columns(self, conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        rows = self._query(
            conn,
            # table_xinfo, because table_info hides generated columns. hidden=1 is a
            # virtual table's hidden column; 2 and 3 are generated (virtual, stored).
            'SELECT cid, name, type, "notnull", dflt_value, pk FROM pragma_table_xinfo(?) '
            "WHERE hidden IN (0, 2, 3) ORDER BY cid",
            (table,),
        )
        return [
            {
                "name": name,
                "data_type": str(data_type or "").lower(),
                # A key column is reported not nullable, as other engines enforce.
                # SQLite itself lets a non-INTEGER key column hold NULL unless it is
                # declared NOT NULL; the model treats the key as the key either way.
                "is_nullable": not notnull and not pk,
                "default": (str(default) if default is not None else None),
                "ordinal": int(cid),
                "pk_position": int(pk or 0),
            }
            for cid, name, data_type, notnull, default, pk in rows
        ]

    @staticmethod
    def _primary_key(columns: list[dict[str, Any]]) -> list[str]:
        keyed = sorted((c for c in columns if c["pk_position"]), key=lambda c: c["pk_position"])
        return [c["name"] for c in keyed]

    def _unique_sets(self, conn: sqlite3.Connection, table: str) -> list[list[str]]:
        indexes = self._query(
            conn,
            'SELECT name, "unique", origin, partial FROM pragma_index_list(?) ORDER BY seq',
            (table,),
        )
        sets: list[list[str]] = []
        for index_name, unique, origin, partial in indexes:
            if not unique or partial or origin == "pk":
                continue
            cols = self._query(
                conn,
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                (index_name,),
            )
            names = [c for (c,) in cols]
            if names and all(names) and names not in sets:  # None = an expression column
                sets.append(names)
        return sets

    def _foreign_keys(
        self,
        conn: sqlite3.Connection,
        table: str,
        by_lower: dict[str, str],
        columns: dict[str, list[dict[str, Any]]],
        pks: dict[str, list[str]],
    ) -> list[ForeignKey]:
        rows = self._query(
            conn,
            'SELECT id, seq, "table", "from", "to" FROM pragma_foreign_key_list(?) '
            "ORDER BY id, seq",
            (table,),
        )
        grouped: dict[int, list[tuple[str, str, Optional[str]]]] = {}
        for fk_id, _seq, ref_table, from_col, to_col in rows:
            grouped.setdefault(int(fk_id), []).append((ref_table, from_col, to_col))

        fks: list[ForeignKey] = []
        local_cols = {c["name"].lower(): c["name"] for c in columns[table]}
        for n, parts in enumerate(grouped.values()):
            ref_table = by_lower.get(str(parts[0][0]).lower())
            if ref_table is None:
                logger.warning("sqlite_fk_target_missing", table=table, references=parts[0][0])
                continue
            ref_cols_known = {c["name"].lower(): c["name"] for c in columns[ref_table]}
            local = [local_cols.get(str(p[1]).lower()) for p in parts]
            if any(p[2] is None for p in parts):
                foreign: list[Optional[str]] = list(pks[ref_table])
            else:
                foreign = [ref_cols_known.get(str(p[2]).lower()) for p in parts]
            if not all(local) or not all(foreign) or len(local) != len(foreign):
                logger.warning(
                    "sqlite_fk_columns_unresolved",
                    table=table,
                    references=ref_table,
                    columns=[p[1] for p in parts],
                )
                continue
            fks.append(
                ForeignKey(
                    columns=[c for c in local if c],
                    foreign_table=ref_table,
                    foreign_columns=[c for c in foreign if c],
                    constraint_name=f"fk_{table}_{n}",
                )
            )
        return fks
