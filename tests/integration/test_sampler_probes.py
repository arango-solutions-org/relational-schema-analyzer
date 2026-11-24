"""Sampler SQL executed against live databases (opt-in).

Closes the gap the DuckDB tests could only narrow. `DuckDbValueSampler` proves the
*shape* of these queries; it cannot vouch for the text, because each sampler writes
its own dialect — DuckDB binds `?` and calls `strpos`, Postgres binds `%s` and casts
with `::float`, MySQL divides and calls `LOCATE`. A green DuckDB run says nothing
about whether the Postgres or MySQL string is even valid.

Until this file existed, every SQL sampler was covered only by mock cursors primed
with a canned number. That verifies the plumbing and not one character of the SQL,
which is exactly how two CSV probes shipped a `TypeError` that fired on their first
contact with real rows (fixed in 0.7.2).

Run with::

    RUN_INTEGRATION=1 \\
      RSA_PG_DSN=postgresql://user:pw@localhost:5432/db \\
      RSA_MYSQL_DSN=mysql://root:@127.0.0.1:3306/rsa_it \\
      RSA_MSSQL_DSN='mssql://sa:pw@127.0.0.1:1433/rsa_it' \\
      pytest tests/integration/test_sampler_probes.py

Each dialect is independently skipped when its DSN is absent, and each builds and
drops its own objects so nothing else in the database is touched.
"""

from __future__ import annotations

import os

import pytest

_RUN = os.environ.get("RUN_INTEGRATION") == "1"

pytestmark = pytest.mark.skipif(
    not _RUN, reason="integration tests are opt-in (set RUN_INTEGRATION=1)"
)

SCHEMA = "rsa_probe_it"

# Rows carrying each denormalization pattern by construction: `zip` determines
# `city`/`state` (embedded lookup), `plan_code` is low-cardinality (redundant
# reference data), `tags` is comma-delimited in 3 of 6 rows (multi-valued), and
# `region_code` references nothing that exists (the case a probe must veto).
_ROWS = """
    (1,'Ann','10001','New York','NY','vip,beta','gold','ZZ'),
    (2,'Bob','10001','New York','NY','beta','gold','ZZ'),
    (3,'Cal','94107','San Francisco','CA','vip','silver','YY'),
    (4,'Dee','94107','San Francisco','CA','beta,trial','silver','YY'),
    (5,'Eve','60601','Chicago','IL','vip,beta,trial','gold','ZZ'),
    (6,'Fay','60601','Chicago','IL','trial','bronze','YY')
"""

_PG_DDL = [
    f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
    f"CREATE SCHEMA {SCHEMA}",
    f"CREATE TABLE {SCHEMA}.plans (code TEXT PRIMARY KEY)",
    f"INSERT INTO {SCHEMA}.plans VALUES ('gold'),('silver'),('bronze'),('platinum')",
    f"CREATE TABLE {SCHEMA}.regions (code TEXT PRIMARY KEY)",
    f"INSERT INTO {SCHEMA}.regions VALUES ('EU'),('US')",
    f"""CREATE TABLE {SCHEMA}.customers (
            id INT PRIMARY KEY, name TEXT, zip TEXT, city TEXT, state TEXT,
            tags TEXT, plan_code TEXT, region_code TEXT)""",
    f"INSERT INTO {SCHEMA}.customers VALUES {_ROWS}",
    f"ALTER TABLE {SCHEMA}.customers ADD COLUMN ghost TEXT",  # #62: never holds a value
]

# MySQL has no CREATE SCHEMA distinct from CREATE DATABASE, and the sampler's
# `schema_name` is the database, so the fixture builds tables in the DSN's database
# and drops exactly those.
_MYSQL_DDL = [
    "DROP TABLE IF EXISTS customers",
    "DROP TABLE IF EXISTS plans",
    "DROP TABLE IF EXISTS regions",
    "CREATE TABLE plans (code VARCHAR(20) PRIMARY KEY)",
    "INSERT INTO plans VALUES ('gold'),('silver'),('bronze'),('platinum')",
    "CREATE TABLE regions (code VARCHAR(20) PRIMARY KEY)",
    "INSERT INTO regions VALUES ('EU'),('US')",
    """CREATE TABLE customers (
           id INT PRIMARY KEY, name VARCHAR(50), zip VARCHAR(10), city VARCHAR(50),
           state VARCHAR(10), tags VARCHAR(80), plan_code VARCHAR(20),
           region_code VARCHAR(20))""",
    f"INSERT INTO customers VALUES {_ROWS}",
    "ALTER TABLE customers ADD COLUMN ghost VARCHAR(20)",  # #62: never holds a value
]

_MYSQL_TEARDOWN = [
    "DROP TABLE IF EXISTS customers",
    "DROP TABLE IF EXISTS plans",
    "DROP TABLE IF EXISTS regions",
]


# SQL Server: tables in the DSN's database, schema dbo. SQL Server was never run live
# by any test before this dialect was added (#59).
_MSSQL_DDL = [
    "DROP TABLE IF EXISTS customers",
    "DROP TABLE IF EXISTS plans",
    "DROP TABLE IF EXISTS regions",
    "CREATE TABLE plans (code NVARCHAR(20) PRIMARY KEY)",
    "INSERT INTO plans VALUES ('gold'),('silver'),('bronze'),('platinum')",
    "CREATE TABLE regions (code NVARCHAR(20) PRIMARY KEY)",
    "INSERT INTO regions VALUES ('EU'),('US')",
    """CREATE TABLE customers (
           id INT PRIMARY KEY, name NVARCHAR(50), zip NVARCHAR(10), city NVARCHAR(50),
           state NVARCHAR(10), tags NVARCHAR(80), plan_code NVARCHAR(20),
           region_code NVARCHAR(20))""",
    f"INSERT INTO customers VALUES {_ROWS}",
    "ALTER TABLE customers ADD ghost NVARCHAR(20)",  # #62: never holds a value
]

_MSSQL_TEARDOWN = _MYSQL_TEARDOWN


def _pg_setup(dsn: str, statements: list[str]) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        for stmt in statements:
            conn.execute(stmt)


def _mysql_setup(dsn: str, statements: list[str]) -> None:
    from relational_schema_analyzer.connectors.mysql import _parse_mysql_url

    import pymysql

    with pymysql.connect(**_parse_mysql_url(dsn), autocommit=True) as conn:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)


def _mssql_setup(dsn: str, statements: list[str]) -> None:
    from relational_schema_analyzer.connectors.mssql import _parse_mssql_url

    import pymssql

    conn = pymssql.connect(**_parse_mssql_url(dsn), autocommit=True)
    try:
        cur = conn.cursor()
        for stmt in statements:
            cur.execute(stmt)
    finally:
        conn.close()


def _pg_sampler(dsn: str):
    from relational_schema_analyzer.fk_inference import PostgresValueSampler

    return PostgresValueSampler(dsn, schema_name=SCHEMA)


def _mysql_sampler(dsn: str):
    from relational_schema_analyzer.connectors.mysql import _parse_mysql_url
    from relational_schema_analyzer.fk_inference import MySQLValueSampler

    database = _parse_mysql_url(dsn).get("database") or ""
    return MySQLValueSampler(dsn, schema_name=database)


def _mssql_sampler(dsn: str):
    from relational_schema_analyzer.fk_inference import SQLServerValueSampler

    return SQLServerValueSampler(dsn, schema_name="dbo")


# dialect -> (dsn env var, driver module, setup fn, ddl, teardown, sampler factory)
_DIALECTS = {
    "postgresql": (
        "RSA_PG_DSN", "psycopg", _pg_setup, _PG_DDL,
        [f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"], _pg_sampler,
    ),
    "mysql": (
        "RSA_MYSQL_DSN", "pymysql", _mysql_setup, _MYSQL_DDL,
        _MYSQL_TEARDOWN, _mysql_sampler,
    ),
    "sqlserver": (
        "RSA_MSSQL_DSN", "pymssql", _mssql_setup, _MSSQL_DDL,
        _MSSQL_TEARDOWN, _mssql_sampler,
    ),
}


@pytest.fixture(params=list(_DIALECTS), scope="module")
def sampler(request):
    dialect = request.param
    env, driver, setup, ddl, teardown, factory = _DIALECTS[dialect]
    dsn = os.environ.get(env)
    if not dsn:
        pytest.skip(f"{env} not set")
    pytest.importorskip(driver)

    setup(dsn, ddl)
    s = factory(dsn)
    s.dialect = dialect  # for failure messages
    yield s
    s.close()
    setup(dsn, teardown)


class TestValueOverlapLive:
    def test_full_containment(self, sampler):
        assert sampler("customers", "plan_code", "plans", "code") == 1.0

    def test_empty_intersection(self, sampler):
        assert sampler("customers", "region_code", "regions", "code") == 0.0

    def test_missing_table_degrades_to_none(self, sampler):
        """A failed probe must return None — never raise, never guess."""
        assert sampler("customers", "plan_code", "nosuchtable", "code") is None


class TestDenormalizationProbesLive:
    def test_functional_dependency(self, sampler):
        assert sampler.group_single_valued("customers", ["zip"], "city") == 1.0
        assert sampler.group_single_valued("customers", ["zip"], "state") == 1.0

    def test_non_dependency(self, sampler):
        assert sampler.group_single_valued("customers", ["zip"], "name") == 0.0

    def test_distinct_ratio(self, sampler):
        assert sampler.distinct_ratio("customers", "plan_code") == 0.5
        assert sampler.distinct_ratio("customers", "id") == 1.0

    def test_delimiter_rate(self, sampler):
        assert sampler.delimiter_rate("customers", "tags", ",") == 0.5
        assert sampler.delimiter_rate("customers", "city", ",") == 0.0

    def test_an_empty_column_is_not_measured(self, sampler):
        """#62: no value to measure is None on every engine, not a 0.0 on most of them."""
        assert sampler.distinct_ratio("customers", "ghost") is None
        assert sampler.delimiter_rate("customers", "ghost", ",") is None
        assert sampler.group_single_valued("customers", ["ghost"], "city") is None

    def test_delimiter_rate_reads_a_non_text_column(self, sampler):
        """A measured zero, not a failed query: Postgres's strpos refuses an integer."""
        assert sampler.delimiter_rate("customers", "id", ",") == 0.0

    def test_probes_degrade_to_none_on_missing_table(self, sampler):
        assert sampler.distinct_ratio("nosuchtable", "plan_code") is None
        assert sampler.delimiter_rate("nosuchtable", "tags", ",") is None
        assert sampler.group_single_valued("nosuchtable", ["zip"], "city") is None
