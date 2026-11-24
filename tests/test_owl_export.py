from __future__ import annotations

import pytest

from relational_schema_analyzer import (
    RelationalSchemaAnalyzer,
    export_owl_jsonld,
    export_owl_turtle,
)
from relational_schema_analyzer.types import Column, ForeignKey, PhysicalSchema, Table

rdflib = pytest.importorskip("rdflib")

REL = "http://arangodb.com/schema/relational#"
PHYS = "http://arangodb.com/schema/physical#"
OWL = "http://www.w3.org/2002/07/owl#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"


def _col(name, dt="integer", nullable=False, pk=False):
    return Column(name=name, data_type=dt, is_nullable=nullable, is_primary_key=pk)


def _basic_schema() -> PhysicalSchema:
    users = Table(
        name="users",
        columns=[_col("id", pk=True), _col("email", "varchar", nullable=True)],
        primary_key=["id"],
    )
    orders = Table(
        name="orders",
        columns=[_col("id", pk=True), _col("user_id")],
        primary_key=["id"],
        foreign_keys=[ForeignKey(column="user_id", foreign_table="users", foreign_column="id")],
    )
    return PhysicalSchema(tables={"users": users, "orders": orders})


def _join_schema() -> PhysicalSchema:
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
    return PhysicalSchema(tables={t.name: t for t in (students, courses, enrollments)})


def _graph(ttl: str):
    g = rdflib.Graph()
    g.parse(data=ttl, format="turtle")
    return g


class TestTurtle:
    def test_parses_as_valid_turtle(self):
        ttl = export_owl_turtle(RelationalSchemaAnalyzer().analyze(_basic_schema()))
        assert len(_graph(ttl)) > 0

    def test_classes_and_object_property_with_domain_range(self):
        g = _graph(export_owl_turtle(RelationalSchemaAnalyzer().analyze(_basic_schema())))
        assert (rdflib.URIRef(REL + "Users"), rdflib.RDF.type, rdflib.URIRef(OWL + "Class")) in g
        rel = rdflib.URIRef(REL + "Orders_Users")
        assert (rel, rdflib.RDF.type, rdflib.URIRef(OWL + "ObjectProperty")) in g
        assert (rel, rdflib.URIRef(RDFS + "domain"), rdflib.URIRef(REL + "Orders")) in g
        assert (rel, rdflib.URIRef(RDFS + "range"), rdflib.URIRef(REL + "Users")) in g

    def test_normal_fk_object_property_is_functional(self):
        g = _graph(export_owl_turtle(RelationalSchemaAnalyzer().analyze(_basic_schema())))
        rel = rdflib.URIRef(REL + "Orders_Users")
        assert (rel, rdflib.RDF.type, rdflib.URIRef(OWL + "FunctionalProperty")) in g
        assert (rel, rdflib.RDF.type,
                rdflib.URIRef(OWL + "InverseFunctionalProperty")) not in g

    def test_pk_datatype_property_is_a_key(self):
        g = _graph(export_owl_turtle(RelationalSchemaAnalyzer().analyze(_basic_schema())))
        pk = rdflib.URIRef(REL + "Users_id")
        assert (pk, rdflib.RDF.type, rdflib.URIRef(OWL + "DatatypeProperty")) in g
        assert (pk, rdflib.RDF.type, rdflib.URIRef(OWL + "FunctionalProperty")) in g
        assert (pk, rdflib.RDF.type, rdflib.URIRef(OWL + "InverseFunctionalProperty")) in g
        assert (pk, rdflib.URIRef(RDFS + "range"),
                rdflib.URIRef("http://www.w3.org/2001/XMLSchema#integer")) in g

    def test_phys_annotations_round_trip_to_source(self):
        """Success criterion S6: phys:* annotations resolve back to table/column/FK."""
        g = _graph(export_owl_turtle(RelationalSchemaAnalyzer().analyze(_basic_schema())))
        table_name = rdflib.URIRef(PHYS + "tableName")
        column_name = rdflib.URIRef(PHYS + "columnName")
        assert (rdflib.URIRef(REL + "Users"), table_name,
                rdflib.Literal("users")) in g
        assert (rdflib.URIRef(REL + "Users_email"), column_name,
                rdflib.Literal("email")) in g
        rel = rdflib.URIRef(REL + "Orders_Users")
        assert (rel, rdflib.URIRef(PHYS + "fromColumns"), rdflib.Literal("user_id")) in g
        assert (rel, rdflib.URIRef(PHYS + "toColumns"), rdflib.Literal("id")) in g

    def test_join_table_relationship_carries_join_annotations(self):
        g = _graph(export_owl_turtle(RelationalSchemaAnalyzer().analyze(_join_schema())))
        rel = rdflib.URIRef(REL + "Students_Courses")
        assert (rel, rdflib.URIRef(PHYS + "mappingStyle"), rdflib.Literal("JOIN_TABLE")) in g
        assert (rel, rdflib.URIRef(PHYS + "joinTable"), rdflib.Literal("enrollments")) in g
        # N:M → neither functional nor inverse-functional.
        assert (rel, rdflib.RDF.type, rdflib.URIRef(OWL + "FunctionalProperty")) not in g

    def test_iri_base_is_configurable(self):
        ttl = export_owl_turtle(
            RelationalSchemaAnalyzer().analyze(_basic_schema()),
            base_iri="http://example.org/c#",
            phys_iri="http://example.org/p#",
        )
        assert "@prefix : <http://example.org/c#> ." in ttl
        assert "@prefix phys: <http://example.org/p#> ." in ttl


class TestJsonLd:
    def test_context_and_graph(self):
        doc = export_owl_jsonld(RelationalSchemaAnalyzer().analyze(_basic_schema()))
        assert doc["@context"]["phys"] == PHYS
        assert doc["@context"]["@vocab"] == REL
        ids = {n["@id"] for n in doc["@graph"]}
        assert {REL + n for n in ("Users", "Orders", "Orders_Users", "Users_id")} <= ids

    def test_jsonld_parses_as_rdf(self):
        doc = export_owl_jsonld(RelationalSchemaAnalyzer().analyze(_basic_schema()))
        import json

        g = rdflib.Graph()
        g.parse(data=json.dumps(doc), format="json-ld")
        assert len(g) > 0


class TestJsonLdStatesWhatTurtleStates:
    """Both formats render one model, so they state the same facts about the same resources.

    JSON-LD used to walk the bundle separately and dropped a join table's parent columns
    and a relationship's schema, which Turtle had (#53). It also wrote ``@id`` as a bare
    local name, a relative IRI that a JSON-LD processor resolves against the document's
    location, so its resources were not Turtle's (#63). Triples are compared whole, with
    full IRIs; only the Turtle-only header (ontology, annotation-property declarations) is
    left out.
    """

    @staticmethod
    def _facts(graph) -> set:
        header = {
            s
            for s, _, o in graph.triples((None, rdflib.RDF.type, None))
            if o in (rdflib.OWL.Ontology, rdflib.OWL.AnnotationProperty)
        }
        return {triple for triple in graph if triple[0] not in header}

    def _both(self, schema, **iris):
        import json

        analysis = RelationalSchemaAnalyzer().analyze(schema)
        jsonld = json.dumps(export_owl_jsonld(analysis, **iris))
        # A base the document must not need: every id has to be absolute already.
        parsed = rdflib.Graph().parse(data=jsonld, format="json-ld", publicID="urn:x-elsewhere:")
        return self._facts(_graph(export_owl_turtle(analysis, **iris))), self._facts(parsed)

    @pytest.mark.parametrize(
        "iris",
        [{}, {"base_iri": "http://example.org/c#", "phys_iri": "http://example.org/p#"}],
        ids=["default-iris", "custom-iris"],
    )
    def test_the_same_resources_whatever_the_document_location(self, iris):
        turtle, jsonld = self._both(_join_schema(), **iris)
        assert jsonld, "nothing parsed"
        assert turtle == jsonld
        base = iris.get("base_iri", REL)
        subjects = {str(s) for s, _, _ in jsonld}
        assert all(s.startswith(base) for s in subjects), subjects - {
            s for s in subjects if s.startswith(base)
        }

    @pytest.mark.parametrize("schema", [_basic_schema, _join_schema], ids=["fk", "join"])
    def test_the_same_facts(self, schema):
        built = schema()
        for table in built.tables.values():
            table.schema_name = "school"  # so relationships carry phys:schemaName
        turtle, jsonld = self._both(built)
        assert turtle - jsonld == set(), "facts only Turtle states"
        assert jsonld - turtle == set(), "facts only JSON-LD states"

    def test_the_join_table_annotations_jsonld_used_to_drop(self):
        built = _join_schema()
        for table in built.tables.values():
            table.schema_name = "school"
        doc = export_owl_jsonld(RelationalSchemaAnalyzer().analyze(built))
        (rel,) = [n for n in doc["@graph"] if n["@id"] == REL + "Students_Courses"]
        assert rel["phys:joinFromParentColumns"] == ["id"]
        assert rel["phys:joinToParentColumns"] == ["id"]
        assert rel["phys:schemaName"] == "school"
