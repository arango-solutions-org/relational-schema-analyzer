"""OWL import: a curated ontology with phys: back-links read back into the bundle shape."""

from __future__ import annotations

import json
import socket
import urllib.request
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import relational_schema_analyzer as rsa
from relational_schema_analyzer import (
    OwlImportError,
    RelationalSchemaAnalyzer,
    export_owl_jsonld,
    export_owl_turtle,
    import_owl_bundle,
)
from relational_schema_analyzer.types import Column, ForeignKey, PhysicalSchema, Table

rdflib = pytest.importorskip("rdflib")

PHYS = "http://arangodb.com/schema/physical#"
_CONTRACT = Path(__file__).resolve().parent.parent / "docs/tool-contract/v1/response.schema.json"

_PREFIXES = f"""\
@prefix : <http://example.org/crm#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix phys: <{PHYS}> .
"""


def _col(name, dt="integer", pk=False):
    return Column(name=name, data_type=dt, is_nullable=False, is_primary_key=pk)


def _composite_schema() -> PhysicalSchema:
    """A composite PK and a composite FK, each in an order that is *not* sorted order,
    so an importer that sorted (or otherwise lost) the order would fail the assertions."""
    accounts = Table(
        name="ACCOUNTS",
        schema_name="CRM",
        columns=[_col("REGION", "varchar", pk=True), _col("ACCOUNT_NO", pk=True),
                 _col("NAME", "varchar")],
        primary_key=["REGION", "ACCOUNT_NO"],
    )
    orders = Table(
        name="ORDERS",
        schema_name="CRM",
        columns=[_col("ORDER_ID", pk=True), _col("ACCT_REGION", "varchar"), _col("ACCT_NO"),
                 _col("TOTAL", "numeric")],
        primary_key=["ORDER_ID"],
        foreign_keys=[
            ForeignKey(
                columns=["ACCT_REGION", "ACCT_NO"],
                foreign_table="ACCOUNTS",
                foreign_columns=["REGION", "ACCOUNT_NO"],
            )
        ],
    )
    return PhysicalSchema(tables={"ACCOUNTS": accounts, "ORDERS": orders})


@pytest.fixture
def analysis():
    return RelationalSchemaAnalyzer().analyze(_composite_schema())


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _to_rdfxml(ttl_path: Path, xml_path: Path) -> Path:
    g = rdflib.Graph()
    g.parse(ttl_path, format="turtle")
    xml_path.write_text(g.serialize(format="xml"), encoding="utf-8")
    return xml_path


def _canonical(bundle: dict) -> str:
    return json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ── Round trip ───────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_turtle_round_trip_preserves_physical_mapping_and_names(self, analysis, tmp_path):
        original = analysis.to_bundle()
        ttl = _write(tmp_path, "crm.ttl", export_owl_turtle(analysis))

        result = import_owl_bundle(ttl)
        bundle = result.to_bundle()
        assert result.analysis.to_bundle() == bundle

        o_pm, i_pm = original["physicalMapping"], bundle["physicalMapping"]
        assert set(i_pm["entities"]) == set(o_pm["entities"]) == {"Accounts", "Orders"}
        for name, o_ent in o_pm["entities"].items():
            i_ent = i_pm["entities"][name]
            assert i_ent["style"] == "TABLE"
            assert i_ent["tableName"] == o_ent["tableName"]
            assert i_ent["schema"] == o_ent["schema"]
            assert i_ent["primaryKey"] == o_ent["primaryKey"]  # ordered, not sorted
            assert {p: m["field"] for p, m in i_ent["properties"].items()} == {
                p: m["field"] for p, m in o_ent["properties"].items()
            }
            for mapping in i_ent["properties"].values():
                assert mapping["columnName"] == mapping["field"]
        assert i_pm["entities"]["Accounts"]["primaryKey"] == ["REGION", "ACCOUNT_NO"]

        assert set(i_pm["relationships"]) == set(o_pm["relationships"])
        for rtype, o_rel in o_pm["relationships"].items():
            i_rel = i_pm["relationships"][rtype]
            for key in ("style", "fromTable", "fromColumns", "toTable", "toColumns"):
                assert i_rel[key] == o_rel[key], key
        fk = i_pm["relationships"]["Orders_Accounts"]
        assert fk["fromColumns"] == ["ACCT_REGION", "ACCT_NO"]
        assert fk["toColumns"] == ["REGION", "ACCOUNT_NO"]
        assert fk["toTable"] == "ACCOUNTS"

    def test_round_trip_preserves_conceptual_names_types_and_terms(self, analysis, tmp_path):
        original = analysis.to_bundle()["conceptualSchema"]
        ttl = _write(tmp_path, "crm.ttl", export_owl_turtle(analysis))
        conceptual = import_owl_bundle(ttl).to_bundle()["conceptualSchema"]

        def props(cs):
            return {
                e["name"]: {p["name"]: (p["type"], bool(p.get("unique"))) for p in e["properties"]}
                for e in cs["entities"]
            }

        assert props(conceptual) == props(original)
        rels = {r["type"]: r for r in conceptual["relationships"]}
        for o_rel in original["relationships"]:
            rel = rels[o_rel["type"]]
            assert (rel["fromEntity"], rel["toEntity"], rel["cardinality"]) == (
                o_rel["fromEntity"], o_rel["toEntity"], o_rel["cardinality"],
            )
            assert rel["term"] == f"http://arangodb.com/schema/relational#{o_rel['type']}"
        accounts = next(e for e in conceptual["entities"] if e["name"] == "Accounts")
        assert accounts["term"] == "http://arangodb.com/schema/relational#Accounts"
        region = next(p for p in accounts["properties"] if p["name"] == "REGION")
        assert region["term"] == "http://arangodb.com/schema/relational#Accounts_REGION"

    def test_rdfxml_equivalent_gives_same_digest_and_bundle(self, analysis, tmp_path):
        ttl = _write(tmp_path, "crm.ttl", export_owl_turtle(analysis))
        owl = _to_rdfxml(ttl, tmp_path / "crm.owl")

        from_ttl = import_owl_bundle(ttl)
        from_xml = import_owl_bundle(owl)
        assert (
            from_ttl.provenance["canonicalGraphDigest"]
            == from_xml.provenance["canonicalGraphDigest"]
        )
        assert _canonical(from_ttl.to_bundle()) == _canonical(from_xml.to_bundle())
        assert from_xml.provenance["documents"][0]["filename"] == "crm.owl"

    def test_join_table_passes_through(self, tmp_path):
        students = Table(name="students", columns=[_col("id", pk=True)], primary_key=["id"])
        courses = Table(name="courses", columns=[_col("id", pk=True)], primary_key=["id"])
        enrollments = Table(
            name="enrollments",
            columns=[_col("student_id", pk=True), _col("course_id", pk=True)],
            primary_key=["student_id", "course_id"],
            foreign_keys=[
                ForeignKey(column="student_id", foreign_table="students", foreign_column="id"),
                ForeignKey(column="course_id", foreign_table="courses", foreign_column="id"),
            ],
        )
        schema = PhysicalSchema(tables={t.name: t for t in (students, courses, enrollments)})
        original = RelationalSchemaAnalyzer().analyze(schema).to_bundle()
        ttl = _write(tmp_path, "j.ttl", export_owl_turtle(original))
        imported = import_owl_bundle(ttl).to_bundle()["physicalMapping"]["relationships"]
        (rtype, o_rel), = original["physicalMapping"]["relationships"].items()
        i_rel = imported[rtype]
        for key in ("style", "joinTable", "joinFromColumns", "joinFromParentColumns",
                    "joinToColumns", "joinToParentColumns"):
            assert i_rel[key] == o_rel[key], key

    def test_same_input_gives_byte_identical_bundle(self, analysis, tmp_path):
        ttl = _write(tmp_path, "crm.ttl", export_owl_turtle(analysis))
        first, second = import_owl_bundle(ttl), import_owl_bundle(str(ttl))
        assert json.dumps(first.to_bundle()) == json.dumps(second.to_bundle())
        assert first.provenance == second.provenance

    def test_digest_ignores_blank_node_labels_and_triple_order(self, tmp_path):
        a = _PREFIXES + """
<http://example.org/crm> a owl:Ontology .
:A a owl:Class ; rdfs:subClassOf [ a owl:Restriction ; owl:onProperty :p ] .
:p a owl:ObjectProperty .
"""
        b = _PREFIXES + """
:p a owl:ObjectProperty .
:A rdfs:subClassOf _:zz . _:zz owl:onProperty :p ; a owl:Restriction .
:A a owl:Class .
<http://example.org/crm> a owl:Ontology .
"""
        da = import_owl_bundle(_write(tmp_path, "a.ttl", a)).provenance
        db = import_owl_bundle(_write(tmp_path, "b.ttl", b)).provenance
        assert da["canonicalGraphDigest"] == db["canonicalGraphDigest"]
        assert da["canonicalGraphDigest"].startswith("sha256:")


# ── Exporter vocabulary ──────────────────────────────────────────────────────


class TestExportVocabulary:
    def test_turtle_declares_and_writes_ordered_key_annotations(self, analysis):
        g = rdflib.Graph()
        g.parse(data=export_owl_turtle(analysis), format="turtle")
        phys = rdflib.Namespace(PHYS)
        owl = rdflib.OWL
        for ap in ("primaryKey", "fromColumnOrder", "toColumnOrder", "toTableName",
                   "observedCardinality"):
            assert (phys[ap], rdflib.RDF.type, owl.AnnotationProperty) in g, ap
        rel = rdflib.URIRef("http://arangodb.com/schema/relational#Accounts")
        assert json.loads(str(g.value(rel, phys.primaryKey))) == ["REGION", "ACCOUNT_NO"]
        fk = rdflib.URIRef("http://arangodb.com/schema/relational#Orders_Accounts")
        assert json.loads(str(g.value(fk, phys.fromColumnOrder))) == ["ACCT_REGION", "ACCT_NO"]
        assert json.loads(str(g.value(fk, phys.toColumnOrder))) == ["REGION", "ACCOUNT_NO"]
        assert str(g.value(fk, phys.toTableName)) == "ACCOUNTS"
        # The existing unordered triples are still written for existing consumers.
        assert {str(o) for o in g.objects(fk, phys.fromColumns)} == {"ACCT_REGION", "ACCT_NO"}

    def test_jsonld_writes_ordered_key_annotations(self, analysis):
        doc = export_owl_jsonld(analysis)
        nodes = {n["@id"]: n for n in doc["@graph"]}
        assert json.loads(nodes["Accounts"]["phys:primaryKey"]) == ["REGION", "ACCOUNT_NO"]
        fk = nodes["Orders_Accounts"]
        assert json.loads(fk["phys:fromColumnOrder"]) == ["ACCT_REGION", "ACCT_NO"]
        assert json.loads(fk["phys:toColumnOrder"]) == ["REGION", "ACCOUNT_NO"]
        assert fk["phys:toTableName"] == "ACCOUNTS"


# ── Curated-ontology rules ───────────────────────────────────────────────────


def _crm(extra: str, header: str = "<http://example.org/crm> a owl:Ontology .") -> str:
    return _PREFIXES + header + "\n" + extra


_ACCOUNT_AND_ORDER = """
:Account a owl:Class ; rdfs:label "Account" ; phys:tableName "ACCOUNTS" ;
  phys:primaryKey "[\\"ACCOUNT_ID\\"]" .
:accountId a owl:DatatypeProperty , owl:FunctionalProperty , owl:InverseFunctionalProperty ;
  rdfs:label "accountId" ; rdfs:domain :Account ; rdfs:range xsd:integer ;
  phys:columnName "ACCOUNT_ID" .
:Order a owl:Class ; rdfs:label "Order" ; phys:tableName "ORDERS" .
"""


class TestCuratedRules:
    def test_view_style_is_read_and_contract_valid(self, tmp_path):
        ttl = _write(tmp_path, "v.ttl", _crm("""
:ActiveAccount a owl:Class ; phys:mappingStyle "VIEW" ; phys:tableName "ACTIVE_ACCOUNTS" .
"""))
        bundle = import_owl_bundle(ttl).to_bundle()
        mapping = bundle["physicalMapping"]["entities"]["ActiveAccount"]
        assert mapping["style"] == "VIEW"
        schema = json.loads(_CONTRACT.read_text())
        pm_schema = schema["$defs"]["AnalysisOutput"]["properties"]["physicalMapping"]
        Draft202012Validator({**pm_schema, "$defs": schema["$defs"]}).validate(
            {**bundle["physicalMapping"], "relationships": {}}
        )

    def test_view_style_survives_export_and_import(self, analysis, tmp_path):
        bundle = analysis.to_bundle()
        bundle["physicalMapping"]["entities"]["Accounts"]["style"] = "VIEW"
        ttl = _write(tmp_path, "v.ttl", export_owl_turtle(bundle))
        imported = import_owl_bundle(ttl).to_bundle()["physicalMapping"]["entities"]
        assert imported["Accounts"]["style"] == "VIEW"
        assert imported["Orders"]["style"] == "TABLE"

    def test_unknown_entity_style_is_refused(self, tmp_path):
        ttl = _write(tmp_path, "s.ttl", _crm(':A a owl:Class ; phys:tableName "A" ; '
                                              'phys:mappingStyle "COLLECTION" .'))
        with pytest.raises(OwlImportError, match="COLLECTION"):
            import_owl_bundle(ttl)

    def test_multi_column_fk_without_order_is_refused(self, tmp_path):
        ttl = _write(tmp_path, "fk.ttl", _crm(_ACCOUNT_AND_ORDER + """
:placedBy a owl:ObjectProperty ; rdfs:domain :Order ; rdfs:range :Account ;
  phys:mappingStyle "FOREIGN_KEY" ;
  phys:fromColumns "ACCT_REGION" , "ACCT_NO" ; phys:toColumns "REGION" , "ACCOUNT_NO" .
"""))
        with pytest.raises(OwlImportError, match=r"placedBy.*unordered"):
            import_owl_bundle(ttl)

    def test_single_repeated_column_is_fine_and_to_table_falls_back(self, tmp_path):
        ttl = _write(tmp_path, "fk.ttl", _crm(_ACCOUNT_AND_ORDER + """
:placedBy a owl:ObjectProperty ; rdfs:label "PLACED_BY" ;
  rdfs:domain :Order ; rdfs:range :Account ; phys:observedCardinality "1:N" ;
  phys:fromColumns "ACCOUNT_ID" ; phys:toColumns "ACCOUNT_ID" .
:placedBy phys:fromColumns "ACCOUNT_ID" .
"""))
        bundle = import_owl_bundle(ttl).to_bundle()
        rel = bundle["physicalMapping"]["relationships"]["PLACED_BY"]
        assert rel == {
            "style": "FOREIGN_KEY",
            "fromTable": "ORDERS",
            "fromColumns": ["ACCOUNT_ID"],
            "toTable": "ACCOUNTS",
            "toColumns": ["ACCOUNT_ID"],
        }
        (concept,) = bundle["conceptualSchema"]["relationships"]
        assert concept["term"] == "http://example.org/crm#placedBy"
        assert (concept["fromEntity"], concept["toEntity"]) == ("Order", "Account")
        assert concept["cardinality"] == "1:N"

    def test_order_literal_contradicting_column_literals_is_refused(self, tmp_path):
        ttl = _write(tmp_path, "fk.ttl", _crm(_ACCOUNT_AND_ORDER + """
:placedBy a owl:ObjectProperty ; rdfs:domain :Order ; rdfs:range :Account ;
  phys:fromColumnOrder "[\\"A\\"]" ; phys:fromColumns "B" ;
  phys:toColumnOrder "[\\"ACCOUNT_ID\\"]" .
"""))
        with pytest.raises(OwlImportError, match="disagrees"):
            import_owl_bundle(ttl)

    def test_malformed_json_array_literal_names_the_subject(self, tmp_path):
        ttl = _write(tmp_path, "pk.ttl", _crm("""
:Account a owl:Class ; phys:tableName "ACCOUNTS" ; phys:primaryKey "[ACCOUNT_ID" .
"""))
        with pytest.raises(OwlImportError, match=r"http://example.org/crm#Account.*primaryKey"):
            import_owl_bundle(ttl)

    def test_json_literal_that_is_not_an_array_of_names_is_refused(self, tmp_path):
        ttl = _write(tmp_path, "pk.ttl", _crm("""
:Account a owl:Class ; phys:tableName "ACCOUNTS" ; phys:primaryKey "{\\"a\\": 1}" .
"""))
        with pytest.raises(OwlImportError, match="JSON array"):
            import_owl_bundle(ttl)

    def test_class_without_table_name_is_conceptual_only(self, tmp_path):
        ttl = _write(tmp_path, "c.ttl", _crm(_ACCOUNT_AND_ORDER + """
:Party a owl:Class ; rdfs:label "Party" .
:partyName a owl:DatatypeProperty ; rdfs:domain :Party ; rdfs:range xsd:string ;
  phys:columnName "NAME" .
"""))
        bundle = import_owl_bundle(ttl).to_bundle()
        names = [e["name"] for e in bundle["conceptualSchema"]["entities"]]
        assert names == ["Account", "Order", "Party"]
        assert "Party" not in bundle["physicalMapping"]["entities"]
        party = next(e for e in bundle["conceptualSchema"]["entities"] if e["name"] == "Party")
        assert [p["name"] for p in party["properties"]] == ["partyName"]
        assert any("partyName" in w for w in bundle["metadata"]["warnings"])

    def test_label_wins_over_local_name_and_unique_from_characteristics(self, tmp_path):
        ttl = _write(tmp_path, "c.ttl", _crm(_ACCOUNT_AND_ORDER))
        bundle = import_owl_bundle(ttl).to_bundle()
        account = bundle["conceptualSchema"]["entities"][0]
        assert account["term"] == "http://example.org/crm#Account"
        (prop,) = account["properties"]
        assert prop == {
            "name": "accountId",
            "term": "http://example.org/crm#accountId",
            "type": "integer",
            "unique": True,
        }
        mapping = bundle["physicalMapping"]["entities"]["Account"]
        assert mapping["primaryKey"] == ["ACCOUNT_ID"]
        assert mapping["properties"] == {
            "accountId": {"field": "ACCOUNT_ID", "columnName": "ACCOUNT_ID"}
        }
        assert bundle["metadata"]["source"] == "owl-import"
        assert bundle["metadata"]["analyzerVersion"] == rsa.__version__

    def test_two_classes_reading_as_one_name_are_refused(self, tmp_path):
        ttl = _write(tmp_path, "d.ttl", _crm("""
:A a owl:Class ; rdfs:label "Thing" .
:B a owl:Class ; rdfs:label "Thing" .
"""))
        with pytest.raises(OwlImportError, match="Thing"):
            import_owl_bundle(ttl)

    def test_relative_iri_without_base_is_refused(self, tmp_path):
        ttl = _write(tmp_path, "r.ttl", _crm("<#Local> a owl:Class ."))
        with pytest.raises(OwlImportError, match="relative IRI"):
            import_owl_bundle(ttl)


# ── Imports, provenance ──────────────────────────────────────────────────────


_CORE = _PREFIXES + """
<http://example.org/core> a owl:Ontology ; owl:versionIRI <http://example.org/core/2.0> .
:Account a owl:Class ; phys:tableName "ACCOUNTS" .
"""


class TestImports:
    def test_missing_local_import_is_refused_by_iri(self, tmp_path):
        root = _write(tmp_path, "root.ttl", _crm(
            "", header="<http://example.org/crm> a owl:Ontology ; "
                       "owl:imports <http://example.org/core> ."))
        with pytest.raises(OwlImportError, match=r"<http://example\.org/core>.*never fetched"):
            import_owl_bundle(root)

    @pytest.mark.parametrize("target", ["http://example.org/core", "http://example.org/core/2.0"])
    def test_import_satisfied_by_ontology_iri_or_version_iri(self, tmp_path, target):
        root = _write(tmp_path, "root.ttl", _crm(
            ':Order a owl:Class ; phys:tableName "ORDERS" .',
            header=f'<http://example.org/crm> a owl:Ontology ; owl:imports <{target}> ; '
                   'owl:versionInfo "3.1" .'))
        core = _write(tmp_path, "core.ttl", _CORE)
        result = import_owl_bundle(root, local_imports=[core])
        assert sorted(result.to_bundle()["physicalMapping"]["entities"]) == ["Account", "Order"]
        prov = result.provenance
        assert prov["rootOntologyIri"] == "http://example.org/crm"
        assert prov["ontologyVersion"] == "3.1"
        assert prov["profileVersion"] == "1"
        assert prov["documents"] == [
            {
                "filename": "root.ttl",
                "ontologyIri": "http://example.org/crm",
                "sha256": __import__("hashlib").sha256(root.read_bytes()).hexdigest(),
            },
            {
                "filename": "core.ttl",
                "ontologyIri": "http://example.org/core",
                "sha256": __import__("hashlib").sha256(core.read_bytes()).hexdigest(),
            },
        ]
        assert result.to_bundle()["metadata"]["ontologyImport"]["canonicalGraphDigest"] == (
            prov["canonicalGraphDigest"]
        )

    def test_ontology_version_falls_back_to_version_iri_then_empty(self, tmp_path):
        core = _write(tmp_path, "core.ttl", _CORE)
        assert import_owl_bundle(core).provenance["ontologyVersion"] == (
            "http://example.org/core/2.0"
        )
        plain = _write(tmp_path, "plain.ttl", _crm(""))
        assert import_owl_bundle(plain).provenance["ontologyVersion"] == ""

    def test_supplied_document_outside_the_closure_is_refused(self, tmp_path):
        root = _write(tmp_path, "root.ttl", _crm(""))
        core = _write(tmp_path, "core.ttl", _CORE)
        with pytest.raises(OwlImportError, match="core.ttl"):
            import_owl_bundle(root, local_imports=[core])

    def test_unsupported_suffix_is_refused(self, tmp_path):
        path = _write(tmp_path, "crm.jsonld", "{}")
        with pytest.raises(OwlImportError, match="unsupported"):
            import_owl_bundle(path)

    def test_document_without_ontology_header_is_refused(self, tmp_path):
        path = _write(tmp_path, "crm.ttl", _PREFIXES + ":A a owl:Class .")
        with pytest.raises(OwlImportError, match="no owl:Ontology"):
            import_owl_bundle(path)


# ── Safety ───────────────────────────────────────────────────────────────────


_XXE = """<?xml version="1.0"?>
<!DOCTYPE rdf:RDF [ <!ENTITY secret SYSTEM "file:///etc/passwd"> ]>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
  <owl:Ontology rdf:about="http://example.org/crm"/>
  <owl:Class rdf:about="http://example.org/crm#A"><rdfs:label>&secret;</rdfs:label></owl:Class>
</rdf:RDF>
"""


class TestSafety:
    def test_import_error_is_a_value_error_exported_at_top_level(self):
        assert issubclass(rsa.OwlImportError, ValueError)
        assert rsa.import_owl_bundle is import_owl_bundle

    @pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
    def test_rdfxml_with_doctype_entity_is_refused(self, tmp_path, encoding):
        path = tmp_path / "xxe.owl"
        path.write_bytes(_XXE.replace('"1.0"?>', f'"1.0" encoding="{encoding}"?>', 1)
                         .encode(encoding))
        with pytest.raises(OwlImportError, match="DOCTYPE"):
            import_owl_bundle(path)

    def test_no_network_during_a_full_import(self, analysis, tmp_path, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("network access attempted")

        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        monkeypatch.setattr(socket, "getaddrinfo", refuse)
        monkeypatch.setattr(urllib.request, "urlopen", refuse)

        # An http:// owl:imports satisfied locally, plus an RDF/XML document: the two
        # places a careless parser would reach out.
        ttl = export_owl_turtle(analysis).replace(
            ': a owl:Ontology ;',
            ': a owl:Ontology ;\n  owl:imports <http://example.org/core> ;',
        )
        root = _write(tmp_path, "crm.ttl", ttl)
        core = _to_rdfxml(_write(tmp_path, "core.ttl", _CORE), tmp_path / "core.owl")
        result = import_owl_bundle(root, local_imports=[core])
        assert "Account" in result.to_bundle()["physicalMapping"]["entities"]

        # And an unsatisfied http:// import is refused, not fetched.
        lonely = _write(tmp_path, "lonely.ttl", ttl)
        with pytest.raises(OwlImportError, match="never fetched"):
            import_owl_bundle(lonely)
