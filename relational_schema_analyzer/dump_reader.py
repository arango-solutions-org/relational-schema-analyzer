"""CSV dump reader (polars-backed).

Lifted from ``r2g/src/r2g/input/dump_reader.py``. Used by the CSV connector to
introspect column names/types and (optionally) stream rows. Requires the ``csv``
extra (``polars``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Generator

import polars as pl

from .log import get_logger

logger = get_logger(__name__)


class DumpReader:
    def __init__(self, file_path: str, delimiter: str = ",", has_header: bool = True):
        self.file_path = Path(file_path)
        self.delimiter = delimiter
        self.has_header = has_header

    def _csv_path(self) -> str:
        return str(self.file_path)

    def _polars_csv_kwargs(self) -> dict[str, Any]:
        return {"separator": self.delimiter, "has_header": self.has_header}

    def read_dataframe(self) -> pl.DataFrame:
        if not self.file_path.exists():
            raise FileNotFoundError(f"Dump file not found: {self.file_path}")
        try:
            return pl.read_csv(self._csv_path(), **self._polars_csv_kwargs())
        except Exception as e:
            logger.error("error_reading_dump", path=str(self.file_path), error=str(e))
            raise

    def row_count(self) -> int:
        if not self.file_path.exists():
            raise FileNotFoundError(f"Dump file not found: {self.file_path}")
        try:
            lf = pl.scan_csv(self._csv_path(), **self._polars_csv_kwargs())
            return int(lf.select(pl.len()).collect().item())
        except Exception as e:
            logger.error("error_counting_rows", path=str(self.file_path), error=str(e))
            raise

    def read_rows(self) -> Generator[Dict[str, Any], None, None]:
        df = self.read_dataframe()
        if self.has_header:
            for row in df.iter_rows(named=True):
                yield dict(row)
        else:
            for values in df.iter_rows(named=False):
                yield {str(i): val for i, val in enumerate(values)}
