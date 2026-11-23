"""Live Postgres: composite key order, and names containing a double quote (#55).

Opt-in like the other live tests: ``RUN_INTEGRATION=1`` and ``RSA_PG_DSN``.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="opt-in"),
]

SCHEMA = "rsa_session_it"


@pytest.fixture()
def pg():
    dsn = os.environ.get("RSA_PG_DSN")
    if not dsn:
        pytest.skip("RSA_PG_DSN not set")
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.execute(f"CREATE SCHEMA {SCHEMA}")
        # Declared in non-alphabetical, non-column order: the key is (z_part, a_part).
        c.execute(
            f"CREATE TABLE {SCHEMA}.line (a_part INT, note TEXT, z_part INT, "
            "PRIMARY KEY (z_part, a_part))"
        )
        c.execute(f'CREATE TABLE {SCHEMA}."we""ird" (id INT PRIMARY KEY, "up""dated" INT)')
        c.execute(f'INSERT INTO {SCHEMA}."we""ird" VALUES (1, 10), (2, 20), (3, 30)')
    yield dsn
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def test_a_composite_key_keeps_its_declared_order(pg):
    from relational_schema_analyzer.connectors.postgres import PostgresConnector

    schema = PostgresConnector(pg, schema_name=SCHEMA).get_schema()
    assert schema.tables["line"].primary_key == ["z_part", "a_part"]


def test_bulk_reads_of_names_containing_a_double_quote(pg, tmp_path):
    from relational_schema_analyzer.connectors.postgres import PostgresConnector

    session = PostgresConnector(pg, schema_name=SCHEMA).open_session()
    try:
        assert session.count_rows('we"ird') == 3
        assert session.count_rows('we"ird', since_column='up"dated', since_value="20") == 2
        rows = list(session.stream_rows('we"ird', since_column='up"dated', since_value="30"))
        assert [r["id"] for r in rows] == [3]
        assert session.dump_table_to_csv('we"ird', tmp_path / "out.csv") == 3
    finally:
        session.close()
