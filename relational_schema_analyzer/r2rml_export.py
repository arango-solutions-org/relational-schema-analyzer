"""R2RML export: the conceptual bundle as a W3C R2RML mapping document.

Where :mod:`owl_export` emits the *ontology* (what the concepts are), this emits
the *mapping* (how to reach them in SQL). The two are designed to be used
together — the class and property IRIs here are byte-identical to the ones
``export_owl_turtle`` declares, so pointing an R2RML processor (Ontop, Morph-KGC,
db2triples, …) at both gives you a virtual knowledge graph over the live database
with no hand-written mapping.

Emitted per the R2RML recommendation (https://www.w3.org/TR/r2rml/):

- ``rr:TriplesMap`` per entity, over a schema-qualified ``rr:logicalTable``
- ``rr:subjectMap`` with an IRI ``rr:template`` built from the primary key, typed
  with ``rr:class``; entities with no PK fall back to a blank-node subject
- ``rr:predicateObjectMap`` + ``rr:column`` per datatype property, carrying the
  ``rr:datatype`` implied by the column's SQL type
- ``rr:parentTriplesMap`` + ``rr:joinCondition`` per relationship — a referencing
  object map for a plain FK, and a dedicated TriplesMap over the join table for
  an N:M association

Turtle is hand-built (rdflib stays an optional extra, as in :mod:`owl_export`);
the output is standard and rdflib-parseable.

Three IRI bases are kept separate because they name genuinely different things:
``base_iri`` for ontology terms (shared with the OWL export), ``data_iri`` for the
row-level subject IRIs the mapping mints, and ``mapping_iri`` for the TriplesMap
resources themselves. All three default to the ``arangodb.com`` host for
ecosystem parity (DESIGN §9.4) and are overridable from the CLI.

Known limit: R2RML has no way to attach properties to a relationship, so the
``attributeColumns`` of an N:M join table cannot be expressed without
reification. They are listed in a comment on the generated TriplesMap rather
than silently dropped.
"""

from __future__ import annotations

import re
from typing import Any

from .exports import export_bundle
from .owl_export import (
    DEFAULT_OWL_BASE_IRI,
    _JSON_TO_XSD,
    _column_for,
    _datatype_local,
    _sanitize_iri_local,
    _ttl_escape,
)
from .quoting import quote_ansi

R2RML_NAMESPACE = "http://www.w3.org/ns/r2rml#"
DEFAULT_R2RML_DATA_IRI = "http://arangodb.com/data/"
DEFAULT_R2RML_MAPPING_IRI = "http://arangodb.com/mapping/r2rml#"

# A column name we can drop into an R2RML template or rr:column bare.
_PLAIN_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


_delimit = quote_ansi  # SQL-delimited identifier, doubling any embedded quote


def _strings(value: Any) -> list[str]:
    """The string items of a bundle list; anything else in the bundle gives ``[]``."""
    return [c for c in value if isinstance(c, str)] if isinstance(value, list) else []


def _sql_column(name: str) -> str:
    """Column name for ``rr:column`` — delimited only when it has to be."""
    return name if _PLAIN_IDENT.match(name) else _delimit(name)


def _qualified_table(table: str, schema: str | None) -> str:
    """Schema-qualified, SQL-delimited table name for ``rr:logicalTable``.

    Always delimited: the R2RML processor passes this straight through to SQL,
    and an undelimited mixed-case name silently resolves to the wrong table on
    PostgreSQL (which folds to lower case).
    """
    return f"{_delimit(schema)}.{_delimit(table)}" if schema else _delimit(table)


def _template_ref(column: str) -> str:
    """A ``{column}`` reference inside an R2RML template."""
    return "{" + (column if _PLAIN_IDENT.match(column) else _delimit(column)) + "}"


def _template_literal(text: str) -> str:
    """Escape literal text for an R2RML template (braces are structural)."""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _triples_map_iri(entity_name: str) -> str:
    return f"map:TriplesMap_{_sanitize_iri_local(entity_name)}"


def _subject_template(data_iri: str, entity_name: str, key_columns: list[str]) -> str:
    """``<data_iri><Entity>/{pk1}/{pk2}`` — an IRI unique per row."""
    prefix = _template_literal(f"{data_iri}{_sanitize_iri_local(entity_name)}/")
    return prefix + "/".join(_template_ref(c) for c in key_columns)


def _xsd_for(prop_type: Any) -> str:
    if not isinstance(prop_type, str):
        return "rdfs:Literal"
    return _JSON_TO_XSD.get(prop_type, "rdfs:Literal")


def _subject_map_lines(
    *, entity_name: str, pk: list[str], columns: list[str], data_iri: str
) -> list[str]:
    """Subject map for an entity, blank-node-based when there is no PK."""
    class_iri = f":{_sanitize_iri_local(entity_name)}"
    if pk:
        template = _subject_template(data_iri, entity_name, pk)
        return [
            "    rr:subjectMap [",
            f'        rr:template "{_ttl_escape(template)}" ;',
            f"        rr:class {class_iri}",
            "    ] ;",
        ]
    # No primary key: there is no stable IRI to mint, so fall back to a blank
    # node keyed on every column. RSA already flags this as `missing_primary_key`
    # / reviewRequired — the mapping stays valid, but the subjects are local to
    # each materialization rather than stable identifiers.
    body = _sanitize_iri_local(entity_name) + "_" + "_".join(_template_ref(c) for c in columns)
    return [
        "    # No primary key on this table — subjects are blank nodes and will not",
        "    # be stable across runs. Declare a key to get resolvable IRIs.",
        "    rr:subjectMap [",
        f'        rr:template "{_ttl_escape(body)}" ;',
        "        rr:termType rr:BlankNode ;",
        f"        rr:class {class_iri}",
        "    ] ;",
    ]


def _datatype_pom_lines(
    *, entity_name: str, prop: dict[str, Any], pm_props: dict[str, Any]
) -> list[str]:
    """A ``rr:predicateObjectMap`` for one column-derived property."""
    prop_name = prop.get("name")
    if not isinstance(prop_name, str) or not prop_name:
        return []
    column = _column_for(prop_name, pm_props)
    predicate = f":{_datatype_local(entity_name, prop_name)}"
    return [
        "    rr:predicateObjectMap [",
        f"        rr:predicate {predicate} ;",
        "        rr:objectMap [",
        f'            rr:column "{_ttl_escape(_sql_column(column))}" ;',
        f"            rr:datatype {_xsd_for(prop.get('type'))}",
        "        ]",
        "    ] ;",
    ]


def _join_condition_lines(child_columns: list[str], parent_columns: list[str]) -> list[str]:
    """``rr:joinCondition`` pairs — child (this table) to parent (referenced)."""
    out: list[str] = []
    for child, parent in zip(child_columns, parent_columns):
        out.extend(
            [
                "            rr:joinCondition [",
                f'                rr:child "{_ttl_escape(_sql_column(child))}" ;',
                f'                rr:parent "{_ttl_escape(_sql_column(parent))}"',
                "            ] ;",
            ]
        )
    if out:
        out[-1] = out[-1].rstrip(" ;")
    return out


def _reference_pom_lines(
    *, predicate: str, parent_map: str, child_columns: list[str], parent_columns: list[str]
) -> list[str]:
    """A referencing object map: predicate → parent TriplesMap over a join."""
    lines = [
        "    rr:predicateObjectMap [",
        f"        rr:predicate {predicate} ;",
        "        rr:objectMap [",
        f"            rr:parentTriplesMap {parent_map} ;",
    ]
    conditions = _join_condition_lines(child_columns, parent_columns)
    if conditions:
        lines.extend(conditions)
    else:
        # Nothing to join on — drop the trailing ';' from parentTriplesMap.
        lines[-1] = lines[-1].rstrip(" ;")
    lines.extend(["        ]", "    ] ;"])
    return lines


def _close_block(lines: list[str]) -> None:
    """Turn the trailing ``;`` of a TriplesMap block into a final ``.``."""
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].rstrip()
        if stripped.endswith(";"):
            lines[i] = stripped[:-1].rstrip() + " ."
            return
        if stripped.endswith("."):
            return


def export_r2rml_turtle(
    analysis: Any,
    *,
    base_iri: str = DEFAULT_OWL_BASE_IRI,
    data_iri: str = DEFAULT_R2RML_DATA_IRI,
    mapping_iri: str = DEFAULT_R2RML_MAPPING_IRI,
) -> str:
    """Serialize the conceptual schema + physical mapping to an R2RML document.

    ``base_iri`` must match the OWL export's for the mapping to populate that
    ontology; the defaults already agree.
    """
    data = export_bundle(analysis)
    cs = data.get("conceptualSchema") or {}
    pm = data.get("physicalMapping") or {}
    entities = cs.get("entities") or []
    rels = cs.get("relationships") or []
    raw_entities = pm.get("entities")
    pm_entities: dict[str, Any] = raw_entities if isinstance(raw_entities, dict) else {}
    pm_rels = pm.get("relationships") if isinstance(pm.get("relationships"), dict) else {}

    lines: list[str] = [
        f"@prefix rr: <{R2RML_NAMESPACE}> .",
        f"@prefix : <{base_iri}> .",
        f"@prefix map: <{mapping_iri}> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "",
        "# R2RML mapping generated by relational-schema-analyzer.",
        "# Class and property IRIs match the companion OWL export.",
        "",
    ]

    # Which entities actually became a TriplesMap (a relationship can only
    # reference one that exists) and what key each is addressed by.
    emitted: dict[str, list[str]] = {}
    for e in entities:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"]:
            continue
        mapping = pm_entities.get(e["name"]) if isinstance(pm_entities, dict) else None
        if not isinstance(mapping, dict) or not mapping.get("tableName"):
            continue
        emitted[e["name"]] = _strings(mapping.get("primaryKey"))

    # ── One TriplesMap per entity ─────────────────────────────────────
    for e in entities:
        name = e.get("name") if isinstance(e, dict) else None
        if not isinstance(name, str) or name not in emitted:
            continue
        mapping = pm_entities[name]
        if not isinstance(mapping, dict):
            continue
        pk = emitted[name]
        columns = [
            p["name"]
            for p in (e.get("properties") or [])
            if isinstance(p, dict) and isinstance(p.get("name"), str)
        ]
        raw_props = mapping.get("properties")
        pm_props: dict[str, Any] = raw_props if isinstance(raw_props, dict) else {}

        block = [f"{_triples_map_iri(name)} a rr:TriplesMap ;"]
        table = _qualified_table(str(mapping["tableName"]), mapping.get("schema"))
        block.append(f'    rr:logicalTable [ rr:tableName "{_ttl_escape(table)}" ] ;')
        block.extend(
            _subject_map_lines(
                entity_name=name, pk=pk, columns=columns or [name], data_iri=data_iri
            )
        )
        for prop in e.get("properties") or []:
            if isinstance(prop, dict):
                block.extend(
                    _datatype_pom_lines(entity_name=name, prop=prop, pm_props=pm_props)
                )

        # Plain FK relationships hang off the referencing entity's TriplesMap.
        for r in rels:
            if not isinstance(r, dict) or r.get("fromEntity") != name:
                continue
            rel_type = r.get("type")
            if not isinstance(rel_type, str) or not rel_type:
                continue
            rel_map = pm_rels.get(rel_type) if isinstance(pm_rels, dict) else None
            if not isinstance(rel_map, dict) or rel_map.get("style") != "FOREIGN_KEY":
                continue
            target = r.get("toEntity")
            if not isinstance(target, str) or target not in emitted:
                continue
            child = _strings(rel_map.get("fromColumns"))
            parent = _strings(rel_map.get("toColumns"))
            block.extend(
                _reference_pom_lines(
                    predicate=f":{_sanitize_iri_local(rel_type)}",
                    parent_map=_triples_map_iri(target),
                    child_columns=child,
                    parent_columns=parent,
                )
            )

        _close_block(block)
        lines.extend(block)
        lines.append("")

    # ── One TriplesMap per N:M join table ─────────────────────────────
    for r in rels:
        if not isinstance(r, dict):
            continue
        rel_type = r.get("type")
        if not isinstance(rel_type, str) or not rel_type:
            continue
        rel_map = pm_rels.get(rel_type) if isinstance(pm_rels, dict) else None
        if not isinstance(rel_map, dict) or rel_map.get("style") != "JOIN_TABLE":
            continue
        from_e, to_e = r.get("fromEntity"), r.get("toEntity")
        if not isinstance(from_e, str) or from_e not in emitted:
            continue
        if not isinstance(to_e, str) or to_e not in emitted:
            continue
        join_table = rel_map.get("joinTable")
        if not isinstance(join_table, str) or not join_table:
            continue

        # The subject is the *from* entity, addressed through the join table's
        # own FK columns — the standard R2RML idiom for an association table.
        child_from = _strings(rel_map.get("joinFromColumns"))
        if not child_from:
            continue
        block = [f"{_triples_map_iri(rel_type)}_Link a rr:TriplesMap ;"]
        table = _qualified_table(join_table, rel_map.get("schema"))
        block.append(f'    rr:logicalTable [ rr:tableName "{_ttl_escape(table)}" ] ;')
        template = _subject_template(data_iri, from_e, child_from)
        block.extend(
            [
                "    rr:subjectMap [",
                f'        rr:template "{_ttl_escape(template)}" ;',
                f"        rr:class :{_sanitize_iri_local(from_e)}",
                "    ] ;",
            ]
        )
        block.extend(
            _reference_pom_lines(
                predicate=f":{_sanitize_iri_local(rel_type)}",
                parent_map=_triples_map_iri(to_e),
                child_columns=_strings(rel_map.get("joinToColumns")),
                parent_columns=_strings(rel_map.get("joinToParentColumns")),
            )
        )
        _close_block(block)

        dropped = _strings(rel_map.get("attributeColumns"))
        if dropped:
            block.append(
                "# NOTE: R2RML cannot attach properties to a relationship; "
                f"{join_table} column(s) "
                f"{', '.join(dropped)} are not mapped. Reify the association "
                "(model the join table as an entity) if you need them."
            )
        lines.extend(block)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
