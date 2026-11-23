"""MCP (Model Context Protocol) server wrapping the v1 tool contract.

Requires the optional extra: ``pip install 'relational-schema-analyzer[mcp]'``.

Transports:

* **stdio** (default) — local IDE / Cursor use::

      relational-schema-analyzer-mcp

* **sse** / **streamable-http** — remote agents::

      relational-schema-analyzer-mcp --transport sse --host 0.0.0.0 --port 8000

**Security.** Remote transports can drive the analyzer against arbitrary sources,
so they are gated by a bearer token: set ``RSA_MCP_TOKEN`` and every HTTP request
must send ``Authorization: Bearer <token>`` (constant-time compared). When unset,
the server still starts but logs a loud warning — never expose an unauthenticated
remote server to an untrusted network. stdio (local) needs no token.

Env fallbacks for the CLI flags: ``RSA_MCP_TRANSPORT`` / ``RSA_MCP_HOST`` /
``RSA_MCP_PORT``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from typing import Any

from .log import keep_logs_off_stdout
from .tool import CONTRACT_VERSION, _error, run_tool

logger = logging.getLogger(__name__)

DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8000
REMOTE_TRANSPORTS = ("sse", "streamable-http")

TRANSPORT_ENV_VAR = "RSA_MCP_TRANSPORT"
HOST_ENV_VAR = "RSA_MCP_HOST"
PORT_ENV_VAR = "RSA_MCP_PORT"
TOKEN_ENV_VAR = "RSA_MCP_TOKEN"


def _require_fastmcp() -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        print(
            "The MCP server requires the 'mcp' package. Install with:\n"
            "  pip install 'relational-schema-analyzer[mcp]'",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    return FastMCP


def _bearer_token_valid(auth_header: str | None, expected: str) -> bool:
    """Constant-time bearer-token check. Empty ``expected`` means no token configured."""
    if not expected:
        return True
    if not auth_header:
        return False
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return secrets.compare_digest(parts[1].strip(), expected)


def _install_auth(app: Any, expected_token: str) -> None:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    async def dispatch(request: Any, call_next: Any) -> Any:
        if not _bearer_token_valid(request.headers.get("authorization"), expected_token):
            return JSONResponse(
                {"ok": False, "error": {"code": "UNAUTHENTICATED",
                                        "message": "missing or invalid bearer token"}},
                status_code=401,
            )
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=dispatch)


def _typed_request(operation: str, **fields: Any) -> dict[str, Any]:
    """Build a v1 request dict for one operation, dropping unset (None) fields."""
    req: dict[str, Any] = {"contractVersion": CONTRACT_VERSION, "operation": operation}
    for key, value in fields.items():
        if value is not None:
            req[key] = value
    return req


def build_app(*, host: str | None = None, port: int | None = None) -> Any:
    """Construct the FastMCP app with generic + per-operation tools."""
    FastMCP = _require_fastmcp()

    mcp = FastMCP(
        "relational-schema-analyzer",
        host=host or DEFAULT_MCP_HOST,
        port=port or DEFAULT_MCP_PORT,
        instructions=(
            "Relational schema analyzer. Use the per-operation tools "
            "(relational_schema_analyzer_snapshot/analyze/owl/r2rml) or the generic "
            "relational_schema_analyzer_run with a v1 tool-contract request dict."
        ),
    )

    @mcp.tool()
    def relational_schema_analyzer_run(request: dict[str, Any]) -> dict[str, Any]:
        """Execute one operation (snapshot | analyze | owl | r2rml) from a v1 request dict."""
        return run_tool(request)

    @mcp.tool()
    def relational_schema_analyzer_run_json(request_json: str) -> dict[str, Any]:
        """Same as ..._run but accepts a JSON string (convenient for some clients)."""
        try:
            req = json.loads(request_json)
        except json.JSONDecodeError as e:
            return _error(None, "INVALID_REQUEST", f"Invalid JSON: {e}")
        if not isinstance(req, dict):
            return _error(None, "INVALID_REQUEST", "request_json must be an object")
        return run_tool(req)

    @mcp.tool()
    def relational_schema_analyzer_snapshot(source: dict[str, Any]) -> dict[str, Any]:
        """Physical-schema snapshot. ``source`` = {type, url, schema?, params?}."""
        return run_tool(_typed_request("snapshot", source=source))

    @mcp.tool()
    def relational_schema_analyzer_analyze(
        source: dict[str, Any] | None = None, input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Analyze a source (or a captured ``input.physical``) into the bundle.

        Pass a previous run's bundle as ``input.previousAnalysis`` to enable bitemporal
        fingerprint-continuity: without it the valid time of an unchanged schema can only be
        reported as ``observed`` (or an ungated catalog date), never carried back to when the
        definition actually became true.
        """
        return run_tool(_typed_request("analyze", source=source, input=input))

    @mcp.tool()
    def relational_schema_analyzer_owl(
        source: dict[str, Any] | None = None,
        input: dict[str, Any] | None = None,
        owl: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """OWL export. ``owl`` = {format: turtle|jsonld, iriBase?, physIriBase?}."""
        return run_tool(_typed_request("owl", source=source, input=input, owl=owl))

    @mcp.tool()
    def relational_schema_analyzer_r2rml(
        source: dict[str, Any] | None = None,
        input: dict[str, Any] | None = None,
        r2rml: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """R2RML mapping export (Turtle).

        ``r2rml`` = {iriBase?, dataIriBase?, mappingIriBase?}. Pair it with the
        ``owl`` tool using the same ``iriBase`` to get an ontology plus a mapping
        that populates it — a virtual knowledge graph over the live database.
        """
        return run_tool(_typed_request("r2rml", source=source, input=input, r2rml=r2rml))

    return mcp


def serve(
    transport: str = "stdio",
    *,
    host: str | None = None,
    port: int | None = None,
    token: str | None = None,
) -> None:
    """Run the MCP server on the chosen transport."""
    # Over stdio, stdout is the JSON-RPC channel: one log line there is a malformed message.
    keep_logs_off_stdout()
    app_obj = build_app(host=host, port=port)

    if transport == "stdio":
        app_obj.run(transport="stdio")
        return

    if transport not in REMOTE_TRANSPORTS:
        raise ValueError(f"Unsupported transport: {transport!r}")

    if not token:
        logger.warning(
            "Serving MCP over %s with no auth token (%s unset): anyone who can reach "
            "%s:%s can drive the analyzer against arbitrary sources. Set %s before "
            "exposing this server.",
            transport, TOKEN_ENV_VAR, app_obj.settings.host, app_obj.settings.port,
            TOKEN_ENV_VAR,
        )

    starlette_app = app_obj.sse_app() if transport == "sse" else app_obj.streamable_http_app()
    if token:
        _install_auth(starlette_app, token)

    import uvicorn

    uvicorn.run(starlette_app, host=app_obj.settings.host, port=app_obj.settings.port)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="relational-schema-analyzer-mcp",
        description="MCP server for the relational schema analyzer (v1 tool contract).",
    )
    parser.add_argument(
        "--transport", choices=("stdio", *REMOTE_TRANSPORTS),
        default=os.environ.get(TRANSPORT_ENV_VAR, "stdio"),
        help="Transport to serve (default: stdio).",
    )
    parser.add_argument(
        "--host", default=os.environ.get(HOST_ENV_VAR, DEFAULT_MCP_HOST),
        help=f"Bind host for remote transports (default: {DEFAULT_MCP_HOST}).",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get(PORT_ENV_VAR, DEFAULT_MCP_PORT)),
        help=f"Bind port for remote transports (default: {DEFAULT_MCP_PORT}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    serve(args.transport, host=args.host, port=args.port, token=os.environ.get(TOKEN_ENV_VAR))


if __name__ == "__main__":
    main()
