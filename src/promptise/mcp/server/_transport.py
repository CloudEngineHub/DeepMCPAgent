"""Transport adapters: stdio, Streamable HTTP, SSE.

Each adapter creates the appropriate read/write streams and calls
``lowlevel_server.run()`` with them.
"""

from __future__ import annotations

import contextlib
import json as _json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcp.server.lowlevel import Server as LowLevelServer

from ._types import TransportType

if TYPE_CHECKING:
    from ._lifecycle import LifecycleManager

logger = logging.getLogger("promptise.server")


# =====================================================================
# CORS configuration
# =====================================================================


@dataclass(frozen=True)
class CORSConfig:
    """CORS configuration for HTTP and SSE transports.

    Args:
        allow_origins: Allowed origin URLs. Use ``["*"]`` to allow all.
        allow_methods: Allowed HTTP methods.
        allow_headers: Allowed request headers.
        allow_credentials: Whether to allow credentials (cookies, auth).
        max_age: Max seconds browsers may cache preflight responses.

    Example::

        server.run(
            transport="http",
            port=8080,
            cors=CORSConfig(
                allow_origins=["https://app.example.com"],
                allow_headers=["Authorization", "x-api-key"],
            ),
        )
    """

    allow_origins: list[str] = field(default_factory=list)
    allow_methods: list[str] = field(default_factory=lambda: ["GET", "POST", "DELETE", "OPTIONS"])
    allow_headers: list[str] = field(
        default_factory=lambda: ["Content-Type", "Authorization", "x-api-key"]
    )
    allow_credentials: bool = False
    max_age: int = 600


# =====================================================================
# Transport-level auth gate (ASGI middleware)
# =====================================================================


class _AuthGateASGI:
    """ASGI middleware that rejects HTTP requests without valid auth.

    Wraps the Starlette application and checks the ``Authorization``
    or ``x-api-key`` header before forwarding to the MCP session manager.
    Only applied when ``require_auth=True`` on the server.

    Supports two auth methods (checked in order):

    1. **Bearer token** (JWT): ``Authorization: Bearer <token>``
    2. **API key**: ``x-api-key: <key>``

    Args:
        app: The inner ASGI application.
        verify_fn: Callable ``(token) → bool`` — the primary verifier
            (typically ``JWTAuth.verify_token``).
        skip_paths: Set of paths that bypass authentication (e.g. the
            token endpoint, which *issues* tokens and so cannot require
            one).
        api_key_verify_fn: Optional callable ``(key) → bool`` for API
            key verification.  When not provided but an ``x-api-key``
            header is present, falls back to ``verify_fn``.
    """

    def __init__(
        self,
        app: Any,
        verify_fn: Callable[[str], bool],
        skip_paths: set[str] | None = None,
        api_key_verify_fn: Callable[[str], bool] | None = None,
    ) -> None:
        self.app = app
        self._verify = verify_fn
        self._skip_paths = skip_paths or set()
        self._verify_api_key = api_key_verify_fn

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # Let lifespan events through unconditionally
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        if scope["type"] in ("http", "websocket"):
            # Skip auth for whitelisted paths (e.g. token endpoint)
            path = scope.get("path", "")
            if path in self._skip_paths:
                await self.app(scope, receive, send)
                return

            headers = dict(scope.get("headers", []))

            # Try Bearer token first (Authorization: Bearer <token>)
            auth_value = headers.get(b"authorization", b"").decode()
            if auth_value and auth_value.startswith("Bearer "):
                token = auth_value[7:]
                if self._verify(token):
                    await self.app(scope, receive, send)
                    return
                await _send_json(
                    send,
                    401,
                    {
                        "error": "Invalid authentication token",
                        "message": "The provided token could not be verified.",
                    },
                )
                return

            # Try API key (x-api-key header)
            api_key = headers.get(b"x-api-key", b"").decode()
            if api_key:
                verify_key = self._verify_api_key or self._verify
                if verify_key(api_key):
                    await self.app(scope, receive, send)
                    return
                await _send_json(
                    send,
                    401,
                    {
                        "error": "Invalid API key",
                        "message": "The provided API key could not be verified.",
                    },
                )
                return

            # No credentials at all
            await _send_json(
                send,
                401,
                {
                    "error": "Authentication required",
                    "message": "This server requires authentication. "
                    "Pass a Bearer token via the Authorization header "
                    "or an API key via the x-api-key header.",
                },
            )
            return

        await self.app(scope, receive, send)


async def _send_json(send: Any, status: int, body: dict[str, Any]) -> None:
    """Send a JSON HTTP response via raw ASGI."""
    payload = _json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(payload)).encode()],
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": payload,
        }
    )


# =====================================================================
# Stdio transport
# =====================================================================


async def run_stdio(
    server: LowLevelServer,
    init_options: Any,
    lifecycle: LifecycleManager,
    *,
    shutdown_timeout: float | None = None,
) -> None:
    """Run the server over stdio (stdin/stdout).

    This is the default transport for local MCP connections.
    """
    from mcp.server.stdio import stdio_server

    await lifecycle.startup()
    try:
        async with stdio_server() as (read_stream, write_stream):
            logger.info("MCP server running on stdio")
            await server.run(
                read_stream,
                write_stream,
                init_options,
                raise_exceptions=False,
            )
    finally:
        await lifecycle.shutdown(timeout=shutdown_timeout)


# =====================================================================
# Streamable HTTP transport
# =====================================================================


async def run_http(
    server: LowLevelServer,
    init_options: Any,
    lifecycle: LifecycleManager,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    shutdown_timeout: float | None = None,
    dashboard: bool = False,
    auth_gate: Callable[[str], bool] | None = None,
    token_endpoint: Any = None,
    cors: CORSConfig | None = None,
) -> None:
    """Run the server over Streamable HTTP.

    Uses the MCP SDK's ``StreamableHTTPSessionManager`` which handles
    session tracking, transport creation, and ``connect()`` lifecycle
    automatically.  Served via Starlette + uvicorn.

    Args:
        dashboard: When True, suppress uvicorn access logs (the live
            dashboard captures request data via middleware instead).
        auth_gate: Optional callable ``(token_or_key) → bool`` for
            transport-level authentication.  Rejects HTTP requests that
            lack a valid ``Authorization: Bearer <token>`` header or
            ``x-api-key`` header.  Bearer tokens are checked first; if
            absent, the ``x-api-key`` header is tried.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Route

    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=False,
        stateless=False,
    )

    # Starlette Route wraps functions/methods in request_response(),
    # but we need raw ASGI (scope, receive, send).  A callable class
    # instance passes Starlette's isfunction/ismethod check and is
    # treated as an ASGI app directly.
    class _AsgiEndpoint:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            # Bridge HTTP headers into a contextvar so that call_tool
            # can populate RequestContext.meta (the MCP SDK does not
            # pass transport-level headers to protocol-level handlers).
            from ._context import set_request_client_info, set_request_headers

            if scope["type"] in ("http", "websocket"):
                raw_headers = scope.get("headers", [])
                headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in raw_headers}
                set_request_headers(headers)
                # Bridge ASGI client info (IP, port)
                client = scope.get("client")
                if client:
                    set_request_client_info(tuple(client))

            await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        await lifecycle.startup()
        try:
            async with session_manager.run():
                yield
        finally:
            await lifecycle.shutdown(timeout=shutdown_timeout)

    routes = [
        Route("/mcp", endpoint=_AsgiEndpoint(), methods=["GET", "POST", "DELETE"]),
    ]

    # Token endpoint (built-in auth for dev/testing)
    if token_endpoint is not None:
        from ._token_endpoint import handle_token_request

        _te_config = token_endpoint

        class _TokenEndpointASGI:
            """ASGI wrapper for the token endpoint."""

            async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
                await handle_token_request(scope, receive, send, _te_config)

        routes.append(
            Route(
                token_endpoint.path,
                endpoint=_TokenEndpointASGI(),
                methods=["POST"],
            )
        )
        logger.info("Token endpoint enabled at %s", token_endpoint.path)

    asgi_app: Any = Starlette(
        routes=routes,
        lifespan=lifespan,
    )

    # CORS middleware (applied before auth gate so preflight works)
    if cors is not None:
        from starlette.middleware.cors import CORSMiddleware

        asgi_app.add_middleware(
            CORSMiddleware,
            allow_origins=cors.allow_origins,
            allow_methods=cors.allow_methods,
            allow_headers=cors.allow_headers,
            allow_credentials=cors.allow_credentials,
            max_age=cors.max_age,
        )

    # Transport-level auth gate (does NOT apply to token endpoint —
    # the gate wraps the whole app but the token endpoint is
    # unauthenticated by design since it *issues* tokens)
    if auth_gate is not None:
        # Build an auth gate that skips the token endpoint path
        _skip_paths = set()
        if token_endpoint is not None:
            _skip_paths.add(token_endpoint.path)
        asgi_app = _AuthGateASGI(asgi_app, auth_gate, skip_paths=_skip_paths)

    import uvicorn

    log_level = "critical" if dashboard else "info"
    config = uvicorn.Config(asgi_app, host=host, port=port, log_level=log_level)
    uv_server = uvicorn.Server(config)
    logger.info("MCP server running on http://%s:%d/mcp", host, port)
    await uv_server.serve()


# =====================================================================
# SSE transport (legacy)
# =====================================================================


async def run_sse(
    server: LowLevelServer,
    init_options: Any,
    lifecycle: LifecycleManager,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    shutdown_timeout: float | None = None,
    dashboard: bool = False,
    auth_gate: Callable[[str], bool] | None = None,
    token_endpoint: Any = None,
    cors: CORSConfig | None = None,
) -> None:
    """Run the server over Server-Sent Events (legacy transport).

    Uses the MCP SDK's ``SseServerTransport``.
    """
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Any) -> Any:
        # Bridge HTTP headers and client info for SSE connection
        from ._context import set_request_client_info, set_request_headers

        raw_headers = request.scope.get("headers", [])
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in raw_headers}
        set_request_headers(headers)
        client = request.scope.get("client")
        if client:
            set_request_client_info(tuple(client))

        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], init_options, raise_exceptions=False)

    async def handle_messages(scope: Any, receive: Any, send: Any) -> None:
        # Bridge HTTP headers and client info for message POST requests
        from ._context import set_request_client_info, set_request_headers

        raw_headers = scope.get("headers", [])
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in raw_headers}
        set_request_headers(headers)
        client = scope.get("client")
        if client:
            set_request_client_info(tuple(client))

        await sse.handle_post_message(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        await lifecycle.startup()
        try:
            yield
        finally:
            await lifecycle.shutdown(timeout=shutdown_timeout)

    routes = [
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=handle_messages),
    ]

    # Token endpoint (built-in auth for dev/testing)
    if token_endpoint is not None:
        from ._token_endpoint import handle_token_request

        _te_config = token_endpoint

        class _TokenEndpointASGI:
            async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
                await handle_token_request(scope, receive, send, _te_config)

        routes.append(
            Route(
                token_endpoint.path,
                endpoint=_TokenEndpointASGI(),
                methods=["POST"],
            )
        )

    asgi_app: Any = Starlette(
        routes=routes,
        lifespan=lifespan,
    )

    # CORS middleware
    if cors is not None:
        from starlette.middleware.cors import CORSMiddleware

        asgi_app.add_middleware(
            CORSMiddleware,
            allow_origins=cors.allow_origins,
            allow_methods=cors.allow_methods,
            allow_headers=cors.allow_headers,
            allow_credentials=cors.allow_credentials,
            max_age=cors.max_age,
        )

    # Transport-level auth gate
    if auth_gate is not None:
        _skip_paths = set()
        if token_endpoint is not None:
            _skip_paths.add(token_endpoint.path)
        asgi_app = _AuthGateASGI(asgi_app, auth_gate, skip_paths=_skip_paths)

    import uvicorn

    log_level = "critical" if dashboard else "info"
    config = uvicorn.Config(asgi_app, host=host, port=port, log_level=log_level)
    uv_server = uvicorn.Server(config)
    logger.info("MCP server running on http://%s:%d/sse (SSE)", host, port)
    await uv_server.serve()


# =====================================================================
# Dispatcher
# =====================================================================


async def run_transport(
    transport_type: TransportType,
    server: LowLevelServer,
    init_options: Any,
    lifecycle: LifecycleManager,
    **kwargs: Any,
) -> None:
    """Dispatch to the appropriate transport runner."""
    runners = {
        TransportType.STDIO: run_stdio,
        TransportType.HTTP: run_http,
        TransportType.SSE: run_sse,
    }
    runner = runners.get(transport_type)
    if runner is None:
        raise ValueError(f"Unsupported transport: {transport_type}")

    if transport_type == TransportType.STDIO:
        # Stdio doesn't accept network/dashboard/auth kwargs
        stdio_kwargs = {k: v for k, v in kwargs.items() if k == "shutdown_timeout"}
        await runner(server, init_options, lifecycle, **stdio_kwargs)
    else:
        await runner(server, init_options, lifecycle, **kwargs)
