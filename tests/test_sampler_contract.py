"""The sampler contract every value sampler must satisfy (issue #10).

One clause carries this file: **sample the local column, test against the whole
referenced column.** Bounding the local side costs precision; bounding the referenced
side destroys the measurement, because a valid foreign key into a table larger than the
bound is then compared against an arbitrary slice of its parent.

Five samplers originally bounded both sides. Nothing caught it because the asymmetry was
stated in the README — prose implementers do not code against — and absent from the
``Sampler`` protocol, which they do. So the rule is asserted here, behaviourally, against
every sampler the module defines.

The registry test at the bottom is the part that survives contact with a sixth sampler:
adding a ``*ValueSampler`` class without registering it fails, even when its own case
skips for want of a database.
"""

from __future__ import annotations

import os
from typing import Any, Callable

import pytest

from relational_schema_analyzer import fk_inference as fk

# A parent larger than the bound is the whole point, so the bound is made small rather
# than the fixtures made huge: same property, in milliseconds.
BOUND = 50
PARENT_ROWS = 500
CHILD_VALUES = 20

#: Every sampler, and how to build one against a disposable fixture. A sampler whose
#: engine is unavailable yields ``None`` and its case skips — but it must appear here.
SAMPLER_REGISTRY: dict[str, str] = {
    "PostgresValueSampler": "RSA_PG_DSN",
    "MySQLValueSampler": "RSA_MYSQL_DSN",
    "SQLServerValueSampler": "RSA_MSSQL_DSN",
    "DatabricksValueSampler": "RSA_DATABRICKS_DSN",
    "SnowflakeValueSampler": "RSA_SNOWFLAKE_DSN",
    "DuckDbValueSampler": "",   # embedded — always available
    "SqliteValueSampler": "",   # embedded — ships with Python
    "CsvValueSampler": "",      # embedded — always available
}


def _duckdb_fixture(tmp_path) -> Callable[[], Any]:
    duckdb = pytest.importorskip("duckdb")
    path = str(tmp_path / "contract.duckdb")
    conn = duckdb.connect(path)
    conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY)")
    conn.execute(f"INSERT INTO customers SELECT * FROM range(1, {PARENT_ROWS + 1})")
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER)")
    # Children reference only the *high* end of the parent. With the referenced side
    # bounded, a LIMIT with no ORDER BY takes the physically-first (lowest) rows and the
    # intersection is empty — the append-only-parent shape that made this a veto, not
    # merely a low score.
    conn.execute(
        f"INSERT INTO orders SELECT i, {PARENT_ROWS} - (i % {CHILD_VALUES}) "
        f"FROM range(1, 201) t(i)"
    )
    # Issue #52: a reference column that holds no value at all.
    conn.execute("ALTER TABLE orders ADD COLUMN ghost_id INTEGER")
    # Issue #12: text keys whose values agree only after case/whitespace normalization.
    conn.execute("CREATE TABLE accounts (code VARCHAR PRIMARY KEY)")
    conn.execute(
        f"INSERT INTO accounts SELECT 'CUST' || i FROM range(1, {PARENT_ROWS + 1}) t(i)"
    )
    conn.execute("CREATE TABLE invoices (id INTEGER, code_lower VARCHAR, code_padded VARCHAR)")
    conn.execute(
        f"INSERT INTO invoices SELECT i, lower('CUST' || (i % {CHILD_VALUES} + 1)), "
        f"rpad('CUST' || (i % {CHILD_VALUES} + 1), 14, ' ') FROM range(1, 201) t(i)"
    )
    conn.close()
    return lambda: fk.DuckDbValueSampler(path, limit=BOUND)


def _sqlite_fixture(tmp_path) -> Callable[[], Any]:
    """The DuckDB fixture's rows, written to a SQLite file."""
    import sqlite3

    path = str(tmp_path / "contract.sqlite")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO customers VALUES (?)", [(i,) for i in range(1, PARENT_ROWS + 1)])
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER)")
    conn.executemany(
        "INSERT INTO orders VALUES (?, ?)",
        [(i, PARENT_ROWS - (i % CHILD_VALUES)) for i in range(1, 201)],
    )
    conn.execute("ALTER TABLE orders ADD COLUMN ghost_id INTEGER")  # #52: all NULL
    conn.execute("CREATE TABLE accounts (code TEXT PRIMARY KEY)")
    conn.executemany(
        "INSERT INTO accounts VALUES (?)", [(f"CUST{i}",) for i in range(1, PARENT_ROWS + 1)]
    )
    conn.execute("CREATE TABLE invoices (id INTEGER, code_lower TEXT, code_padded TEXT)")
    conn.executemany(
        "INSERT INTO invoices VALUES (?, ?, ?)",
        [
            (i, f"cust{i % CHILD_VALUES + 1}", f"CUST{i % CHILD_VALUES + 1}".ljust(14))
            for i in range(1, 201)
        ],
    )
    conn.commit()
    conn.close()
    return lambda: fk.SqliteValueSampler(path, limit=BOUND)


def _csv_fixture(tmp_path) -> Callable[[], Any]:
    pytest.importorskip("polars")
    d = tmp_path / "csvdemo"
    d.mkdir()
    (d / "customers.csv").write_text(
        "id\n" + "\n".join(str(i) for i in range(1, PARENT_ROWS + 1)), encoding="utf-8"
    )
    (d / "orders.csv").write_text(
        "id,customer_id,ghost_id\n"  # ghost_id: always empty (#52)
        + "\n".join(f"{i},{PARENT_ROWS - (i % CHILD_VALUES)}," for i in range(1, 201)),
        encoding="utf-8",
    )
    # Issue #12: text keys whose values agree only after case/whitespace normalization.
    (d / "accounts.csv").write_text(
        "code\n" + "\n".join(f"CUST{i}" for i in range(1, PARENT_ROWS + 1)),
        encoding="utf-8",
    )
    (d / "invoices.csv").write_text(
        "id,code_lower,code_padded\n"
        + "\n".join(
            f"{i},cust{i % CHILD_VALUES + 1},{('CUST' + str(i % CHILD_VALUES + 1)).ljust(14)}"
            for i in range(1, 201)
        ),
        encoding="utf-8",
    )
    return lambda: fk.CsvValueSampler(str(d), limit=BOUND)


EMBEDDED = {
    "DuckDbValueSampler": _duckdb_fixture,
    "SqliteValueSampler": _sqlite_fixture,
    "CsvValueSampler": _csv_fixture,
}


class TestReferencedSideIsNotBounded:
    """A valid FK must score 1.0 even when the parent is larger than the bound."""

    @pytest.mark.parametrize("name", sorted(EMBEDDED))
    def test_valid_fk_into_a_large_parent_scores_one(self, name, tmp_path):
        sampler = EMBEDDED[name](tmp_path)()
        try:
            score = sampler("orders", "customer_id", "customers", "id")
        finally:
            close = getattr(sampler, "close", None)
            if close:
                close()
        assert score is not None, f"{name} could not evaluate the pair"
        assert score == pytest.approx(1.0), (
            f"{name} scored {score} for a foreign key where every child value exists in "
            f"the parent. The parent has {PARENT_ROWS} rows and the bound is {BOUND}, so "
            "a score below 1.0 means the referenced side was sampled (issue #10)."
        )

    @pytest.mark.parametrize("name", sorted(EMBEDDED))
    def test_a_genuine_non_match_still_scores_zero(self, name, tmp_path):
        """The fix must not make everything score 1.0 — absence must still read as absence."""
        sampler = EMBEDDED[name](tmp_path)()
        try:
            score = sampler("orders", "id", "customers", "id")  # overlapping ranges
            absent = sampler("customers", "id", "orders", "customer_id")
        finally:
            close = getattr(sampler, "close", None)
            if close:
                close()
        assert absent is not None and absent < 0.5, (
            f"{name}: most parent ids are absent from the child column, so this must be "
            f"low; got {absent}"
        )
        assert score is not None


class TestCaseAndWhitespaceDoNotVeto:
    """A key that differs only in case or padding is still the same key (issue #12).

    Strict comparison scores these zero, and zero is the hard veto in ``_apply_sampler``
    — so before this the real foreign key was not left unproven, it was deleted. The
    rescued result must also *say* it was rescued, which is what ``normalized`` carries.
    """

    @pytest.mark.parametrize("name", sorted(EMBEDDED))
    @pytest.mark.parametrize("column", ["code_lower", "code_padded"])
    def test_a_key_differing_only_in_case_or_padding_still_matches(
        self, name, column, tmp_path
    ):
        sampler = EMBEDDED[name](tmp_path)()
        try:
            score = sampler("invoices", column, "accounts", "code")
        finally:
            close = getattr(sampler, "close", None)
            if close:
                close()
        assert score is not None, f"{name} could not evaluate the pair"
        assert score == pytest.approx(1.0), (
            f"{name} scored {score} on '{column}', where every child value names a real "
            "parent and differs only in case or trailing spaces. Zero here is the hard "
            "veto, which deletes a valid foreign key (issue #12)."
        )
        assert fk._normalized(score), (
            f"{name} matched '{column}' but did not mark the result normalized, so the "
            "evidence cannot tell a reviewer the raw values disagree."
        )

    @pytest.mark.parametrize("name", sorted(EMBEDDED))
    def test_an_exact_match_is_not_labelled_normalized(self, name, tmp_path):
        """The label must mean something: an exact match must not claim to be rescued."""
        sampler = EMBEDDED[name](tmp_path)()
        try:
            score = sampler("orders", "customer_id", "customers", "id")
        finally:
            close = getattr(sampler, "close", None)
            if close:
                close()
        assert score == pytest.approx(1.0)
        assert not fk._normalized(score), (
            f"{name} marked an exactly-matching foreign key as normalized; the label "
            "would then appear on candidates whose raw values agree perfectly."
        )


class TestAnEmptySampleIsNotEvaluated:
    """No non-null local value means the pair was not evaluated: ``None``, never ``0.0``.

    Zero is the hard veto. Postgres, DuckDB and SQLite divided by ``GREATEST(COUNT(*), 1)``,
    so an all-NULL reference column scored 0.0 and deleted a correct, name-matched key,
    while the other engines returned ``None`` for the same column and kept it (#52).
    """

    @pytest.mark.parametrize("name", sorted(EMBEDDED))
    def test_an_all_null_reference_column_is_not_a_zero(self, name, tmp_path):
        sampler = EMBEDDED[name](tmp_path)()
        try:
            score = sampler("orders", "ghost_id", "customers", "id")
        finally:
            close = getattr(sampler, "close", None)
            if close:
                close()
        assert score is None, f"{name} scored {score!r} for a column with no values"


class TestKeyProbeCoverage:
    """Which samplers can discover keys from data — and which cannot, on purpose.

    ``profile_primary_keys`` / ``draft_key_overlay`` exist for sources whose catalog
    declares no keys. They reach an engine through ``KeyProbe``: ``column_stats`` and
    ``combo_stats``. For a long time only Snowflake implemented them, so the feature
    could not run on the other six — issue #18, found when a Postgres source declaring
    no keys had no path at all.

    This is a visibility test, not a requirement: it records the current answer so
    adding or omitting a probe is a decision someone makes, not something that drifts.
    """

    #: Samplers that implement KeyProbe today.
    KEY_PROBE_CAPABLE = {"SnowflakeValueSampler", "PostgresValueSampler"}

    @pytest.mark.parametrize("name", sorted(SAMPLER_REGISTRY))
    def test_key_probe_support_matches_the_record(self, name):
        cls = getattr(fk, name)
        has = all(callable(getattr(cls, m, None)) for m in ("column_stats", "combo_stats"))
        expected = name in self.KEY_PROBE_CAPABLE
        assert has is expected, (
            f"{name} key-probe support is now {has}, recorded as {expected}. Adding "
            "column_stats/combo_stats closes part of issue #18 for that engine — update "
            "KEY_PROBE_CAPABLE deliberately, and say so in the release history."
        )

    @pytest.mark.parametrize("name", sorted(KEY_PROBE_CAPABLE))
    def test_every_key_probe_is_also_governable(self, name):
        """Key profiling runs whole-table counts, so a caller must be able to bound it.

        r2g's ``suggest-keys`` promises a query budget, a per-query timeout, a query
        tag and a cost report on every source it accepts. A sampler that gains
        column_stats/combo_stats without these would run unbounded on a production
        database the moment a caller opened the command to that source type.
        """
        import inspect

        cls = getattr(fk, name)
        params = inspect.signature(cls.__init__).parameters
        assert {"max_queries", "statement_timeout_s", "query_tag"} <= set(params), (
            f"{name} profiles keys but cannot be governed"
        )
        assert isinstance(getattr(cls, "stats", None), property), f"{name} reports no stats"

    def test_the_gap_is_still_real(self):
        """Fails once every sampler can profile keys — then this class can go."""
        missing = set(SAMPLER_REGISTRY) - self.KEY_PROBE_CAPABLE
        assert missing, "all samplers now implement KeyProbe; issue #18 is closed"


class TestRegistryIsComplete:
    """A new sampler cannot be added without appearing in the contract above."""

    def test_every_sampler_class_is_registered(self):
        defined = {
            n for n in dir(fk)
            if n.endswith("ValueSampler") and isinstance(getattr(fk, n), type)
        }
        missing = defined - set(SAMPLER_REGISTRY)
        assert not missing, (
            "These samplers are not in SAMPLER_REGISTRY, so the referenced-side contract "
            f"(issue #10) is unasserted for them: {sorted(missing)}. Add them, with the "
            "env var naming their DSN, or '' if the engine is embedded."
        )

    def test_registry_names_only_real_classes(self):
        stale = {n for n in SAMPLER_REGISTRY if not isinstance(getattr(fk, n, None), type)}
        assert not stale, f"SAMPLER_REGISTRY names classes that no longer exist: {sorted(stale)}"

    def test_embedded_samplers_are_actually_exercised(self):
        """Registered-but-unexercised is the quiet way coverage disappears."""
        embedded = {n for n, env in SAMPLER_REGISTRY.items() if not env}
        assert embedded == set(EMBEDDED), (
            "Samplers registered as embedded must have a fixture in EMBEDDED: "
            f"{sorted(embedded ^ set(EMBEDDED))}"
        )


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="opt-in")
class TestLiveEnginesHonourTheContract:
    """Same assertion against the engines that need a DSN. Postgres is where this was found."""

    def test_postgres(self):
        dsn = os.environ.get("RSA_PG_DSN")
        if not dsn:
            pytest.skip("RSA_PG_DSN not set")
        psycopg = pytest.importorskip("psycopg")
        schema = "rsa_contract_it"
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.execute(f"CREATE SCHEMA {schema}")
            c.execute(f"CREATE TABLE {schema}.customers (id INT PRIMARY KEY)")
            c.execute(f"INSERT INTO {schema}.customers SELECT generate_series(1, {PARENT_ROWS})")
            c.execute(f"CREATE TABLE {schema}.orders (id INT PRIMARY KEY, customer_id INT)")
            c.execute(
                f"INSERT INTO {schema}.orders SELECT g, {PARENT_ROWS} - mod(g, {CHILD_VALUES}) "
                f"FROM generate_series(1, 200) g"
            )
            # The database itself vouches for the ground truth.
            c.execute(
                f"ALTER TABLE {schema}.orders ADD CONSTRAINT fk "
                f"FOREIGN KEY (customer_id) REFERENCES {schema}.customers(id)"
            )
            c.execute(f"ALTER TABLE {schema}.orders ADD COLUMN ghost_id INT")  # #52
        sampler = fk.PostgresValueSampler(dsn, schema_name=schema, limit=BOUND)
        try:
            score = sampler("orders", "customer_id", "customers", "id")
            empty = sampler("orders", "ghost_id", "customers", "id")
        finally:
            sampler.close()
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        assert score == pytest.approx(1.0), f"Postgres scored {score} for a database-enforced FK"
        assert empty is None, f"Postgres scored {empty!r} for a column with no values (#52)"
