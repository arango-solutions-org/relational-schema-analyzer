"""Snowflake connector conformance via fakesnow (embedded, always-on CI).

``fakesnow`` patches ``snowflake-connector-python`` in-process (DuckDB-backed), so
this runs the *real* ``SnowflakeConnector.get_schema()`` against a local fake —
no cloud account. fakesnow's coverage bounds what we assert: it supports columns
(default/ordinal/comment), table comments, PK, and CURRENT_VERSION(), but not FK /
unique-key / view introspection — so those capabilities are off here (they get
recorded-fixture + opt-in live coverage elsewhere).
"""

from __future__ import annotations

import pytest

from tests import _conformance as conf

fakesnow = pytest.importorskip("fakesnow")
pytest.importorskip("snowflake.connector")

from relational_schema_analyzer.connectors.snowflake import SnowflakeConnector  # noqa: E402

_FAKESNOW_CAPABILITIES = {
    conf.ORDINAL,
    conf.DEFAULTS,
    conf.COMMENTS,
    conf.PROVENANCE_VERSION,
}

_DDL = [
    "CREATE TABLE users ("
    " id INT PRIMARY KEY,"
    " email VARCHAR(100) NOT NULL,"
    " status VARCHAR(10) DEFAULT 'active',"
    " created_at TIMESTAMP)",
    "COMMENT ON TABLE users IS 'people'",
    "COMMENT ON COLUMN users.email IS 'contact email'",
    "CREATE TABLE orders ("
    " id INT PRIMARY KEY,"
    " user_id INT NOT NULL,"
    " total NUMBER(10,2))",
]


@pytest.fixture
def snowflake_shop():
    import snowflake.connector as sc

    with fakesnow.patch():
        conn = sc.connect(database="DB1", schema="PUBLIC")
        cur = conn.cursor()
        for stmt in _DDL:
            cur.execute(stmt)
        cur.close()
        conn.close()
        yield SnowflakeConnector("snowflake://u:p@acct/DB1/PUBLIC").get_schema()


def test_snowflake_conformance(snowflake_shop):
    conf.assert_shop_conformance(
        snowflake_shop, dialect="snowflake", capabilities=_FAKESNOW_CAPABILITIES
    )


def test_provenance_reports_snowflake(snowflake_shop):
    assert snowflake_shop.source.dialect == "snowflake"
    assert snowflake_shop.source.database == "DB1"
    assert snowflake_shop.source.namespace == "PUBLIC"


def test_column_enrichment(snowflake_shop):
    users = conf._find_table(snowflake_shop, "users")
    status = conf._find_col(users, "status")
    assert status.default is not None
    assert conf._find_col(users, "email").comment == "contact email"
    assert users.comment == "people"


def test_names_containing_a_double_quote(tmp_path):
    """Schema reads and bulk reads double an embedded ``"`` rather than breaking (#55)."""
    import snowflake.connector as sc

    with fakesnow.patch():
        conn = sc.connect(database="DB1", schema="PUBLIC")
        cur = conn.cursor()
        cur.execute('CREATE TABLE "WE""IRD" (ID INT PRIMARY KEY, "UP""DATED" INT)')
        cur.execute('INSERT INTO "WE""IRD" VALUES (1, 10), (2, 20), (3, 30)')
        cur.close()
        conn.close()

        connector = SnowflakeConnector("snowflake://u:p@acct/DB1/PUBLIC")
        table = conf._find_table(connector.get_schema(), 'WE"IRD')
        assert table.primary_key == ["ID"]

        session = connector.open_session()
        try:
            assert session.count_rows('WE"IRD') == 3
            assert session.count_rows('WE"IRD', since_column='UP"DATED', since_value="20") == 2
            rows = list(session.stream_rows('WE"IRD', since_column='UP"DATED', since_value="30"))
            assert [r["ID"] for r in rows] == [3]
        finally:
            session.close()
