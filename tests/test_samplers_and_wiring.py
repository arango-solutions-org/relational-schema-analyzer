"""The injected seams, and whether taxonomy discovery is actually reachable.

Two separate gaps this covers:

1. `discriminator.ValueEnumerator` and `taxonomy.SpecializationCounter` were injection
   points with no implementation, so in practice only declared CHECK constraints produced
   discriminators and the specialization constraints were always null.
2. `taxonomy.discover` was imported by nothing, so `RelationalSchemaAnalyzer.analyze`
   returned the same bundle it always had.
"""

import duckdb
import pytest

from relational_schema_analyzer.analyzer import RelationalSchemaAnalyzer
from relational_schema_analyzer.samplers import (
    executor_from_connection,
    make_specialization_counter,
    make_value_enumerator,
)
from relational_schema_analyzer.taxonomy import TAXONOMY_AVAILABLE
from tests.test_discriminator import accounts_table, col, schema_of, subtype_table

requires_taxonomy = pytest.mark.skipif(
    not TAXONOMY_AVAILABLE, reason="conceptual-taxonomy not installed"
)

SUBTYPES = {
    "mortgage_account": [col("routing_number"), col("principal", "numeric")],
    "checking_account": [col("routing_number"), col("overdraft_limit", "numeric")],
    "savings_account": [col("routing_number"), col("apy", "numeric")],
    "insurance_account": [col("premium", "numeric")],
}


@pytest.fixture
def db():
    """A real specialization: 10 accounts, disjoint subtypes, one uncovered parent row."""
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE account (account_id INTEGER PRIMARY KEY, name TEXT, account_type TEXT)"
    )
    rows = [
        (1, "a", "mortgage"),
        (2, "b", "mortgage"),
        (3, "c", "checking"),
        (4, "d", "checking"),
        (5, "e", "savings"),
        (6, "f", "insurance"),
        (7, "g", "insurance"),
    ]
    conn.executemany("INSERT INTO account VALUES (?, ?, ?)", rows)
    # One parent row deliberately in no subtype → partial specialization.
    conn.execute("INSERT INTO account VALUES (8, 'h', 'mortgage')")

    for table, ids in [
        ("mortgage_account", [1, 2]),
        ("checking_account", [3, 4]),
        ("savings_account", [5]),
        ("insurance_account", [6, 7]),
    ]:
        conn.execute(f"CREATE TABLE {table} (account_id INTEGER PRIMARY KEY)")
        conn.executemany(f"INSERT INTO {table} VALUES (?)", [(i,) for i in ids])
    return conn


# ── value enumerator ─────────────────────────────────────────────────────────


def test_enumerator_returns_distinct_values(db):
    values = make_value_enumerator(executor_from_connection(db))("account", "account_type", 64)
    assert sorted(values) == ["checking", "insurance", "mortgage", "savings"]


def test_enumerator_skips_nulls(db):
    db.execute("INSERT INTO account VALUES (99, 'x', NULL)")
    values = make_value_enumerator(executor_from_connection(db))("account", "account_type", 64)
    assert None not in values and "None" not in values


def test_enumerator_returns_none_on_a_missing_table(db):
    """Unmeasurable is not the same as empty — the detector must be able to tell."""
    assert make_value_enumerator(executor_from_connection(db))("no_such", "c", 8) is None


def test_enumerator_finds_an_undeclared_discriminator(db):
    """DuckDB has no CHECK enum here, so this is the sampled path doing the work."""
    from relational_schema_analyzer.discriminator import detect_discriminators

    schema = schema_of(accounts_table(check=False))
    found = detect_discriminators(
        schema, enumerator=make_value_enumerator(executor_from_connection(db))
    )
    assert [(c.column, c.source) for c in found] == [("account_type", "sampled")]
    assert found[0].values == ["checking", "insurance", "mortgage", "savings"]


# ── specialization counter ───────────────────────────────────────────────────


def test_counter_measures_disjointness_and_coverage(db):
    counts = make_specialization_counter(executor_from_connection(db))(
        "account", "account_id", list(SUBTYPES)
    )
    assert counts["total"] == 8
    assert counts["inMultiple"] == 0  # disjoint
    assert counts["inNone"] == 1  # partial: account 8 is in no subtype


def test_counter_detects_overlap(db):
    """A key in two subtypes makes the specialization overlapping, not disjoint."""
    db.execute("INSERT INTO checking_account VALUES (1)")  # already a mortgage
    counts = make_specialization_counter(executor_from_connection(db))(
        "account", "account_id", list(SUBTYPES)
    )
    assert counts["inMultiple"] == 1


def test_counter_is_not_inflated_by_duplicate_child_rows(db):
    """Scalar subqueries rather than joins — a join would multiply the parent row count."""
    db.execute("DROP TABLE mortgage_account")
    db.execute("CREATE TABLE mortgage_account (account_id INTEGER)")
    db.executemany("INSERT INTO mortgage_account VALUES (?)", [(1,), (1,), (2,)])
    counts = make_specialization_counter(executor_from_connection(db))(
        "account", "account_id", list(SUBTYPES)
    )
    assert counts["total"] == 8


def test_counter_returns_none_when_unmeasurable(db):
    assert (
        make_specialization_counter(executor_from_connection(db))("no_such", "id", ["also_no"])
        is None
    )


def test_counter_returns_none_without_children(db):
    assert make_specialization_counter(executor_from_connection(db))("account", "id", []) is None


def test_identifiers_are_quoted_not_interpolated(db):
    """A table name is not a bind parameter, so quoting is the only guard."""
    db.execute('CREATE TABLE "odd name" (account_id INTEGER PRIMARY KEY)')
    db.execute('INSERT INTO "odd name" VALUES (1)')
    counts = make_specialization_counter(executor_from_connection(db))(
        "account", "account_id", ["odd name"]
    )
    assert counts is not None and counts["total"] == 8


# ── analyzer wiring ──────────────────────────────────────────────────────────


def specialization_schema():
    return schema_of(
        accounts_table(),
        *[subtype_table(name, cols) for name, cols in SUBTYPES.items()],
    )


def test_no_taxonomy_by_default():
    analysis = RelationalSchemaAnalyzer().analyze(specialization_schema())
    assert "taxonomyStatus" not in analysis.metadata
    assert not analysis.to_bundle()["conceptualSchema"].get("abstractClasses")


@requires_taxonomy
def test_enabling_discovery_reaches_the_bundle():
    analysis = RelationalSchemaAnalyzer(discover_taxonomy=True).analyze(specialization_schema())
    conceptual = analysis.to_bundle()["conceptualSchema"]

    assert conceptual.get("abstractClasses"), "discovery ran but nothing reached the bundle"
    assert conceptual.get("subClassOfProposals")
    assert analysis.metadata["taxonomyStatus"]["status"] == "ok"


@requires_taxonomy
def test_specialization_parent_is_the_real_table_not_a_synthesized_one():
    analysis = RelationalSchemaAnalyzer(discover_taxonomy=True).analyze(specialization_schema())
    tops = [
        c
        for c in analysis.to_bundle()["conceptualSchema"]["abstractClasses"]
        if len(c["members"]) == 4
    ]
    assert len(tops) == 1
    assert tops[0]["synthesized"] is False


@requires_taxonomy
def test_counter_makes_constraints_measured_rather_than_null(db):
    counter = make_specialization_counter(executor_from_connection(db))
    plain = RelationalSchemaAnalyzer(discover_taxonomy=True).analyze(specialization_schema())
    measured = RelationalSchemaAnalyzer(
        discover_taxonomy=True, specialization_counter=counter
    ).analyze(specialization_schema())

    def top(analysis):
        return next(
            c
            for c in analysis.to_bundle()["conceptualSchema"]["abstractClasses"]
            if len(c["members"]) == 4
        )

    assert top(plain)["disjoint"] is None, "unmeasured must be null, never false"
    assert top(measured)["disjoint"] is True
    assert top(measured)["complete"] is False  # account 8 belongs to no subtype


@requires_taxonomy
def test_discovery_failure_never_fails_the_analysis(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("enricher exploded")

    monkeypatch.setattr("relational_schema_analyzer.taxonomy.discover", boom)
    analysis = RelationalSchemaAnalyzer(discover_taxonomy=True).analyze(specialization_schema())

    assert analysis.conceptual.entities, "analysis was lost"
    assert analysis.metadata["taxonomyStatus"]["status"] == "degraded"


# ── dialects and bounds (#56) ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source_type, enumerate_sql, parent_sample",
    [
        (None, 'SELECT DISTINCT "c" FROM "s"."t" WHERE "c" IS NOT NULL LIMIT 9',
         'FROM (SELECT "id" FROM "s"."p" LIMIT 100) p'),
        ("postgresql", 'SELECT DISTINCT "c" FROM "s"."t" WHERE "c" IS NOT NULL LIMIT 9',
         'FROM (SELECT "id" FROM "s"."p" LIMIT 100) p'),
        # MySQL without ANSI_QUOTES reads "c" as a string, so it needs backticks.
        ("mysql", "SELECT DISTINCT `c` FROM `s`.`t` WHERE `c` IS NOT NULL LIMIT 9",
         "FROM (SELECT `id` FROM `s`.`p` LIMIT 100) p"),
        ("mariadb", "SELECT DISTINCT `c` FROM `s`.`t` WHERE `c` IS NOT NULL LIMIT 9",
         "FROM (SELECT `id` FROM `s`.`p` LIMIT 100) p"),
        ("databricks", "SELECT DISTINCT `c` FROM `s`.`t` WHERE `c` IS NOT NULL LIMIT 9",
         "FROM (SELECT `id` FROM `s`.`p` LIMIT 100) p"),
        # SQL Server has no LIMIT.
        ("sqlserver", "SELECT DISTINCT TOP (9) [c] FROM [s].[t] WHERE [c] IS NOT NULL",
         "FROM (SELECT TOP (100) [id] FROM [s].[p]) p"),
    ],
)
def test_each_dialect_gets_its_own_quoting_and_row_cap(source_type, enumerate_sql, parent_sample):
    seen: list[str] = []
    enumerate_values = make_value_enumerator(
        lambda sql, params: seen.append(sql) or [("x",)], schema="s", source_type=source_type
    )
    count = make_specialization_counter(
        lambda sql, params: seen.append(sql) or [(1, 0, 0)],
        schema="s", source_type=source_type, limit=100,
    )
    enumerate_values("t", "c", 8)
    count("p", "id", ["child"])
    assert seen[0] == enumerate_sql
    assert parent_sample in seen[1]


def test_a_failed_query_is_unmeasured_not_retried_unbounded():
    """The old fallback dropped the row cap and read the whole column."""
    calls: list[str] = []
    enumerate_values = make_value_enumerator(lambda sql, params: calls.append(sql) or None)
    assert enumerate_values("t", "c", 8) is None
    assert calls == ['SELECT DISTINCT "c" FROM "t" WHERE "c" IS NOT NULL LIMIT 9']


def test_counter_reads_a_bounded_sample_of_parent_rows(db):
    counts = make_specialization_counter(executor_from_connection(db), limit=3)(
        "account", "account_id", list(SUBTYPES)
    )
    assert counts["total"] == 3, "the parent table holds 8 rows; only 3 may be read"
