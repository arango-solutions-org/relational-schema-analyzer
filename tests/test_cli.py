from __future__ import annotations

import json
from pathlib import Path

import pytest

from relational_schema_analyzer.cli import main
from relational_schema_analyzer.types import PhysicalSchema

pytest.importorskip("polars")

_CSV_DIR = Path(__file__).resolve().parent / "fixtures" / "csv_demo"


@pytest.fixture
def snapshot_file(tmp_path) -> str:
    out = tmp_path / "physical.json"
    rc = main(["snapshot", "--source", "csv", "--url", str(_CSV_DIR), "-o", str(out)])
    assert rc == 0
    return str(out)


class TestSnapshot:
    def test_help_names_every_source_type(self, capsys):
        # The list was hand-written and named 5 of the 10 types (#60).
        from relational_schema_analyzer.connectors import SUPPORTED_SOURCE_TYPES

        with pytest.raises(SystemExit):
            main(["snapshot", "--help"])
        help_text = " ".join(capsys.readouterr().out.split())
        for source_type in SUPPORTED_SOURCE_TYPES:
            assert source_type in help_text

    def test_writes_valid_physical_schema(self, snapshot_file):
        schema = PhysicalSchema.load_from_file(snapshot_file)
        assert sorted(schema.tables) == ["authors", "books", "loans", "members"]

    def test_to_stdout(self, capsys):
        rc = main(["snapshot", "--source", "csv", "--url", str(_CSV_DIR)])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "tables" in data


class TestAnalyze:
    def test_emits_bundle_from_snapshot(self, snapshot_file, capsys):
        rc = main(["analyze", "--from-snapshot", snapshot_file])
        assert rc == 0
        bundle = json.loads(capsys.readouterr().out)
        assert set(bundle) == {"conceptualSchema", "physicalMapping", "metadata"}
        assert {e["name"] for e in bundle["conceptualSchema"]["entities"]} == {
            "Authors", "Books", "Loans", "Members",
        }

    def test_requires_source_or_snapshot(self):
        with pytest.raises(SystemExit):
            main(["analyze"])


class TestOverlay:
    """``--overlay`` is accepted on every subcommand and applied identically to a live
    introspection and a captured snapshot."""

    @pytest.fixture
    def overlay_file(self, tmp_path) -> str:
        path = tmp_path / "keys.overlay.json"
        path.write_text(
            json.dumps({
                "version": 1,
                "tables": {
                    "books": {
                        "foreignKeys": [
                            {
                                "columns": ["author_id"],
                                "references": {"table": "authors", "columns": ["id"]},
                            }
                        ]
                    }
                },
            }),
            encoding="utf-8",
        )
        return str(path)

    def test_snapshot_applies_overlay_to_a_live_source(self, overlay_file, tmp_path):
        out = tmp_path / "physical.json"
        rc = main([
            "snapshot", "--source", "csv", "--url", str(_CSV_DIR),
            "--overlay", overlay_file, "-o", str(out),
        ])
        assert rc == 0
        schema = PhysicalSchema.load_from_file(str(out))
        (fk,) = schema.tables["books"].foreign_keys
        assert fk.foreign_table == "authors"
        assert fk.enforced is False

    def test_analyze_from_snapshot_applies_overlay(self, snapshot_file, overlay_file, capsys):
        rc = main(["analyze", "--from-snapshot", snapshot_file, "--overlay", overlay_file])
        assert rc == 0
        bundle = json.loads(capsys.readouterr().out)
        assert "overlay_declared_keys" in bundle["metadata"]["detectedPatterns"]
        rels = bundle["conceptualSchema"]["relationships"]
        # The overlaid FK is declared, so it is no longer guessed from column names —
        # while `loans`, which the overlay says nothing about, still relies on inference.
        books = next(r for r in rels if r["fromEntity"] == "Books")
        assert not books.get("inferred")
        assert any(r.get("inferred") for r in rels if r["fromEntity"] == "Loans")

    def test_bad_overlay_exits_with_a_useful_message(self, snapshot_file, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"tables": {"nosuch": {"primaryKey": ["id"]}}}), "utf-8")
        with pytest.raises(SystemExit) as err:
            main(["analyze", "--from-snapshot", snapshot_file, "--overlay", str(path)])
        assert "unknown table 'nosuch'" in str(err.value)


class TestOwl:
    def test_turtle_to_file(self, snapshot_file, tmp_path):
        ttl = tmp_path / "out.ttl"
        rc = main(["owl", "--from-snapshot", snapshot_file, "--format", "turtle", "-o", str(ttl)])
        assert rc == 0
        text = ttl.read_text(encoding="utf-8")
        assert "a owl:Class" in text
        assert 'phys:tableName "authors"' in text

    def test_jsonld_to_stdout(self, snapshot_file, capsys):
        rc = main(["owl", "--from-snapshot", snapshot_file, "--format", "jsonld"])
        assert rc == 0
        doc = json.loads(capsys.readouterr().out)
        assert "@graph" in doc and "@context" in doc

    def test_iri_base_override(self, snapshot_file, capsys):
        rc = main([
            "owl", "--from-snapshot", snapshot_file, "--format", "turtle",
            "--iri-base", "http://example.org/c#",
            "--phys-iri-base", "http://example.org/p#",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "@prefix : <http://example.org/c#> ." in out
