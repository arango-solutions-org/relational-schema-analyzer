"""Over stdio, the MCP server's stdout carries JSON-RPC and nothing else.

The CLI's fix for log lines on stdout (``tests/test_cli_stdout_is_only_the_bundle.py``)
was applied only to the CLI. The MCP server never called it, so with structlog
installed and unconfigured, any log line during a tool call went to stdout, between
two protocol messages::

    {"jsonrpc":"2.0","id":1,"result":{...}}
    2026-11-23 08:32:33 [warning  ] sqlite_fk_target_missing  references=ghosts table=orphan
    {"jsonrpc":"2.0","id":2,"result":{...}}

This runs the real server in a fresh process (so structlog starts unconfigured, as it
does for a client launching it) and makes a call that is known to log a warning.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading

import pytest

pytest.importorskip("mcp", reason="the MCP server needs the [mcp] extra")
pytest.importorskip("structlog", reason="the bug only exists with structlog")


def _call_over_stdio(messages: list[dict], wait_for_id: int) -> tuple[list[str], str]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "relational_schema_analyzer.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    watchdog = threading.Timer(60, proc.kill)  # a hung server fails the test, not CI
    watchdog.start()
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        lines = []
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            try:
                if json.loads(line).get("id") == wait_for_id:
                    break
            except ValueError:
                continue  # not JSON: keep it, the test reports it
        proc.stdin.close()
        proc.terminate()
        stderr = proc.stderr.read()
        proc.wait(timeout=10)
        return lines, stderr
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()


def test_a_warning_during_a_tool_call_goes_to_stderr_not_the_protocol(tmp_path):
    db = tmp_path / "shop.sqlite"
    conn = sqlite3.connect(db)
    # A reference to a missing table: the connector skips it and logs a warning.
    conn.execute("CREATE TABLE orphan (id INTEGER PRIMARY KEY, ghost_id INTEGER REFERENCES ghosts(id))")
    conn.commit()
    conn.close()

    lines, stderr = _call_over_stdio(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "relational_schema_analyzer_snapshot",
                    "arguments": {"source": {"type": "sqlite", "url": str(db)}},
                },
            },
        ],
        wait_for_id=2,
    )

    not_json = []
    for line in lines:
        try:
            json.loads(line)
        except ValueError:
            not_json.append(line)
    assert not not_json, f"non-protocol lines on stdout: {not_json}"
    assert any(json.loads(line).get("id") == 2 for line in lines), "the call never answered"
    assert "sqlite_fk_target_missing" in stderr, "the warning has to go somewhere"
