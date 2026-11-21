"""OWL import: a curated ontology with ``phys:`` back-links → the normal analysis bundle.

The inverse of :mod:`owl_export`. A partner who curates an ontology by hand — naming the
concepts, and pointing each one at the table, column and foreign key that holds it — hands
over Turtle or RDF/XML instead of a database. :func:`import_owl_bundle` reads it into the
same ``{conceptualSchema, physicalMapping, metadata}`` bundle the analyzer emits, so a
consumer (``r2g``) reconciles it against a schema snapshot exactly as it would an inferred
analysis. Every element also carries ``term``: the IRI it was read from.

Safety, because the documents come from outside:

- **Local files only.** Nothing is fetched. Each ``owl:imports`` must be satisfied by a
  supplied document whose ontology IRI (or ``owl:versionIRI``) matches it; an unsatisfied
  import is an error naming the IRI, and a supplied document the closure never reaches is
  an error too, rather than being merged in silently.
- **No DTDs.** RDF/XML containing a ``DOCTYPE`` (and so any ``ENTITY`` declaration) is
  refused before rdflib sees it, which rules out external-entity (XXE) reads and
  entity-expansion bombs whatever the document's encoding.
- **No guessed column order.** RDF triples are unordered, so a composite key is read from
  the ordered JSON-array literals (``phys:primaryKey``, ``phys:fromColumnOrder``,
  ``phys:toColumnOrder``). Several repeated ``phys:fromColumns`` literals with no order
  literal are an error, not a guess.
- **Deterministic.** The same closure yields a byte-identical bundle, and a Turtle closure
  and its RDF/XML equivalent yield the same bundle and the same ``canonicalGraphDigest``
  (blank nodes canonicalized, N-Triples sorted, SHA-256).

Requires the ``owl`` extra (rdflib), like the OWL export tests.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .analyzer import Analysis
from .conceptual import ConceptualSchema
from .mapping import PhysicalMapping
from .metadata import GENERATOR
from .owl_export import _JSON_TO_XSD, DEFAULT_OWL_PHYSICAL_IRI

PROFILE_VERSION = "1"
"""Version of the curated-ontology profile (the ``phys:`` vocabulary this reads)."""

_FORMAT_BY_SUFFIX = {".ttl": "turtle", ".owl": "xml", ".rdf": "xml", ".xml": "xml"}

# Every document is parsed against this base, so a relative IRI the document does not
# resolve itself (no ``@base`` / ``xml:base``) lands under it and is refused. Without a
# fixed base rdflib would invent one from the working directory, and the same document
# would yield different IRIs on different machines.
_UNRESOLVED_BASE = "urn:x-relational-schema-analyzer:unresolved-base:"

_XSD = "http://www.w3.org/2001/XMLSchema#"
_RDFS = "http://www.w3.org/2000/01/rdf-schema#"

# XSD → conceptual JSON type: the inverse of the exporter's ``_JSON_TO_XSD`` plus the XSD
# numeric types a hand-curated ontology uses that the exporter never writes. ``rdfs:Literal``
# is the exporter's range for both ``object`` and ``array``; it reads back as ``string``,
# the type that claims least.
_XSD_TO_JSON: dict[str, str] = {
    **{
        xsd.replace("xsd:", _XSD).replace("rdfs:", _RDFS): json_type
        for json_type, xsd in _JSON_TO_XSD.items()
        if xsd != "rdfs:Literal"
    },
    **{
        _XSD + local: "integer"
        for local in (
            "int", "long", "short", "byte", "nonNegativeInteger", "positiveInteger",
            "nonPositiveInteger", "negativeInteger", "unsignedLong", "unsignedInt",
            "unsignedShort", "unsignedByte",
        )
    },
    **{_XSD + local: "float" for local in ("double", "float")},
}

_ENTITY_STYLES = ("TABLE", "VIEW")
_RELATIONSHIP_STYLES = ("FOREIGN_KEY", "JOIN_TABLE")


class OwlImportError(ValueError):
    """A curated ontology bundle cannot be imported safely or unambiguously."""


@dataclass(frozen=True)
class OwlImportResult:
    """What :func:`import_owl_bundle` returns.

    ``analysis`` is an ordinary :class:`Analysis`; ``analysis.to_bundle()`` is the
    bundle. ``provenance`` describes the documents it was read from:
    ``canonicalGraphDigest`` (``"sha256:<hex>"``, identical for equivalent
    serializations), ``rootOntologyIri``, ``ontologyVersion`` (root ``owl:versionInfo``,
    else its ``owl:versionIRI``, else ``""``), ``profileVersion`` and ``documents``
    (``[{filename, ontologyIri, sha256}]``, root first; ``sha256`` is the hex digest of
    the file's bytes).
    """

    analysis: Analysis
    provenance: dict[str, Any]

    def to_bundle(self) -> dict[str, Any]:
        return self.analysis.to_bundle()


@dataclass(frozen=True)
class _Document:
    filename: str
    sha256: str
    graph: Any
    ontology_iri: str
    version_iri: str | None
    imports: tuple[str, ...]


def import_owl_bundle(
    root_path: str | os.PathLike[str],
    *,
    local_imports: Iterable[str | os.PathLike[str]] = (),
    phys_iri: str = DEFAULT_OWL_PHYSICAL_IRI,
) -> OwlImportResult:
    """Read a curated OWL ontology (and its local imports) into an analysis bundle.

    ``root_path`` is the root ontology; ``local_imports`` are the documents that satisfy
    its ``owl:imports`` closure. Turtle is read from ``.ttl``, RDF/XML from ``.owl`` /
    ``.rdf`` / ``.xml``. ``phys_iri`` is the physical-annotation namespace (the
    exporter's ``--phys-iri-base``).

    Raises :class:`OwlImportError` for anything that cannot be imported safely or
    unambiguously; the message names the document, subject or IRI at fault.
    """
    try:
        import rdflib  # noqa: F401 - availability check; the helpers import what they use
    except ImportError as err:  # pragma: no cover - exercised only without the extra
        raise OwlImportError(
            "Importing OWL requires rdflib. Install it with: "
            "pip install 'relational-schema-analyzer[owl]'"
        ) from err

    root = _load_document(Path(root_path))
    supplied = [_load_document(Path(p)) for p in local_imports]
    closure = _import_closure(root, supplied)

    from rdflib import Graph

    merged = Graph()
    for document in closure:
        for triple in document.graph:
            merged.add(triple)

    digest = _canonical_graph_digest(merged)
    ontology_version = _ontology_version(root)
    conceptual, physical, subclass_proposals, warnings = _build(merged, phys_iri)

    ontology_import = {
        "rootOntologyIri": root.ontology_iri,
        "ontologyVersion": ontology_version,
        "canonicalGraphDigest": digest,
        "profileVersion": PROFILE_VERSION,
    }
    metadata: dict[str, Any] = {
        "source": "owl-import",
        "generator": GENERATOR,
        "analyzerVersion": __version__,
        "version": __version__,
        "analyzedCollectionCounts": {
            "documentCollections": len(conceptual["entities"]),
            "edgeCollections": len(conceptual["relationships"]),
        },
        "reviewRequired": bool(warnings),
        "warnings": warnings,
        # Serialization-independent on purpose: per-file names and hashes live in
        # ``provenance`` only, so a Turtle closure and its RDF/XML equivalent produce the
        # same bundle.
        "ontologyImport": ontology_import,
    }
    analysis = Analysis(
        conceptual=ConceptualSchema.from_json(conceptual),
        physical_mapping=PhysicalMapping.from_json(physical),
        metadata=metadata,
        subclass_proposals=subclass_proposals,
    )
    provenance = {
        **ontology_import,
        "documents": [
            {"filename": d.filename, "ontologyIri": d.ontology_iri, "sha256": d.sha256}
            for d in closure
        ],
    }
    return OwlImportResult(analysis=analysis, provenance=provenance)


# ── Reading documents ─────────────────────────────────────────────────────────


def _load_document(path: Path) -> _Document:
    from rdflib import OWL, RDF, BNode, Graph, URIRef

    name = path.name
    fmt = _FORMAT_BY_SUFFIX.get(path.suffix.lower())
    if fmt is None:
        raise OwlImportError(
            f"{name}: unsupported ontology serialization (expected .ttl, .owl, .rdf or .xml)"
        )
    try:
        data = path.read_bytes()
    except OSError as err:
        raise OwlImportError(f"{name}: cannot read ontology document: {err}") from err
    if not data.strip():
        raise OwlImportError(f"{name}: ontology document is empty")
    if fmt == "xml":
        _refuse_doctype(data, name)

    graph = Graph()
    try:
        graph.parse(data=data, format=fmt, publicID=_UNRESOLVED_BASE)
    except Exception as err:  # noqa: BLE001 - rdflib raises many parser-specific types
        kind = "Turtle" if fmt == "turtle" else "RDF/XML"
        raise OwlImportError(f"{name}: not valid {kind}: {err}") from err

    for term in sorted({str(t) for triple in graph for t in triple if isinstance(t, URIRef)}):
        if term.startswith(_UNRESOLVED_BASE):
            raise OwlImportError(
                f"{name}: relative IRI <{term[len(_UNRESOLVED_BASE):]}> has no base; "
                "declare @base / xml:base or use absolute IRIs"
            )

    ontologies = sorted(set(graph.subjects(RDF.type, OWL.Ontology)), key=str)
    if not ontologies:
        raise OwlImportError(f"{name}: declares no owl:Ontology")
    if len(ontologies) > 1:
        listed = ", ".join(f"<{o}>" for o in ontologies)
        raise OwlImportError(f"{name}: declares more than one owl:Ontology ({listed})")
    ontology = ontologies[0]
    if isinstance(ontology, BNode):
        raise OwlImportError(f"{name}: its owl:Ontology has no IRI")

    versions = sorted({str(v) for v in graph.objects(ontology, OWL.versionIRI)})
    if len(versions) > 1:
        raise OwlImportError(f"{name}: <{ontology}> has more than one owl:versionIRI")

    imports: set[str] = set()
    for subject, target in graph.subject_objects(OWL.imports):
        if not isinstance(target, URIRef):
            raise OwlImportError(f"{name}: owl:imports on <{subject}> is not an IRI")
        imports.add(str(target))

    return _Document(
        filename=name,
        sha256=hashlib.sha256(data).hexdigest(),
        graph=graph,
        ontology_iri=str(ontology),
        version_iri=versions[0] if versions else None,
        imports=tuple(sorted(imports)),
    )


class _DoctypeFound(Exception):
    pass


class _DoctypeTrap:
    """SAX lexical handler that stops at the first DOCTYPE (hence any ENTITY)."""

    def startDTD(self, name: str, public_id: str | None, system_id: str | None) -> None:
        raise _DoctypeFound

    def endDTD(self) -> None:
        pass

    def startCDATA(self) -> None:
        pass

    def endCDATA(self) -> None:
        pass

    def comment(self, content: str) -> None:
        pass

    def startEntity(self, name: str) -> None:
        pass

    def endEntity(self, name: str) -> None:
        pass


def _refuse_doctype(data: bytes, name: str) -> None:
    """Refuse RDF/XML that declares a DOCTYPE, before any parser acts on it.

    Done with expat through SAX rather than by searching the bytes, so a UTF-16 document
    cannot hide the declaration. External entities are switched off as well, so even this
    pre-pass cannot read or fetch anything; the handler fires on the declaration itself,
    before any entity it declares could be expanded.
    """
    import xml.sax
    from xml.sax.handler import (
        ContentHandler,
        feature_external_ges,
        feature_external_pes,
        property_lexical_handler,
    )
    from xml.sax.xmlreader import InputSource

    parser = xml.sax.make_parser()
    parser.setFeature(feature_external_ges, False)
    parser.setFeature(feature_external_pes, False)
    parser.setProperty(property_lexical_handler, _DoctypeTrap())
    parser.setContentHandler(ContentHandler())
    source = InputSource()
    source.setByteStream(io.BytesIO(data))
    try:
        parser.parse(source)
    except _DoctypeFound:
        raise OwlImportError(
            f"{name}: RDF/XML with a DOCTYPE or ENTITY declaration is refused "
            "(external entities could read local files or the network)"
        ) from None
    except xml.sax.SAXParseException as err:
        raise OwlImportError(f"{name}: not well-formed XML: {err}") from err


def _import_closure(root: _Document, supplied: list[_Document]) -> list[_Document]:
    """Root first, then each document its ``owl:imports`` reach, in discovery order."""
    by_iri: dict[str, _Document] = {}
    for document in [root, *supplied]:
        for key in (document.ontology_iri, document.version_iri):
            if not key:
                continue
            other = by_iri.get(key)
            if other is not None and other is not document:
                raise OwlImportError(
                    f"<{key}> is declared by both {other.filename} and {document.filename}"
                )
            by_iri[key] = document

    closure: list[_Document] = []
    seen: set[int] = set()
    pending = [root]
    while pending:
        document = pending.pop(0)
        if id(document) in seen:
            continue
        seen.add(id(document))
        closure.append(document)
        for iri in document.imports:
            target = by_iri.get(iri)
            if target is None:
                raise OwlImportError(
                    f"owl:imports <{iri}> (in {document.filename}) is not satisfied by any "
                    "supplied local document; imports are never fetched"
                )
            pending.append(target)

    unused = [d.filename for d in supplied if id(d) not in seen]
    if unused:
        raise OwlImportError(
            f"supplied document(s) not reached by the root's owl:imports: {', '.join(unused)}"
        )
    return closure


def _canonical_graph_digest(graph: Any) -> str:
    """``sha256:`` of the sorted N-Triples of the blank-node-canonicalized graph."""
    from rdflib.compare import to_canonical_graph

    canonical = to_canonical_graph(graph)
    lines = sorted(
        line for line in canonical.serialize(format="nt").splitlines() if line.strip()
    )
    blob = ("\n".join(lines) + "\n").encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _ontology_version(root: _Document) -> str:
    from rdflib import OWL, Literal, URIRef

    infos = sorted(
        str(v)
        for v in root.graph.objects(URIRef(root.ontology_iri), OWL.versionInfo)
        if isinstance(v, Literal)
    )
    if infos:
        return infos[0]
    return root.version_iri or ""


# ── Building the bundle ───────────────────────────────────────────────────────


def _local_name(iri: str) -> str:
    for sep in ("#", "/", ":"):
        if sep in iri:
            tail = iri.rsplit(sep, 1)[1]
            if tail:
                return tail
    return iri


def _display_name(graph: Any, subject: Any) -> str:
    """``rdfs:label`` if present, else the IRI's local name.

    With several labels: an untagged one, else an English one, else the first in sort
    order — so the choice is deterministic and does not depend on parse order.
    """
    from rdflib import RDFS, Literal

    labels = [o for o in graph.objects(subject, RDFS.label) if isinstance(o, Literal)]
    for preferred in (
        [str(o) for o in labels if not o.language],
        [str(o) for o in labels if o.language and o.language.lower().startswith("en")],
        [str(o) for o in labels],
    ):
        candidates = sorted(p for p in preferred if p.strip())
        if candidates:
            return candidates[0].strip()
    return _local_name(str(subject))


def _literals(graph: Any, subject: Any, predicate: Any) -> list[str]:
    from rdflib import Literal

    values: set[str] = set()
    for obj in graph.objects(subject, predicate):
        if not isinstance(obj, Literal):
            raise OwlImportError(
                f"<{subject}>: {_short(predicate)} must be a literal, got <{obj}>"
            )
        values.add(str(obj))
    return sorted(values)


def _single(graph: Any, subject: Any, predicate: Any) -> str | None:
    values = _literals(graph, subject, predicate)
    if len(values) > 1:
        raise OwlImportError(
            f"<{subject}>: {_short(predicate)} has {len(values)} different values "
            f"({', '.join(repr(v) for v in values)})"
        )
    return values[0] if values else None


def _json_list(graph: Any, subject: Any, predicate: Any) -> list[str] | None:
    raw = _single(graph, subject, predicate)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError as err:
        raise OwlImportError(
            f"<{subject}>: {_short(predicate)} is not a JSON array literal: {raw!r}"
        ) from err
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, str) and v for v in value)
    ):
        raise OwlImportError(
            f"<{subject}>: {_short(predicate)} must be a non-empty JSON array of column "
            f"names, got {raw!r}"
        )
    if len(set(value)) != len(value):
        raise OwlImportError(f"<{subject}>: {_short(predicate)} repeats a column: {raw!r}")
    return list(value)


def _ordered_columns(
    graph: Any, subject: Any, order_predicate: Any | None, columns_predicate: Any
) -> list[str]:
    """Ordered columns: the JSON-array literal if present, else at most one repeated literal."""
    unordered = _literals(graph, subject, columns_predicate)
    ordered = _json_list(graph, subject, order_predicate) if order_predicate is not None else None
    if ordered is not None:
        if unordered and set(unordered) != set(ordered):
            raise OwlImportError(
                f"<{subject}>: {_short(order_predicate)} {ordered} disagrees with "
                f"{_short(columns_predicate)} {unordered}"
            )
        return ordered
    if len(unordered) > 1:
        hint = f" and no {_short(order_predicate)}" if order_predicate is not None else ""
        raise OwlImportError(
            f"<{subject}>: {len(unordered)} {_short(columns_predicate)} values{hint}; RDF "
            "triples are unordered, so the column order cannot be recovered"
        )
    return unordered


def _short(predicate: Any) -> str:
    return f"phys:{_local_name(str(predicate))}"


def _named_classes(graph: Any) -> list[Any]:
    from rdflib import OWL, RDF, RDFS, URIRef

    excluded = {OWL.Thing, OWL.Nothing}
    found = {
        s
        for cls in (OWL.Class, RDFS.Class)
        for s in graph.subjects(RDF.type, cls)
        if isinstance(s, URIRef) and s not in excluded
    }
    return sorted(found, key=str)


def _class_refs(
    graph: Any, subject: Any, predicate: Any, classes: dict[Any, str]
) -> tuple[list[Any], bool]:
    """Named classes a ``rdfs:domain`` / ``rdfs:range`` points at, and whether any was
    left unresolved (a restriction, an intersection, an undeclared IRI)."""
    from rdflib import OWL, BNode
    from rdflib.collection import Collection

    out: set[Any] = set()
    unresolved = False
    for obj in graph.objects(subject, predicate):
        if obj == OWL.Thing:
            continue
        if obj in classes:
            out.add(obj)
            continue
        union = graph.value(obj, OWL.unionOf) if isinstance(obj, BNode) else None
        if union is not None:
            members = list(Collection(graph, union))
            if members and all(m in classes for m in members):
                out.update(members)
                continue
        unresolved = True
    return sorted(out, key=str), unresolved


def _unique_names(kind: str, iris: list[Any], names: dict[Any, str]) -> None:
    seen: dict[str, Any] = {}
    for iri in iris:
        name = names[iri]
        if name in seen:
            raise OwlImportError(
                f"{kind} <{seen[name]}> and <{iri}> both read as {name!r}; give one a "
                "distinct rdfs:label"
            )
        seen[name] = iri


def _build(
    graph: Any, phys_iri: str
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[str]]:
    from rdflib import OWL, RDF, RDFS, Namespace, URIRef

    phys = Namespace(phys_iri)
    warnings: list[str] = []

    # ── Classes ──
    class_iris = _named_classes(graph)
    class_names = {iri: _display_name(graph, iri) for iri in class_iris}
    _unique_names("classes", class_iris, class_names)

    entities: dict[Any, dict[str, Any]] = {}
    pm_entities: dict[str, dict[str, Any]] = {}
    subclass_proposals: list[dict[str, Any]] = []
    for iri in class_iris:
        name = class_names[iri]
        entity: dict[str, Any] = {
            "name": name,
            "term": str(iri),
            "labels": [name],
            "properties": [],
            "source": "human",
        }
        supers = sorted(
            class_names[s] for s in graph.objects(iri, RDFS.subClassOf) if s in class_names
        )
        if len(supers) == 1:
            entity["subClassOf"] = supers[0]
        for superclass in supers if len(supers) > 1 else []:
            subclass_proposals.append(
                {"subClass": name, "superClass": superclass, "source": "human"}
            )
        entities[iri] = entity

        table = _single(graph, iri, phys.tableName)
        style = _single(graph, iri, phys.mappingStyle)
        schema = _single(graph, iri, phys.schemaName)
        primary_key = _json_list(graph, iri, phys.primaryKey)
        if table is None:
            if style is not None or schema is not None or primary_key is not None:
                raise OwlImportError(
                    f"<{iri}>: has physical annotations but no phys:tableName"
                )
            continue  # conceptual only
        style = (style or "TABLE").upper()
        if style not in _ENTITY_STYLES:
            raise OwlImportError(
                f"<{iri}>: phys:mappingStyle {style!r} is not one of {list(_ENTITY_STYLES)}"
            )
        mapping: dict[str, Any] = {"style": style, "tableName": table}
        if schema:
            mapping["schema"] = schema
        if primary_key is not None:
            mapping["primaryKey"] = primary_key
        mapping["properties"] = {}
        pm_entities[name] = mapping

    # ── Datatype properties ──
    unattached: list[dict[str, Any]] = []
    datatype_iris = sorted(
        {s for s in graph.subjects(RDF.type, OWL.DatatypeProperty) if isinstance(s, URIRef)},
        key=str,
    )
    characteristics = {
        s: (
            (s, RDF.type, OWL.FunctionalProperty) in graph,
            (s, RDF.type, OWL.InverseFunctionalProperty) in graph,
        )
        for s in datatype_iris
    }
    for iri in datatype_iris:
        pname = _display_name(graph, iri)
        prop: dict[str, Any] = {"name": pname, "term": str(iri), "type": _json_type(graph, iri)}
        functional, inverse_functional = characteristics[iri]
        if functional and inverse_functional:
            prop["unique"] = True
        column = _single(graph, iri, phys.columnName)
        prop_table = _single(graph, iri, phys.tableName)
        domains, unresolved = _class_refs(graph, iri, RDFS.domain, class_names)
        if not domains:
            unattached.append(prop)
            if unresolved or column is not None:
                warnings.append(
                    f"datatype property <{iri}> has no named rdfs:domain class; listed "
                    "under conceptualSchema.properties and not physically mapped"
                )
            continue
        placed = False
        for domain in domains:
            entity = entities[domain]
            if any(p["name"] == pname for p in entity["properties"]):
                raise OwlImportError(
                    f"class <{domain}> has two datatype properties named {pname!r}"
                )
            entity["properties"].append(dict(prop))
            entity_mapping = pm_entities.get(entity["name"])
            if column is None or entity_mapping is None:
                continue
            if prop_table is not None and prop_table != entity_mapping["tableName"]:
                continue
            for other, other_mapping in entity_mapping["properties"].items():
                if other_mapping["field"] == column:
                    raise OwlImportError(
                        f"column {entity_mapping['tableName']}.{column} is mapped by both "
                        f"{other!r} and {pname!r} on class <{domain}>"
                    )
            entity_mapping["properties"][pname] = {"field": column, "columnName": column}
            placed = True
        if column is not None and not placed:
            if prop_table is not None:
                raise OwlImportError(
                    f"<{iri}>: phys:tableName {prop_table!r} matches no rdfs:domain "
                    "class's phys:tableName"
                )
            warnings.append(
                f"datatype property <{iri}> has phys:columnName but its domain class has no "
                "phys:tableName; not physically mapped"
            )

    # ── Object properties ──
    object_iris = sorted(
        {s for s in graph.subjects(RDF.type, OWL.ObjectProperty) if isinstance(s, URIRef)},
        key=str,
    )
    object_names = {iri: _display_name(graph, iri) for iri in object_iris}
    _unique_names("object properties", object_iris, object_names)
    physical_predicates = {
        phys[p]
        for p in (
            "mappingStyle", "tableName", "schemaName", "fromColumns", "toColumns",
            "fromColumnOrder", "toColumnOrder", "toTableName", "joinTable",
            "joinFromColumns", "joinFromParentColumns", "joinToColumns",
            "joinToParentColumns",
        )
    }
    relationships: list[dict[str, Any]] = []
    pm_relationships: dict[str, dict[str, Any]] = {}
    for iri in object_iris:
        rtype = object_names[iri]
        has_physical = any(
            True for p in physical_predicates for _ in graph.objects(iri, p)
        )
        domains, _ = _class_refs(graph, iri, RDFS.domain, class_names)
        ranges, _ = _class_refs(graph, iri, RDFS.range, class_names)
        if len(domains) != 1 or len(ranges) != 1:
            problem = (
                f"object property <{iri}> needs exactly one named rdfs:domain and one named "
                f"rdfs:range class (found {len(domains)} and {len(ranges)})"
            )
            if has_physical:
                raise OwlImportError(problem + " to place its physical mapping")
            warnings.append(problem + "; skipped")
            continue
        relationship: dict[str, Any] = {
            "type": rtype,
            "term": str(iri),
            "fromEntity": class_names[domains[0]],
            "toEntity": class_names[ranges[0]],
            "properties": [],
            "source": "human",
        }
        cardinality = _single(graph, iri, phys.observedCardinality)
        if cardinality:
            relationship["cardinality"] = cardinality
        inverses = sorted(
            object_names[o] for o in graph.objects(iri, OWL.inverseOf) if o in object_names
        )
        if inverses:
            relationship["inverseOf"] = inverses[0]
        relationships.append(relationship)
        if has_physical:
            pm_relationships[rtype] = _relationship_mapping(
                graph, iri, phys, pm_entities.get(relationship["fromEntity"]),
                pm_entities.get(relationship["toEntity"]),
            )

    conceptual = {
        "entities": sorted(entities.values(), key=lambda e: e["name"]),
        "relationships": sorted(relationships, key=lambda r: r["type"]),
        "properties": sorted(unattached, key=lambda p: (p["name"], p["term"])),
    }
    for entity in conceptual["entities"]:
        entity["properties"].sort(key=lambda p: p["name"])
    physical = {
        "entities": {
            name: {**m, "properties": dict(sorted(m["properties"].items()))}
            for name, m in sorted(pm_entities.items())
        },
        "relationships": dict(sorted(pm_relationships.items())),
    }
    subclass_proposals.sort(key=lambda p: (p["subClass"], p["superClass"]))
    return conceptual, physical, subclass_proposals, sorted(warnings)


def _json_type(graph: Any, iri: Any) -> str:
    from rdflib import RDFS

    types = {_XSD_TO_JSON.get(str(r), "string") for r in graph.objects(iri, RDFS.range)}
    return types.pop() if len(types) == 1 else "string"


def _relationship_mapping(
    graph: Any,
    iri: Any,
    phys: Any,
    from_mapping: dict[str, Any] | None,
    to_mapping: dict[str, Any] | None,
) -> dict[str, Any]:
    style = _single(graph, iri, phys.mappingStyle)
    join_table = _single(graph, iri, phys.joinTable)
    style = (style or ("JOIN_TABLE" if join_table else "FOREIGN_KEY")).upper()
    if style not in _RELATIONSHIP_STYLES:
        raise OwlImportError(
            f"<{iri}>: phys:mappingStyle {style!r} is not one of {list(_RELATIONSHIP_STYLES)}"
        )
    schema = _single(graph, iri, phys.schemaName)

    if style == "JOIN_TABLE":
        if not join_table:
            raise OwlImportError(f"<{iri}>: JOIN_TABLE mapping has no phys:joinTable")
        mapping: dict[str, Any] = {"style": style, "joinTable": join_table}
        if schema:
            mapping["schema"] = schema
        for key in (
            "joinFromColumns", "joinFromParentColumns", "joinToColumns", "joinToParentColumns",
        ):
            columns = _ordered_columns(graph, iri, None, phys[key])
            if columns:
                mapping[key] = columns
        return mapping

    from_table = _single(graph, iri, phys.tableName) or (
        from_mapping.get("tableName") if from_mapping else None
    )
    to_table = _single(graph, iri, phys.toTableName) or (
        to_mapping.get("tableName") if to_mapping else None
    )
    from_columns = _ordered_columns(graph, iri, phys.fromColumnOrder, phys.fromColumns)
    to_columns = _ordered_columns(graph, iri, phys.toColumnOrder, phys.toColumns)
    if not from_table:
        raise OwlImportError(
            f"<{iri}>: FOREIGN_KEY mapping has no phys:tableName and its domain class has none"
        )
    if not to_table:
        raise OwlImportError(
            f"<{iri}>: FOREIGN_KEY mapping has no phys:toTableName and its range class has none"
        )
    if not from_columns or not to_columns:
        raise OwlImportError(
            f"<{iri}>: FOREIGN_KEY mapping needs both from and to columns "
            "(phys:fromColumnOrder / phys:toColumnOrder)"
        )
    if len(from_columns) != len(to_columns):
        raise OwlImportError(
            f"<{iri}>: FOREIGN_KEY has {len(from_columns)} from column(s) but "
            f"{len(to_columns)} to column(s)"
        )
    mapping = {
        "style": style,
        "fromTable": from_table,
        "fromColumns": from_columns,
        "toTable": to_table,
        "toColumns": to_columns,
    }
    if schema:
        mapping["schema"] = schema
    return mapping
