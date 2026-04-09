"""Tool transforms for MCP servers.

Transforms modify tool definitions at discovery time (``list_tools``),
enabling patterns like namespace prefixing, visibility filtering, and
dynamic tool description rewriting.

Example::

    from promptise.mcp.server import MCPServer, NamespaceTransform

    server = MCPServer(name="api")
    server.add_transform(NamespaceTransform(prefix="myapp"))
    # All tool names will be prefixed with "myapp_" when listed

"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from ._context import RequestContext
from ._types import ToolDef


class ToolTransform(Protocol):
    """Protocol for tool transforms.

    A transform receives a list of ``ToolDef`` objects and returns a
    (potentially modified) list.  Transforms are applied in order during
    ``list_tools`` — each sees the output of the previous one.
    """

    def apply(self, tools: list[ToolDef], ctx: RequestContext | None = None) -> list[ToolDef]: ...


class NamespaceTransform:
    """Prefix all tool names with a namespace.

    Example::

        transform = NamespaceTransform(prefix="myapp")
        # "search" → "myapp_search"
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def apply(self, tools: list[ToolDef], ctx: RequestContext | None = None) -> list[ToolDef]:
        result: list[ToolDef] = []
        for t in tools:
            new_name = f"{self._prefix}_{t.name}" if self._prefix else t.name
            result.append(replace(t, name=new_name))
        return result


class VisibilityTransform:
    """Hide tools based on a predicate.

    Hidden tools are removed from ``list_tools`` results but remain
    callable (for backwards compatibility with cached tool lists).

    Example::

        transform = VisibilityTransform(
            hidden={"admin_delete": lambda ctx: "admin" not in ctx.state.get("roles", set())}
        )
    """

    def __init__(
        self,
        hidden: dict[str, Callable[[RequestContext | None], bool]] | None = None,
    ) -> None:
        self._hidden = hidden or {}

    def apply(self, tools: list[ToolDef], ctx: RequestContext | None = None) -> list[ToolDef]:
        result: list[ToolDef] = []
        for t in tools:
            predicate = self._hidden.get(t.name)
            if predicate is not None and predicate(ctx):
                continue
            result.append(t)
        return result


class TagFilterTransform:
    """Only expose tools that have at least one of the required tags.

    Example::

        transform = TagFilterTransform(required_tags={"public"})
        # Only tools tagged "public" are listed
    """

    def __init__(self, required_tags: set[str]) -> None:
        self._required = required_tags

    def apply(self, tools: list[ToolDef], ctx: RequestContext | None = None) -> list[ToolDef]:
        return [t for t in tools if set(t.tags) & self._required]
