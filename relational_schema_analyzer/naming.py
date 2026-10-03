"""Identifier naming helpers (dependency-free subset).

Extracted from ``r2g/src/r2g/naming.py``. Only the paradigm-neutral string
utilities are lifted (``split_identifier``, ``pluralize``, ``singularize``,
``convert_identifier``). The ``apply_naming_convention`` routine — which mutates
the ArangoDB ``MappingConfig`` — stays in ``r2g``.
"""

from __future__ import annotations

import re
from typing import Literal

NameCase = Literal["preserve", "snake", "camel", "pascal"]

# Split an identifier into words. Handles snake_case, kebab-case, dotted, spaced,
# camelCase, PascalCase and acronym runs (e.g. "HTTPServer" -> ["HTTP", "Server"]).
_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+|[0-9]+")


def split_identifier(name: str) -> list[str]:
    """Break ``name`` into normalized word tokens (order preserved)."""
    words: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        if part:
            words.extend(_WORD_RE.findall(part))
    return [w for w in words if w]


def pluralize(word: str) -> str:
    """Best-effort English plural for table / relationship name heuristics.

    Intentionally simple (no irregular-noun table): ``y`` after a consonant →
    ``ies``; sibilant endings (``s``/``x``/``z``/``ch``/``sh``) → ``es``; else
    append ``s``. Used only for fuzzy name matching, never for stored names.
    """
    if not word:
        return word
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def singularize(word: str) -> str:
    """Best-effort English singular, the inverse of :func:`pluralize`.

    ``ies`` → ``y``; ``ses``/``ches``/``shes``/``xes``/``zes`` → drop ``es``; a
    trailing ``s`` (but not ``ss``) → drop ``s``. Used only for fuzzy name
    matching (e.g. ``orders`` ↔ ``order``), never for stored names.

    Matching is case-insensitive and the replacement follows the case of the
    trailing letter, so Snowflake-style ``CUSTOMERS`` singularizes like
    ``customers``. It used to come back unchanged, which silently disabled
    every upper-case match (the same fix as r2g-arango#4).
    """
    if not word:
        return word
    lower = word.lower()
    if lower.endswith("ies") and len(word) > 3:
        return word[:-3] + ("Y" if word[-1].isupper() else "y")
    if lower.endswith(("ses", "ches", "shes", "xes", "zes")):
        return word[:-2]
    if lower.endswith("s") and not lower.endswith("ss"):
        return word[:-1]
    return word


def convert_identifier(name: str, style: NameCase) -> str:
    """Re-case ``name`` into ``style``.

    Returns ``name`` unchanged when ``style`` is ``"preserve"`` or when the
    identifier yields no word tokens (e.g. it is empty or all punctuation).
    """
    if style == "preserve" or not name:
        return name
    words = split_identifier(name)
    if not words:
        return name
    lower = [w.lower() for w in words]
    if style == "snake":
        return "_".join(lower)
    if style == "pascal":
        return "".join(w.capitalize() for w in lower)
    if style == "camel":
        return lower[0] + "".join(w.capitalize() for w in lower[1:])
    return name
