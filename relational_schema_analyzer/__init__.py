"""relational-schema-analyzer.

Analyze a relational database schema into a conceptual model (entities /
relationships / properties) with a mapping back to the source relational schema,
plus optional OWL (Turtle / JSON-LD) exports.

This is the relational analogue of ``arangodb-schema-analyzer`` and emits the same
tool-contract bundle shape ``{conceptualSchema, physicalMapping, metadata}``.

See ``docs/DESIGN.md`` and ``docs/IMPLEMENTATION-PLAN.md``. The implementation is
delivered in phases; the public API below is the target surface (Phase 1+).
"""

from __future__ import annotations

# Defined before the submodule imports below because ``analyzer``/``metadata``
# read ``relational_schema_analyzer.__version__`` at import time.
__version__ = "0.15.0"

from .analyzer import Analysis, RelationalSchemaAnalyzer
from .bitemporal import (
    PriorRun,
    bitemporal_metadata,
    stamp_bitemporal,
)
from .discriminator import DiscriminatorCandidate, DiscriminatorOptions, detect_discriminators
from .samplers import (
    executor_from_connection,
    make_specialization_counter,
    make_value_enumerator,
)
from .conceptual import ConceptualSchema
from .connectors import (
    SUPPORTED_SOURCE_TYPES,
    BulkReadableSource,
    SourceConnector,
    supports_bulk_read,
    create_connector,
    create_source_connector,
)
from .fk_inference import (
    DuckDbValueSampler,
    SqliteValueSampler,
    InferenceOptions,
    InferredForeignKey,
    create_value_sampler,
    infer_foreign_keys,
)
from .exports import export_bundle
from .mapping import PhysicalMapping
from .metadata import fingerprint_physical_schema
from .overlay import (
    OverlayError,
    apply_key_overlay,
    load_key_overlay,
    overlay_applied,
    overlay_summary,
)
from .providers import list_providers, register_provider
from .owl_export import export_owl_jsonld, export_owl_turtle
from .r2rml_export import (
    DEFAULT_R2RML_DATA_IRI,
    DEFAULT_R2RML_MAPPING_IRI,
    export_r2rml_turtle,
)
from .schema_diff import diff_schemas
from .tool import run_tool
from .topo_sort import topological_sort_tables
from .types import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    PhysicalSchema,
    Schema,
    SourceProvenance,
    Table,
)

__all__ = [
    "__version__",
    # Phase 1 — physical core
    "create_connector",
    "create_source_connector",
    "BulkReadableSource",
    "SourceConnector",
    "supports_bulk_read",
    "SUPPORTED_SOURCE_TYPES",
    "PhysicalSchema",
    "Schema",
    "Table",
    "Column",
    "ForeignKey",
    "Index",
    "CheckConstraint",
    "SourceProvenance",
    "diff_schemas",
    "topological_sort_tables",
    "infer_foreign_keys",
    "InferredForeignKey",
    "InferenceOptions",
    "create_value_sampler",
    "DuckDbValueSampler",
    "SqliteValueSampler",
    # Declared-key overlay (for sources whose catalog declares no keys)
    "apply_key_overlay",
    "load_key_overlay",
    "overlay_applied",
    "overlay_summary",
    "OverlayError",
    # Bitemporal stamping (valid time + transaction time)
    "stamp_bitemporal",
    "PriorRun",
    "bitemporal_metadata",
    # Phase 2 — conceptual model + baseline
    "RelationalSchemaAnalyzer",
    "DiscriminatorCandidate",
    "DiscriminatorOptions",
    "detect_discriminators",
    "executor_from_connection",
    "make_specialization_counter",
    "make_value_enumerator",
    "Analysis",
    "ConceptualSchema",
    "PhysicalMapping",
    "fingerprint_physical_schema",
    # Phase 3 — exports
    "export_bundle",
    "export_owl_turtle",
    "export_owl_jsonld",
    "export_r2rml_turtle",
    "DEFAULT_R2RML_DATA_IRI",
    "DEFAULT_R2RML_MAPPING_IRI",
    # Phase 5 — tool contract / MCP
    "run_tool",
    # Phase 4 — optional LLM refinement
    "register_provider",
    "list_providers",
]
