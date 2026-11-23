"""Open Semantic Interchange (OSI) source connector: ``*.osi.yaml`` -> PhysicalSchema.

The second **data-catalog** source (DESIGN §9.3.1). OSI is a vendor-agnostic,
YAML/JSON semantic-model spec (Snowflake, dbt Labs, Salesforce/Tableau, et al.;
current draft ``0.2.0.dev0``). We treat it as read-only *catalog* metadata and map
its structural constructs onto the physical model:

    * ``semantic_model[].datasets[]``    -> Table  (``source`` "db.schema.table" ->
      schema_name + ``extra['osiSource']``; ``description`` -> comment)
    * ``dataset.fields[]``               -> Column (``description`` -> comment;
      declaration order -> ordinal)
    * ``dataset.primary_key``            -> primary key (members non-nullable)
    * ``dataset.unique_keys[]``          -> unique constraints (+ is_unique)
    * ``relationships[]`` (many-to-one)  -> ForeignKey on the ``from`` dataset
      (``from_columns`` -> ``to`` dataset ``to_columns``)

**Known limitation — OSI carries no column SQL types.** A field declares only a
per-dialect ``expression`` and an optional ``dimension.is_time`` flag, so we cannot
recover a real ``data_type``: ``is_time`` fields map to ``temporal`` and everything
else degrades to ``string``. Consumers that need precise types should introspect the
warehouse directly (or enrich via a live connector). Model-level ``metrics`` are
aggregate expressions, not physical columns, so they are intentionally ignored.

``connection_string`` is the path to an ``.osi.yaml``/``.yaml`` file (or a directory
containing one). Requires PyYAML — ``pip install relational-schema-analyzer[osi]``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..log import get_logger
from ..bitemporal import FILE
from ..bitemporal import file_mtime_iso
from ..bitemporal import to_iso as _iso
from ..types import Column, ForeignKey, Schema, SourceProvenance, Table
from .base import is_one_to_one, unique_columns

logger = get_logger(__name__)

_YAML_GLOBS = ("*.osi.yaml", "*.osi.yml", "*.yaml", "*.yml")


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except ImportError as err:  # pragma: no cover - exercised via monkeypatch
        raise ImportError(
            "The OSI connector requires PyYAML. "
            "Install it with: pip install 'relational-schema-analyzer[osi]'"
        ) from err
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as err:
        raise RuntimeError(f"Failed to parse OSI model {path}: {err}") from err


def _resolve_model_path(connection_string: str) -> Path:
    p = Path(connection_string).expanduser()
    if p.is_dir():
        for pattern in _YAML_GLOBS:
            matches = sorted(p.glob(pattern))
            if matches:
                return matches[0]
        raise RuntimeError(f"No OSI model (*.osi.yaml / *.yaml) found under {p}")
    return p


def _parse_source(source: Any) -> tuple[str | None, str | None]:
    """Split an OSI ``source`` ("db.schema.table") into (database, schema)."""
    if not isinstance(source, str) or not source.strip():
        return None, None
    parts = [seg for seg in source.split(".") if seg]
    database = parts[-3] if len(parts) >= 3 else None
    schema = parts[-2] if len(parts) >= 2 else None
    return database, schema


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class OsiConnector:
    """Introspect an OSI semantic model (YAML/JSON) into a :class:`Schema`."""

    def __init__(self, connection_string: str, schema_name: str = "public") -> None:
        self.connection_string = connection_string
        self.schema_name = schema_name  # informational; OSI carries its own source scope

    def get_schema(self) -> Schema:
        path = _resolve_model_path(self.connection_string)
        doc = _load_yaml(path)
        if not isinstance(doc, dict):
            raise RuntimeError("OSI model must be a mapping at the top level")

        models = _as_list(doc.get("semantic_model"))
        version = doc.get("version")

        # Bitemporal valid time (addendum §3). OSI has no per-dataset date, so the best
        # available signal is the document's own generation stamp, falling back to the
        # file's mtime. Either way it is `file`: it dates the *model document*, not the
        # warehouse objects it describes, and those drift apart as soon as the model lags a
        # migration. OSI already degrades types honestly (§9.3.1); time is no different.
        raw_meta = doc.get("metadata")
        doc_meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        generated_at = _iso(
            doc.get("generated_at")
            or doc.get("updated_at")
            or doc_meta.get("generated_at")
        ) or file_mtime_iso(path)

        schema = Schema()
        db_hint: str | None = None
        for model in models:
            if not isinstance(model, dict):
                continue
            for dataset in _as_list(model.get("datasets")):
                if not isinstance(dataset, dict):
                    continue
                table = self._build_table(dataset)
                if table is None:
                    continue
                database, _ = _parse_source(dataset.get("source"))
                db_hint = db_hint or database
                if generated_at:
                    table.valid_from = generated_at
                    table.valid_time_source = FILE
                schema.tables[table.name] = table
            self._apply_relationships(model, schema)

        schema.source = self._provenance(models, version, db_hint)
        return schema

    def _provenance(
        self, models: list[Any], version: Any, db_hint: str | None
    ) -> SourceProvenance:
        model_name = None
        for model in models:
            if isinstance(model, dict) and model.get("name"):
                model_name = str(model["name"])
                break
        return SourceProvenance(
            dialect="osi",
            server_version=str(version) if version is not None else None,
            database=db_hint or model_name,
            namespace=None,
        )

    def _build_table(self, dataset: dict[str, Any]) -> Table | None:
        name = dataset.get("name")
        if not name:
            return None
        _, schema_name = _parse_source(dataset.get("source"))

        pk = [str(c) for c in _as_list(dataset.get("primary_key"))]
        pk_set = set(pk)

        unique_constraints: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()
        for uk in _as_list(dataset.get("unique_keys")):
            cols = [str(c) for c in _as_list(uk)]
            key = tuple(cols)
            if cols and key not in seen:
                seen.add(key)
                unique_constraints.append(cols)

        # Single-column uniqueness (PK or a 1-col unique key) marks the column unique.
        single_unique = unique_columns(pk, unique_constraints)

        columns: list[Column] = []
        for ordinal, field in enumerate(_as_list(dataset.get("fields"))):
            if not isinstance(field, dict):
                continue
            col = self._build_column(field, ordinal, pk_set, single_unique)
            if col is not None:
                columns.append(col)

        extra: dict[str, Any] = {}
        source = dataset.get("source")
        if isinstance(source, str) and source.strip():
            extra["osiSource"] = source.strip()

        return Table(
            name=str(name),
            columns=columns,
            primary_key=pk,
            schema_name=schema_name,
            comment=(dataset.get("description") or None),
            unique_constraints=unique_constraints,
            extra=extra,
        )

    def _build_column(
        self,
        field: dict[str, Any],
        ordinal: int,
        pk_set: set[str],
        single_unique: set[str],
    ) -> Column | None:
        name = field.get("name")
        if not name:
            return None
        name = str(name)
        # OSI has no SQL type; the only type signal is dimension.is_time.
        raw_dim = field.get("dimension")
        dimension: dict = raw_dim if isinstance(raw_dim, dict) else {}
        data_type = "timestamp" if dimension.get("is_time") else ""
        return Column(
            name=name,
            data_type=data_type,
            is_nullable=(name not in pk_set),
            is_primary_key=(name in pk_set),
            is_unique=(name in single_unique),
            comment=(field.get("description") or None),
            ordinal=ordinal,
        )

    def _apply_relationships(self, model: dict[str, Any], schema: Schema) -> None:
        for rel in _as_list(model.get("relationships")):
            if not isinstance(rel, dict):
                continue
            from_ds = rel.get("from") or rel.get("from_dataset")
            to_ds = rel.get("to") or rel.get("to_dataset")
            from_cols = [str(c) for c in _as_list(rel.get("from_columns"))]
            to_cols = [str(c) for c in _as_list(rel.get("to_columns"))]
            if not (from_ds and to_ds and from_cols and to_cols):
                continue
            table = schema.tables.get(str(from_ds))
            if table is None:
                logger.debug("OSI relationship %s references unknown dataset %s",
                             rel.get("name"), from_ds)
                continue
            # OSI relationships are many-to-one (from -> to): the from columns are
            # unique only when they exactly match a unique key / PK of the from table.
            table.foreign_keys.append(
                ForeignKey(
                    columns=from_cols,
                    foreign_table=str(to_ds),
                    foreign_columns=to_cols,
                    constraint_name=(rel.get("name") or None),
                    is_unique=is_one_to_one(from_cols, table.primary_key, table.unique_constraints),
                )
            )
