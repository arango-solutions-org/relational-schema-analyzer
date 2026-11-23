"""The CLI and ``run_tool`` load, overlay and stamp a schema the same way (#60).

They used to build that pipeline separately and disagreed about a snapshot that already
carried a stamp: the tool kept it, the CLI re-stamped it with the time the file was re-read.
Both now go through ``pipeline.load_physical``, and these tests hold them to one answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from relational_schema_analyzer import run_tool
from relational_schema_analyzer.bitemporal import stamp_bitemporal
from relational_schema_analyzer.cli import main
from relational_schema_analyzer.connectors import create_connector

pytest.importorskip("polars")

_CSV_DIR = Path(__file__).resolve().parent / "fixtures" / "csv_demo"
_SEEN = "2020-01-01T00:00:00+00:00"
_OVERLAY = {
    "version": 1,
    "tables": {
        "books": {
            "foreignKeys": [
                {"columns": ["author_id"], "references": {"table": "authors", "columns": ["id"]}}
            ]
        }
    },
}


@pytest.fixture
def stamped(tmp_path) -> Path:
    """A snapshot stamped when the source was seen, long before this test re-reads it."""
    schema = stamp_bitemporal(create_connector("csv", str(_CSV_DIR)).get_schema(), now=_SEEN)
    path = tmp_path / "physical.json"
    path.write_text(json.dumps(schema.model_dump(mode="json")), encoding="utf-8")
    return path


def _cli_snapshot(tmp_path, *args: str) -> dict:
    out = tmp_path / "out.json"
    assert main(["snapshot", *args, "-o", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def _tool_snapshot(physical: dict, **input_extra) -> dict:
    response = run_tool(
        {"operation": "snapshot", "input": {"physical": physical, **input_extra}}
    )
    assert response["ok"], response
    return response["result"]["physical"]


def test_the_cli_keeps_the_stamp_a_snapshot_already_carries(stamped, tmp_path):
    physical = _cli_snapshot(tmp_path, "--from-snapshot", str(stamped))
    assert physical["transaction_time"] == _SEEN


def test_the_cli_and_the_tool_return_the_same_schema(stamped, tmp_path):
    captured = json.loads(stamped.read_text(encoding="utf-8"))
    assert _cli_snapshot(tmp_path, "--from-snapshot", str(stamped)) == _tool_snapshot(captured)


def test_an_overlay_does_not_change_when_the_source_was_seen(stamped, tmp_path):
    overlay = tmp_path / "keys.json"
    overlay.write_text(json.dumps(_OVERLAY), encoding="utf-8")
    physical = _cli_snapshot(
        tmp_path, "--from-snapshot", str(stamped), "--overlay", str(overlay)
    )
    assert physical["transaction_time"] == _SEEN
    assert [fk["foreign_table"] for fk in physical["tables"]["books"]["foreign_keys"]] == [
        "authors"
    ]


def test_a_prior_run_restamps_in_both(stamped, tmp_path):
    # With a prior run the fingerprint rule has something to compare against, so the
    # schema is stamped afresh and linked to its predecessor, by either entry point.
    captured = json.loads(stamped.read_text(encoding="utf-8"))
    analysis = run_tool({"operation": "analyze", "input": {"physical": captured}})
    metadata = analysis["result"]["analysis"]["metadata"]
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps(metadata), encoding="utf-8")

    from_cli = _cli_snapshot(tmp_path, "--from-snapshot", str(stamped), "--prior-run", str(prior))
    from_tool = _tool_snapshot(captured, previousAnalysis=metadata)
    for physical in (from_cli, from_tool):
        assert physical["transaction_time"] != _SEEN
        assert physical["predecessor_fingerprint"] == metadata["physicalSchemaFingerprint"]


def test_a_live_source_is_always_stamped(tmp_path):
    physical = _cli_snapshot(tmp_path, "--source", "csv", "--url", str(_CSV_DIR))
    assert physical["transaction_time"]
