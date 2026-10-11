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

Clients obtain tokens with a standard OAuth 2.0 (RFC 6749) form-encoded
request::

    POST /auth/token
    Content-Type: application/x-www-form-urlencoded

    grant_type=client_credentials&client_id=agent-admin&client_secret=admin-pass

The client may instead authenticate with HTTP Basic
(``Authorization: Basic base64(client_id:client_secret)``, RFC 6749
section 2.3.1).  A JSON body with the same fields is still accepted for
backward compatibility (``grant_type`` defaults to ``client_credentials``
there)::

    POST /auth/token
    Content-Type: application/json

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
    Content-Type: application/x-www-form-urlencoded

    grant_type=refresh_token&refresh_token=prf_...

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

import base64
import binascii
import hashlib
import hmac as _hmac
import json
import logging
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote_plus

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

    The body is form-encoded (``application/x-www-form-urlencoded``, as
    RFC 6749 specifies; ``grant_type`` is required) or, for backward
    compatibility, a JSON object (``grant_type`` defaults to
    ``client_credentials``).  Client credentials come from the body or from
    an HTTP Basic ``Authorization`` header.  Errors follow RFC 6749
    section 5.2 (``error`` + ``error_description``; ``message`` repeats the
    description for older clients).

    This is a raw ASGI handler (not a Starlette endpoint) so it has
    zero additional dependencies beyond what we already use.
    """
    if scope["type"] != "http":
        await _send_json(send, 400, {"error": "invalid_request"})
        return

    method = scope.get("method", "GET")

    # Only POST allowed
    if method != "POST":
        await _send_error(
            send,
            405,
            "method_not_allowed",
            "Use POST to request a token.",
        )
        return

    # Read request body
    body = b""
    max_body_size = 65_536  # 64 KiB
    while True:
        message = await receive()
        body += message.get("body", b"")
        if len(body) > max_body_size:
            await _send_error(
                send,
                413,
                "payload_too_large",
                "Request body exceeds 64 KiB limit.",
            )
            return
        if not message.get("more_body", False):
            break

    headers = _headers(scope)
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded":
        # RFC 6749: form-encoded parameters, each at most once.
        try:
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
        except UnicodeDecodeError:
            await _send_error(send, 400, "invalid_request", "Request body must be UTF-8.")
            return
        repeated = sorted(k for k, v in fields.items() if len(v) > 1)
        if repeated:
            await _send_error(
                send,
                400,
                "invalid_request",
                f"Parameters must not be repeated: {', '.join(repeated)}.",
            )
            return
        data: dict[str, Any] = {k: v[0] for k, v in fields.items()}
        # RFC 6749 requires grant_type; JSON requests keep their old default.
        if "grant_type" not in data:
            await _send_error(send, 400, "invalid_request", "'grant_type' is required.")
            return
    else:
        # JSON body (the original format, kept for backward compatibility).
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if not isinstance(parsed, dict):
            await _send_error(
                send,
                400,
                "invalid_request",
                "Request body must be form-encoded "
                "(Content-Type: application/x-www-form-urlencoded) or a JSON object.",
            )
            return
        data = parsed
        data.setdefault("grant_type", "client_credentials")

    # HTTP Basic client authentication (RFC 6749 section 2.3.1)
    basic = _basic_credentials(headers.get("authorization", ""))
    if basic is not None:
        basic_id, basic_secret = basic
        if ("client_id" in data and data["client_id"] != basic_id) or "client_secret" in data:
            # RFC 6749 section 2.3: one authentication method per request.
            await _send_error(
                send,
                400,
                "invalid_request",
                "Use either HTTP Basic or client_secret in the body, not both.",
            )
            return
        data["client_id"] = basic_id
        data["client_secret"] = basic_secret

    grant_type = data.get("grant_type")

    if grant_type == "client_credentials":
        await _handle_client_credentials(send, data, config)
    elif grant_type == "refresh_token":
        await _handle_refresh_token(send, data, config)
    else:
        await _send_error(
            send,
            400,
            "unsupported_grant_type",
            f"Unsupported grant_type: {grant_type!r}. Use 'client_credentials' or 'refresh_token'.",
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

    if (
        not client_id
        or not client_secret
        or not isinstance(client_id, str)
        or not isinstance(client_secret, str)
    ):
        await _send_error(
            send,
            400,
            "invalid_request",
            "Both 'client_id' and 'client_secret' are required.",
        )
        return

    # Validate credentials
    client_config = config.clients.get(client_id)
    stored_secret = client_config.get("secret", "") if client_config else ""
    if client_config is None or not _hmac.compare_digest(
        stored_secret.encode(), client_secret.encode()
    ):
        logger.warning("Token request rejected: invalid credentials for '%s'", client_id)
        await _send_error(
            send,
            401,
            "invalid_client",
            "Invalid client_id or client_secret.",
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
        await _send_error(
            send,
            400,
            "invalid_request",
            "Refresh tokens are disabled on this server.",
        )
        return

    raw_refresh = data.get("refresh_token", "")
    if not raw_refresh or not isinstance(raw_refresh, str):
        await _send_error(
            send,
            400,
            "invalid_request",
            "'refresh_token' is required.",
        )
        return

    # Consume the refresh token (one-time use — rotation)
    client_id = config.refresh_store.consume(raw_refresh)
    if client_id is None:
        logger.warning("Refresh token rejected: invalid or expired")
        await _send_error(
            send,
            401,
            "invalid_grant",
            "Invalid or expired refresh token.",
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
        await _send_error(
            send,
            401,
            "invalid_client",
            "Client is no longer registered.",
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


def _headers(scope: Any) -> dict[str, str]:
    """Request headers as a lower-cased ``{name: value}`` dict."""
    out: dict[str, str] = {}
    for name, value in scope.get("headers") or []:
        key = name.decode("latin-1") if isinstance(name, bytes) else str(name)
        val = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        out[key.lower()] = val
    return out


def _basic_credentials(authorization: str) -> tuple[str, str] | None:
    """``(client_id, client_secret)`` from an HTTP Basic header, else ``None``.

    RFC 6749 section 2.3.1: both parts are form-urlencoded before being
    joined with ``:`` and base64-encoded.
    """
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded.strip():
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    client_id, sep, client_secret = decoded.partition(":")
    if not sep:
        return None
    return unquote_plus(client_id), unquote_plus(client_secret)


async def _send_error(send: Any, status: int, error: str, description: str) -> None:
    """Send an OAuth 2.0 error response (RFC 6749 section 5.2).

    ``error_description`` is the standard field; ``message`` carries the
    same text for clients written against earlier versions.
    """
    await _send_json(
        send, status, {"error": error, "error_description": description, "message": description}
    )


async def _send_json(send: Any, status: int, body: dict[str, Any]) -> None:
    """Send a JSON HTTP response via raw ASGI.

    Token responses carry credentials, so they are never cached
    (RFC 6749 section 5.1).
    """
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(payload)).encode()],
                [b"cache-control", b"no-store"],
                [b"pragma", b"no-cache"],
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": payload,
        }
    )
