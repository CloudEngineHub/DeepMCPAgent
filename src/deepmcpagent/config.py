"""Typed server specifications and conversion helpers for FastMCP configuration."""

from __future__ import annotations

from typing import Dict, List, Literal, Mapping, Union

from pydantic import BaseModel, Field


class _BaseServer(BaseModel):
    """Base model for server specs."""

    class Config:
        extra = "forbid"


class StdioServerSpec(_BaseServer):
    """Specification for a local MCP server launched via stdio.

    NOTE:
        The FastMCP Python client typically expects HTTP/SSE transports. Using
        `StdioServerSpec` requires a different adapter or an HTTP shim in front
        of the stdio server. Keep this for future expansion or custom runners.

    Attributes:
        command: Executable to launch (e.g., "python").
        args: Positional arguments for the process.
        env: Environment variables to set for the process.
        cwd: Optional working directory.
        keep_alive: Whether the client should try to keep a persistent session.
    """

    command: str
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    keep_alive: bool = True


class HTTPServerSpec(_BaseServer):
    """Specification for a remote MCP server reachable via HTTP/SSE.

    Attributes:
        url: Full endpoint URL for the MCP server (e.g., http://127.0.0.1:8000/mcp).
        transport: The transport mechanism ("http", "streamable-http", or "sse").
        headers: Optional request headers (e.g., Authorization tokens).
        auth: Optional auth hint if your FastMCP deployment consumes it.
    """

    url: str
    transport: Literal["http", "streamable-http", "sse"] = "http"
    headers: Dict[str, str] = Field(default_factory=dict)
    auth: str | None = None


ServerSpec = Union[StdioServerSpec, HTTPServerSpec]
"""Union of supported server specifications."""


def servers_to_mcp_config(servers: Mapping[str, ServerSpec]) -> Dict[str, Dict[str, object]]:
    """Convert programmatic server specs to the FastMCP configuration dict.

    Args:
        servers: Mapping of server name to specification.

    Returns:
        Dict suitable for initializing `fastmcp.Client({"mcpServers": ...})`.
    """
    cfg: Dict[str, Dict[str, object]] = {}
    for name, s in servers.items():
        if isinstance(s, StdioServerSpec):
            cfg[name] = {
                "transport": "stdio",
                "command": s.command,
                "args": s.args,
                "env": s.env or None,
                "cwd": s.cwd or None,
                "keep_alive": s.keep_alive,
            }
        else:
            entry: Dict[str, object] = {
                "transport": s.transport,
                "url": s.url,
            }
            if s.headers:
                entry["headers"] = s.headers
            if s.auth is not None:
                entry["auth"] = s.auth
            cfg[name] = entry
    return cfg
