"""Environment variable resolution for .superagent configuration.

This module provides utilities to parse and resolve ${ENV_VAR} syntax
in configuration strings, with support for default values and recursive
resolution through nested data structures.

Supported syntax:
    - ${VAR_NAME} - Required variable (raises if not found)
    - ${VAR_NAME:-default_value} - Optional variable with default

Examples:
    >>> import os
    >>> os.environ["API_KEY"] = "secret123"
    >>> resolve_env_var("Bearer ${API_KEY}")
    'Bearer secret123'
    >>> resolve_env_var("${MISSING:-default}")
    'default'
"""

from __future__ import annotations

import os
import re
import string
from collections.abc import Iterator
from typing import Any, NamedTuple

from .exceptions import EnvVarNotFoundError

# Regex pattern for ${VAR_NAME} or ${VAR_NAME:-default_value}
# Matches: ${VARIABLE_NAME} or ${VARIABLE_NAME:-some default value}
#
# Kept as the reference definition of the syntax (and for callers that
# import it).  Matching it against untrusted text takes quadratic time on
# crafted input (``${A:-${a:-${a:-...`` with no closing brace), so this
# module matches with :func:`iter_env_references` instead, which finds
# exactly the same references in one left-to-right pass.
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*?)(?::-(.*?))?\}")

_NAME_START = frozenset(string.ascii_letters + "_")
_NAME_CHARS = _NAME_START | frozenset(string.digits)


class EnvReference(NamedTuple):
    """One ``${VAR}`` / ``${VAR:-default}`` reference in a string."""

    start: int
    """Index of the ``$``."""
    end: int
    """Index just past the closing ``}``."""
    name: str
    """The variable name."""
    default: str | None
    """The text after ``:-`` (``None`` without a default)."""


def iter_env_references(text: str) -> Iterator[EnvReference]:
    """The ``${VAR}`` references in *text*, left to right, in linear time.

    Finds the same references as ``ENV_VAR_PATTERN.finditer(text)``: a
    name is ``[A-Za-z_][A-Za-z0-9_]*``; a default runs to the first ``}``
    and can't span a line break; references don't nest (in
    ``${A:-${B}}`` the default of ``A`` is ``${B``).  Unlike the regex,
    a string full of unterminated references can't make it backtrack:
    every character is looked at a bounded number of times.
    """
    n = len(text)
    # Positions of the next "}" / line break at or after the current
    # default; -2 = not looked up yet, -1 = there is none.  Defaults start
    # further right each time, so these only move forward.
    next_close = next_newline = -2
    pos = text.find("${")
    while pos != -1:
        k = pos + 2
        if k < n and text[k] in _NAME_START:
            k += 1
            while k < n and text[k] in _NAME_CHARS:
                k += 1
            if k < n and text[k] == "}":
                yield EnvReference(pos, k + 1, text[pos + 2 : k], None)
                pos = text.find("${", k + 1)
                continue
            if text.startswith(":-", k):
                default_start = k + 2
                if next_close != -1 and next_close < default_start:
                    next_close = text.find("}", default_start)
                if next_close == -1:
                    return  # no "}" left: no later reference can close either
                if next_newline != -1 and next_newline < default_start:
                    next_newline = text.find("\n", default_start)
                if next_newline == -1 or next_newline > next_close:
                    yield EnvReference(
                        pos, next_close + 1, text[pos + 2 : k], text[default_start:next_close]
                    )
                    pos = text.find("${", next_close + 1)
                    continue
        pos = text.find("${", pos + 1)


def has_env_reference(text: str) -> bool:
    """Whether *text* holds at least one ``${VAR}`` reference."""
    return next(iter_env_references(text), None) is not None


def resolve_env_var(
    value: str,
    *,
    context: str | None = None,
    allow_missing: bool = False,
) -> str:
    """Resolve environment variables in a string.

    Supports syntax:
    - ${VAR_NAME} - Required variable (raises if not found)
    - ${VAR_NAME:-default} - Optional variable with default

    Args:
        value: String potentially containing ${VAR} references.
        context: Optional context for error messages (e.g., "servers.math.url").
        allow_missing: If True, leave unresolved vars as-is instead of raising.

    Returns:
        String with all environment variables resolved.

    Raises:
        EnvVarNotFoundError: If a required variable is not found and
            allow_missing is False.

    Examples:
        >>> os.environ["API_KEY"] = "secret123"
        >>> resolve_env_var("Bearer ${API_KEY}")
        'Bearer secret123'
        >>> resolve_env_var("${MISSING:-default}")
        'default'
        >>> resolve_env_var("${MISSING}")  # Raises EnvVarNotFoundError
    """

    def replacer(ref: EnvReference) -> str:
        """Replace a single ${VAR} reference with its value."""
        var_name = ref.name
        default_value = ref.default  # None if no :- clause

        env_value = os.environ.get(var_name)

        if env_value is not None:
            return env_value

        if default_value is not None:
            return default_value

        if allow_missing:
            return value[ref.start : ref.end]  # Return original ${VAR}

        raise EnvVarNotFoundError(var_name, context)

    parts: list[str] = []
    last = 0
    for ref in iter_env_references(value):
        parts.append(value[last : ref.start])
        parts.append(replacer(ref))
        last = ref.end
    if not parts:
        return value
    parts.append(value[last:])
    return "".join(parts)


def resolve_env_in_dict(
    data: dict[str, Any],
    *,
    context_prefix: str = "",
) -> dict[str, Any]:
    """Recursively resolve environment variables in a dictionary.

    Resolves ${VAR} references in all string values throughout a nested
    dictionary structure, including in lists and nested dicts.

    Args:
        data: Dictionary potentially containing env var references.
        context_prefix: Prefix for error context (e.g., "servers.math").

    Returns:
        New dictionary with all env vars resolved.

    Raises:
        EnvVarNotFoundError: If any required variable is not found.

    Examples:
        >>> os.environ["TOKEN"] = "abc123"
        >>> resolve_env_in_dict({"auth": "${TOKEN}"})
        {'auth': 'abc123'}
        >>> resolve_env_in_dict({"server": {"url": "${API_URL}"}})
        {'server': {'url': 'http://...'}}
    """
    result: dict[str, Any] = {}

    for key, value in data.items():
        ctx = f"{context_prefix}.{key}" if context_prefix else key

        if isinstance(value, str):
            result[key] = resolve_env_var(value, context=ctx)
        elif isinstance(value, dict):
            result[key] = resolve_env_in_dict(value, context_prefix=ctx)
        elif isinstance(value, list):
            resolved_list: list[Any] = []
            for i, item in enumerate(value):
                item_ctx = f"{ctx}[{i}]"
                if isinstance(item, str):
                    resolved_list.append(resolve_env_var(item, context=item_ctx))
                elif isinstance(item, dict):
                    resolved_list.append(resolve_env_in_dict(item, context_prefix=item_ctx))
                elif isinstance(item, list):
                    # Nested lists: wrap in a dict, resolve, extract
                    resolved_list.append(
                        resolve_env_in_dict({"_": item}, context_prefix=item_ctx)["_"]
                    )
                else:
                    resolved_list.append(item)
            result[key] = resolved_list
        else:
            result[key] = value

    return result


def validate_all_env_vars_available(
    data: dict[str, Any],
) -> list[str]:
    """Check which environment variables are referenced but not available.

    This is useful for pre-validation before attempting to load a config.
    Scans the entire data structure for ${VAR} references and checks if
    each variable is set in the environment.

    Args:
        data: Parsed configuration data (dict from YAML/JSON).

    Returns:
        List of missing environment variable names (empty if all available).
        Variables with defaults (${VAR:-default}) are not included.

    Examples:
        >>> data = {"api_key": "${OPENAI_API_KEY}", "url": "${API_URL:-http://default}"}
        >>> missing = validate_all_env_vars_available(data)
        >>> if missing:
        ...     print(f"Missing env vars: {', '.join(missing)}")
    """
    missing: set[str] = set()

    def check_string(s: str) -> None:
        """Extract and check env vars from a single string."""
        for ref in iter_env_references(s):
            # Only required if no default
            if ref.default is None and ref.name not in os.environ:
                missing.add(ref.name)

    def check_dict(d: dict[str, Any]) -> None:
        """Recursively check a dict for env var references."""
        for value in d.values():
            if isinstance(value, str):
                check_string(value)
            elif isinstance(value, dict):
                check_dict(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        check_string(item)

    check_dict(data)

    return sorted(missing)
