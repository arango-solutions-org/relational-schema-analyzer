"""Keep credentials out of error messages, tracebacks and logs (#57).

A database driver's error text goes into RSA's own error message, and from there into
logs, CLI output, MCP and tool-contract responses, and any printed traceback. Whether a
driver repeats the password or token depends on the driver and the failure: Snowflake's
was caught doing it (#5); Postgres and MySQL were not, in the failures tried. So every
connector treats driver text as untrusted:

* :func:`secrets_in` collects the secrets a connection string or parameter dict carries;
* :func:`redact` replaces each of them with ``***``;
* :func:`connection_error` builds the exception to raise **``from None``**. A chained
  ``__cause__`` is the driver's original exception, raw, and any traceback prints it.

The driver's exception type and its (masked) text stay in the message, which is most of
what a person debugging a failed connection needs.

No imports beyond the standard library, so connectors and samplers can both use it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit

#: Parameter names whose value is a credential, matched case-insensitively. Covers the
#: drivers RSA uses: Postgres/libpq, MySQL, SQL Server, Databricks (``access_token``),
#: Snowflake (password, key file and its passphrase, OAuth token, MFA passcode) and
#: DuckDB/MotherDuck (``motherduck_token``).
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "passcode",
        "token",
        "access_token",
        "motherduck_token",
        "private_key_file",
        "private_key_file_pwd",
        "private_key_passphrase",
        "passphrase",
        "client_secret",
        "secret",
        "api_key",
    }
)

MASK = "***"

# libpq's key=value form: ``host=db password=s3cret`` or ``password='s3 cret'``.
_KEYWORD_VALUE = re.compile(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)")


def _sensitive(name: str) -> bool:
    return name.lower() in SENSITIVE_KEYS


def _with_encodings(value: str) -> list[str]:
    """The value as given, decoded and percent-encoded: whatever form it was handed in,
    a driver (or a URL it repeats) may show another."""
    if not value:
        return []
    decoded = unquote(value)
    return [value, decoded, quote(decoded, safe=""), quote_plus(decoded, safe="")]


def secrets_in(*sources: Any) -> list[str]:
    """Every credential in ``sources``, longest first.

    A source is a connection string (URL or libpq ``key=value`` form), a mapping of
    connection parameters, or ``None``. Anything else is ignored.
    """
    found: set[str] = set()
    for source in sources:
        if isinstance(source, Mapping):
            for key, value in source.items():
                if isinstance(key, str) and _sensitive(key) and value not in (None, ""):
                    found.update(_with_encodings(str(value)))
        elif isinstance(source, str) and source:
            found.update(_secrets_in_string(source))
    return sorted((s for s in found if s), key=len, reverse=True)


def _secrets_in_string(text: str) -> set[str]:
    found: set[str] = set()
    if "://" in text:
        try:
            password = urlsplit(text).password
        except ValueError:
            password = None  # a malformed URL still gets the scans below
        if password:
            found.update(_with_encodings(password))
    if "?" in text:  # a query string, with or without a scheme (``md:db?motherduck_token=``)
        for name, value in parse_qsl(text.split("?", 1)[1], keep_blank_values=False):
            if _sensitive(name):
                found.update(_with_encodings(value))
    for name, value in _KEYWORD_VALUE.findall(text):
        if _sensitive(name):
            if len(value) >= 2 and value[0] == value[-1] == "'":
                value = value[1:-1].replace("\\'", "'").replace("\\\\", "\\")
            # Unquoted, the value may run into a query string's next parameter: mask both
            # readings, so neither the whole run nor its first segment is left.
            found.update(_with_encodings(value))
            found.update(_with_encodings(value.split("&", 1)[0]))
    return found


def redact(text: str, secrets: Iterable[str]) -> str:
    """``text`` with every secret replaced by ``***``, longest first."""
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, MASK)
    return text


def connection_error(what: str, err: BaseException, secrets: Iterable[str]) -> RuntimeError:
    """The exception to raise ``from None`` when a connection (or a read through it) fails.

    ``"<what>: <DriverError>: <driver text, secrets masked>"``.
    """
    return RuntimeError(f"{what}: {type(err).__name__}: {redact(str(err), secrets)}")
