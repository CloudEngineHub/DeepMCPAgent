"""Batch tool call support for MCP servers.

Allows executing multiple tool calls in a single request for
reduced latency. This is implemented as a meta-tool that
dispatches to the registered tool handlers.

Example::

    from promptise.mcp.server import MCPServer, register_batch_tool

    server = MCPServer(name="api")

    @server.tool()
    async def search(query: str) -> list[dict]:
        return await db.search(query)

    register_batch_tool(server, name="batch_call", max_parallel=10)

    # Clients call "batch_call" with:
    # {"calls": [{"tool": "search", "args": {"query": "a"}}, ...]}
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("promptise.server")


def register_batch_tool(
    server: Any,
    *,
    name: str = "batch_call",
    description: str = "Execute multiple tool calls in parallel.",
    max_parallel: int = 10,
) -> None:
    """Register a batch meta-tool on the server.

    The batch tool accepts a list of ``{tool, args}`` dicts and
    executes them in parallel, returning a list of results.

    Args:
        server: The MCPServer instance.
        name: Name of the batch meta-tool.
        description: Description for the batch tool.
        max_parallel: Maximum concurrent calls per batch.
    """
    from ._testing import TestClient

    @server.tool(name=name, description=description)
    async def _batch_call(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Execute a batch of tool calls.

        Each sub-call runs through the full TestClient pipeline
        with the parent request's auth context propagated, ensuring
        guards and middleware apply to every call in the batch.
        """
        # Propagate the caller's auth context to sub-calls.
        # get_context() returns the full RequestContext (not just client info).
        from ._context import get_context

        parent_ctx = get_context()
        meta = {}
        if parent_ctx is not None:
            meta = dict(parent_ctx.meta) if parent_ctx.meta else {}

        # Enforce max batch size — reject oversized requests
        max_batch = max_parallel * 2
        if len(calls) > max_batch:
            return [
                {"status": "error", "error": f"Batch size {len(calls)} exceeds max {max_batch}"}
            ]

        client = TestClient(server, meta=meta)
        sem = asyncio.Semaphore(max_parallel)

        async def _run_one(call: dict[str, Any]) -> dict[str, Any]:
            tool_name = call.get("tool", "")
            args = call.get("args", {})
            async with sem:
                try:
                    result = await client.call_tool(tool_name, args)
                    return {
                        "tool": tool_name,
                        "status": "ok",
                        "result": [r.text if hasattr(r, "text") else str(r) for r in result],
                    }
                except Exception as exc:
                    return {
                        "tool": tool_name,
                        "status": "error",
                        "error": str(exc),
                    }

        tasks = [_run_one(c) for c in calls[:max_batch]]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return list(results)
