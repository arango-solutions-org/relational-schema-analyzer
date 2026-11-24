"""A key overlay through the tool contract: ``analysisOptions.keyOverlay`` (#71).

The CLI's ``--overlay`` had no contract equivalent, so AOE and the MCP server, which call
``run_tool``, could not declare keys for a BigQuery, Glue or Hive source at all. These tests
hold the contract field to the CLI's behaviour and the schema to what ``overlay.py`` accepts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from relational_schema_analyzer import overlay as overlay_module
from relational_schema_analyzer import run_tool
from relational_schema_analyzer.bitemporal import stamp_bitemporal
from relational_schema_analyzer.cli import main
from relational_schema_analyzer.connectors import create_connector

pytest.importorskip("polars")

_CSV_DIR = Path(__file__).resolve().parent / "fixtures" / "csv_demo"
_SCHEMA = Path(__file__).resolve().parent.parent / "docs/tool-contract/v1/request.schema.json"
_OVERLAY = {
    "version": 1,
    "description": "books reference authors",
    "tables": {
        "books": {
            "foreignKeys": [
                {
                    "columns": ["author_id"],
                    "references": {"table": "authors", "columns": ["id"]},
                    "comment": "every book has an author",
                }
            ]
        }
    },
}


def _run(operation: str, *, overlay=None, **request) -> dict:
    if overlay is not None:
        request["analysisOptions"] = {"keyOverlay": overlay}
    return run_tool({"contractVersion": "1", "operation": operation, **request})


def _books_fks(physical: dict) -> list[dict]:
    return physical["tables"]["books"].get("foreign_keys", [])


def _live() -> dict:
    return {"source": {"type": "csv", "url": str(_CSV_DIR)}}


class TestTheToolAppliesIt:
    def test_to_a_live_source(self):
        response = _run("snapshot", overlay=_OVERLAY, **_live())
        assert response["ok"], response
        (fk,) = _books_fks(response["result"]["physical"])
        assert fk["foreign_table"] == "authors"
        assert fk["constraint_name"].startswith(overlay_module.OVERLAY_CONSTRAINT_PREFIX)
        assert fk["enforced"] is False  # asserted, not verified

    def test_to_a_captured_schema(self):
        captured = create_connector("csv", str(_CSV_DIR)).get_schema().model_dump(mode="json")
        assert _books_fks(captured) == []
        response = _run("snapshot", overlay=_OVERLAY, input={"physical": captured})
        assert response["ok"], response
        assert [fk["foreign_table"] for fk in _books_fks(response["result"]["physical"])] == [
            "authors"
        ]

    def test_the_bundle_says_where_the_keys_came_from(self):
        response = _run("analyze", overlay=_OVERLAY, **_live())
        assert response["ok"], response
        patterns = response["result"]["analysis"]["metadata"]["detectedPatterns"]
        assert "overlay_declared_keys" in patterns

    def test_without_it_nothing_changes(self):
        response = _run("snapshot", **_live())
        assert _books_fks(response["result"]["physical"]) == []

    def test_it_matches_the_cli(self, tmp_path):
        # One pipeline (pipeline.load_physical): the same overlay on the same stamped
        # snapshot gives the same schema through either entry point.
        stamped = stamp_bitemporal(
            create_connector("csv", str(_CSV_DIR)).get_schema(), now="2020-01-01T00:00:00+00:00"
        ).model_dump(mode="json")
        snapshot_file = tmp_path / "physical.json"
        snapshot_file.write_text(json.dumps(stamped), encoding="utf-8")
        overlay_file = tmp_path / "keys.json"
        overlay_file.write_text(json.dumps(_OVERLAY), encoding="utf-8")
        out = tmp_path / "out.json"
        assert main([
            "snapshot", "--from-snapshot", str(snapshot_file),
            "--overlay", str(overlay_file), "-o", str(out),
        ]) == 0

        response = _run("snapshot", overlay=_OVERLAY, input={"physical": stamped})
        assert response["result"]["physical"] == json.loads(out.read_text(encoding="utf-8"))


class TestABadOverlayFailsTheRequest:
    """An overlay that silently did nothing would leave the ontology empty with no reason."""

    @pytest.mark.parametrize(
        ("overlay", "named"),
        [
            ({"tables": {"bookz": {"primaryKey": ["id"]}}}, "bookz"),
            ({"tables": {"books": {"primaryKey": ["isbnn"]}}}, "isbnn"),
            ({"tables": {"books": {"primarykey": ["id"]}}}, "primarykey"),
            ({"version": 2, "tables": {}}, "version"),
            ("keys.json", "object"),
        ],
    )
    def test_with_invalid_request_naming_the_mistake(self, overlay, named):
        response = _run("analyze", overlay=overlay, **_live())
        assert response["ok"] is False
        assert response["error"]["code"] == "INVALID_REQUEST"
        assert named in response["error"]["message"]


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(_SCHEMA.read_text(encoding="utf-8"))


class TestTheContractDeclaresIt:
    def _errors(self, schema, request) -> list[str]:
        import jsonschema

        return [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(request)]

    def test_a_request_with_an_overlay_is_valid(self, schema):
        request = {"contractVersion": "1", "operation": "analyze", **_live(),
                   "analysisOptions": {"keyOverlay": _OVERLAY}}
        assert not self._errors(schema, request)

    @pytest.mark.parametrize(
        "bad",
        [
            {},  # no tables
            {"tables": {"books": {"primarykey": ["id"]}}},  # misspelled key
            {"tables": {"books": {"primaryKey": []}}},  # empty column list
            {"version": 2, "tables": {}},
            {"tables": {"books": {"foreignKeys": [{"columns": ["author_id"]}]}}},  # no target
        ],
    )
    def test_a_malformed_overlay_is_rejected(self, schema, bad):
        import jsonschema

        request = {"contractVersion": "1", "operation": "analyze", **_live(),
                   "analysisOptions": {"keyOverlay": bad}}
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(request))
        assert errors
        # Rejected for what is wrong inside the overlay, not because the field is unknown.
        assert all(list(e.absolute_path)[:2] == ["analysisOptions", "keyOverlay"] for e in errors)

    def test_the_schema_admits_exactly_the_keys_overlay_py_accepts(self, schema):
        # Drift guard: overlay.py rejects unknown keys at every level, so a key the schema
        # admits and the code refuses (or the reverse) is a contract that lies.
        defs = schema["$defs"]
        table = defs["KeyOverlayTable"]["properties"]
        fk = table["foreignKeys"]["items"]["properties"]
        assert set(defs["KeyOverlay"]["properties"]) == overlay_module._ROOT_KEYS
        assert set(table) == overlay_module._TABLE_KEYS
        assert set(fk) == overlay_module._FK_KEYS
        assert set(fk["references"]["properties"]) == overlay_module._REF_KEYS
        assert defs["KeyOverlay"]["properties"]["version"]["const"] == (
            overlay_module.SUPPORTED_OVERLAY_VERSION
        )


class TestTheMcpToolsPassItThrough:
    def test_typed_request_puts_it_in_analysis_options(self):
        from relational_schema_analyzer import mcp_server as srv

        request = srv._typed_request("snapshot", source={"type": "csv"}, key_overlay=_OVERLAY)
        assert request["analysisOptions"] == {"keyOverlay": _OVERLAY}

    def test_the_analyze_tool_applies_it(self):
        import asyncio

        pytest.importorskip("mcp")
        from relational_schema_analyzer import mcp_server as srv

        result = asyncio.run(
            srv.build_app().call_tool(
                "relational_schema_analyzer_analyze",
                {"source": _live()["source"], "key_overlay": _OVERLAY},
            )
        )
        payload = result[1] if isinstance(result, tuple) else result
        text = json.dumps(payload, default=str)
        assert "overlay_declared_keys" in text
