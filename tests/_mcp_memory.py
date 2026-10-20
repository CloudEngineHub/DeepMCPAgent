"""MCP SDK helpers for tests that work with mcp 1.x and 2.x.

mcp 1.x has ``mcp.shared.memory.create_connected_server_and_client_session``;
2.x removed it.  :func:`connected_session` does the same with the memory
streams both versions have: it runs the lowlevel *server* and yields an
initialised ``ClientSession`` (the pre-2026 handshake, like 1.x).

:func:`call_tool_directly` calls the lowlevel server's ``tools/call``
handler in the test's own task, with no request context (1.x:
``server.request_handlers[CallToolRequest]``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from promptise.mcp._compat import MCP_V2


@asynccontextmanager
async def connected_session(
    server: Any, *, raise_exceptions: bool = False, **client_kwargs: Any
) -> AsyncIterator[ClientSession]:
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: server.run(
                    server_read,
                    server_write,
                    server.create_initialization_options(),
                    raise_exceptions=raise_exceptions,
                )
            )
            try:
                async with ClientSession(client_read, client_write, **client_kwargs) as session:
                    await session.initialize()
                    yield session
            finally:
                tg.cancel_scope.cancel()


async def call_tool_directly(server: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """The ``CallToolResult`` of *server*'s ``tools/call`` handler, called in this task."""
    import mcp.types as t

    params = t.CallToolRequestParams(name=name, arguments=arguments or {})
    if MCP_V2:
        entry = server.get_request_handler("tools/call")
        return await entry.handler(None, params)
    handler = server.request_handlers[t.CallToolRequest]
    result = await handler(t.CallToolRequest(method="tools/call", params=params))
    return result.root
