"""FastMCP client wrapper that supports multiple servers via a single configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any  # <-- add

from fastmcp import Client as FastMCPClient

from .config import ServerSpec, servers_to_mcp_config


class FastMCPMulti:
    """Create a single FastMCP client wired to multiple servers.

    The client is configured using the `mcpServers` dictionary generated from
    the typed server specifications.

    Args:
        servers: Mapping of server name to server spec.
    """

    def __init__(self, servers: Mapping[str, ServerSpec]) -> None:
        mcp_cfg = {"mcpServers": servers_to_mcp_config(servers)}
        self._client: Any = FastMCPClient(mcp_cfg)  # <-- annotate as Any

    @property
    def client(self) -> Any:  # <-- return Any to avoid unparameterized generic
        """Return the underlying FastMCP client instance."""
        return self._client
