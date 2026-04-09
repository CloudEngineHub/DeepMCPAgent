"""Public test utilities for the Promptise MCP Server Framework.

Example::

    from promptise.mcp.server.testing import TestClient

    client = TestClient(server, meta={"authorization": "Bearer xxx"})
    result = await client.call_tool("search", {"query": "revenue"})
"""

from ._testing import TestClient

__all__ = ["TestClient"]
