"""Class-abstraction discovery, delegated to ``conceptual-taxonomy``.

The mirror of ``schema_analyzer.taxonomy`` in the ArangoDB analyzer. Both assemble the
inputs their own paradigm knows about and hand them to one shared implementation, so the
same taxonomy comes out whichever side analyzed the data — which is the whole point, since a
divergence would mean physical structure leaking into the conceptual layer.

One asymmetry worth naming: in ArangoDB, class-table inheritance has to be *measured* — the
analyzer probes whether one collection's ``_key`` set is a subset of another's. Relationally
it is **declared**: a child table whose primary key is also a foreign key to its parent says
so in the constraint. Containment is 1.0 by definition, no sampling, no probe budget.

Optional dependency, like every other optional capability here: absent it, discovery is
skipped rather than fatal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional

from .discriminator import (
    DiscriminatorCandidate,
    detect_discriminators,
    shared_pk_children,  # re-exported: AOE imports it from here
    specialization_parents,
)
from .types import Schema

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard
    from conceptual_taxonomy import (
        Discriminator,
        KeyContainment,
        SpecializationMeasurement,
        discover_abstractions,
    )

    TAXONOMY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via the degradation test
    TAXONOMY_AVAILABLE = False

#: ``counter(parent_table, pk_column, child_tables) -> {"total", "inMultiple", "inNone"}``
#: Injected rather than implemented here: the SQL is dialect-sensitive and RSA supports seven
#: sources. Returning ``None`` means "not evaluated", which yields ``null`` constraints
#: rather than a guess.
SpecializationCounter = Callable[[str, str, list[str]], Optional[dict[str, int]]]


def build_inputs(
    schema: Schema,
    entity_name_by_table: dict[str, str],
    *,
    discriminators: list[DiscriminatorCandidate] | None = None,
    counter: SpecializationCounter | None = None,
) -> tuple[list[Any], list[Any], list[Any]]:
    """Assemble ``(discriminators, key_containment, measurements)`` for the shared library.

    Entity names, not table names, cross the boundary — the library works on the conceptual
    layer and should never see a physical identifier.
    """
    if not TAXONOMY_AVAILABLE:
        return [], [], []

    candidates = detect_discriminators(schema) if discriminators is None else discriminators
    specialization = specialization_parents(schema, candidates)

    disc_inputs: list[Any] = []
    for candidate in candidates:
        entity = entity_name_by_table.get(candidate.table)
        if entity is None:
            continue
        disc_inputs.append(
            Discriminator(
                container=candidate.table,
                field=candidate.column,
                values=list(candidate.values),
                # Set only for specialization: a discriminator on a table that is also a
                # shared-PK parent means the supertype already exists and nothing needs
                # synthesizing. On plain single-table inheritance it stays None.
                parent_entity=entity if candidate.table in specialization else None,
            )
        )

    containment: list[Any] = []
    measurements: list[Any] = []
    for parent_table, child_tables in sorted(shared_pk_children(schema).items()):
        parent_entity = entity_name_by_table.get(parent_table)
        if parent_entity is None:
            continue
        children = [entity_name_by_table[t] for t in child_tables if t in entity_name_by_table]
        for child_entity in children:
            # Declared, not sampled: the FK constraint *is* the containment guarantee.
            containment.append(KeyContainment(child=child_entity, parent=parent_entity, ratio=1.0))

        if counter is not None and len(children) >= 2:
            counts = _count(counter, schema, parent_table, child_tables)
            if counts is not None:
                measurements.append(
                    SpecializationMeasurement(
                        parent=parent_entity,
                        parent_keys_in_multiple_children=counts.get("inMultiple"),
                        parent_keys_in_no_child=counts.get("inNone"),
                        parent_key_count=counts.get("total"),
                    )
                )

    return disc_inputs, containment, measurements


def _count(
    counter: SpecializationCounter, schema: Schema, parent_table: str, child_tables: list[str]
) -> dict[str, int] | None:
    parent = schema.tables.get(parent_table)
    if parent is None or len(parent.primary_key) != 1:
        return None
    try:
        return counter(parent_table, parent.primary_key[0], list(child_tables))
    except Exception as err:  # noqa: BLE001
        logger.warning("specialization count failed for %s: %s", parent_table, err)
        return None


def discover(
    bundle: dict[str, Any],
    schema: Schema,
    *,
    counter: SpecializationCounter | None = None,
    namer: Any = None,
) -> dict[str, Any] | None:
    """Run abstraction discovery over an analysis bundle. ``None`` if the dep is absent."""
    if not TAXONOMY_AVAILABLE:
        logger.info("conceptual-taxonomy not installed; skipping abstraction discovery")
        return None

    physical = bundle.get("physicalMapping") or {}
    entity_name_by_table: dict[str, str] = {}
    for name, mapping in (physical.get("entities") or {}).items():
        if isinstance(mapping, dict) and isinstance(mapping.get("tableName"), str):
            entity_name_by_table[mapping["tableName"]] = name

    disc_inputs, containment, measurements = build_inputs(
        schema, entity_name_by_table, counter=counter
    )
    result = discover_abstractions(
        {
            "conceptualSchema": bundle.get("conceptualSchema") or {},
            "physicalMapping": physical,
        },
        discriminators=disc_inputs,
        key_containment=containment,
        measurements=measurements,
        namer=namer,
    )
    return result.to_json()


def merge_into_bundle(bundle: dict[str, Any], proposals: dict[str, Any] | None) -> dict[str, Any]:
    """Fold proposals in additively.

    Existing ``entity["subClassOf"]`` values written by the deterministic baseline are left
    alone; the library's edges arrive alongside them as proposals carrying mechanism,
    confidence and evidence, so a consumer can arbitrate rather than being handed a verdict.
    """
    if not proposals:
        return bundle

    conceptual = bundle.setdefault("conceptualSchema", {})
    entities = conceptual.setdefault("entities", [])
    known = {e.get("name") for e in entities if isinstance(e, dict)}

    for abstract in proposals.get("abstractClasses") or []:
        name = abstract.get("conceptualClass")
        if not isinstance(name, str) or name in known:
            continue
        entities.append(
            {
                "name": name,
                "labels": [name],
                # No physicalMapping entry by design — a synthesized class has no table.
                "abstract": True,
                "properties": [dict(p) for p in abstract.get("sharedProperties") or []],
                "source": abstract.get("source", "baseline"),
            }
        )
        known.add(name)

    conceptual["abstractClasses"] = list(proposals.get("abstractClasses") or [])
    conceptual["subClassOfProposals"] = list(proposals.get("subClassOf") or [])
    return bundle
