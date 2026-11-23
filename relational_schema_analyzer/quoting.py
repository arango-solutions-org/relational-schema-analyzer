"""The three SQL identifier-quoting rules, one function each.

Table and column names cannot travel as bind parameters, so every engine-specific
query quotes them, and the quote character inside a name must be doubled or the
statement breaks (#55). Each rule lives here once; the connectors and samplers
keep their own names for them (``quote_identifier``, ``_quote_ident``, ``_qi``)
as aliases or thin wrappers.

No imports, so anything in the package can use it without an import cycle.
"""

from __future__ import annotations


def quote_ansi(name: str) -> str:
    """``"name"``, doubling ``"``: Postgres, Snowflake, DuckDB, SQLite, R2RML."""
    return '"' + name.replace('"', '""') + '"'


def quote_backtick(name: str) -> str:
    """```name```, doubling a backtick: MySQL / MariaDB and Databricks."""
    return "`" + name.replace("`", "``") + "`"


def quote_bracket(name: str) -> str:
    """``[name]``, doubling ``]``: SQL Server."""
    return "[" + name.replace("]", "]]") + "]"
