"""Decorator internals for @server.tool(), @server.resource(), @server.prompt().

Introspects function signatures at registration time:
- Extracts parameter names, types, defaults
- Detects ``Depends()`` markers for dependency injection
- Detects ``RequestContext`` typed parameters for auto-injection
- Builds Pydantic models for input validation
- Uses docstrings as descriptions when none provided
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

from ._context import RequestContext
from ._types import PromptDef, ResourceDef, ToolDef
from ._validation import build_input_model


class _DependsMarker:
    """Sentinel that marks a parameter as dependency-injected.

    This is the *internal* type.  The public ``Depends`` class is in
    ``_di.py`` and creates instances of this marker.
    """

    def __init__(self, dependency: Callable[..., Any], *, use_cache: bool = True) -> None:
        self.dependency = dependency
        self.use_cache = use_cache


def _get_description(func: Callable[..., Any], explicit: str | None) -> str:
    """Return *explicit* description, or fall back to the first line of the docstring."""
    if explicit:
        return explicit
    doc = inspect.getdoc(func)
    if doc:
        return doc.split("\n")[0].strip()
    return func.__name__


def _excluded_params(func: Callable[..., Any]) -> set[str]:
    """Identify parameters that should NOT appear in the input schema.

    Excluded:
    - ``self``
    - Parameters annotated as ``RequestContext``
    - Parameters whose default is a ``_DependsMarker``
    """
    excluded: set[str] = set()
    sig = inspect.signature(func)
    hints = {}
    try:
        hints = get_type_hints(func)
    except Exception:
        pass

    for name, param in sig.parameters.items():
        if name == "self":
            excluded.add(name)
            continue
        # Check type annotation
        ann = hints.get(name, param.annotation)
        if ann is RequestContext:
            excluded.add(name)
            continue
        # Check if default is a Depends() marker
        if isinstance(param.default, _DependsMarker):
            excluded.add(name)
            continue

    return excluded


def build_tool_def(
    func: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    auth: bool = False,
    rate_limit: str | None = None,
    timeout: float | None = None,
    guards: list[Any] | None = None,
    roles: list[str] | None = None,
    annotations: Any | None = None,
    max_concurrent: int | None = None,
) -> ToolDef:
    """Build a ``ToolDef`` from a decorated function."""
    tool_name = name or func.__name__
    tool_desc = _get_description(func, description)
    excluded = _excluded_params(func)

    _, schema = build_input_model(func, exclude=excluded)

    return ToolDef(
        name=tool_name,
        description=tool_desc,
        handler=func,
        input_schema=schema,
        tags=tags or [],
        auth=auth,
        rate_limit=rate_limit,
        timeout=timeout,
        guards=guards or [],
        roles=roles or [],
        annotations=annotations,
        max_concurrent=max_concurrent,
    )


def build_resource_def(
    func: Callable[..., Any],
    *,
    uri: str,
    name: str | None = None,
    description: str | None = None,
    mime_type: str = "text/plain",
    is_template: bool = False,
) -> ResourceDef:
    """Build a ``ResourceDef`` from a decorated function."""
    res_name = name or func.__name__
    res_desc = _get_description(func, description)

    return ResourceDef(
        uri=uri,
        name=res_name,
        description=res_desc,
        handler=func,
        mime_type=mime_type,
        is_template=is_template,
    )


def build_prompt_def(
    func: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
) -> PromptDef:
    """Build a ``PromptDef`` from a decorated function."""
    prompt_name = name or func.__name__
    prompt_desc = _get_description(func, description)

    # Build argument list from signature (for MCP PromptArgument)
    sig = inspect.signature(func)
    excluded = _excluded_params(func)
    arguments: list[dict[str, Any]] = []
    for param_name, param in sig.parameters.items():
        if param_name in excluded:
            continue
        arg: dict[str, Any] = {"name": param_name}
        doc = inspect.getdoc(func) or ""
        # Try to extract per-param description from docstring (Args: section)
        arg["description"] = _extract_param_doc(doc, param_name) or param_name
        arg["required"] = param.default is inspect.Parameter.empty
        arguments.append(arg)

    return PromptDef(
        name=prompt_name,
        description=prompt_desc,
        handler=func,
        arguments=arguments,
    )


def _extract_param_doc(docstring: str, param_name: str) -> str | None:
    """Extract a parameter description from a Google-style docstring.

    Looks for ``param_name:`` or ``param_name (type):`` in an Args section.
    """
    in_args = False
    for line in docstring.split("\n"):
        stripped = line.strip()
        if stripped.lower().startswith("args:"):
            in_args = True
            continue
        if in_args:
            if not stripped or (not line.startswith(" ") and not line.startswith("\t")):
                in_args = False
                continue
            # Match "param_name:" or "param_name (type):"
            if stripped.startswith(f"{param_name}:") or stripped.startswith(f"{param_name} ("):
                _, _, rest = stripped.partition(":")
                return rest.strip() or None
    return None
