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
from typing import Any

from .exceptions import EnvVarNotFoundError

# Regex pattern for ${VAR_NAME} or ${VAR_NAME:-default_value}
# Matches: ${VARIABLE_NAME} or ${VARIABLE_NAME:-some default value}
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*?)(?::-(.*?))?\}")


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

    def replacer(match: re.Match[str]) -> str:
        """Replace a single ${VAR} match with its value."""
        var_name = match.group(1)
        default_value = match.group(2)  # None if no :- clause

        env_value = os.environ.get(var_name)

        if env_value is not None:
            return env_value

        if default_value is not None:
            return default_value

        if allow_missing:
            return match.group(0)  # Return original ${VAR}

        raise EnvVarNotFoundError(var_name, context)

    return ENV_VAR_PATTERN.sub(replacer, value)


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
            resolved_list = []
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
        for match in ENV_VAR_PATTERN.finditer(s):
            var_name = match.group(1)
            default_value = match.group(2)
            # Only required if no default
            if default_value is None and var_name not in os.environ:
                missing.add(var_name)

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
