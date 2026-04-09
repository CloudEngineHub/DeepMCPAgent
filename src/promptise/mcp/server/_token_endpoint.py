"""Built-in OAuth 2.0 token issuer for development and testing.

Provides an HTTP endpoint that issues JWT access tokens and refresh
tokens so developers can quickly set up authenticated MCP servers
without an external Identity Provider (IdP).

Supports two OAuth 2.0 grant types:

- **client_credentials** — exchange ``client_id`` + ``client_secret``
  for an access token and a refresh token.
- **refresh_token** — exchange a valid refresh token for a new access
  token (and a rotated refresh token) without re-sending credentials.

**For production use**, prefer a proper IdP such as Auth0, Keycloak,
or Okta.

Usage::

    from promptise.mcp.server import MCPServer, JWTAuth, AuthMiddleware

    server = MCPServer(name="secure-api")
    jwt_auth = JWTAuth(secret="my-secret")
    server.add_middleware(AuthMiddleware(jwt_auth))

    # Register clients that can request tokens
    server.enable_token_endpoint(
        jwt_auth=jwt_auth,
        clients={
            "agent-admin":  {"secret": "admin-pass",  "roles": ["admin"]},
            "agent-viewer": {"secret": "viewer-pass", "roles": ["viewer"]},
        },
    )

    server.run(transport="http")

Clients obtain tokens via::

    POST /auth/token
    {"grant_type": "client_credentials", "client_id": "agent-admin", "client_secret": "admin-pass"}

    Response:
    {
        "access_token": "eyJ...",
        "refresh_token": "prf_...",
        "token_type": "bearer",
        "expires_in": 3600
    }

Refresh tokens via::

    POST /auth/token
    {"grant_type": "refresh_token", "refresh_token": "prf_..."}

    Response:
    {
        "access_token": "eyJ...",
        "refresh_token": "prf_...",   // rotated — old one is invalidated
        "token_type": "bearer",
        "expires_in": 3600
    }

Then connect with the token::

    from promptise.mcp.client import MCPClient

    token = await MCPClient.fetch_token(
        "http://localhost:8080/auth/token",
        client_id="agent-admin",
        client_secret="admin-pass",
    )
    async with MCPClient(url="http://localhost:8080/mcp", bearer_token=token) as c:
        tools = await c.list_tools()
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import secrets
import threading
import time
from typing import Any

logger = logging.getLogger("promptise.server")


# ---------------------------------------------------------------------------
# Refresh token store
# ---------------------------------------------------------------------------


class _RefreshTokenStore:
    """Thread-safe in-memory store for refresh tokens.

    Each refresh token maps to the ``client_id`` that owns it plus
    an expiry timestamp.  Tokens are rotated on use (the old one is
    invalidated and a new one is issued).
    """

    def __init__(self, *, max_tokens: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._max = max_tokens
        # token_hash → {"client_id": str, "expires_at": float}
        self._store: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _hash(token: str) -> str:
        """Store only a SHA-256 hash — never the raw token."""
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self, client_id: str, ttl: int) -> str:
        """Create a new refresh token for *client_id*."""
        raw = f"prf_{secrets.token_urlsafe(48)}"
        h = self._hash(raw)
        with self._lock:
            # Evict oldest if at capacity
            if len(self._store) >= self._max:
                oldest = next(iter(self._store))
                del self._store[oldest]
            self._store[h] = {
                "client_id": client_id,
                "expires_at": time.time() + ttl,
            }
        return raw

    def consume(self, raw_token: str) -> str | None:
        """Validate and consume a refresh token (one-time use).

        Returns the ``client_id`` if valid, or ``None``.
        """
        h = self._hash(raw_token)
        with self._lock:
            entry = self._store.pop(h, None)
        if entry is None:
            return None
        if time.time() > entry["expires_at"]:
            return None
        return entry["client_id"]

    def revoke_client(self, client_id: str) -> int:
        """Revoke all refresh tokens for a client. Returns count revoked."""
        with self._lock:
            to_remove = [h for h, v in self._store.items() if v["client_id"] == client_id]
            for h in to_remove:
                del self._store[h]
        return len(to_remove)

    @property
    def size(self) -> int:
        return len(self._store)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TokenEndpointConfig:
    """Configuration for the built-in OAuth 2.0 token issuer.

    Args:
        jwt_auth: The ``JWTAuth`` instance used to sign tokens.
        clients: Mapping of ``client_id`` to client configuration.
            Each config must have ``"secret"`` and optionally
            ``"roles"``, ``"expires_in"``, and ``"claims"``.
        path: HTTP path for the endpoint (default ``/auth/token``).
        default_expires_in: Default access token lifetime in seconds.
        refresh_token_ttl: Refresh token lifetime in seconds
            (default 30 days).  Set to ``0`` to disable refresh tokens.
    """

    def __init__(
        self,
        jwt_auth: Any,
        clients: dict[str, dict[str, Any]],
        *,
        path: str = "/auth/token",
        default_expires_in: int = 3600,
        refresh_token_ttl: int = 30 * 86400,
    ) -> None:
        self.jwt_auth = jwt_auth
        self.clients = dict(clients)
        self.path = path
        self.default_expires_in = default_expires_in
        self.refresh_token_ttl = refresh_token_ttl
        self.refresh_store = _RefreshTokenStore()


# ---------------------------------------------------------------------------
# ASGI handler
# ---------------------------------------------------------------------------


async def handle_token_request(
    scope: Any,
    receive: Any,
    send: Any,
    config: TokenEndpointConfig,
) -> None:
    """ASGI handler for the ``/auth/token`` endpoint.

    Implements two OAuth 2.0 grant types:

    - ``client_credentials`` — exchange client_id + client_secret for
      an access token and (optionally) a refresh token.
    - ``refresh_token`` — exchange a valid refresh token for a new
      access token and a rotated refresh token.

    This is a raw ASGI handler (not a Starlette endpoint) so it has
    zero additional dependencies beyond what we already use.
    """
    if scope["type"] != "http":
        await _send_json(send, 400, {"error": "invalid_request"})
        return

    method = scope.get("method", "GET")

    # Only POST allowed
    if method != "POST":
        await _send_json(
            send,
            405,
            {
                "error": "method_not_allowed",
                "message": "Use POST to request a token.",
            },
        )
        return

    # Read request body
    body = b""
    max_body_size = 65_536  # 64 KiB
    while True:
        message = await receive()
        body += message.get("body", b"")
        if len(body) > max_body_size:
            await _send_json(
                send,
                413,
                {
                    "error": "payload_too_large",
                    "message": "Request body exceeds 64 KiB limit.",
                },
            )
            return
        if not message.get("more_body", False):
            break

    # Parse JSON body
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        await _send_json(
            send,
            400,
            {
                "error": "invalid_request",
                "message": "Request body must be valid JSON.",
            },
        )
        return

    # Route by grant_type (default to client_credentials for backward compat)
    grant_type = data.get("grant_type", "client_credentials")

    if grant_type == "client_credentials":
        await _handle_client_credentials(send, data, config)
    elif grant_type == "refresh_token":
        await _handle_refresh_token(send, data, config)
    else:
        await _send_json(
            send,
            400,
            {
                "error": "unsupported_grant_type",
                "message": f"Unsupported grant_type: {grant_type!r}. "
                "Use 'client_credentials' or 'refresh_token'.",
            },
        )


# ---------------------------------------------------------------------------
# Grant type handlers
# ---------------------------------------------------------------------------


async def _handle_client_credentials(
    send: Any,
    data: dict[str, Any],
    config: TokenEndpointConfig,
) -> None:
    """Handle ``grant_type=client_credentials``."""
    client_id = data.get("client_id", "")
    client_secret = data.get("client_secret", "")

    if not client_id or not client_secret:
        await _send_json(
            send,
            400,
            {
                "error": "invalid_request",
                "message": "Both 'client_id' and 'client_secret' are required.",
            },
        )
        return

    # Validate credentials
    client_config = config.clients.get(client_id)
    stored_secret = client_config.get("secret", "") if client_config else ""
    if client_config is None or not _hmac.compare_digest(
        stored_secret.encode(), client_secret.encode()
    ):
        logger.warning("Token request rejected: invalid credentials for '%s'", client_id)
        await _send_json(
            send,
            401,
            {
                "error": "invalid_client",
                "message": "Invalid client_id or client_secret.",
            },
        )
        return

    await _issue_tokens(send, client_id, client_config, config)


async def _handle_refresh_token(
    send: Any,
    data: dict[str, Any],
    config: TokenEndpointConfig,
) -> None:
    """Handle ``grant_type=refresh_token``."""
    if config.refresh_token_ttl <= 0:
        await _send_json(
            send,
            400,
            {
                "error": "invalid_request",
                "message": "Refresh tokens are disabled on this server.",
            },
        )
        return

    raw_refresh = data.get("refresh_token", "")
    if not raw_refresh:
        await _send_json(
            send,
            400,
            {
                "error": "invalid_request",
                "message": "'refresh_token' is required.",
            },
        )
        return

    # Consume the refresh token (one-time use — rotation)
    client_id = config.refresh_store.consume(raw_refresh)
    if client_id is None:
        logger.warning("Refresh token rejected: invalid or expired")
        await _send_json(
            send,
            401,
            {
                "error": "invalid_grant",
                "message": "Invalid or expired refresh token.",
            },
        )
        return

    # Look up client config (client may have been removed since the refresh
    # token was issued — fail gracefully)
    client_config = config.clients.get(client_id)
    if client_config is None:
        logger.warning(
            "Refresh token rejected: client '%s' no longer registered",
            client_id,
        )
        await _send_json(
            send,
            401,
            {
                "error": "invalid_client",
                "message": "Client is no longer registered.",
            },
        )
        return

    logger.info("Token refreshed for client '%s'", client_id)
    await _issue_tokens(send, client_id, client_config, config)


# ---------------------------------------------------------------------------
# Token issuance (shared between both grant types)
# ---------------------------------------------------------------------------


async def _issue_tokens(
    send: Any,
    client_id: str,
    client_config: dict[str, Any],
    config: TokenEndpointConfig,
) -> None:
    """Issue an access token (and optionally a refresh token)."""
    roles = client_config.get("roles", [])
    expires_in = client_config.get("expires_in", config.default_expires_in)

    claims = {"sub": client_id, "roles": roles}

    # Include any extra claims the developer configured
    extra_claims = client_config.get("claims", {})
    claims.update(extra_claims)

    access_token = config.jwt_auth.create_token(claims, expires_in=expires_in)

    response: dict[str, Any] = {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": expires_in,
    }

    # Issue a refresh token if enabled
    if config.refresh_token_ttl > 0:
        refresh_token = config.refresh_store.issue(client_id, config.refresh_token_ttl)
        response["refresh_token"] = refresh_token

    logger.info(
        "Token issued for client '%s' with roles %s (expires_in=%ds)",
        client_id,
        roles,
        expires_in,
    )

    await _send_json(send, 200, response)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _send_json(send: Any, status: int, body: dict[str, Any]) -> None:
    """Send a JSON HTTP response via raw ASGI."""
    payload = json.dumps(body).encode()
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
