"""Tool transforms for MCP servers.

Transforms change the tool list a client discovers (``tools/list`` and the
``docs://manifest`` resource): prefix names, hide tools per caller, keep
only tagged tools, rewrite descriptions.  They run in the order they were
added, after ``hide_unauthorized_tools`` filtering, each on the output of
the previous one.

Transforms shape discovery only.  A tool a transform hides can still be
called by its name, so access decisions belong in guards (``roles=``,
``guards=``).  A call by a renamed tool's listed name reaches the tool:
renaming transforms implement ``resolve_name()``, which maps a listed name
back to the registered one.

Example::

    from promptise.mcp.server import MCPServer, NamespaceTransform

    server = MCPServer(name="api")
    server.add_transform(NamespaceTransform(prefix="myapp"))
    # Clients see "myapp_search"; a call to "myapp_search" runs "search"
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol

from ._context import RequestContext, clear_context, set_context
from ._types import ToolDef

logger = logging.getLogger("promptise.server")


class ToolTransform(Protocol):
    """Protocol for tool transforms.

    A transform receives a list of ``ToolDef`` objects and returns a
    (potentially modified) list.  Transforms added with
    ``MCPServer.add_transform()`` are applied in order during
    ``tools/list``, each on the output of the previous one.  *ctx* is the
    listing request's context: its ``meta`` holds the request headers and,
    when the server has an ``AuthMiddleware``, ``client_id``, ``client``
    and ``state["roles"]`` are set for an authenticated caller.  ``apply``
    may be a coroutine function.

    A transform that renames tools should also define
    ``resolve_name(name: str) -> str | None``, returning the registered name
    for a listed one (``None`` when *name* is not one of its renamings), so
    that calls by the listed name reach the tool.  Return copies
    (``dataclasses.replace``), never mutate the definitions you receive.
    """

    def apply(self, tools: list[ToolDef], ctx: RequestContext | None = None) -> list[ToolDef]: ...


class NamespaceTransform:
    """Prefix all tool names with a namespace.

    A call by the prefixed name runs the tool; the unprefixed name keeps
    working too.

    Example::

        transform = NamespaceTransform(prefix="myapp")
        # "search" → "myapp_search"
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def resolve_name(self, name: str) -> str | None:
        """The registered name for a listed ``<prefix>_<name>``, else ``None``."""
        if self._prefix and name.startswith(f"{self._prefix}_"):
            return name[len(self._prefix) + 1 :]
        return None

    def apply(self, tools: list[ToolDef], ctx: RequestContext | None = None) -> list[ToolDef]:
        result: list[ToolDef] = []
        for t in tools:
            new_name = f"{self._prefix}_{t.name}" if self._prefix else t.name
            result.append(replace(t, name=new_name))
        return result


class VisibilityTransform:
    """Hide tools based on a predicate.

    Hidden tools are removed from ``list_tools`` results but remain
    callable (for backwards compatibility with cached tool lists); guard a
    tool that must not be called.  A predicate receives the listing
    request's context (``None`` when applied by hand without one); a
    predicate that raises hides its tool.

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
            if predicate is not None:
                try:
                    hidden = predicate(ctx)
                except Exception:
                    logger.debug(
                        "Visibility predicate for %r raised; hiding it", t.name, exc_info=True
                    )
                    hidden = True
                if hidden:
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


async def apply_transforms(
    transforms: list[Any],
    tools: list[ToolDef],
    *,
    server_name: str,
    middlewares: list[Any],
    meta: dict[str, Any],
) -> list[ToolDef]:
    """Run *transforms* in order over *tools* for one ``tools/list`` request.

    The context the transforms see is authenticated with the server's
    ``AuthMiddleware`` (if any) from the request headers in *meta*; a caller
    whose credentials don't verify gets an unauthenticated context.
    """
    from ._auth import AuthMiddleware

    ctx = RequestContext(server_name=server_name, meta=dict(meta), request_type="tool")
    auth_mw = next((m for m in middlewares if isinstance(m, AuthMiddleware)), None)
    if auth_mw is not None:
        set_context(ctx)
        try:
            await auth_mw.authenticate(ctx)
        except Exception:
            logger.debug("tools/list caller did not authenticate", exc_info=True)
        finally:
            clear_context()
    result = list(tools)
    for transform in transforms:
        out = transform.apply(result, ctx)
        if inspect.isawaitable(out):
            out = await out
        result = list(out)
    return result


def resolve_transformed_name(transforms: list[Any], name: str) -> str:
    """Map a listed tool name back through the renaming transforms (last first)."""
    for transform in reversed(transforms):
        resolver = getattr(transform, "resolve_name", None)
        if resolver is None:
            continue
        resolved = resolver(name)
        if resolved is not None:
            name = resolved
    return name
