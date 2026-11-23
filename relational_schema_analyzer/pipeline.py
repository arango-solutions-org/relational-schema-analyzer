"""The one load → overlay → stamp pipeline that the CLI and ``run_tool`` share.

They used to build it separately and had drifted (#60): the CLI re-stamped a snapshot that
was already stamped, dating it to when the file was re-read, while the tool kept the stamp.
Each caller still owns its own argument checking and error reporting; what happens to the
schema is decided here once.
"""

from __future__ import annotations

from typing import Any, Optional

from .bitemporal import PriorRun, stamp_bitemporal
from .connectors import create_connector
from .overlay import apply_key_overlay
from .types import PhysicalSchema


def load_physical(
    *,
    captured: Optional[PhysicalSchema] = None,
    source_type: Optional[str] = None,
    url: Optional[str] = None,
    schema_name: str = "public",
    source_params: Optional[dict[str, Any]] = None,
    overlay: Optional[dict[str, Any]] = None,
    prior: Optional[PriorRun] = None,
) -> PhysicalSchema:
    """A stamped :class:`PhysicalSchema`, from a captured snapshot or a live source.

    1. ``captured`` if given, otherwise introspect ``source_type`` at ``url``.
    2. Apply the key ``overlay``, if any. It describes the schema, not how the schema was
       obtained, so a live read and a captured snapshot get it the same way.
    3. Stamp valid and transaction time (DESIGN-ADDENDUM-bitemporal), after the overlay
       because an overlay changes the structure the fingerprint rule compares. A schema
       that already carries a stamp keeps it unless ``prior`` is given: re-stamping would
       date a captured snapshot to when it was re-read, not when the source was seen.
    """
    physical = captured
    if physical is None:
        if not source_type or not url:
            raise ValueError("need a captured schema, or a source type and url")
        physical = create_connector(
            source_type, url, schema_name=schema_name, source_params=source_params
        ).get_schema()
    if overlay is not None:
        physical = apply_key_overlay(physical, overlay)
    if physical.transaction_time and prior is None:
        return physical
    return stamp_bitemporal(physical, prior=prior)
