"""Minimal structured-logging shim.

The connectors call ``logger.info("event_name", key=value, ...)`` in the
structlog style. To keep this library dependency-light (no structlog, no
credential-scrubbing machinery from the originating project), we provide a tiny
adapter over the stdlib :mod:`logging` module that accepts arbitrary keyword
fields and renders them as ``key=value`` pairs.

If ``structlog`` is installed, we defer to it so applications that already
configure structured logging get native structured events.
"""

from __future__ import annotations

import logging
import sys
from typing import Any


def keep_logs_off_stdout() -> None:
    """Send this process's log output to stderr, so stdout carries only RSA's output.

    A library must not configure logging. But the CLI and the MCP server are
    applications, and there RSA both may and must: the CLI's stdout is the bundle, and
    the stdio MCP server's stdout is the JSON-RPC channel. Both call this first.

    The bug this closes (reported by r2g for the CLI; the MCP server had the same
    hole): this module defers to ``structlog`` when it is installed, and an
    **unconfigured** structlog writes through ``PrintLogger`` to **stdout**, at debug
    level. So a single log line during a run lands in the middle of the output::

        $ relational-schema-analyzer analyze --source csv --url ./d --prior-run prev.json
        2026-10-26 12:03:51 [debug    ] bitemporal_fingerprint_unchanged carried_from=...
        {"conceptualSchema": ...

    and the caller's ``json.load`` fails. Over MCP stdio, the client reads the log
    line as a malformed protocol message.

    Invisible without structlog: RSA requires it nowhere, and the stdlib fallback
    writes to **stderr** at WARNING, where both the stream and the level hide it.

    An application that has already configured structlog keeps its own configuration;
    this only claims the default.
    """
    try:
        import structlog
    except ImportError:  # the stdlib fallback already writes to stderr
        return
    if structlog.is_configured():
        return  # the embedding application owns its logging; leave it alone
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))


try:  # pragma: no cover - exercised indirectly when structlog is present
    import structlog

    def get_logger(name: str) -> Any:
        return structlog.get_logger(name)

except ImportError:  # pragma: no cover - default lightweight path

    class _BoundLogger:
        """stdlib-backed logger with a structlog-like keyword interface."""

        def __init__(self, name: str) -> None:
            self._log = logging.getLogger(name)

        @staticmethod
        def _render(event: str, fields: dict[str, Any]) -> str:
            if not fields:
                return event
            extras = " ".join(f"{k}={v!r}" for k, v in fields.items())
            return f"{event} {extras}"

        def debug(self, event: str, **fields: Any) -> None:
            self._log.debug(self._render(event, fields))

        def info(self, event: str, **fields: Any) -> None:
            self._log.info(self._render(event, fields))

        def warning(self, event: str, **fields: Any) -> None:
            self._log.warning(self._render(event, fields))

        def error(self, event: str, **fields: Any) -> None:
            self._log.error(self._render(event, fields))

    def get_logger(name: str) -> Any:
        return _BoundLogger(name)
