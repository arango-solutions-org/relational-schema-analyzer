from __future__ import annotations

import pytest

from relational_schema_analyzer.connectors.base import (
    SUPPORTED_SOURCE_TYPES,
    SourceConnector,
    create_source_connector,
    expand_env_vars,
    is_mysql,
    is_postgresql,
    is_sqlserver,
    normalize_source_type,
)
from relational_schema_analyzer.connectors.postgres import PostgresConnector


class TestSupportedTypes:
    def test_tuple_includes_all_known_types(self):
        for t in ("postgresql", "mysql", "sqlserver", "snowflake", "csv"):
            assert t in SUPPORTED_SOURCE_TYPES


class TestNormalization:
    def test_pg_aliases_fold_to_postgresql(self):
        for alias in ("postgres", "pg", "POSTGRESQL", "  Pg  ", None, ""):
            assert normalize_source_type(alias) == "postgresql"

    def test_mariadb_folds_to_mysql(self):
        assert normalize_source_type("mariadb") == "mysql"
        assert normalize_source_type("MySQL") == "mysql"

    def test_mssql_aliases_fold_to_sqlserver(self):
        for alias in ("mssql", "sqlserver", "sql_server", "SQLSERVER"):
            assert normalize_source_type(alias) == "sqlserver"

    def test_other_types_passthrough(self):
        assert normalize_source_type("csv") == "csv"
        assert normalize_source_type("snowflake") == "snowflake"

    def test_is_postgresql_and_is_mysql_predicates(self):
        assert is_postgresql("pg") is True
        assert is_postgresql("mysql") is False
        assert is_mysql("mariadb") is True
        assert is_mysql("postgresql") is False

    def test_is_sqlserver_predicate(self):
        assert is_sqlserver("mssql") is True
        assert is_sqlserver("sqlserver") is True
        assert is_sqlserver("mysql") is False


class TestExpandEnvVars:
    def test_inline_vars_expanded(self, monkeypatch):
        monkeypatch.setenv("DB_USER", "alice")
        monkeypatch.setenv("DB_PASSWORD", "s3cret")
        out = expand_env_vars("postgresql://$DB_USER:$DB_PASSWORD@host:5432/db")
        assert out == "postgresql://alice:s3cret@host:5432/db"

    def test_braced_form_expanded(self, monkeypatch):
        monkeypatch.setenv("PG_CONN", "postgresql://u:p@h/db")
        assert expand_env_vars("${PG_CONN}") == "postgresql://u:p@h/db"

    def test_whole_string_ref_expanded(self, monkeypatch):
        monkeypatch.setenv("PG_CONN", "postgresql://u:p@h/db")
        assert expand_env_vars("$PG_CONN") == "postgresql://u:p@h/db"

    def test_literal_dsn_unchanged(self, monkeypatch):
        # The common case: a real DSN with no $ is returned untouched.
        dsn = "postgresql://u:p@h:5432/db"
        assert expand_env_vars(dsn) == dsn

    def test_unknown_var_left_intact(self, monkeypatch):
        monkeypatch.delenv("NOPE_VAR", raising=False)
        assert expand_env_vars("postgresql://$NOPE_VAR@h/db") == "postgresql://$NOPE_VAR@h/db"

    def test_empty_string(self):
        assert expand_env_vars("") == ""


class TestFactory:
    def test_builds_postgres_connector_from_postgresql(self):
        conn = create_source_connector("postgresql", "postgresql://u:p@h/db")
        assert isinstance(conn, PostgresConnector)

    def test_accepts_short_aliases(self):
        for alias in ("postgres", "pg", "POSTGRESQL", "  PG  "):
            conn = create_source_connector(alias, "postgresql://u:p@h/db")
            assert isinstance(conn, PostgresConnector)

    def test_builds_snowflake_connector_from_snowflake(self):
        from relational_schema_analyzer.connectors.snowflake import SnowflakeConnector

        conn = create_source_connector(
            "snowflake", "snowflake://u:p@xy12345/ANALYTICS/CORE"
        )
        assert isinstance(conn, SnowflakeConnector)

    def test_builds_csv_connector_with_params(self):
        from relational_schema_analyzer.connectors.csv_source import CsvConnector

        conn = create_source_connector(
            "csv",
            "/tmp/dumps",
            source_params={"delimiter": "\t", "has_header": False},
        )
        assert isinstance(conn, CsvConnector)
        assert conn.delimiter == "\t"
        assert conn.has_header is False

    def test_builds_mysql_connector_from_mysql(self):
        from relational_schema_analyzer.connectors.mysql import MySQLConnector

        conn = create_source_connector("mysql", "mysql://u:p@h/shop")
        assert isinstance(conn, MySQLConnector)
        assert conn.schema_name == "shop"

    def test_mariadb_alias_builds_mysql_connector(self):
        from relational_schema_analyzer.connectors.mysql import MySQLConnector

        conn = create_source_connector("mariadb", "mariadb://u:p@h/shop")
        assert isinstance(conn, MySQLConnector)

    def test_builds_sqlserver_connector(self):
        from relational_schema_analyzer.connectors.mssql import SQLServerConnector

        conn = create_source_connector("sqlserver", "mssql://u:p@h/shop")
        assert isinstance(conn, SQLServerConnector)
        assert conn.schema_name == "dbo"

    def test_mssql_alias_builds_sqlserver_connector(self):
        from relational_schema_analyzer.connectors.mssql import SQLServerConnector

        assert isinstance(
            create_source_connector("mssql", "mssql://u:p@h/shop"), SQLServerConnector
        )

    def test_unknown_source_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported source type"):
            create_source_connector("oracle", "oracle://u:p@h/db")


class TestProtocolConformance:
    def test_postgres_connector_satisfies_protocol(self):
        conn = PostgresConnector("postgresql://u:p@h/db")
        assert isinstance(conn, SourceConnector)

    def test_snowflake_connector_satisfies_protocol(self):
        from relational_schema_analyzer.connectors.snowflake import SnowflakeConnector

        conn = SnowflakeConnector("snowflake://u:p@xy12345/ANALYTICS/CORE")
        assert isinstance(conn, SourceConnector)

    def test_mysql_connector_satisfies_protocol(self):
        from relational_schema_analyzer.connectors.mysql import MySQLConnector

        conn = MySQLConnector("mysql://u:p@h/shop")
        assert isinstance(conn, SourceConnector)

    def test_sqlserver_connector_satisfies_protocol(self):
        from relational_schema_analyzer.connectors.mssql import SQLServerConnector

        conn = SQLServerConnector("mssql://u:p@h/shop")
        assert isinstance(conn, SourceConnector)

    def test_csv_connector_satisfies_protocol(self):
        from relational_schema_analyzer.connectors.csv_source import CsvConnector

        conn = CsvConnector("/tmp/dumps")
        assert isinstance(conn, SourceConnector)


def test_quote_identifier_doubles_an_embedded_quote():
    """Wrapping without doubling broke any name containing ``"`` (#55)."""
    from relational_schema_analyzer.connectors.base import quote_identifier

    assert quote_identifier("orders") == '"orders"'
    assert quote_identifier('we"ird') == '"we""ird"'
    assert quote_identifier("") == '""'


class TestSharedTableAssembly:
    """The rules every connector used to write out for itself (#58)."""

    def test_unique_columns_are_one_column_keys(self):
        from relational_schema_analyzer.connectors.base import unique_columns

        assert unique_columns(["id"], [["email"], ["code", "region"]]) == {"id", "email"}
        assert unique_columns(["a", "b"], []) == set(), "a composite key makes no column unique"

    def test_one_to_one_is_exactly_a_key(self):
        from relational_schema_analyzer.connectors.base import is_one_to_one

        assert is_one_to_one(["customer_id"], ["id"], [["customer_id"]])
        assert is_one_to_one(["order_id", "line_no"], ["line_no", "order_id"], [])
        assert not is_one_to_one(["order_id"], ["line_no", "order_id"], []), "part of a key"
        assert not is_one_to_one(["cust"], ["id"], [])

    def test_fold_resolves_foreign_keys_through_the_referenced_constraint(self):
        from relational_schema_analyzer.connectors.base import fold_constraints

        tc = [("pk_c", "PRIMARY KEY", "customer"), ("uq_e", "UNIQUE", "customer"),
              ("fk_o", "FOREIGN KEY", "orders"), ("fk_x", "FOREIGN KEY", "orders")]
        kcu = [("pk_c", "id", 1), ("uq_e", "email", 1), ("fk_o", "customer_id", 1),
               ("fk_x", "ghost_id", 1)]
        rc = [("fk_o", "pk_c"), ("fk_x", "no_such_constraint")]
        pks, uniques, fks = fold_constraints(tc, kcu, rc, enforced=False)
        assert pks == {"customer": ["id"]} and uniques == {"customer": [["email"]]}
        (fk,) = fks["orders"]  # the unresolvable one is skipped
        assert (fk.columns, fk.foreign_table, fk.foreign_columns) == (["customer_id"], "customer", ["id"])
        assert fk.enforced is False

    def test_group_foreign_keys_keeps_column_pairs_in_row_order(self):
        from relational_schema_analyzer.connectors.base import group_foreign_keys

        rows = [
            {"C": "fk_line", "COL": "line_no", "T": "line", "R": "line_no"},
            {"C": "fk_line", "COL": "order_id", "T": "line", "R": "order_id"},
            {"C": "fk_cust", "COL": "cust", "T": "customer", "R": "id"},
        ]
        fks = group_foreign_keys(rows, constraint="C", column="COL", ref_table="T", ref_column="R")
        assert [(f.constraint_name, f.columns, f.foreign_columns) for f in fks] == [
            ("fk_line", ["line_no", "order_id"], ["line_no", "order_id"]),
            ("fk_cust", ["cust"], ["id"]),
        ]
