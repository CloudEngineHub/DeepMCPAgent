"""Single-server MCP client with token-based authentication.

Wraps the official MCP SDK's transport and session APIs into a clean
async context manager that handles:

- Transport selection (Streamable HTTP, SSE, stdio)
- Bearer token injection (from IdP or server token endpoint)
- API key injection (simple pre-shared secret)
- Custom header injection on every HTTP request
- Proper session lifecycle (initialize → use → close)

The client **never** generates JWTs.  Tokens are obtained externally
(from an Identity Provider or the server's built-in token endpoint)
and passed in via ``bearer_token``, ``api_key``, or ``headers``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractAsyncContextManager
from typing import Any

from mcp.client.session import ClientSession
from mcp.types import CallToolResult, ListToolsResult, Tool

logger = logging.getLogger(__name__)


class MCPClientError(RuntimeError):
    """Raised when an MCP client operation fails."""


class MCPClient:
    """Production-grade MCP client for a single server.

    Supports HTTP (Streamable HTTP), SSE, and stdio transports with
    Bearer token or API key authentication.

    Args:
        url: Server endpoint URL (for HTTP/SSE).  Mutually exclusive
            with ``command``.
        transport: ``"http"`` (default), ``"sse"``, or ``"stdio"``.
        headers: Extra HTTP headers sent on every request.
        bearer_token: Pre-issued Bearer token.  When provided, an
            ``Authorization: Bearer <token>`` header is injected
            automatically.  Obtain tokens from your Identity Provider
            or the server's token endpoint — the client never
            generates tokens itself.
        api_key: Pre-shared API key.  When provided, an ``x-api-key``
            header is injected automatically.  Use this for simple
            secret-based auth when JWT is overkill.
        command: Executable for stdio transport (e.g. ``"python"``).
        args: Arguments for the stdio command.
        env: Environment variables for the stdio process.
        timeout: HTTP request timeout in seconds.

    Example — unauthenticated::

        async with MCPClient(url="http://localhost:8080/mcp") as client:
            tools = await client.list_tools()

    Example — with Bearer token::

        async with MCPClient(
            url="http://localhost:8080/mcp",
            bearer_token="eyJhbGciOiJIUzI1NiIs...",
        ) as client:
            result = await client.call_tool("search", {"query": "python"})

    Example — with API key::

        async with MCPClient(
            url="http://localhost:8080/mcp",
            api_key="my-secret-key",
        ) as client:
            tools = await client.list_tools()

    Example — fetch token from server endpoint::

        token = await MCPClient.fetch_token(
            "http://localhost:8080/auth/token",
            client_id="my-agent",
            client_secret="agent-secret",
        )
        async with MCPClient(
            url="http://localhost:8080/mcp",
            bearer_token=token,
        ) as client:
            tools = await client.list_tools()
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        transport: str = "http",
        headers: dict[str, str] | None = None,
        bearer_token: str | None = None,
        api_key: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._url = url
        self._transport = transport
        self._headers = dict(headers or {})
        self._command = command
        self._args = args or []
        self._env = env or {}
        self._timeout = timeout

        # Session state (set on __aenter__)
        self._session: ClientSession | None = None
        self._transport_ctx: AbstractAsyncContextManager[Any] | None = None
        self._session_ctx: AbstractAsyncContextManager[ClientSession] | None = None

        # Inject Bearer token as Authorization header
        if bearer_token:
            self._headers["authorization"] = f"Bearer {bearer_token}"

        # Inject API key as x-api-key header
        if api_key:
            self._headers["x-api-key"] = api_key

    # ------------------------------------------------------------------
    # Token acquisition helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def fetch_token(
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 10.0,
    ) -> str:
        """Fetch a Bearer token from a server's token endpoint.

        This is a convenience for development/testing when the MCP server
        has a built-in token issuer enabled via
        :meth:`~promptise.mcp.server.MCPServer.enable_token_endpoint`.

        For production, obtain tokens from your Identity Provider
        (Auth0, Keycloak, Okta, etc.) and pass them directly via
        ``bearer_token``.

        Args:
            token_url: Full URL of the token endpoint
                (e.g. ``http://localhost:8080/auth/token``).
            client_id: Client identifier registered on the server.
            client_secret: Client secret for authentication.
            timeout: HTTP request timeout in seconds.

        Returns:
            The access token string (ready to pass as ``bearer_token``).

        Raises:
            MCPClientError: On network errors or authentication failure.
        """
        try:
            import httpx
        except ImportError as exc:
            raise MCPClientError(
                "httpx is required for fetch_token(). Install it with: pip install httpx"
            ) from exc

        try:
            async with httpx.AsyncClient(timeout=timeout) as http:
                resp = await http.post(
                    token_url,
                    json={
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                )
                if resp.status_code != 200:
                    body = resp.text
                    raise MCPClientError(f"Token request failed (HTTP {resp.status_code}): {body}")
                data = resp.json()
                token = data.get("access_token")
                if not token:
                    raise MCPClientError(f"Token response missing 'access_token': {data}")
                return token
        except httpx.HTTPError as exc:
            raise MCPClientError(f"Token request failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> MCPClient:
        """Connect to the server and initialise the session."""
        if self._transport in ("http", "streamable-http"):
            await self._connect_http()
        elif self._transport == "sse":
            await self._connect_sse()
        elif self._transport == "stdio":
            await self._connect_stdio()
        else:
            raise MCPClientError(f"Unknown transport: {self._transport!r}")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Close the session and transport.

        Suppresses ``CancelledError`` during cleanup — the MCP SDK's
        Streamable HTTP transport may raise it when terminating sessions.
        """
        try:
            if self._session_ctx is not None:
                await self._session_ctx.__aexit__(*exc)
        except BaseException:
            logger.debug("Session cleanup error", exc_info=True)  # Session cleanup is best-effort
        try:
            if self._transport_ctx is not None:
                await self._transport_ctx.__aexit__(*exc)
        except BaseException:
            logger.debug(
                "Transport cleanup error", exc_info=True
            )  # Transport cleanup is best-effort
        self._session = None
        self._session_ctx = None
        self._transport_ctx = None

    # ------------------------------------------------------------------
    # Transport connection helpers
    # ------------------------------------------------------------------

    async def _connect_http(self) -> None:
        if not self._url:
            raise MCPClientError("url is required for HTTP transport")

        from mcp.client.streamable_http import streamablehttp_client

        self._transport_ctx = streamablehttp_client(
            url=self._url,
            headers=self._headers or None,
            timeout=self._timeout,
        )
        read_stream, write_stream, _ = await self._transport_ctx.__aenter__()

        self._session_ctx = ClientSession(read_stream, write_stream)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()

    async def _connect_sse(self) -> None:
        if not self._url:
            raise MCPClientError("url is required for SSE transport")

        from mcp.client.sse import sse_client

        self._transport_ctx = sse_client(
            url=self._url,
            headers=self._headers or None,
            timeout=self._timeout,
        )
        read_stream, write_stream = await self._transport_ctx.__aenter__()

        self._session_ctx = ClientSession(read_stream, write_stream)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()

    async def _connect_stdio(self) -> None:
        if not self._command:
            raise MCPClientError("command is required for stdio transport")

        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env or None,
        )
        self._transport_ctx = stdio_client(params)
        read_stream, write_stream = await self._transport_ctx.__aenter__()

        self._session_ctx = ClientSession(read_stream, write_stream)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()

    # ------------------------------------------------------------------
    # MCP operations
    # ------------------------------------------------------------------

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise MCPClientError("Not connected. Use 'async with MCPClient(...) as client:'")
        return self._session

    async def list_tools(self) -> list[Tool]:
        """List all tools from the connected server.

        Returns:
            List of MCP ``Tool`` objects with name, description, inputSchema.
        """
        session = self._require_session()
        try:
            result: ListToolsResult = await session.list_tools()
            return list(result.tools)
        except Exception as exc:
            raise MCPClientError(f"Failed to list tools: {exc}") from exc

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Call a tool on the connected server.

        Args:
            name: Tool name.
            arguments: Tool arguments dict.

        Returns:
            MCP ``CallToolResult`` with content list.
        """
        session = self._require_session()
        try:
            return await session.call_tool(name, arguments)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise MCPClientError(f"Timeout calling tool '{name}': {exc}") from exc
        except ConnectionError as exc:
            raise MCPClientError(f"Connection lost calling tool '{name}': {exc}") from exc
        except MCPClientError:
            raise  # Don't double-wrap
        except Exception as exc:
            raise MCPClientError(f"Failed to call tool '{name}': {exc}") from exc

    @property
    def session(self) -> ClientSession | None:
        """The underlying MCP ``ClientSession``, or ``None`` if not connected."""
        return self._session

    @property
    def headers(self) -> dict[str, str]:
        """Current HTTP headers (read-only copy)."""
        return dict(self._headers)
