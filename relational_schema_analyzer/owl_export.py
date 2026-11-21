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
- ``phys:*`` annotations pointing back to the source table / column / FK / join table,
  including the ordered-key annotations ``phys:primaryKey``, ``phys:fromColumnOrder`` and
  ``phys:toColumnOrder`` (one JSON-array literal each) plus ``phys:toTableName``, so that
  :func:`relational_schema_analyzer.owl_import.import_owl_bundle` can read the physical
  mapping back exactly (README "Curated-ontology vocabulary" lists every predicate)

Default IRIs keep the ``arangodb.com`` host (DESIGN §9.4) for ecosystem parity; both
are overridable via the ``base_iri`` / ``phys_iri`` arguments (CLI ``--iri-base`` /
``--phys-iri-base``).
"""

from __future__ import annotations

import json
import re
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
    "observedCardinality",
    # Ordered-key vocabulary (read back by ``owl_import.import_owl_bundle``). RDF triples
    # are unordered, so the repeated ``fromColumns`` / ``toColumns`` literals above cannot
    # say which column of a composite key comes first; these carry the order as one
    # JSON-array literal each. Additive: every triple a consumer already reads is kept.
    "primaryKey",
    "fromColumnOrder",
    "toColumnOrder",
    "toTableName",
)


def _json_array_literal(values: list[Any]) -> str:
    """One JSON array of strings, compact, for the ordered ``phys:*`` annotations."""
    return json.dumps([str(v) for v in values], ensure_ascii=False, separators=(",", ":"))


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


def export_owl_turtle(
    analysis: Any,
    *,
    base_iri: str = DEFAULT_OWL_BASE_IRI,
    phys_iri: str = DEFAULT_OWL_PHYSICAL_IRI,
) -> str:
    """Serialize the conceptual schema + physical mapping to OWL Turtle."""
    data = _as_bundle(analysis)
    cs = data.get("conceptualSchema") or {}
    pm = data.get("physicalMapping") or {}
    entities = cs.get("entities") or []
    superclasses = _subclass_map(cs)
    rels = cs.get("relationships") or []
    pm_entities = pm.get("entities") if isinstance(pm.get("entities"), dict) else {}
    pm_rels = pm.get("relationships") if isinstance(pm.get("relationships"), dict) else {}

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
    for ap in _PHYS_ANNOTATION_PROPERTIES:
        lines.append(f"phys:{ap} a owl:AnnotationProperty .")
    lines.append("")

    # ── Classes + datatype properties ─────────────────────────────────
    datatype_props: list[str] = []
    for e in entities:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"]:
            continue
        name = e["name"]
        iri = f":{_sanitize_iri_local(name)}"
        lines.append(f"{iri} a owl:Class ;")
        lines.append(f'  rdfs:label "{_ttl_escape(name)}" .')
        for superclass in superclasses.get(name, []):
            lines.append(f"{iri} rdfs:subClassOf :{_sanitize_iri_local(superclass)} .")

        mapping = pm_entities.get(name) if isinstance(pm_entities, dict) else None
        pm_props = mapping.get("properties") if isinstance(mapping, dict) else {}
        if isinstance(mapping, dict):
            if mapping.get("style"):
                lines.append(f'{iri} phys:mappingStyle "{_ttl_escape(str(mapping["style"]))}" .')
            if mapping.get("tableName"):
                lines.append(f'{iri} phys:tableName "{_ttl_escape(str(mapping["tableName"]))}" .')
            if mapping.get("schema"):
                lines.append(f'{iri} phys:schemaName "{_ttl_escape(str(mapping["schema"]))}" .')
            primary_key = mapping.get("primaryKey")
            if isinstance(primary_key, list) and primary_key:
                literal = _ttl_escape(_json_array_literal(primary_key))
                lines.append(f'{iri} phys:primaryKey "{literal}" .')
        lines.append("")

        for prop in e.get("properties") or []:
            if not isinstance(prop, dict) or not isinstance(prop.get("name"), str):
                continue
            datatype_props.extend(
                _datatype_property_lines(
                    entity_name=name,
                    entity_iri=iri,
                    prop=prop,
                    pm_props=pm_props if isinstance(pm_props, dict) else {},
                    table_name=mapping.get("tableName") if isinstance(mapping, dict) else None,
                )
            )

    lines.extend(datatype_props)

    # ── Object properties (relationships) ─────────────────────────────
    for r in rels:
        if not isinstance(r, dict) or not isinstance(r.get("type"), str) or not r["type"]:
            continue
        lines.extend(
            _object_property_lines(
                r,
                pm_rels.get(r["type"]) if isinstance(pm_rels, dict) else None,
            )
        )

    return "\n".join(lines).rstrip() + "\n"


def _datatype_property_lines(
    *,
    entity_name: str,
    entity_iri: str,
    prop: dict[str, Any],
    pm_props: dict[str, Any],
    table_name: str | None,
) -> list[str]:
    pname = prop["name"]
    local = _datatype_local(entity_name, pname)
    iri = f":{local}"
    xsd = _JSON_TO_XSD.get(str(prop.get("type")), "xsd:string")
    is_key = bool(prop.get("unique"))

    types = ["owl:DatatypeProperty"]
    if is_key:
        types += ["owl:FunctionalProperty", "owl:InverseFunctionalProperty"]

    out = [
        f"{iri} a {', '.join(types)} ;",
        f'  rdfs:label "{_ttl_escape(pname)}" ;',
        f"  rdfs:domain {entity_iri} ;",
        f"  rdfs:range {xsd} .",
    ]
    field = None
    pm_prop = pm_props.get(pname) if isinstance(pm_props, dict) else None
    if isinstance(pm_prop, dict):
        field = pm_prop.get("field")
    column = field if isinstance(field, str) and field else pname
    out.append(f'{iri} phys:columnName "{_ttl_escape(column)}" .')
    if table_name:
        out.append(f'{iri} phys:tableName "{_ttl_escape(str(table_name))}" .')
    out.append("")
    return out


def _object_property_lines(r: dict[str, Any], mapping: dict[str, Any] | None) -> list[str]:
    rtype = r["type"]
    iri = f":{_sanitize_iri_local(rtype)}"
    from_e = r.get("fromEntity")
    to_e = r.get("toEntity")
    cardinality = r.get("cardinality")
    functional, inverse_functional = _cardinality_characteristics(
        cardinality if isinstance(cardinality, str) else None
    )

    types = ["owl:ObjectProperty"]
    if functional:
        types.append("owl:FunctionalProperty")
    if inverse_functional:
        types.append("owl:InverseFunctionalProperty")

    out = [f"{iri} a {', '.join(types)} ;", f'  rdfs:label "{_ttl_escape(rtype)}" ;']
    if isinstance(from_e, str) and from_e:
        out.append(f"  rdfs:domain :{_sanitize_iri_local(from_e)} ;")
    if isinstance(to_e, str) and to_e:
        out.append(f"  rdfs:range :{_sanitize_iri_local(to_e)} ;")
    # Close the predicate-list block.
    out[-1] = out[-1].rstrip(" ;") + " ."

    if isinstance(cardinality, str) and cardinality:
        out.append(f'{iri} phys:observedCardinality "{_ttl_escape(cardinality)}" .')
    if isinstance(r.get("inverseOf"), str) and r["inverseOf"]:
        out.append(f"{iri} owl:inverseOf :{_sanitize_iri_local(r['inverseOf'])} .")

    if isinstance(mapping, dict):
        if mapping.get("style"):
            out.append(f'{iri} phys:mappingStyle "{_ttl_escape(str(mapping["style"]))}" .')
        for key, phys in (("joinTable", "joinTable"),):
            if mapping.get(key):
                out.append(f'{iri} phys:{phys} "{_ttl_escape(str(mapping[key]))}" .')
        if mapping.get("fromTable"):
            out.append(f'{iri} phys:tableName "{_ttl_escape(str(mapping["fromTable"]))}" .')
        if mapping.get("toTable"):
            out.append(f'{iri} phys:toTableName "{_ttl_escape(str(mapping["toTable"]))}" .')
        if mapping.get("schema"):
            out.append(f'{iri} phys:schemaName "{_ttl_escape(str(mapping["schema"]))}" .')
        for key, phys in (
            ("fromColumns", "fromColumns"),
            ("toColumns", "toColumns"),
            ("joinFromColumns", "joinFromColumns"),
            ("joinFromParentColumns", "joinFromParentColumns"),
            ("joinToColumns", "joinToColumns"),
            ("joinToParentColumns", "joinToParentColumns"),
        ):
            value = mapping.get(key)
            if isinstance(value, list):
                for col in value:
                    out.append(f'{iri} phys:{phys} "{_ttl_escape(str(col))}" .')
        for key, phys in (("fromColumns", "fromColumnOrder"), ("toColumns", "toColumnOrder")):
            value = mapping.get(key)
            if isinstance(value, list) and value:
                literal = _ttl_escape(_json_array_literal(value))
                out.append(f'{iri} phys:{phys} "{literal}" .')
    out.append("")
    return out


def export_owl_jsonld(
    analysis: Any,
    *,
    base_iri: str = DEFAULT_OWL_BASE_IRI,
    phys_iri: str = DEFAULT_OWL_PHYSICAL_IRI,
) -> dict[str, Any]:
    """JSON-LD serialization of the same OWL conceptual model as the Turtle export."""
    data = _as_bundle(analysis)
    cs = data.get("conceptualSchema") or {}
    pm = data.get("physicalMapping") or {}
    entities = cs.get("entities") or []
    superclasses = _subclass_map(cs)
    rels = cs.get("relationships") or []
    pm_entities = pm.get("entities") if isinstance(pm.get("entities"), dict) else {}
    pm_rels = pm.get("relationships") if isinstance(pm.get("relationships"), dict) else {}

    graph: list[dict[str, Any]] = []

    for e in entities:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"]:
            continue
        name = e["name"]
        node: dict[str, Any] = {
            "@id": _sanitize_iri_local(name),
            "@type": "owl:Class",
            "rdfs:label": name,
        }
        supers = superclasses.get(name, [])
        if len(supers) == 1:
            node["rdfs:subClassOf"] = {"@id": _sanitize_iri_local(supers[0])}
        elif supers:
            node["rdfs:subClassOf"] = [{"@id": _sanitize_iri_local(s)} for s in supers]
        mapping = pm_entities.get(name) if isinstance(pm_entities, dict) else None
        if isinstance(mapping, dict):
            if mapping.get("style"):
                node["phys:mappingStyle"] = str(mapping["style"])
            if mapping.get("tableName"):
                node["phys:tableName"] = str(mapping["tableName"])
            if mapping.get("schema"):
                node["phys:schemaName"] = str(mapping["schema"])
            primary_key = mapping.get("primaryKey")
            if isinstance(primary_key, list) and primary_key:
                node["phys:primaryKey"] = _json_array_literal(primary_key)
        graph.append(node)

        pm_props = mapping.get("properties") if isinstance(mapping, dict) else {}
        for prop in e.get("properties") or []:
            if not isinstance(prop, dict) or not isinstance(prop.get("name"), str):
                continue
            pname = prop["name"]
            types = ["owl:DatatypeProperty"]
            if prop.get("unique"):
                types += ["owl:FunctionalProperty", "owl:InverseFunctionalProperty"]
            dnode: dict[str, Any] = {
                "@id": _datatype_local(name, pname),
                "@type": types,
                "rdfs:label": pname,
                "rdfs:domain": {"@id": _sanitize_iri_local(name)},
                "rdfs:range": {"@id": _JSON_TO_XSD.get(str(prop.get("type")), "xsd:string")},
            }
            field = None
            if isinstance(pm_props, dict) and isinstance(pm_props.get(pname), dict):
                field = pm_props[pname].get("field")
            dnode["phys:columnName"] = field if isinstance(field, str) and field else pname
            if isinstance(mapping, dict) and mapping.get("tableName"):
                dnode["phys:tableName"] = str(mapping["tableName"])
            graph.append(dnode)

    for r in rels:
        if not isinstance(r, dict) or not isinstance(r.get("type"), str) or not r["type"]:
            continue
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
        node = {"@id": _sanitize_iri_local(rtype), "@type": types, "rdfs:label": rtype}
        if isinstance(r.get("fromEntity"), str) and r["fromEntity"]:
            node["rdfs:domain"] = {"@id": _sanitize_iri_local(r["fromEntity"])}
        if isinstance(r.get("toEntity"), str) and r["toEntity"]:
            node["rdfs:range"] = {"@id": _sanitize_iri_local(r["toEntity"])}
        if isinstance(cardinality, str) and cardinality:
            node["phys:observedCardinality"] = cardinality
        if isinstance(r.get("inverseOf"), str) and r["inverseOf"]:
            node["owl:inverseOf"] = {"@id": _sanitize_iri_local(r["inverseOf"])}
        mapping = pm_rels.get(rtype) if isinstance(pm_rels, dict) else None
        if isinstance(mapping, dict):
            if mapping.get("style"):
                node["phys:mappingStyle"] = str(mapping["style"])
            for key in ("joinTable",):
                if mapping.get(key):
                    node[f"phys:{key}"] = str(mapping[key])
            for key in ("fromColumns", "toColumns", "joinFromColumns", "joinToColumns"):
                if isinstance(mapping.get(key), list):
                    node[f"phys:{key}"] = [str(c) for c in mapping[key]]
            if mapping.get("fromTable"):
                node["phys:tableName"] = str(mapping["fromTable"])
            if mapping.get("toTable"):
                node["phys:toTableName"] = str(mapping["toTable"])
            for key, phys in (("fromColumns", "fromColumnOrder"), ("toColumns", "toColumnOrder")):
                if isinstance(mapping.get(key), list) and mapping[key]:
                    node[f"phys:{phys}"] = _json_array_literal(mapping[key])
        graph.append(node)

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
