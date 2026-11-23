"""CSV directory source connector.

A CSV source is a directory of delimited files: each file is one table,
the filename stem is the table name, and the header row supplies the
column names. Column types are inferred from a bounded sample using
Polars' CSV type inference, then mapped onto the same source-agnostic
type strings (``integer`` / ``double precision`` / ``boolean`` /
``timestamp`` / ``text``) that the transformer's type map already
understands.

CSV has no notion of primary or foreign keys. We apply a small,
documented heuristic so auto-mapping produces a usable ``_key``: the
first of ``id`` / ``{table}_id`` / ``{singular(table)}_id`` /
``{table}id`` / ``{singular(table)}id`` (case-insensitive) that exists
and is **unique and non-null in the read sample** is treated as the
primary key (e.g. ``customers.csv`` keyed on ``customer_id``). When the
file has no data rows we fall back to a name-only match so introspection
still proposes a key. Relationships can be recovered afterwards with FK
inference (``r2g source infer-fks``), whose value-overlap sampling and
name heuristics both rely on these detected keys.

Both schema introspection (:meth:`CsvConnector.get_schema`) and bulk
reads for loads (:class:`CsvSession`) are supported, so a CSV source
can be streamed into ArangoDB exactly like a relational source.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Iterator, Optional

import polars as pl

from ..dump_reader import DumpReader
from ..log import get_logger
from ..naming import singularize
from ..bitemporal import FILE
from ..bitemporal import file_mtime_iso
from ..types import CheckConstraint, Column, Schema, SourceProvenance, Table

logger = get_logger(__name__)

# Operators running the Studio UI / MCP server on a shared host can set this
# to confine CSV sources to a trusted directory tree. When unset (the default,
# matching the frictionless local-CLI posture) any path is accepted.
CSV_BASE_DIR_ENV = "R2G_CSV_BASE_DIR"


class CsvSourceError(RuntimeError):
    """Raised when a CSV source directory is invalid or outside the jail."""


def resolve_source_directory(connection_string: str) -> Path:
    """Resolve a CSV source path, enforcing ``R2G_CSV_BASE_DIR`` when set.

    The directory is ``expanduser``-ed for the returned value, but the jail
    check resolves symlinks and ``..`` on both the target and the base dir
    first so the confinement cannot be escaped. With no base dir configured,
    the path is returned unchanged.
    """
    directory = Path(connection_string).expanduser()
    base = os.environ.get(CSV_BASE_DIR_ENV)
    if not base:
        return directory
    base_resolved = Path(base).expanduser().resolve()
    target_resolved = directory.resolve()
    if target_resolved != base_resolved and base_resolved not in target_resolved.parents:
        raise CsvSourceError(
            f"CSV source directory '{target_resolved}' is outside the allowed "
            f"base directory '{base_resolved}' (set by {CSV_BASE_DIR_ENV})."
        )
    return directory

# Polars dtype "base name" -> source-agnostic type string understood by
# r2g.config.DEFAULT_TYPE_MAP / pg_type_to_json_type.
_POLARS_TYPE_MAP: dict[str, str] = {
    "Int8": "integer",
    "Int16": "integer",
    "Int32": "integer",
    "Int64": "integer",
    "UInt8": "integer",
    "UInt16": "integer",
    "UInt32": "integer",
    "UInt64": "integer",
    "Float32": "double precision",
    "Float64": "double precision",
    "Boolean": "boolean",
    "Date": "date",
    "Datetime": "timestamp",
    "Time": "text",
    "Utf8": "text",
    "String": "text",
}

CSV_EXTENSIONS = (".csv", ".tsv", ".txt")


def resolve_csv_table_path(directory: Path, table: str) -> Path | None:
    """Return ``<directory>/<table><ext>`` for the first matching CSV extension,
    or ``None`` if no such file exists. Shared by the connector and the FK
    value-overlap sampler so the table→file convention stays in one place."""
    for ext in CSV_EXTENSIONS:
        candidate = directory / f"{table}{ext}"
        if candidate.is_file():
            return candidate
    return None


def _polars_dtype_to_type(dtype: Any) -> str:
    """Map a Polars dtype to a source-agnostic type string."""
    base = str(dtype).split("(")[0].strip()
    return _POLARS_TYPE_MAP.get(base, "text")


class CsvConnector:
    """Introspect and read a directory of CSV files as a schema.

    ``connection_string`` is the directory path. ``schema_name`` is
    carried for protocol parity but is otherwise unused (a CSV directory
    has no schema namespace).
    """

    def __init__(
        self,
        connection_string: str,
        *,
        schema_name: str = "public",
        delimiter: str = ",",
        has_header: bool = True,
        sample_rows: int = 1000,
        sample_enums: bool = False,
        enum_threshold: int = 25,
    ) -> None:
        self.connection_string = connection_string
        self.schema_name = schema_name
        self.delimiter = delimiter
        self.has_header = has_header
        self.sample_rows = sample_rows
        # Opt-in light sampling: low-cardinality columns → enum candidates
        # (feeding downstream sh:in). Off by default (AOE contract §5).
        self.sample_enums = sample_enums
        self.enum_threshold = enum_threshold
        self.directory = resolve_source_directory(connection_string)

    def _table_files(self) -> dict[str, Path]:
        if not self.directory.is_dir():
            raise RuntimeError(f"CSV source directory not found: {self.directory}")
        files: dict[str, Path] = {}
        for path in sorted(self.directory.iterdir()):
            if path.is_file() and path.suffix.lower() in CSV_EXTENSIONS:
                files[path.stem] = path
        if not files:
            raise RuntimeError(
                f"No CSV/TSV files found in {self.directory} "
                f"(looked for {', '.join(CSV_EXTENSIONS)})"
            )
        return files

    def get_schema(self) -> Schema:
        schema = Schema(
            source=SourceProvenance(dialect="csv", database=self.directory.name)
        )
        for table_name, path in self._table_files().items():
            table = self._process_file(table_name, path)
            # Bitemporal valid time (addendum §3): a file's mtime is the closest thing a
            # CSV has to "when this definition became true". Reported as `file`, never
            # `catalog` — mtime is reset by a copy, a checkout or a sync, so it dates the
            # *file* and not the data. Naming the source honestly is what lets a consumer
            # decide how much to lean on it.
            stamp = file_mtime_iso(path)
            if stamp:
                table.valid_from = stamp
                table.valid_time_source = FILE
            schema.tables[table_name] = table
        return schema

    def open_session(self) -> "CsvSession":
        return CsvSession(
            self.connection_string,
            schema_name=self.schema_name,
            delimiter=self.delimiter,
            has_header=self.has_header,
        )

    def _process_file(self, table_name: str, path: Path) -> Table:
        try:
            frame = pl.read_csv(
                str(path),
                separator=self.delimiter,
                has_header=self.has_header,
                n_rows=self.sample_rows,
                infer_schema_length=self.sample_rows,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("csv_introspect_failed", path=str(path), error=str(e))
            raise RuntimeError(f"Failed to read CSV header for '{table_name}': {e}")

        pk = self._detect_primary_key(table_name, frame)
        pk_set = set(pk)
        single_pk = len(pk) == 1

        columns: list[Column] = []
        for ordinal, (col_name, dtype) in enumerate(zip(frame.columns, frame.dtypes)):
            is_pk = col_name in pk_set
            columns.append(
                Column(
                    name=col_name,
                    data_type=_polars_dtype_to_type(dtype),
                    is_nullable=not is_pk,
                    is_primary_key=is_pk,
                    # CSV has no declared constraints; the only honest uniqueness
                    # signal is the sampled single-column primary key.
                    is_unique=is_pk and single_pk,
                    ordinal=ordinal,
                )
            )

        checks = self._enum_candidates(frame, pk_set) if self.sample_enums else []
        return Table(
            name=table_name,
            columns=columns,
            primary_key=pk,
            foreign_keys=[],
            check_constraints=checks,
        )

    def _enum_candidates(
        self, frame: "pl.DataFrame", pk_set: set[str]
    ) -> list[CheckConstraint]:
        """Opt-in: low-cardinality non-key columns → candidate enum constraints."""
        if frame.height == 0:
            return []
        out: list[CheckConstraint] = []
        for col in frame.columns:
            if col in pk_set:
                continue
            series = frame.get_column(col).drop_nulls()
            if series.len() == 0:
                continue
            distinct = series.n_unique()
            if 1 <= distinct <= self.enum_threshold and distinct < series.len():
                values = sorted(str(v) for v in series.unique().to_list())
                joined = ", ".join(repr(v) for v in values)
                out.append(
                    CheckConstraint(
                        name=f"{col}_enum",
                        expression=f"{col} IN ({joined})",
                        columns=[col],
                        enum_values=values,
                    )
                )
        return out

    @staticmethod
    def _is_unique_key(frame: "pl.DataFrame", col: str) -> bool:
        """True when ``col`` is non-null and fully distinct in the sample."""
        series = frame.get_column(col)
        n = series.len()
        if n == 0:
            return False
        return series.null_count() == 0 and series.n_unique() == n

    def _detect_primary_key(self, table_name: str, frame: "pl.DataFrame") -> list[str]:
        """Pick a single-column primary key by name + sample uniqueness.

        Candidate names are tried in priority order; the first that maps
        to a real column wins. With data rows present the winner must be
        unique and non-null in the sample, so we never invent a key from
        a non-unique column. With no data rows we accept the first
        name match (legacy behaviour) so a fresh / empty export still
        gets a proposed key.
        """
        by_lower: dict[str, str] = {}
        for c in frame.columns:
            by_lower.setdefault(c.lower(), c)

        t = table_name.lower()
        singular = singularize(t)
        candidate_names: list[str] = []
        for cand in ("id", f"{t}_id", f"{singular}_id", f"{t}id", f"{singular}id"):
            real = by_lower.get(cand)
            if real and real not in candidate_names:
                candidate_names.append(real)

        if not candidate_names:
            return []

        has_rows = frame.height > 0
        if not has_rows:
            return [candidate_names[0]]

        for col in candidate_names:
            if self._is_unique_key(frame, col):
                return [col]
        return []


class CsvSession:
    """Bulk-read session over a CSV directory.

    There is no live connection to manage; each table read opens its
    file lazily via :class:`DumpReader`. The session object exists to
    satisfy the :class:`~r2g.connectors.session.SourceSession` protocol
    so the streaming pipeline can treat CSV like any other source.
    """

    def __init__(
        self,
        connection_string: str,
        *,
        schema_name: str = "public",
        delimiter: str = ",",
        has_header: bool = True,
    ) -> None:
        self.connection_string = connection_string
        self.schema_name = schema_name
        self.delimiter = delimiter
        self.has_header = has_header
        self.directory = resolve_source_directory(connection_string)

    def _resolve(self, table: str) -> Path:
        path = resolve_csv_table_path(self.directory, table)
        if path is None:
            raise RuntimeError(f"No CSV file for table '{table}' in {self.directory}")
        return path

    def _reader(self, table: str) -> DumpReader:
        return DumpReader(
            str(self._resolve(table)),
            delimiter=self.delimiter,
            has_header=self.has_header,
        )

    def count_rows(
        self,
        table: str,
        *,
        since_column: Optional[str] = None,
        since_value: Optional[str] = None,
    ) -> int:
        return self._reader(table).row_count()

    def stream_rows(
        self,
        table: str,
        *,
        batch_size: int = 10_000,
        since_column: Optional[str] = None,
        since_value: Optional[str] = None,
    ) -> Iterator[dict[str, Any]]:
        yield from self._reader(table).read_rows()

    def dump_table_to_csv(
        self,
        table: str,
        out_path: Path,
        *,
        header: bool = True,
    ) -> int:
        src = self._resolve(table)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out_path)
        return self._reader(table).row_count()

    def close(self) -> None:
        return None

    def __enter__(self) -> "CsvSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


__all__ = ["CsvConnector", "CsvSession"]
