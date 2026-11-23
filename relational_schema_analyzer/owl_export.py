"""OWL export (Turtle + JSON-LD) with physical back-link annotations.

Serializes the conceptual schema to OWL, mirroring
``arango-schema-mapper/schema_analyzer/owl_export.py`` (DESIGN §5). Turtle is
hand-built (no rdflib dependency on the core path); the output is standard,
rdflib-parseable, and carries the same ``phys:`` annotation namespace as the
ArangoDB analyzer so ``arango-ontoextract`` ingests it unchanged (success
criterion S4).

Emitted:
- ``owl:Class`` per entity (+ ``rdfs:subClassOf`` for inheritance candidates)
- ``owl:DatatypeProperty`` per column-derived property (``rdfs:domain`` / ``rdfs:range``);
  primary-key / unique properties also get ``owl:FunctionalProperty`` +
  ``owl:InverseFunctionalProperty``
- ``owl:ObjectProperty`` per relationship (``rdfs:domain`` / ``rdfs:range``,
  ``owl:inverseOf``), with functional / inverse-functional from cardinality
- ``phys:*`` annotations pointing back to the source table / column / FK / join table

Default IRIs keep the ``arangodb.com`` host (DESIGN §9.4) for ecosystem parity; both
are overridable via the ``base_iri`` / ``phys_iri`` arguments (CLI ``--iri-base`` /
``--phys-iri-base``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_OWL_BASE_IRI = "http://arangodb.com/schema/relational#"
DEFAULT_OWL_PHYSICAL_IRI = "http://arangodb.com/schema/physical#"

# JSON/conceptual type → XSD datatype (see typemap.pg_type_to_json_type).
_JSON_TO_XSD: dict[str, str] = {
    "integer": "xsd:integer",
    "float": "xsd:decimal",
    "boolean": "xsd:boolean",
    "string": "xsd:string",
    "object": "rdfs:Literal",
    "array": "rdfs:Literal",
}

_PHYS_ANNOTATION_PROPERTIES = (
    "mappingStyle",
    "tableName",
    "schemaName",
    "columnName",
    "fromColumns",
    "toColumns",
    "joinTable",
    "joinFromColumns",
    "joinFromParentColumns",
    "joinToColumns",
    "joinToParentColumns",
)


def _as_bundle(analysis: Any) -> dict[str, Any]:
    """Accept an ``Analysis`` (with ``to_bundle()``) or a bundle dict."""
    if hasattr(analysis, "to_bundle"):
        return analysis.to_bundle()
    if isinstance(analysis, dict):
        return analysis
    raise TypeError("expected an Analysis or a bundle dict")


def _ttl_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _sanitize_iri_local(name: str) -> str:
    """Sanitize a string for use as a Turtle IRI local name (PN_LOCAL)."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if sanitized and sanitized[0].isdigit():
        sanitized = "_" + sanitized
    return sanitized or "_Unknown"


def _cardinality_characteristics(cardinality: str | None) -> tuple[bool, bool]:
    """(functional, inverse_functional) for the emitted domain→range direction.

    Our baseline cardinality is written from the relationship's own direction
    (domain = ``fromEntity``): a normal FK is ``1:N`` and is *functional* (each
    ``from`` row references exactly one ``to`` row); a key/1:1 FK is also
    inverse-functional; an N:M join is neither. (This intentionally differs from
    the ArangoDB analyzer's map, which uses the opposite cardinality convention.)
    """
    return {
        "1:1": (True, True),
        "1:N": (True, False),
        "N:1": (False, True),
        "N:M": (False, False),
    }.get(cardinality or "", (False, False))


def _datatype_local(entity_name: str, prop_name: str) -> str:
    return f"{_sanitize_iri_local(entity_name)}_{_sanitize_iri_local(prop_name)}"


def _subclass_map(cs: dict[str, Any]) -> dict[str, list[str]]:
    """``{entity: [superclass, ...]}`` from both sources of subsumption.

    ``entity["subClassOf"]`` is the deterministic baseline's single shared-PK inference.
    ``subClassOfProposals`` carries the richer taxonomy from ``conceptual-taxonomy`` —
    discriminator values, sibling abstraction, specialization — which can legitimately give
    a class more than one parent, so this returns a list rather than replacing one scalar
    with another.
    """
    out: dict[str, list[str]] = {}

    def add(sub: Any, sup: Any) -> None:
        if isinstance(sub, str) and sub and isinstance(sup, str) and sup and sub != sup:
            supers = out.setdefault(sub, [])
            if sup not in supers:
                supers.append(sup)

    for entity in cs.get("entities") or []:
        if isinstance(entity, dict):
            add(entity.get("name"), entity.get("subClassOf"))

    for proposal in cs.get("subClassOfProposals") or []:
        if isinstance(proposal, dict):
            add(proposal.get("subClass"), proposal.get("superClass"))

    return out


def _column_for(prop_name: str, pm_props: Any) -> str:
    """The source column behind a property: its mapping's ``field``, else its own name.

    Shared with ``r2rml_export`` so the OWL ``phys:columnName`` and the R2RML
    ``rr:column`` cannot name different columns.
    """
    physical = pm_props.get(prop_name) if isinstance(pm_props, dict) else None
    column = physical.get("field") if isinstance(physical, dict) else None
    return column if isinstance(column, str) and column else prop_name


# ── One model, two serializations ───────────────────────────────────────
#
# Turtle and JSON-LD used to walk the bundle separately, and JSON-LD drifted: it dropped
# the join tables' parent columns and the relationships' schema (#53). Both now render
# the same nodes, so a fact can only be missing from both.


@dataclass
class _Iri:
    """A resource: ``:Local`` in this ontology, or a prefixed name such as ``xsd:string``."""

    ref: str


@dataclass
class _Node:
    """One OWL resource, independent of serialization.

    ``head`` is what Turtle writes inside the resource's predicate list (domain, range);
    ``statements`` are written as separate triples, in order. A statement's value is an
    :class:`_Iri`, a literal string, or a list of literals (always a JSON-LD array, even
    with one element). A predicate repeated with single values becomes a JSON-LD array
    only when it repeats, as ``rdfs:subClassOf`` does for a class with several parents.
    """

    local: str
    types: str | list[str]  # a string stays a JSON-LD scalar (owl:Class)
    label: str
    head: list[tuple[str, _Iri]] = field(default_factory=list)
    statements: list[tuple[str, Any]] = field(default_factory=list)

    def literal(self, predicate: str, value: Any) -> None:
        if value:
            self.statements.append((predicate, str(value)))


def _owl_model(data: dict[str, Any]) -> tuple[list[tuple[_Node, list[_Node]]], list[_Node]]:
    """``([(class, [its datatype properties]), ...], [object properties])``."""
    cs = data.get("conceptualSchema") or {}
    pm = data.get("physicalMapping") or {}
    superclasses = _subclass_map(cs)
    pm_entities: dict[str, Any] = pm["entities"] if isinstance(pm.get("entities"), dict) else {}
    pm_rels: dict[str, Any] = (
        pm["relationships"] if isinstance(pm.get("relationships"), dict) else {}
    )

    classes: list[tuple[_Node, list[_Node]]] = []
    for e in cs.get("entities") or []:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"]:
            continue
        name = e["name"]
        node = _Node(_sanitize_iri_local(name), "owl:Class", name)
        for superclass in superclasses.get(name, []):
            node.statements.append(("rdfs:subClassOf", _Iri(f":{_sanitize_iri_local(superclass)}")))
        mapping = pm_entities.get(name)
        if isinstance(mapping, dict):
            node.literal("phys:mappingStyle", mapping.get("style"))
            node.literal("phys:tableName", mapping.get("tableName"))
            node.literal("phys:schemaName", mapping.get("schema"))
        pm_props = mapping.get("properties") if isinstance(mapping, dict) else {}
        table_name = mapping.get("tableName") if isinstance(mapping, dict) else None
        props = [
            _datatype_property_node(name, node.local, prop, pm_props, table_name)
            for prop in e.get("properties") or []
            if isinstance(prop, dict) and isinstance(prop.get("name"), str)
        ]
        classes.append((node, props))

    relationships = [
        _object_property_node(r, pm_rels.get(r["type"]))
        for r in cs.get("relationships") or []
        if isinstance(r, dict) and isinstance(r.get("type"), str) and r["type"]
    ]
    return classes, relationships


def _datatype_property_node(
    entity_name: str, entity_local: str, prop: dict[str, Any], pm_props: Any, table_name: Any
) -> _Node:
    pname = prop["name"]
    types = ["owl:DatatypeProperty"]
    if prop.get("unique"):
        types += ["owl:FunctionalProperty", "owl:InverseFunctionalProperty"]
    node = _Node(
        _datatype_local(entity_name, pname),
        types,
        pname,
        head=[
            ("rdfs:domain", _Iri(f":{entity_local}")),
            ("rdfs:range", _Iri(_JSON_TO_XSD.get(str(prop.get("type")), "xsd:string"))),
        ],
    )
    node.statements.append(("phys:columnName", _column_for(pname, pm_props)))
    node.literal("phys:tableName", table_name)
    return node


def _object_property_node(r: dict[str, Any], mapping: Any) -> _Node:
    rtype = r["type"]
    cardinality = r.get("cardinality")
    functional, inverse_functional = _cardinality_characteristics(
        cardinality if isinstance(cardinality, str) else None
    )
    types = ["owl:ObjectProperty"]
    if functional:
        types.append("owl:FunctionalProperty")
    if inverse_functional:
        types.append("owl:InverseFunctionalProperty")

    node = _Node(_sanitize_iri_local(rtype), types, rtype)
    for predicate, key in (("rdfs:domain", "fromEntity"), ("rdfs:range", "toEntity")):
        if isinstance(r.get(key), str) and r[key]:
            node.head.append((predicate, _Iri(f":{_sanitize_iri_local(r[key])}")))
    if isinstance(cardinality, str):
        node.literal("phys:observedCardinality", cardinality)
    if isinstance(r.get("inverseOf"), str) and r["inverseOf"]:
        node.statements.append(("owl:inverseOf", _Iri(f":{_sanitize_iri_local(r['inverseOf'])}")))

    if isinstance(mapping, dict):
        node.literal("phys:mappingStyle", mapping.get("style"))
        node.literal("phys:joinTable", mapping.get("joinTable"))
        node.literal("phys:tableName", mapping.get("fromTable"))
        node.literal("phys:schemaName", mapping.get("schema"))
        for key in (
            "fromColumns",
            "toColumns",
            "joinFromColumns",
            "joinFromParentColumns",
            "joinToColumns",
            "joinToParentColumns",
        ):
            if isinstance(mapping.get(key), list):
                node.statements.append((f"phys:{key}", [str(c) for c in mapping[key]]))
    return node


def _turtle_node(node: _Node) -> list[str]:
    iri = f":{node.local}"
    types = node.types if isinstance(node.types, str) else ", ".join(node.types)
    out = [f"{iri} a {types} ;", f'  rdfs:label "{_ttl_escape(node.label)}" ;']
    out += [f"  {predicate} {value.ref} ;" for predicate, value in node.head]
    out[-1] = out[-1][: -len(" ;")] + " ."  # close the predicate list
    for predicate, value in node.statements:
        if isinstance(value, _Iri):
            out.append(f"{iri} {predicate} {value.ref} .")
        elif isinstance(value, list):
            out += [f'{iri} {predicate} "{_ttl_escape(v)}" .' for v in value]
        else:
            out.append(f'{iri} {predicate} "{_ttl_escape(value)}" .')
    out.append("")
    return out


def _jsonld_value(value: Any) -> Any:
    if isinstance(value, _Iri):
        return {"@id": value.ref[1:] if value.ref.startswith(":") else value.ref}
    return list(value) if isinstance(value, list) else value


def _jsonld_node(node: _Node) -> dict[str, Any]:
    out: dict[str, Any] = {"@id": node.local, "@type": node.types, "rdfs:label": node.label}
    for predicate, value in node.head:
        out[predicate] = _jsonld_value(value)
    grouped: dict[str, list[Any]] = {}
    for predicate, value in node.statements:
        grouped.setdefault(predicate, []).append(value)
    for predicate, values in grouped.items():
        if len(values) == 1:
            out[predicate] = _jsonld_value(values[0])
        else:
            out[predicate] = [_jsonld_value(v) for v in values]
    return out


def export_owl_turtle(
    analysis: Any,
    *,
    base_iri: str = DEFAULT_OWL_BASE_IRI,
    phys_iri: str = DEFAULT_OWL_PHYSICAL_IRI,
) -> str:
    """Serialize the conceptual schema + physical mapping to OWL Turtle."""
    classes, relationships = _owl_model(_as_bundle(analysis))
    lines: list[str] = [
        f"@prefix : <{base_iri}> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        f"@prefix phys: <{phys_iri}> .",
        "",
        ": a owl:Ontology ;",
        '  rdfs:label "Conceptual Schema" ;',
        '  rdfs:comment "Conceptual schema inferred from a relational physical schema." .',
        "",
    ]
    lines += [f"phys:{ap} a owl:AnnotationProperty ." for ap in _PHYS_ANNOTATION_PROPERTIES]
    lines.append("")
    # Every class first, then every datatype property, then the object properties.
    for cls, _ in classes:
        lines += _turtle_node(cls)
    for _, props in classes:
        for prop in props:
            lines += _turtle_node(prop)
    for rel in relationships:
        lines += _turtle_node(rel)
    return "\n".join(lines).rstrip() + "\n"


def export_owl_jsonld(
    analysis: Any,
    *,
    base_iri: str = DEFAULT_OWL_BASE_IRI,
    phys_iri: str = DEFAULT_OWL_PHYSICAL_IRI,
) -> dict[str, Any]:
    """JSON-LD serialization of the same OWL conceptual model as the Turtle export."""
    classes, relationships = _owl_model(_as_bundle(analysis))
    graph: list[dict[str, Any]] = []
    for cls, props in classes:
        graph.append(_jsonld_node(cls))
        graph += [_jsonld_node(prop) for prop in props]
    graph += [_jsonld_node(rel) for rel in relationships]
    return {
        "@context": {
            "@vocab": base_iri,
            "owl": "http://www.w3.org/2002/07/owl#",
            "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
            "xsd": "http://www.w3.org/2001/XMLSchema#",
            "phys": phys_iri,
        },
        "@graph": graph,
    }
