"""Server composition — mount sub-servers into a parent server.

Allows composing multiple MCPServer instances into a single server,
each with its own namespace prefix.

Example::

    from promptise.mcp.server import MCPServer, mount

    main_server = MCPServer(name="gateway")
    math_server = MCPServer(name="math")
    db_server = MCPServer(name="database")

    @math_server.tool()
    async def add(a: int, b: int) -> int:
        return a + b

    @db_server.tool()
    async def query(sql: str) -> list:
        return []

    mount(main_server, math_server, prefix="math")
    mount(main_server, db_server, prefix="db")
    # Tools: math_add, db_query

    main_server.run()
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("promptise.server")


def mount(
    parent: Any,
    child: Any,
    *,
    prefix: str = "",
    tags: list[str] | None = None,
) -> int:
    """Mount a child server's tools, resources, and prompts into a parent.

    All tool names are prefixed with ``{prefix}_`` if a prefix is given.
    Tags from the parent are merged with child tags.

    Args:
        parent: The parent ``MCPServer`` to mount into.
        child: The child ``MCPServer`` whose registrations will be copied.
        prefix: Namespace prefix for tool names.
        tags: Additional tags applied to all mounted tools.

    Returns:
        Number of tools mounted.
    """
    extra_tags = tags or []
    count = 0

    # Mount tools
    for tdef in child._tool_registry.list_all():
        name = f"{prefix}_{tdef.name}" if prefix else tdef.name
        merged_tags = list(tdef.tags) + extra_tags

        from ._types import ToolDef

        new_def = ToolDef(
            name=name,
            description=tdef.description,
            handler=tdef.handler,
            input_schema=tdef.input_schema,
            tags=merged_tags,
            auth=tdef.auth,
            rate_limit=tdef.rate_limit,
            timeout=tdef.timeout,
            guards=list(tdef.guards),
            roles=list(tdef.roles),
            router_middleware=list(tdef.router_middleware),
            annotations=tdef.annotations,
            max_concurrent=tdef.max_concurrent,
        )
        parent._tool_registry.register(new_def)

        # Copy input model
        if tdef.name in child._input_models:
            parent._input_models[name] = child._input_models[tdef.name]

        count += 1

    # Mount resources
    for rdef in child._resource_registry.list_all():
        parent._resource_registry.register(rdef)

    for rdef in child._resource_registry.list_templates():
        parent._resource_registry.register(rdef)

    # Mount prompts
    for pdef in child._prompt_registry.list_all():
        parent._prompt_registry.register(pdef)

    # Copy exception handlers
    for exc_type, handler in child._exception_handlers._handlers.items():
        parent._exception_handlers.register(exc_type, handler)

    logger.info(
        "Mounted %d tools from '%s' into '%s' (prefix=%r)",
        count,
        child.name,
        parent.name,
        prefix,
    )
    return count
