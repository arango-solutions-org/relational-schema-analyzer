"""dbt catalog source connector: a dbt ``manifest.json`` -> PhysicalSchema.

The first **data-catalog** source (DESIGN §9.3.1). dbt is the best-fit catalog because
its *tests* and *contracts* carry exactly the metadata the physical model prizes:

    * model / seed / snapshot   -> Table  (``config.materialized == view`` -> is_view)
    * documented column         -> Column (data_type -> type_category; description ->
      comment; ordinal from declared order)
    * contract PRIMARY KEY / column constraint   -> primary key
    * ``unique`` test / UNIQUE constraint        -> unique constraint (+ is_unique)
    * ``not_null`` test / NOT NULL constraint    -> non-nullable column
    * ``accepted_values`` test / CHECK constraint-> CheckConstraint (enum_values)
    * ``relationships`` test / FOREIGN KEY       -> ForeignKey (to model + field)

Pure JSON — no live warehouse, no extra dependency (stdlib ``json``). ``connection_string``
is the path to ``manifest.json`` (or a project/``target`` directory containing it).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..log import get_logger
from ..bitemporal import FILE
from ..bitemporal import to_iso as _iso
from ..types import CheckConstraint, Column, ForeignKey, Schema, SourceProvenance, Table
from .base import mark_one_to_one

logger = get_logger(__name__)

_MODEL_RESOURCE_TYPES = frozenset({"model", "seed", "snapshot"})
_VIEW_MATERIALIZATIONS = frozenset({"view", "materialized_view"})
_ref_re = re.compile(r"""['"]([^'"]+)['"]""")


def _resolve_manifest_path(connection_string: str) -> Path:
    p = Path(connection_string).expanduser()
    if p.is_dir():
        for candidate in (p / "manifest.json", p / "target" / "manifest.json"):
            if candidate.is_file():
                return candidate
        raise RuntimeError(f"No manifest.json found under {p}")
    return p


def _ref_model_name(to_value: Any) -> str | None:
    """Parse the referenced model name from a relationships/FK ``to`` value.

    Accepts ``ref('users')`` / ``ref('shop', 'users')`` (last quoted token wins),
    a bare model name, or a compiled relation (last dotted segment).
    """
    if not isinstance(to_value, str) or not to_value.strip():
        return None
    matches = _ref_re.findall(to_value)
    if matches:
        return matches[-1]
    return to_value.strip().split(".")[-1].strip('`"[]')


class _TableAcc:
    """Mutable accumulator for one table while parsing the manifest."""

    def __init__(self, name: str, *, schema_name: str | None, is_view: bool,
                 comment: str | None) -> None:
        self.name = name
        self.schema_name = schema_name
        self.is_view = is_view
        self.comment = comment
        self.columns: list[dict[str, Any]] = []
        self.pk: list[str] = []
        self.not_null: set[str] = set()
        self.unique_cols: set[str] = set()
        self.unique_sets: list[list[str]] = []
        self.checks: list[CheckConstraint] = []
        self.fks: list[ForeignKey] = []


class DbtManifestConnector:
    """Introspect a dbt ``manifest.json`` into a :class:`Schema`."""

    def __init__(self, connection_string: str, schema_name: str = "public") -> None:
        self.connection_string = connection_string
        self.schema_name = schema_name  # informational; dbt spans schemas

    def get_schema(self) -> Schema:
        path = _resolve_manifest_path(self.connection_string)
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception as err:
            raise RuntimeError(f"Failed to read dbt manifest {path}: {err}") from err
        if not isinstance(manifest, dict):
            raise RuntimeError("dbt manifest must be a JSON object")

        nodes = manifest.get("nodes") or {}
        accs: dict[str, _TableAcc] = {}
        name_by_uid: dict[str, str] = {}

        # Pass 1: models/seeds/snapshots -> tables + columns + contract constraints.
        for uid, node in nodes.items():
            if not isinstance(node, dict) or node.get("resource_type") not in _MODEL_RESOURCE_TYPES:
                continue
            name = node.get("name")
            if not name:
                continue
            materialized = (node.get("config") or {}).get("materialized")
            acc = _TableAcc(
                name,
                schema_name=node.get("schema"),
                is_view=(materialized in _VIEW_MATERIALIZATIONS),
                comment=(node.get("description") or None),
            )
            self._parse_columns(node, acc)
            self._parse_node_constraints(node, acc)
            accs[uid] = acc
            name_by_uid[uid] = name

        # Pass 2: tests -> not_null / unique / accepted_values / relationships.
        for node in nodes.values():
            if not isinstance(node, dict) or node.get("resource_type") != "test":
                continue
            self._parse_test(node, accs, name_by_uid)

        schema = Schema(source=self._provenance(manifest))
        generated_at = _iso((manifest.get('metadata') or {}).get('generated_at'))
        for acc in accs.values():
            table = self._finalize(acc)
            # Bitemporal valid time (addendum §3): the manifest's own `generated_at`.
            # Reported as `file`, not `catalog` — it dates *the manifest*, i.e. when dbt
            # last compiled, which is not when the warehouse table changed. The two drift
            # apart the moment a compile lags a migration, so claiming `catalog` here would
            # be a more precise-sounding lie.
            if generated_at:
                table.valid_from = generated_at
                table.valid_time_source = FILE
            schema.tables[acc.name] = table
        return schema

    def _provenance(self, manifest: dict[str, Any]) -> SourceProvenance:
        meta = manifest.get("metadata") or {}
        return SourceProvenance(
            dialect=meta.get("adapter_type") or "dbt",
            server_version=meta.get("dbt_version"),
            database=meta.get("project_name"),
            namespace=None,
        )

    def _parse_columns(self, node: dict[str, Any], acc: _TableAcc) -> None:
        columns = node.get("columns") or {}
        for ordinal, (col_name, col) in enumerate(columns.items()):
            col = col if isinstance(col, dict) else {}
            acc.columns.append(
                {
                    "name": col.get("name") or col_name,
                    "data_type": (col.get("data_type") or "").lower(),
                    "comment": col.get("description") or None,
                    "ordinal": ordinal,
                }
            )
            for c in col.get("constraints") or []:
                self._apply_constraint(c, acc, default_cols=[col.get("name") or col_name])

    def _parse_node_constraints(self, node: dict[str, Any], acc: _TableAcc) -> None:
        for c in node.get("constraints") or []:
            self._apply_constraint(c, acc, default_cols=list(c.get("columns") or []))

    def _apply_constraint(
        self, c: Any, acc: _TableAcc, *, default_cols: list[str]
    ) -> None:
        if not isinstance(c, dict):
            return
        ctype = (c.get("type") or "").lower()
        cols = list(c.get("columns") or []) or default_cols
        if ctype == "primary_key":
            for col in cols:
                if col not in acc.pk:
                    acc.pk.append(col)
        elif ctype == "not_null":
            acc.not_null.update(cols)
        elif ctype == "unique":
            if len(cols) == 1:
                acc.unique_cols.add(cols[0])
            if cols:
                acc.unique_sets.append(cols)
        elif ctype == "check":
            acc.checks.append(
                CheckConstraint(name=c.get("name"), expression=c.get("expression") or "",
                                columns=cols)
            )
        elif ctype == "foreign_key":
            to_table = _ref_model_name(c.get("to"))
            to_cols = list(c.get("to_columns") or [])
            if to_table and cols and to_cols:
                acc.fks.append(
                    ForeignKey(columns=cols, foreign_table=to_table, foreign_columns=to_cols)
                )

    def _parse_test(
        self, node: dict[str, Any], accs: dict[str, _TableAcc], name_by_uid: dict[str, str]
    ) -> None:
        meta = node.get("test_metadata") or {}
        tname = (meta.get("name") or "").lower()
        kwargs = meta.get("kwargs") or {}
        column = node.get("column_name") or kwargs.get("column_name")

        acc = self._attached_table(node, accs)
        if acc is None:
            return

        if tname == "not_null" and column:
            acc.not_null.add(column)
        elif tname == "unique" and column:
            acc.unique_cols.add(column)
            acc.unique_sets.append([column])
        elif tname == "accepted_values" and column:
            values = [str(v) for v in (kwargs.get("values") or [])]
            if values:
                acc.checks.append(
                    CheckConstraint(
                        name=f"{acc.name}_{column}_accepted_values",
                        expression=f"{column} in ({', '.join(repr(v) for v in values)})",
                        columns=[column],
                        enum_values=values,
                    )
                )
        elif tname == "relationships" and column:
            to_table = _ref_model_name(kwargs.get("to"))
            field = kwargs.get("field")
            if to_table and field:
                acc.fks.append(
                    ForeignKey(columns=[column], foreign_table=to_table,
                               foreign_columns=[field])
                )

    def _attached_table(
        self, node: dict[str, Any], accs: dict[str, _TableAcc]
    ) -> _TableAcc | None:
        attached = node.get("attached_node")
        if attached and attached in accs:
            return accs[attached]
        # Fall back to the first model in depends_on (older dbt manifests).
        for uid in (node.get("depends_on") or {}).get("nodes") or []:
            if uid in accs:
                return accs[uid]
        return None

    def _finalize(self, acc: _TableAcc) -> Table:
        pk_set = set(acc.pk)
        single_unique = set(acc.unique_cols)
        if len(acc.pk) == 1:
            single_unique.add(acc.pk[0])

        columns = [
            Column(
                name=c["name"],
                data_type=c["data_type"],
                is_nullable=(c["name"] not in acc.not_null and c["name"] not in pk_set),
                is_primary_key=(c["name"] in pk_set),
                is_unique=(c["name"] in single_unique),
                comment=c["comment"],
                ordinal=c["ordinal"],
            )
            for c in acc.columns
        ]

        # Deduplicate unique sets.
        seen: set[tuple[str, ...]] = set()
        unique_constraints: list[list[str]] = []
        for u in acc.unique_sets:
            key = tuple(u)
            if key not in seen:
                seen.add(key)
                unique_constraints.append(list(u))

        mark_one_to_one(acc.fks, acc.pk, unique_constraints)

        return Table(
            name=acc.name,
            columns=columns,
            primary_key=acc.pk,
            foreign_keys=acc.fks,
            is_view=acc.is_view,
            comment=acc.comment,
            schema_name=acc.schema_name,
            unique_constraints=unique_constraints,
            check_constraints=acc.checks,
        )
