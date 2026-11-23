"""Abstraction over relational source connectors.

R2G started as a PostgreSQL-only tool; `PostgresConnector` was the single
concrete implementation with a hand-rolled constructor signature. PRD
Phase 6 (Snowflake) calls out an explicit "source abstraction layer"
(P6.5) so the schema-reader, dump, and streaming paths can accept other
relational sources behind a common interface.

This module intentionally stays minimal:

- `SourceConnector` is a structural `Protocol` describing the
  operations we actually perform on a connector today: introspect
  `connection_string` / `schema_name` attributes, return a populated
  `Schema` from :meth:`get_schema`, and (Phase 6 slice 3) open a
  :class:`SourceSession` for bulk reads via :meth:`open_session`.
  `PostgresConnector` and `SnowflakeConnector` both satisfy the
  protocol.
- `SUPPORTED_SOURCE_TYPES` is the registry of source types the catalog,
  UI, and MCP server are allowed to create. Adding a new type is a
  single edit here plus a concrete implementation.
- `create_source_connector` is the thin factory the UI / MCP /
  `source snapshot` / `stream` / `source dump` commands call. It
  lazy-imports the concrete class so optional dependencies (e.g.
  ``snowflake-connector-python``) are only loaded when the user
  actually has a source of that type.

As of Phase 6 slice 3 the streaming pipeline and dump-tables CLI both
consume connectors through this protocol; the PG-only fast paths live
inside ``PostgresSession`` and the Snowflake equivalents inside
``SnowflakeSession``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any, Protocol, TypeGuard, runtime_checkable

from ..quoting import quote_ansi
from .session import SourceSession
from ..types import Column, ForeignKey, Schema, Table


def expand_env_vars(connection_string: str) -> str:
    """Expand ``$VAR`` / ``${VAR}`` references in a connection string.

    r2g's convention is to keep credentials in environment variables (or
    ``r2g secrets``) rather than store them in the catalog — both as a whole
    string (``$PG_CONN``) and inline within a DSN
    (``postgresql://$DB_USER:$DB_PASSWORD@host/db``, as produced by
    ``r2g catalog import-source``). Expansion is applied centrally so every
    path (CLI, UI, MCP) behaves the same. Unknown variables are left intact,
    and strings without ``$`` are returned unchanged (so literal DSNs — the
    common case — are untouched).
    """
    if connection_string and "$" in connection_string:
        return os.path.expandvars(connection_string)
    return connection_string


@runtime_checkable
class SourceConnector(Protocol):
    """What *every* source can do: describe itself.

    Deliberately narrow. Four of the nine connectors cannot read rows at all, and
    two of those four never will: a dbt ``manifest.json`` and an OSI ``*.osi.yaml``
    are **data catalogs** (DESIGN §9.3.1 — "read-only", "no live system"). They say
    a table called ``customers`` exists with these columns and this primary key;
    they contain no customer rows. There is nothing for them to stream.

    Promising bulk reads here made :func:`create_source_connector` return a type
    that lies about four of its nine results: a caller would read the declaration,
    call ``open_session``, and get :class:`AttributeError` — with their own type
    checker having approved the call, because this said it was safe.

    A source that *does* hold rows satisfies :class:`BulkReadableSource` as well.
    """

    connection_string: str
    schema_name: str

    def get_schema(self) -> Schema:
        """Introspect the upstream source and return a populated :class:`Schema`."""
        ...


@runtime_checkable
class BulkReadableSource(SourceConnector, Protocol):
    """A source that holds rows and can stream them, not merely describe them.

    Satisfied today by Postgres, MySQL, SQL Server, Snowflake and CSV. DuckDB and
    Databricks are real databases that *could* satisfy it and do not yet — a gap,
    unlike the catalog sources, for which the question does not arise.

    Ask with :func:`supports_bulk_read` rather than ``hasattr``: it narrows the type
    for the checker, so the branch that streams is verified rather than hoped for.
    """

    def open_session(self) -> SourceSession:
        """Open a bulk-read session with consistent-snapshot semantics.

        Each call returns a fresh session; callers are expected to call
        :meth:`SourceSession.close` when done. Parallel workers each
        obtain their own session so they do not contend for a single
        cursor.
        """
        ...


def supports_bulk_read(connector: SourceConnector) -> TypeGuard[BulkReadableSource]:
    """Can this source's rows be read, or does it only describe a schema?

    ``TypeGuard`` so that inside a ``if supports_bulk_read(c):`` branch the checker
    knows ``c.open_session()`` exists — the guarantee the old single protocol
    claimed for every source and could not keep for four of them.

    Asks about ``open_session`` directly rather than ``isinstance(c,
    BulkReadableSource)``. A ``runtime_checkable`` protocol carrying data members
    also requires ``connection_string`` and ``schema_name`` to be *present on the
    instance*, so a half-constructed connector, or one exposing those some other
    way, would answer "cannot stream" when it can. The only thing this function is
    asked is whether the rows can be read.
    """
    return callable(getattr(connector, "open_session", None))


SUPPORTED_SOURCE_TYPES: tuple[str, ...] = (
    "postgresql",
    "mysql",
    "sqlserver",
    "snowflake",
    "duckdb",
    "sqlite",
    "databricks",
    "dbt",
    "osi",
    "csv",
)

# Aliases that all mean PostgreSQL. Missing/empty source types default to PG.
_PG_ALIASES: frozenset[str] = frozenset({"postgresql", "postgres", "pg"})

# Aliases that all mean MySQL (MariaDB is wire- and introspection-compatible).
_MYSQL_ALIASES: frozenset[str] = frozenset({"mysql", "mariadb"})

# Aliases that all mean Microsoft SQL Server.
_MSSQL_ALIASES: frozenset[str] = frozenset({"sqlserver", "mssql", "sql_server"})
_SQLITE_ALIASES: frozenset[str] = frozenset({"sqlite", "sqlite3"})


# The ANSI rule Postgres and Snowflake follow: ``"name"`` with any ``"`` doubled (#55).
quote_identifier = quote_ansi


def merge_unique_sets(
    constraints: list[list[str]],
    indexes: list[list[str]],
    primary_key: list[str],
) -> list[list[str]]:
    """Declared UNIQUE constraints, then unique indexes that add a new key.

    An index whose column set is already a UNIQUE constraint or the primary key
    adds nothing: a ``CREATE UNIQUE INDEX`` that repeats the PK would otherwise
    surface as a second, phantom candidate key.
    """
    merged = list(constraints)
    seen = {frozenset(u) for u in constraints}
    if primary_key:
        seen.add(frozenset(primary_key))
    for cols in indexes:
        if frozenset(cols) not in seen:
            seen.add(frozenset(cols))
            merged.append(cols)
    return merged


# ── Table assembly shared by the connectors ─────────────────────────────────
#
# Every connector decided "is this column unique on its own" and "is this foreign key
# one-to-one" the same way, written out nine times; three built identical tables (#58).


def unique_columns(primary_key: list[str], unique_sets: list[list[str]]) -> set[str]:
    """Columns unique on their own: a one-column UNIQUE set, or a one-column primary key."""
    single = {u[0] for u in unique_sets if len(u) == 1}
    if len(primary_key) == 1:
        single.add(primary_key[0])
    return single


def is_one_to_one(
    columns: list[str], primary_key: list[str], unique_sets: list[list[str]]
) -> bool:
    """A foreign key is one-to-one when its columns are exactly a unique set or the key."""
    keys = [set(u) for u in unique_sets]
    if primary_key:
        keys.append(set(primary_key))
    return set(columns) in keys


def mark_one_to_one(
    fks: Iterable[ForeignKey], primary_key: list[str], unique_sets: list[list[str]]
) -> None:
    """Set each foreign key's ``is_unique`` (see :func:`is_one_to_one`)."""
    for fk in fks:
        fk.is_unique = is_one_to_one(fk.columns, primary_key, unique_sets)


def build_table(
    *,
    name: str,
    schema_name: str,
    columns: list[dict[str, Any]],
    primary_key: list[str],
    unique_sets: list[list[str]],
    fks: list[ForeignKey],
    is_view: bool = False,
    comment: str | None = None,
) -> Table:
    """A :class:`Table` from per-column dicts.

    Each dict has ``name``, ``data_type``, ``is_nullable``, ``default`` and ``ordinal``, and
    may have ``comment``. Uniqueness and each foreign key's ``is_unique`` follow from the
    key sets.
    """
    pk = set(primary_key)
    single = unique_columns(primary_key, unique_sets)
    mark_one_to_one(fks, primary_key, unique_sets)
    return Table(
        name=name,
        columns=[
            Column(
                name=c["name"],
                data_type=c["data_type"],
                is_nullable=c["is_nullable"],
                is_primary_key=c["name"] in pk,
                is_unique=c["name"] in single,
                default=c["default"],
                comment=c.get("comment"),
                ordinal=c["ordinal"],
            )
            for c in columns
        ],
        primary_key=primary_key,
        foreign_keys=fks,
        is_view=is_view,
        comment=comment,
        schema_name=schema_name,
        unique_constraints=[list(u) for u in unique_sets],
    )


def fold_constraints(
    table_constraints: Iterable[tuple[Any, Any, Any]],
    key_column_usage: Iterable[tuple[Any, Any, Any]],
    referential_constraints: Iterable[tuple[Any, Any]],
    *,
    enforced: bool = True,
) -> tuple[dict[str, list[str]], dict[str, list[list[str]]], dict[str, list[ForeignKey]]]:
    """Primary keys, unique sets and foreign keys per table, from ``information_schema``.

    Takes the rows of ``table_constraints`` (name, type, table), ``key_column_usage``
    (name, column, position; ordered by name and position) and ``referential_constraints``
    (name, referenced unique constraint). A foreign key whose referenced constraint cannot
    be resolved is skipped. Each connector keeps its own queries.
    """
    cols_by_constraint: dict[str, list[str]] = {}
    for cname, col, _pos in key_column_usage:
        cols_by_constraint.setdefault(cname, []).append(col)
    type_by_constraint: dict[str, str] = {}
    table_by_constraint: dict[str, str] = {}
    for cname, ctype, table_name in table_constraints:
        type_by_constraint[cname] = ctype
        table_by_constraint[cname] = table_name
    referenced = {cname: uc for cname, uc in referential_constraints}

    pks: dict[str, list[str]] = {}
    uniques: dict[str, list[list[str]]] = {}
    fks: dict[str, list[ForeignKey]] = {}
    for cname, ctype in type_by_constraint.items():
        table_name = table_by_constraint[cname]
        cols = cols_by_constraint.get(cname, [])
        if ctype == "PRIMARY KEY":
            pks[table_name] = cols
        elif ctype == "UNIQUE":
            uniques.setdefault(table_name, []).append(cols)
        elif ctype == "FOREIGN KEY":
            uc = referenced.get(cname)
            ref_table = table_by_constraint.get(uc) if uc else None
            ref_cols = cols_by_constraint.get(uc, []) if uc else []
            if not ref_table or not ref_cols:
                continue
            fks.setdefault(table_name, []).append(
                ForeignKey(
                    columns=cols,
                    foreign_table=ref_table,
                    foreign_columns=ref_cols,
                    constraint_name=cname,
                    enforced=enforced,
                )
            )
    return pks, uniques, fks


def group_foreign_keys(
    rows: Iterable[Mapping[str, Any]],
    *,
    constraint: str,
    column: str,
    ref_table: str,
    ref_column: str,
) -> list[ForeignKey]:
    """One :class:`ForeignKey` per constraint, from one row per column pair, in row order.

    The key names say which field of each row holds what, so each connector keeps its
    catalog's own column names.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = grouped.setdefault(
            row[constraint],
            {
                "columns": [],
                "foreign_table": row[ref_table],
                "foreign_columns": [],
                "constraint_name": row[constraint],
            },
        )
        bucket["columns"].append(row[column])
        bucket["foreign_columns"].append(row[ref_column])
    return [ForeignKey(**b) for b in grouped.values()]


def normalize_source_type(source_type: str | None) -> str:
    """Canonicalize a source-type string.

    Empty/``None`` defaults to ``"postgresql"`` (R2G's historical default), the
    ``postgres`` / ``pg`` aliases fold to ``"postgresql"``, ``mariadb`` folds to
    ``"mysql"``, ``mssql`` / ``sql_server`` fold to ``"sqlserver"``, and ``sqlite3``
    folds to ``"sqlite"``.
    """
    key = (source_type or "postgresql").strip().lower()
    if key in _PG_ALIASES:
        return "postgresql"
    if key in _MYSQL_ALIASES:
        return "mysql"
    if key in _MSSQL_ALIASES:
        return "sqlserver"
    if key in _SQLITE_ALIASES:
        return "sqlite"
    return key


def is_postgresql(source_type: str | None) -> bool:
    """True when ``source_type`` denotes PostgreSQL (incl. ``postgres`` / ``pg``)."""
    return normalize_source_type(source_type) == "postgresql"


def is_mysql(source_type: str | None) -> bool:
    """True when ``source_type`` denotes MySQL / MariaDB."""
    return normalize_source_type(source_type) == "mysql"


def is_sqlserver(source_type: str | None) -> bool:
    """True when ``source_type`` denotes Microsoft SQL Server."""
    return normalize_source_type(source_type) == "sqlserver"


def serialize_rows(rows: list[dict]) -> list[dict]:
    """Convert non-JSON-serializable DB values to JSON-safe forms.

    ``datetime``/``date`` → ISO string, ``Decimal`` → float, ``bytes`` → hex.
    Used by the data-preview paths in the UI and MCP servers.
    """
    import datetime as dt
    from decimal import Decimal

    result = []
    for row in rows:
        converted: dict[str, Any] = {}
        for k, v in row.items():
            if isinstance(v, (dt.datetime, dt.date)):
                converted[k] = v.isoformat()
            elif isinstance(v, Decimal):
                converted[k] = float(v)
            elif isinstance(v, bytes):
                converted[k] = v.hex()
            else:
                converted[k] = v
        result.append(converted)
    return result


def create_connector(
    source_type: str,
    connection_string: str,
    schema_name: str = "public",
    *,
    source_params: dict | None = None,
) -> SourceConnector:
    """Return a connector matching ``source_type``.

    Concrete classes are imported lazily so that users who never touch a
    given source type do not pay for its optional dependency. Unknown /
    unsupported types raise :class:`ValueError`; missing optional deps
    raise :class:`ImportError` with a pip-install hint.

    ``source_params`` carries type-specific configuration (e.g. CSV
    ``delimiter`` / ``has_header``). Every other source ignores it.
    """

    key = normalize_source_type(source_type)
    params = source_params or {}
    # Resolve $VAR / ${VAR} credential references (env / r2g secrets) so the
    # catalog never has to store secrets and imported sources connect cleanly.
    connection_string = expand_env_vars(connection_string)
    if key == "postgresql":
        from .postgres import PostgresConnector

        return PostgresConnector(connection_string, schema_name=schema_name)
    if key == "mysql":
        from .mysql import MySQLConnector

        return MySQLConnector(connection_string, schema_name=schema_name)
    if key == "sqlserver":
        from .mssql import SQLServerConnector

        return SQLServerConnector(connection_string, schema_name=schema_name)
    if key == "snowflake":
        from .snowflake import SnowflakeConnector

        return SnowflakeConnector(connection_string, schema_name=schema_name)
    if key == "duckdb":
        from .duckdb_source import DuckDbConnector

        return DuckDbConnector(connection_string, schema_name=schema_name)
    if key == "sqlite":
        from .sqlite_source import SqliteConnector

        return SqliteConnector(connection_string, schema_name=schema_name)
    if key == "databricks":
        from .databricks_source import DatabricksConnector

        return DatabricksConnector(connection_string, schema_name=schema_name)
    if key == "dbt":
        from .dbt_manifest import DbtManifestConnector

        return DbtManifestConnector(connection_string, schema_name=schema_name)
    if key == "osi":
        from .osi import OsiConnector

        return OsiConnector(connection_string, schema_name=schema_name)
    if key == "csv":
        from .csv_source import CsvConnector

        return CsvConnector(
            connection_string,
            schema_name=schema_name,
            delimiter=params.get("delimiter", ","),
            has_header=bool(params.get("has_header", True)),
        )
    raise ValueError(
        f"Unsupported source type '{source_type}'. "
        f"Expected one of: {', '.join(SUPPORTED_SOURCE_TYPES)}."
    )


# Back-compat alias: r2g (and ported tests) call ``create_source_connector``.
create_source_connector = create_connector
