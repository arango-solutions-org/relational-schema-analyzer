"""Relational physical mapping model.

The relational analogue of ``arango-schema-mapper``'s ``schema_analyzer/mapping.py``.
Records the back-reference from each conceptual element to the relational source so
downstream consumers (and OWL ``phys:*`` annotations) can resolve a concept to the
exact table / column / FK it came from (DESIGN §3.3).

Style enums diverge from the ArangoDB analyzer (the documented relational variant):

    Entity:        TABLE | VIEW
    Relationship:  FOREIGN_KEY | JOIN_TABLE

``VIEW`` is read from a curated ontology (``phys:mappingStyle "VIEW"``, see
``owl_import.py``); the baseline analyzer itself emits ``TABLE`` for every entity.

Entity mapping dict shape:
    {"style": "TABLE" | "VIEW", "tableName": str, "schema"?: str,
     "primaryKey": [str],
     "properties": {conceptualName: {"field": str, "sqlType": str,
                                     "nullable": bool, "unique"?: bool}}}

``field`` is the column name (the contract's required back-reference). A bundle read
from OWL carries only ``field`` and an identical ``columnName`` alias: ``sqlType`` and
``nullable`` are not in the ontology.

Relationship mapping dict shapes:
    FOREIGN_KEY: {"style": "FOREIGN_KEY", "fromTable", "fromColumns",
                  "toTable", "toColumns", "enforced"?: False, "inferred"?: True}
    JOIN_TABLE:  {"style": "JOIN_TABLE", "joinTable", "schema"?: str,
                  "joinFromColumns", "joinFromParentColumns",
                  "joinToColumns", "joinToParentColumns",
                  "attributeColumns", "enforced"?: False}

The ``join*ParentColumns`` pairs carry the columns each FK *references* on the
entity table, so a consumer can build a complete join condition (R2RML's
``rr:child`` / ``rr:parent``) instead of assuming the parent is keyed on its PK.

``enforced`` is omitted when the source guarantees referential integrity, so a
missing key always means "trustworthy"; it is ``False`` for informational-only
lakehouse constraints and for inferred relationships.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EntityMappingStyle = Literal["TABLE", "VIEW"]
RelationshipMappingStyle = Literal["FOREIGN_KEY", "JOIN_TABLE"]


@dataclass
class PhysicalMapping:
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    relationships: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "PhysicalMapping":
        return cls()

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PhysicalMapping":
        ent = data.get("entities", {})
        rel = data.get("relationships", {})
        return cls(
            entities=dict(ent) if isinstance(ent, dict) else {},
            relationships=dict(rel) if isinstance(rel, dict) else {},
        )

    def to_json(self) -> dict[str, Any]:
        return {"entities": self.entities, "relationships": self.relationships}

    def get_entity_mapping(self, entity_name: str) -> dict[str, Any] | None:
        return self.entities.get(entity_name)

    def get_relationship_mapping(self, rel_type: str) -> dict[str, Any] | None:
        return self.relationships.get(rel_type)
