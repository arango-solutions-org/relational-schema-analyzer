"""Live MySQL and SQL Server: the discriminator and specialization probes run there (#56).

They used ANSI double quotes everywhere, which MySQL rejects without ``ANSI_QUOTES``, and
``LIMIT``, which SQL Server does not have. Opt-in like the other live tests:
``RUN_INTEGRATION=1`` plus ``RSA_MYSQL_DSN`` / ``RSA_MSSQL_DSN``.
"""

from __future__ import annotations

import os

import pytest

from relational_schema_analyzer.samplers import (
    executor_from_connection,
    make_specialization_counter,
    make_value_enumerator,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="opt-in"),
]

_DDL = [
    "CREATE TABLE rsa56_account (account_id INT PRIMARY KEY, account_type VARCHAR(20))",
    "CREATE TABLE rsa56_mortgage (account_id INT PRIMARY KEY)",
    "CREATE TABLE rsa56_checking (account_id INT PRIMARY KEY)",
    "INSERT INTO rsa56_account VALUES (1, 'mortgage'), (2, 'mortgage'), (3, 'checking'), (4, 'checking')",
    "INSERT INTO rsa56_mortgage VALUES (1), (2)",
    "INSERT INTO rsa56_checking VALUES (3)",
]
_DROP = ["DROP TABLE IF EXISTS rsa56_mortgage", "DROP TABLE IF EXISTS rsa56_checking",
         "DROP TABLE IF EXISTS rsa56_account"]


def _exercise(conn, source_type: str) -> None:
    cur = conn.cursor()
    for stmt in _DROP + _DDL:
        cur.execute(stmt)
    conn.commit()
    try:
        executor = executor_from_connection(conn)
        values = make_value_enumerator(executor, source_type=source_type)(
            "rsa56_account", "account_type", 8
        )
        assert sorted(values or []) == ["checking", "mortgage"]
        counts = make_specialization_counter(executor, source_type=source_type)(
            "rsa56_account", "account_id", ["rsa56_mortgage", "rsa56_checking"]
        )
        assert counts == {"total": 4, "inMultiple": 0, "inNone": 1}
        sampled = make_specialization_counter(executor, source_type=source_type, limit=2)(
            "rsa56_account", "account_id", ["rsa56_mortgage", "rsa56_checking"]
        )
        assert sampled is not None and sampled["total"] == 2
    finally:
        for stmt in _DROP:
            cur.execute(stmt)
        conn.commit()


def test_mysql():
    dsn = os.environ.get("RSA_MYSQL_DSN")
    if not dsn:
        pytest.skip("RSA_MYSQL_DSN not set")
    pymysql = pytest.importorskip("pymysql")
    from relational_schema_analyzer.connectors.mysql import _parse_mysql_url

    conn = pymysql.connect(**_parse_mysql_url(dsn))
    try:
        _exercise(conn, "mysql")
    finally:
        conn.close()


def test_sqlserver():
    dsn = os.environ.get("RSA_MSSQL_DSN")
    if not dsn:
        pytest.skip("RSA_MSSQL_DSN not set")
    pymssql = pytest.importorskip("pymssql")
    from relational_schema_analyzer.connectors.mssql import _parse_mssql_url

    conn = pymssql.connect(**_parse_mssql_url(dsn))
    try:
        _exercise(conn, "sqlserver")
    finally:
        conn.close()
