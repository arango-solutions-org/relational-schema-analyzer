"""Programmatic v1 tool-contract entrypoint: ``run_tool(request) -> response``.

A single dict-in / dict-out function that drives the ``snapshot`` / ``analyze`` /
``owl`` / ``r2rml`` operations. It is the substrate the MCP server (and any
non-interactive tool caller) wraps, and it is fully testable without the ``mcp``
package.

Request shape (relational variant of the shared tool contract):

    {
      "contractVersion": "1",
      "requestId": "opt-id",
      "operation": "snapshot" | "analyze" | "owl" | "r2rml",
      "source": {                       # a live source to introspect ...
        "type": "postgresql|mysql|sqlserver|snowflake|duckdb|sqlite|databricks|csv",
        "url": "<connection string / DSN / path>",
        "schema": "public",             # optional namespace
        "params": { ... }               # optional source_params (e.g. csv delimiter)
      },
      "input": {
        "physical": { ... },            # ... OR a previously captured PhysicalSchema
        "previousAnalysis": { ... }     # optional: a previous run's bundle, enabling
                                        # bitemporal fingerprint-continuity (see below)
      },
      "owl": { "format": "turtle"|"jsonld", "iriBase": "...", "physIriBase": "..." },
      "r2rml": { "iriBase": "...", "dataIriBase": "...", "mappingIriBase": "..." }
    }

``r2rml`` always emits Turtle (R2RML has no other standard serialization); its
``iriBase`` should match the ``owl`` operation's so the mapping populates the
ontology that export declares.

``input.previousAnalysis`` is the contract-level equivalent of the CLI's ``--prior-run``,
and it matters more than it looks: RSA is stateless, so without a prior observation the
bitemporal resolver can only report ``observed`` or an ungated catalog date — the
``fingerprint-continuity`` half of DESIGN-ADDENDUM-bitemporal is unreachable. The consumers
that commissioned bitemporal stamping (AOE, the fabric's catalog builder) call RSA through
*this* entrypoint rather than the CLI, so leaving it out would have shipped the feature
working only for the one caller that did not ask for it.

The field name is deliberately **not** a new one: ``input.previousAnalysis`` is already
declared in the shared request contract (and ``input`` is ``additionalProperties: false``,
so a minted ``priorRun`` would have been rejected outright by any validating consumer).
Reusing it keeps RSA and ``arango-schema-analyzer`` converged on a field that predates both.

Response envelope (aligned with the shared response contract):

    { "contractVersion": "1", "requestId": ..., "operation": ..., "ok": bool,
      "result": { ... } | "error": { "code": str, "message": str } }
"""

from __future__ import annotations

from typing import Any

from .analyzer import RelationalSchemaAnalyzer
from .bitemporal import PriorRun
from .owl_export import export_owl_jsonld, export_owl_turtle
from .pipeline import load_physical
from .r2rml_export import export_r2rml_turtle
from .types import PhysicalSchema

CONTRACT_VERSION = "1"
_OPERATIONS = ("snapshot", "analyze", "owl", "r2rml")


def _response(request: dict[str, Any], *, ok: bool, **extra: Any) -> dict[str, Any]:
    resp: dict[str, Any] = {
        "contractVersion": CONTRACT_VERSION,
        "operation": request.get("operation") if isinstance(request, dict) else None,
        "ok": ok,
    }
    if isinstance(request, dict) and request.get("requestId"):
        resp["requestId"] = request["requestId"]
    resp.update(extra)
    return resp


def _error(request: Any, code: str, message: str) -> dict[str, Any]:
    return _response(
        request if isinstance(request, dict) else {}, ok=False,
        error={"code": code, "message": message},
    )


def _load_physical(request: dict[str, Any]) -> PhysicalSchema:
    """Introspect a live source, or load a previously captured PhysicalSchema.

    Runs the pipeline the CLI runs too (``pipeline.py``), so a contract caller gets the
    same bitemporal stamp as a CLI caller. ``input.previousAnalysis`` gates it.
    """
    given = request.get("input") or {}
    previous = given.get("previousAnalysis") if isinstance(given, dict) else None
    prior = PriorRun.from_metadata(previous) if isinstance(previous, dict) else None

    if isinstance(given, dict) and given.get("physical") is not None:
        captured = PhysicalSchema.model_validate(given["physical"])
        return load_physical(captured=captured, prior=prior)

    source = request.get("source")
    if not isinstance(source, dict) or not source.get("type") or not source.get("url"):
        raise ValueError("request needs 'input.physical' or 'source' with 'type' and 'url'")
    return load_physical(
        source_type=source["type"],
        url=source["url"],
        schema_name=source.get("schema", "public"),
        source_params=source.get("params"),
        prior=prior,
    )


def run_tool(request: dict[str, Any]) -> dict[str, Any]:
    """Execute one tool-contract operation and return the response envelope."""
    if not isinstance(request, dict):
        return _error(None, "INVALID_REQUEST", "request must be a JSON object")

    operation = request.get("operation")
    if operation not in _OPERATIONS:
        return _error(
            request, "INVALID_REQUEST",
            f"operation must be one of {', '.join(_OPERATIONS)}",
        )

    try:
        physical = _load_physical(request)
    except ValueError as err:
        return _error(request, "INVALID_REQUEST", str(err))
    except Exception as err:  # noqa: BLE001 - surface source/connection failures cleanly
        return _error(request, "SOURCE_ERROR", str(err))

    try:
        if operation == "snapshot":
            return _response(
                request, ok=True, result={"physical": physical.model_dump(mode="json")}
            )

        analysis = RelationalSchemaAnalyzer().analyze(physical)

        if operation == "analyze":
            return _response(request, ok=True, result={"analysis": analysis.to_bundle()})

        if operation == "r2rml":
            r2rml_opts = request.get("r2rml") or {}
            r2rml_kwargs = {}
            if r2rml_opts.get("iriBase"):
                r2rml_kwargs["base_iri"] = r2rml_opts["iriBase"]
            if r2rml_opts.get("dataIriBase"):
                r2rml_kwargs["data_iri"] = r2rml_opts["dataIriBase"]
            if r2rml_opts.get("mappingIriBase"):
                r2rml_kwargs["mapping_iri"] = r2rml_opts["mappingIriBase"]
            return _response(
                request,
                ok=True,
                result={
                    "r2rml": {
                        "format": "turtle",
                        "content": export_r2rml_turtle(analysis, **r2rml_kwargs),
                    }
                },
            )

        # operation == "owl"
        owl_opts = request.get("owl") or {}
        fmt = owl_opts.get("format", "turtle")
        kwargs = {}
        if owl_opts.get("iriBase"):
            kwargs["base_iri"] = owl_opts["iriBase"]
        if owl_opts.get("physIriBase"):
            kwargs["phys_iri"] = owl_opts["physIriBase"]
        content: Any
        if fmt == "jsonld":
            content = export_owl_jsonld(analysis, **kwargs)
        elif fmt == "turtle":
            content = export_owl_turtle(analysis, **kwargs)
        else:
            return _error(request, "INVALID_REQUEST", "owl.format must be turtle or jsonld")
        return _response(request, ok=True, result={"owl": {"format": fmt, "content": content}})
    except Exception as err:  # noqa: BLE001 - never let an operation crash the server
        return _error(request, "ANALYSIS_ERROR", str(err))
