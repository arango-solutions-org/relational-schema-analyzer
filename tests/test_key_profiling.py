"""Primary-key candidates from data -- via fakesnow and SnowflakeValueSampler as the probe."""

from __future__ import annotations

import pytest

fakesnow = pytest.importorskip("fakesnow")
pytest.importorskip("snowflake.connector")

from relational_schema_analyzer.connectors.snowflake import SnowflakeConnector  # noqa: E402
from relational_schema_analyzer.fk_inference import SnowflakeValueSampler  # noqa: E402
from relational_schema_analyzer.key_profiling import (  # noqa: E402
    _id_like,
    profile_primary_keys,
)

_GEN = "FROM TABLE(GENERATOR(ROWCOUNT => {n}))"
_DDL = [
    # Several columns are unique; only the identifier should win.
    "CREATE TABLE CUSTOMERS (CUSTOMER_ID INT, EMAIL VARCHAR, FULL_NAME VARCHAR,"
    " SIGNUP_TS TIMESTAMP, REGION VARCHAR, SCORE FLOAT, ACTIVE BOOLEAN)",
    "INSERT INTO CUSTOMERS SELECT seq4() + 1, 'u' || seq4() || '@x.io', 'Name ' || seq4(),"
    " DATEADD(minute, seq4(), '2026-01-01'::TIMESTAMP), 'R' || MOD(seq4(), 4),"
    " seq4() / 7.0, MOD(seq4(), 2) = 0 " + _GEN.format(n=50),
    # 150 rows: LATE_DUP is unique in the first 100 (the sample) and repeats after.
    "CREATE TABLE EVENTS (EVENT_ID INT, LATE_DUP INT)",
    "INSERT INTO EVENTS SELECT seq4() + 1, seq4() + 1 " + _GEN.format(n=100),
    "INSERT INTO EVENTS SELECT seq4() + 101, seq4() + 1 " + _GEN.format(n=50),
    # Neither column alone is unique; the pair is.
    "CREATE TABLE ORDER_LINES (ORDER_ID INT, PRODUCT_ID INT, QTY INT)",
    "INSERT INTO ORDER_LINES SELECT FLOOR(seq4() / 5) + 1, MOD(seq4(), 5) + 1, 1 "
    + _GEN.format(n=40),
    "CREATE TABLE NULLY (ITEM_ID INT)",
    "INSERT INTO NULLY VALUES (1), (2), (NULL), (4), (5), (6), (7), (8), (9), (10), (11)",
    "CREATE TABLE TINY (THING_ID INT)",
    "INSERT INTO TINY VALUES (1), (2), (3)",
    "CREATE TABLE EMPTY_T (E_ID INT)",
    # Six identifier-like columns, 15 pairs; only the last pair, (E_ID, F_ID), is a key.
    "CREATE TABLE WIDE (A_ID INT, B_ID INT, C_ID INT, D_ID INT, E_ID INT, F_ID INT)",
    "INSERT INTO WIDE SELECT MOD(seq4(), 2), MOD(seq4(), 2), MOD(seq4(), 2), MOD(seq4(), 2),"
    " FLOOR(seq4() / 5) + 1, MOD(seq4(), 5) + 1 " + _GEN.format(n=40),
    # Case-folded camelCase keys (orderId -> ORDERID): no underscore, but named after tables.
    "CREATE TABLE ITEMS_BY_ORDER (ORDERID INT, CUSTOMERID INT, QTY INT)",
    "INSERT INTO ITEMS_BY_ORDER SELECT FLOOR(seq4() / 5) + 1, MOD(seq4(), 5) + 1, 1 "
    + _GEN.format(n=40),
    # ACCOUNT and ACCOUNTS singularize alike; ACCOUNT_ID is ACCOUNT's own key.
    "CREATE TABLE ACCOUNT (ACCOUNT_ID INT, NOTE VARCHAR)",
    "INSERT INTO ACCOUNT SELECT seq4() + 1, 'n' || MOD(seq4(), 3) " + _GEN.format(n=20),
    "CREATE TABLE ACCOUNTS (X INT)",
    # singularize mangles ORDER_STATUS into ORDER_STATU.
    "CREATE TABLE ORDER_STATUS (LABEL_CODE VARCHAR, ORDER_STATUS_ID INT)",
    "INSERT INTO ORDER_STATUS SELECT 'L' || seq4(), seq4() + 1 " + _GEN.format(n=20),
    # A key plus another identifier: (ORDER_ID, CUSTOMER_ID) is unique only
    # because ORDER_ID is -- a superkey, which must not be reported as a key.
    "CREATE TABLE ORDERS (ORDER_ID INT, CUSTOMER_ID INT)",
    "INSERT INTO ORDERS SELECT seq4() + 1, MOD(seq4(), 7) + 1 " + _GEN.format(n=30),
    # CUSTOMER_ID comes first and is unique here (one session per customer), so
    # only the "named after another table" rule stops it beating SESSION_KEY.
    "CREATE TABLE SESSIONS (CUSTOMER_ID INT, SESSION_KEY INT)",
    "INSERT INTO SESSIONS SELECT seq4() + 1, seq4() + 1000 " + _GEN.format(n=20),
]


@pytest.fixture
def env():
    import snowflake.connector as sc

    with fakesnow.patch():
        conn = sc.connect(database="DB1", schema="PUBLIC")
        cur = conn.cursor()
        for stmt in _DDL:
            cur.execute(stmt)
        cur.close()
        schema = SnowflakeConnector("snowflake://u:p@acct/DB1/PUBLIC").get_schema()
        yield schema, conn
        conn.close()


def _probe(conn, **kw):
    return SnowflakeValueSampler(connection=conn, schema_name="PUBLIC", limit=100, **kw)


def test_the_identifier_outranks_columns_unique_by_accident(env):
    schema, conn = env
    profile = profile_primary_keys(schema, _probe(conn), tables=["CUSTOMERS"])
    ranked = [k.columns[0] for k in profile.candidates["CUSTOMERS"]]

    assert ranked[0] == "CUSTOMER_ID"
    assert set(ranked) == {"CUSTOMER_ID", "EMAIL", "FULL_NAME", "SIGNUP_TS"}
    best = profile.best("CUSTOMERS")
    assert "named after its table (CUSTOMERS -> CUSTOMER_ID)" in best.reasons
    assert best.rows == 50 and best.distinct == 50


def test_floats_booleans_and_duplicates_are_never_candidates(env):
    schema, conn = env
    ranked = {
        k.columns[0]
        for k in profile_primary_keys(schema, _probe(conn), tables=["CUSTOMERS"]).candidates[
            "CUSTOMERS"
        ]
    }
    assert not ranked & {"SCORE", "ACTIVE", "REGION"}


def test_sample_uniqueness_is_confirmed_over_the_whole_table(env):
    schema, conn = env
    probe = _probe(conn)
    profile = profile_primary_keys(schema, probe, tables=["EVENTS"])

    assert [k.columns for k in profile.candidates["EVENTS"]] == [("EVENT_ID",)]
    assert profile.best("EVENTS").rows == 150
    assert probe.stats["queries_run"] == 2  # sample, then confirmation


def test_a_table_the_sample_covered_needs_no_second_query(env):
    schema, conn = env
    probe = _probe(conn)
    profile_primary_keys(schema, probe, tables=["CUSTOMERS"])
    assert probe.stats["queries_run"] == 1


def test_composite_key_found_only_when_no_single_column_is_one(env):
    schema, conn = env
    profile = profile_primary_keys(schema, _probe(conn), tables=["ORDER_LINES"])
    assert [k.columns for k in profile.candidates["ORDER_LINES"]] == [("ORDER_ID", "PRODUCT_ID")]


def test_a_single_null_disqualifies_a_column(env):
    schema, conn = env
    assert profile_primary_keys(schema, _probe(conn), tables=["NULLY"]).candidates["NULLY"] == []


def test_tiny_tables_are_reported_but_discounted(env):
    schema, conn = env
    best = profile_primary_keys(schema, _probe(conn), tables=["TINY"]).best("TINY")
    assert best.columns == ("THING_ID",)
    assert best.score < 0.5
    assert any("only 3 rows" in r for r in best.reasons)


def test_declared_empty_and_unknown_tables_are_reported_not_guessed(env):
    schema, conn = env
    schema.tables["TINY"].primary_key = ["THING_ID"]
    profile = profile_primary_keys(schema, _probe(conn), tables=["TINY", "EMPTY_T", "NOPE"])
    assert profile.declared == ["TINY"]
    assert profile.not_evaluated == {"EMPTY_T": "empty table", "NOPE": "not in schema"}
    assert profile.candidates == {}


def test_a_spent_budget_leaves_tables_undecided(env):
    schema, conn = env
    probe = _probe(conn, max_queries=1)
    profile = profile_primary_keys(schema, probe, tables=["CUSTOMERS", "EVENTS"])
    assert profile.best("CUSTOMERS").columns == ("CUSTOMER_ID",)
    assert "EVENTS" in profile.not_evaluated
    assert "EVENTS" not in profile.candidates


def test_profiling_never_mutates_the_schema(env):
    schema, conn = env
    before = schema.model_dump()
    profile_primary_keys(schema, _probe(conn))
    assert schema.model_dump() == before


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ACCOUNT_ID", True),
        ("account_key", True),
        ("ORDER_NO", True),
        ("accountId", True),
        ("uuid", True),
        ("PAID", False),
        ("GRID", False),
        ("VALID", False),
        ("ID", False),
    ],
)
def test_identifier_like_names(name, expected):
    # Bare "ID" is scored separately ("named ID"), not as a suffix match.
    assert _id_like(name) is expected


@pytest.mark.parametrize(
    ("word", "singular"),
    [
        ("customers", "customer"),
        ("CUSTOMERS", "CUSTOMER"),
        ("CATEGORIES", "CATEGORY"),
        ("categories", "category"),
        ("HEALTH_SIGNALS", "HEALTH_SIGNAL"),
        ("BOXES", "BOX"),
        ("ADDRESS", "ADDRESS"),
    ],
)
def test_singularize_handles_snowflake_upper_case(word, singular):
    # Upper-case names used to come back unchanged, disabling every upper-case match.
    from relational_schema_analyzer.naming import singularize

    assert singularize(word) == singular


def test_a_superkey_is_not_reported_when_a_single_column_is_a_key(env):
    schema, conn = env
    profile = profile_primary_keys(schema, _probe(conn), tables=["ORDERS"])
    assert [k.columns for k in profile.candidates["ORDERS"]] == [("ORDER_ID",)]


def test_views_are_never_given_keys(env):
    # fakesnow cannot introspect views, so mark one by hand.
    schema, conn = env
    schema.tables["CUSTOMERS"].is_view = True
    profile = profile_primary_keys(schema, _probe(conn), tables=["CUSTOMERS"])
    assert "CUSTOMERS" in profile.not_evaluated
    assert "CUSTOMERS" not in profile.candidates


def test_a_column_named_after_another_table_loses_to_the_real_key(env):
    schema, conn = env
    ranked = profile_primary_keys(schema, _probe(conn), tables=["SESSIONS"]).candidates["SESSIONS"]
    assert [k.columns[0] for k in ranked] == ["SESSION_KEY", "CUSTOMER_ID"]
    assert "named after another table (CUSTOMERS), so likely a reference to it" in ranked[1].reasons


def test_a_declined_composite_probe_leaves_the_table_undecided(env):
    # The sample uses the whole budget; every pair probe then declines. That is
    # "could not check", never "has no key".
    schema, conn = env
    profile = profile_primary_keys(schema, _probe(conn, max_queries=1), tables=["ORDER_LINES"])
    assert "ORDER_LINES" in profile.not_evaluated
    assert "ORDER_LINES" not in profile.candidates


def test_a_truncated_pair_search_is_reported_not_called_keyless(env):
    schema, conn = env
    profile = profile_primary_keys(schema, _probe(conn), tables=["WIDE"], max_pair_checks=10)
    assert "WIDE" not in profile.candidates
    assert "10 of 15" in profile.not_evaluated["WIDE"]
    found = profile_primary_keys(schema, _probe(conn), tables=["WIDE"], max_pair_checks=15)
    assert found.best("WIDE").columns == ("E_ID", "F_ID")


def test_case_folded_camel_case_keys_are_identifiers(env):
    schema, conn = env
    profile = profile_primary_keys(schema, _probe(conn), tables=["ITEMS_BY_ORDER"])
    assert profile.best("ITEMS_BY_ORDER").columns == ("ORDERID", "CUSTOMERID")


@pytest.mark.parametrize("accounts_is_view", [False, True])
def test_a_tables_own_key_is_never_treated_as_a_reference(env, accounts_is_view):
    schema, conn = env
    schema.tables["ACCOUNTS"].is_view = accounts_is_view
    best = profile_primary_keys(schema, _probe(conn), tables=["ACCOUNT"]).best("ACCOUNT")
    assert best.columns == ("ACCOUNT_ID",)
    assert not any("another table" in r for r in best.reasons)


def test_own_name_matches_tables_singularize_mangles(env):
    schema, conn = env
    best = profile_primary_keys(schema, _probe(conn), tables=["ORDER_STATUS"]).best("ORDER_STATUS")
    assert best.columns == ("ORDER_STATUS_ID",)
    assert "named after its table (ORDER_STATUS -> ORDER_STATUS_ID)" in best.reasons


def test_a_declared_unique_key_is_not_reprofiled(env):
    schema, conn = env
    schema.tables["EVENTS"].unique_constraints = [["EVENT_ID"]]
    probe = _probe(conn)
    profile = profile_primary_keys(schema, probe, tables=["EVENTS"])
    assert "EVENTS" not in profile.candidates
    assert "UNIQUE" in profile.not_evaluated["EVENTS"]
    assert probe.stats["queries_run"] == 0
