"""Tool result streaming support for MCP servers.

Provides a ``StreamingResult`` class that tools can use to yield
partial results. The framework collects yielded items and returns
them as a content list.

Example::

    from promptise.mcp.server import MCPServer, StreamingResult

    server = MCPServer(name="api")

    @server.tool()
    async def search_stream(query: str) -> StreamingResult:
        result = StreamingResult()
        async for hit in search_engine.stream(query):
            result.add(hit)
        return result
"""

from __future__ import annotations

import json
from typing import Any


class StreamingResult:
    """Collects partial results for streaming-style tool responses.

    Tools that produce results incrementally can use this class to
    accumulate items.  The framework serialises the collected items
    as a JSON array in a single ``TextContent`` response.

    For true server-push streaming, the MCP protocol would need
    streaming result support.  This class provides the collection
    pattern for when that lands.

    Example::

        result = StreamingResult()
        result.add({"title": "Result 1", "score": 0.95})
        result.add({"title": "Result 2", "score": 0.87})
        return result  # → TextContent with JSON array
    """

    def __init__(self) -> None:
        self._items: list[Any] = []
        self._metadata: dict[str, Any] = {}

    def add(self, item: Any) -> None:
        """Add a partial result item."""
        self._items.append(item)

    def add_many(self, items: list[Any]) -> None:
        """Add multiple items at once."""
        self._items.extend(items)

    def set_metadata(self, key: str, value: Any) -> None:
        """Attach metadata to the streaming result."""
        self._metadata[key] = value

    @property
    def items(self) -> list[Any]:
        """Access collected items."""
        return self._items

    @property
    def metadata(self) -> dict[str, Any]:
        """Access result metadata."""
        return self._metadata

    def __len__(self) -> int:
        return len(self._items)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict for JSON encoding."""
        result: dict[str, Any] = {"items": self._items, "count": len(self._items)}
        if self._metadata:
            result["metadata"] = self._metadata
        return result

    def __str__(self) -> str:
        return json.dumps(self.to_dict(), default=str)
