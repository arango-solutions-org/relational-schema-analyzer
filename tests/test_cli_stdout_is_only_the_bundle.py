"""stdout carries the bundle and nothing else (reported by r2g).

``log.py`` defers to ``structlog`` when it is installed, and an **unconfigured**
structlog writes through ``PrintLogger`` to **stdout**, at debug level. So one
``logger.debug`` during a run landed in the middle of the JSON::

    $ relational-schema-analyzer analyze --source csv --url ./d --prior-run prev.json
    2026-10-26 12:03:51 [debug    ] bitemporal_fingerprint_unchanged carried_from=...
    {"conceptualSchema": ...

and the caller's ``json.load`` failed. ``--prior-run`` was implicated only because
it is the one flag whose path logs at debug.

Why it survived: **RSA never installs structlog.** It is in no extra, so CI always
took the stdlib fallback — which writes to *stderr* at *WARNING*, where both the
stream and the level hide it. The bug was only ever visible to an application that
brought structlog of its own, which is exactly what r2g does.

These tests therefore force the structlog path rather than trusting the ambient
environment, so the hole cannot reopen the same way.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

from relational_schema_analyzer.cli import main
from relational_schema_analyzer.log import keep_logs_off_stdout as _keep_logs_off_stdout

structlog = pytest.importorskip("structlog", reason="the bug only exists with structlog")


@pytest.fixture()
def unconfigured_structlog():
    """structlog as a fresh process sees it — the state that writes to stdout."""
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


@pytest.fixture()
def csv_source(tmp_path):
    (tmp_path / "authors.csv").write_text("id,name\n1,a\n2,b\n", encoding="utf-8")
    (tmp_path / "books.csv").write_text(
        "id,title,author_id\n1,x,1\n2,y,2\n", encoding="utf-8"
    )
    return tmp_path


def _run(argv: list[str]) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return out.getvalue(), err.getvalue(), code


class TestStdoutStaysMachineReadable:
    def test_a_run_with_prior_run_still_emits_only_json(
        self, unconfigured_structlog, csv_source, tmp_path
    ):
        """The reported reproduction, end to end."""
        first, _, code = _run(["analyze", "--source", "csv", "--url", str(csv_source)])
        assert code == 0
        prior = tmp_path / "prior.json"
        prior.write_text(first, encoding="utf-8")

        out, _, code = _run(
            [
                "analyze",
                "--source",
                "csv",
                "--url",
                str(csv_source),
                "--prior-run",
                str(prior),
            ]
        )
        assert code == 0
        json.loads(out)  # the assertion: it parses at all

    def test_a_log_line_emitted_mid_run_goes_to_stderr(self, unconfigured_structlog):
        """Directly: configure as the CLI does, then log, and see where it lands.

        The configure call has to happen *inside* the redirect. structlog binds the
        stream object when it is configured, not when it writes — so configuring
        first would capture the real stderr and this would assert against the wrong
        object. That binding is correct for a CLI, whose streams do not move.
        """
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            _keep_logs_off_stdout()
            structlog.get_logger("t").debug("a_debug_event", detail="x")
        assert "a_debug_event" not in out.getvalue(), "a log line reached stdout"
        assert "a_debug_event" in err.getvalue(), "and it has to go somewhere"


class TestItDoesNotStealLoggingFromAnApplication:
    def test_an_already_configured_structlog_is_left_alone(self, unconfigured_structlog):
        """A library may not seize logging an application has already set up."""
        sentinel = io.StringIO()
        structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sentinel))
        _keep_logs_off_stdout()
        structlog.get_logger("t").warning("app_owned_event")
        assert "app_owned_event" in sentinel.getvalue(), (
            "the application's own destination was overwritten"
        )

    def test_calling_it_twice_is_harmless(self, unconfigured_structlog):
        _keep_logs_off_stdout()
        _keep_logs_off_stdout()
        out = io.StringIO()
        with redirect_stdout(out):
            structlog.get_logger("t").warning("still_not_on_stdout")
        assert "still_not_on_stdout" not in out.getvalue()
