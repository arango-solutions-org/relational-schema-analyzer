"""No connector or sampler can print a credential in an error, a traceback or a log (#57).

Only Snowflake used to mask: its driver was caught repeating the password (#5). The others
put the driver's text straight into their message and chained the driver's exception, which
any traceback prints raw. Postgres and MySQL did not repeat the password in the failures
tried, but a driver update or an untried failure could, and nothing would say so.

Each test uses a fake driver that does the worst thing a driver can: puts the secret,
as given and percent-encoded, into its error.
"""

from __future__ import annotations

import traceback
from types import SimpleNamespace
from typing import Any

import pytest

from relational_schema_analyzer.redact import MASK, connection_error, redact, secrets_in

SECRET = "pa/ss w0rd!"
_CURSORS = SimpleNamespace(DictCursor=object, SSDictCursor=object, SSCursor=object)
ENCODED = "pa%2Fss%20w0rd%21"


class LeakyDriverError(Exception):
    """A driver error that repeats every credential it was given."""


def _leak(*args: Any, **kwargs: Any) -> Any:
    raise LeakyDriverError(f"login failed: args={args!r} kwargs={kwargs!r} raw={SECRET} url={ENCODED}")


def _assert_masked(exc: BaseException) -> None:
    text = "".join(traceback.format_exception(exc))
    assert SECRET not in text and ENCODED not in text, text
    assert MASK in str(exc)
    assert "LeakyDriverError" in str(exc)  # the driver's error type survives, for debugging
    assert exc.__cause__ is None and exc.__suppress_context__  # raised ``from None``


class TestSecretsIn:
    def test_url_password_as_written_and_decoded(self):
        assert {ENCODED, SECRET} <= set(secrets_in(f"postgresql://u:{ENCODED}@h/db"))

    def test_sensitive_query_parameters(self):
        # No scheme, so this is read as a query string; the next parameter is not a secret.
        found = secrets_in("md:my_db?motherduck_token=tok123&schema=main")
        assert "tok123" in found
        assert redact("token tok123, schema main", found) == f"token {MASK}, schema main"
        assert "dapi9" in secrets_in("databricks://h/x?access_token=dapi9&catalog=c")

    def test_libpq_keyword_form(self):
        assert "s3 cr'et" in secrets_in("host=db user=u password='s3 cr\\'et' dbname=x")
        assert secrets_in("host=db password=plain dbname=x") == ["plain"]

    def test_parameter_mapping(self):
        params = {"user": "u", "password": "p1", "private_key_file_pwd": "kp", "token": None}
        assert set(secrets_in(params)) == {"p1", "kp"}

    def test_nothing_to_find(self):
        assert secrets_in(None, "", "/tmp/demo.duckdb", {"user": "u"}) == []

    def test_longest_first_so_a_prefix_cannot_leave_a_tail(self):
        assert redact("abc and ab", ["ab", "abc"]) == f"{MASK} and {MASK}"

    def test_connection_error_names_the_driver_error(self):
        err = connection_error("Failed to connect", LeakyDriverError(f"x {SECRET}"), [SECRET])
        assert str(err) == f"Failed to connect: LeakyDriverError: x {MASK}"


# ── connectors ───────────────────────────────────────────────────────────────


def test_postgres_connector(monkeypatch):
    pytest.importorskip("psycopg")
    from relational_schema_analyzer.connectors import postgres

    monkeypatch.setattr(postgres.psycopg, "connect", _leak)
    with pytest.raises(RuntimeError, match="Failed to fetch schema from PostgreSQL") as info:
        postgres.PostgresConnector(f"postgresql://u:{ENCODED}@h/db").get_schema()
    _assert_masked(info.value)


def test_postgres_session(monkeypatch):
    pytest.importorskip("psycopg")
    from relational_schema_analyzer.connectors import postgres

    monkeypatch.setattr(postgres.psycopg, "connect", _leak)
    with pytest.raises(RuntimeError, match="Failed to connect to PostgreSQL") as info:
        postgres.PostgresSession(f"postgresql://u:{ENCODED}@h/db").connection  # noqa: B018
    _assert_masked(info.value)


@pytest.mark.parametrize(
    ("module", "loader", "cls", "url", "what"),
    [
        ("mysql", "_load_pymysql", "MySQLConnector", f"mysql://u:{ENCODED}@h/db", "MySQL"),
        ("mssql", "_load_pymssql", "SQLServerConnector", f"mssql://u:{ENCODED}@h/db", "SQL Server"),
        (
            "databricks_source",
            "_load_databricks",
            "DatabricksConnector",
            f"databricks://token:{ENCODED}@h/sql/1.0/warehouses/x?catalog=c",
            "Databricks",
        ),
        ("snowflake", "_load_snowflake_connector", "SnowflakeConnector", f"snowflake://u:{ENCODED}@acct/db/sch", "Snowflake"),
    ],
)
def test_driver_based_connectors(monkeypatch, module, loader, cls, url, what):
    import importlib

    mod = importlib.import_module(f"relational_schema_analyzer.connectors.{module}")
    monkeypatch.setattr(mod, loader, lambda: SimpleNamespace(connect=_leak, cursors=_CURSORS))
    with pytest.raises(RuntimeError, match=f"Failed to connect to {what}") as info:
        getattr(mod, cls)(url).get_schema()
    _assert_masked(info.value)


@pytest.mark.parametrize(
    ("module", "loader", "cls", "url", "what"),
    [
        ("mysql", "_load_pymysql", "MySQLConnector", f"mysql://u:{ENCODED}@h/db", "MySQL"),
        ("mssql", "_load_pymssql", "SQLServerConnector", f"mssql://u:{ENCODED}@h/db", "SQL Server"),
        ("snowflake", "_load_snowflake_connector", "SnowflakeConnector", f"snowflake://u:{ENCODED}@acct/db/sch", "Snowflake"),
    ],
)
def test_bulk_read_sessions(monkeypatch, module, loader, cls, url, what):
    import importlib

    mod = importlib.import_module(f"relational_schema_analyzer.connectors.{module}")
    monkeypatch.setattr(mod, loader, lambda: SimpleNamespace(connect=_leak, cursors=_CURSORS))
    session = getattr(mod, cls)(url).open_session()
    with pytest.raises(RuntimeError, match=f"Failed to connect to {what}") as info:
        session.connection  # noqa: B018
    _assert_masked(info.value)


def test_duckdb_motherduck_token(monkeypatch):
    from relational_schema_analyzer.connectors import duckdb_source

    monkeypatch.setattr(duckdb_source, "_load_duckdb", lambda: SimpleNamespace(connect=_leak))
    with pytest.raises(RuntimeError, match="Failed to open DuckDB database") as info:
        duckdb_source.DuckDbConnector(f"md:db?motherduck_token={ENCODED}").get_schema()
    _assert_masked(info.value)


def test_the_tool_response_carries_no_secret(monkeypatch):
    pytest.importorskip("psycopg")
    from relational_schema_analyzer import run_tool
    from relational_schema_analyzer.connectors import postgres

    monkeypatch.setattr(postgres.psycopg, "connect", _leak)
    response = run_tool(
        {"operation": "snapshot", "source": {"type": "postgresql", "url": f"postgresql://u:{ENCODED}@h/db"}}
    )
    assert response["ok"] is False
    assert SECRET not in str(response) and ENCODED not in str(response)


# ── samplers log a failure instead of raising; the log line is masked too ──


@pytest.fixture
def warnings(monkeypatch):
    """Every warning a sampler logs, as text."""
    from relational_schema_analyzer import fk_inference

    seen: list[str] = []

    class _Recorder:
        def warning(self, event: str, **kw: Any) -> None:
            seen.append(f"{event} {kw}")

        def __getattr__(self, name: str) -> Any:
            return lambda *a, **k: None

    monkeypatch.setattr(fk_inference, "logger", _Recorder())
    return seen


@pytest.mark.parametrize(
    ("loader_module", "loader", "cls", "url"),
    [
        ("mysql", "_load_pymysql", "MySQLValueSampler", f"mysql://u:{ENCODED}@h/db"),
        ("mssql", "_load_pymssql", "SQLServerValueSampler", f"mssql://u:{ENCODED}@h/db"),
    ],
)
def test_derived_samplers_mask_their_log(monkeypatch, warnings, loader_module, loader, cls, url):
    import importlib

    from relational_schema_analyzer import fk_inference

    mod = importlib.import_module(f"relational_schema_analyzer.connectors.{loader_module}")
    monkeypatch.setattr(mod, loader, lambda: SimpleNamespace(connect=_leak))
    assert getattr(fk_inference, cls)(url)("orders", "customer_id", "customers", "id") is None
    assert warnings and all(SECRET not in w and ENCODED not in w for w in warnings), warnings
    assert any(MASK in w for w in warnings)


def test_postgres_sampler_masks_its_log(monkeypatch, warnings):
    pytest.importorskip("psycopg")
    from relational_schema_analyzer import fk_inference

    import psycopg

    monkeypatch.setattr(psycopg, "connect", _leak)
    sampler = fk_inference.PostgresValueSampler(f"postgresql://u:{ENCODED}@h/db")
    assert sampler("orders", "customer_id", "customers", "id") is None
    assert warnings and all(SECRET not in w and ENCODED not in w for w in warnings), warnings


def test_snowflake_sampler_masks_its_log(monkeypatch, warnings):
    from relational_schema_analyzer import fk_inference
    from relational_schema_analyzer.connectors import snowflake

    monkeypatch.setattr(snowflake, "_load_snowflake_connector", lambda: SimpleNamespace(connect=_leak))
    sampler = fk_inference.SnowflakeValueSampler(f"snowflake://u:{ENCODED}@acct/db/sch")
    assert sampler.distinct_ratio("T", "C") is None
    assert warnings and all(SECRET not in w and ENCODED not in w for w in warnings), warnings
