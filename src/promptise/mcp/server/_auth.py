"""Authentication providers for MCP server tools.

Provides pluggable auth that integrates as middleware.  After
authentication, the :class:`~._context.ClientContext` on the request
is fully populated with client identity, roles, scopes, standard JWT
claims, IP address, and user-agent.

Example::

    from promptise.mcp.server import MCPServer, AuthMiddleware, JWTAuth

    auth = JWTAuth(secret="my-secret")
    server = MCPServer(name="secure-api")
    server.add_middleware(AuthMiddleware(auth))

    @server.tool(auth=True)
    async def secret_data(ctx: RequestContext) -> str:
        print(ctx.client.client_id)   # "agent-007"
        print(ctx.client.roles)       # {"admin"}
        print(ctx.client.scopes)      # {"read", "write"}
        print(ctx.client.issuer)      # "https://auth.example.com"
        print(ctx.client.ip_address)  # "10.0.0.1"
        return "classified"
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from ._context import ClientContext, RequestContext, get_request_client_info
from ._errors import AuthenticationError

logger = logging.getLogger("promptise.server")

# Type alias for the on_authenticate enrichment hook.
OnAuthenticateHook = Callable[[ClientContext, RequestContext], Awaitable[None] | None]


@runtime_checkable
class AuthProvider(Protocol):
    """Protocol for authentication providers."""

    async def authenticate(self, ctx: RequestContext) -> str:
        """Authenticate the request.

        Returns:
            Client identifier string on success.

        Raises:
            AuthenticationError: On authentication failure.
        """
        ...


# =====================================================================
# Token verification cache
# =====================================================================


class _TokenCache:
    """LRU cache for verified JWT payloads with TTL expiry.

    Avoids re-computing HMAC-SHA256 + base64 decoding on every request
    when the same token is reused (which is the common case for agents).

    Thread-safe for use in multi-worker uvicorn deployments.

    Args:
        max_size: Maximum cached tokens.  Oldest entries evicted first.
    """

    def __init__(self, max_size: int = 256) -> None:
        self._store: OrderedDict[str, tuple[dict[str, Any], float]] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, token: str) -> dict[str, Any] | None:
        """Return cached payload if valid, else ``None``.

        The read path is lock-free for performance under high concurrency.
        Python's GIL guarantees ``dict.get()`` atomicity.  Best-effort
        LRU reordering and expiry eviction are wrapped in try/except to
        tolerate benign races with concurrent ``put()`` calls.
        """
        entry = self._store.get(token)
        if entry is None:
            return None
        payload, cached_at = entry
        # Honour JWT expiry — evict if token has since expired
        exp = payload.get("exp")
        if exp is not None and exp < time.time():
            try:
                del self._store[token]
            except KeyError:
                pass  # Concurrent eviction — benign
            return None
        # Best-effort LRU reordering (race with put is harmless)
        try:
            self._store.move_to_end(token)
        except KeyError:
            pass  # Concurrent eviction — benign
        return payload

    def put(self, token: str, payload: dict[str, Any]) -> None:
        """Cache a verified payload."""
        with self._lock:
            if token in self._store:
                self._store.move_to_end(token)
                self._store[token] = (payload, time.time())
                return
            if len(self._store) >= self._max_size:
                self._store.popitem(last=False)  # evict oldest
            self._store[token] = (payload, time.time())

    def invalidate(self, token: str) -> None:
        """Remove a specific token from the cache."""
        with self._lock:
            self._store.pop(token, None)

    def clear(self) -> None:
        """Remove all cached tokens."""
        with self._lock:
            self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


class JWTAuth:
    """JWT-based authentication provider.

    Validates JWT tokens from the request metadata using HMAC-SHA256.
    Verified tokens are cached in an LRU to avoid repeated crypto
    operations on the hot path.

    Args:
        secret: Shared secret for HS256 signature verification.
        meta_key: Key in ``ctx.meta`` where the token is expected.
        cache_size: Max number of verified tokens to cache (0 to disable).
    """

    def __init__(
        self,
        secret: str,
        *,
        meta_key: str = "authorization",
        cache_size: int = 256,
    ) -> None:
        self._secret = secret.encode()
        self._meta_key = meta_key
        self._cache = _TokenCache(max_size=cache_size) if cache_size > 0 else None

    async def authenticate(self, ctx: RequestContext) -> str:
        token = ctx.meta.get(self._meta_key, "")
        if token.startswith("Bearer "):
            token = token[7:]
        if not token:
            raise AuthenticationError("Missing authentication token")

        # Fast path: return cached payload without crypto
        if self._cache is not None:
            cached_payload = self._cache.get(token)
            if cached_payload is not None:
                ctx.state["_jwt_payload"] = cached_payload
                return cached_payload.get("sub", cached_payload.get("client_id", "unknown"))

        payload = self._verify_token(token)

        # Cache the verified payload
        if self._cache is not None:
            self._cache.put(token, payload)

        # Store full payload for downstream use (e.g. role extraction)
        ctx.state["_jwt_payload"] = payload
        return payload.get("sub", payload.get("client_id", "unknown"))

    def _verify_token(self, token: str) -> dict[str, Any]:
        """Verify and decode a HS256 JWT token."""
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthenticationError("Malformed JWT token")

        header_b64, payload_b64, signature_b64 = parts

        # Validate header algorithm — reject tokens that claim anything other
        # than HS256 to prevent algorithm confusion attacks.
        try:
            header_json = base64.urlsafe_b64decode(header_b64 + "==")
            header = json.loads(header_json)
        except Exception:
            raise AuthenticationError("Invalid JWT header")

        if header.get("alg") != "HS256":
            raise AuthenticationError(
                f"Unsupported JWT algorithm: {header.get('alg')!r}. Only HS256 is accepted."
            )

        # Verify signature
        signing_input = f"{header_b64}.{payload_b64}".encode()
        expected_sig = hmac.new(self._secret, signing_input, hashlib.sha256).digest()

        try:
            actual_sig = base64.urlsafe_b64decode(signature_b64 + "==")
        except Exception:
            raise AuthenticationError("Invalid JWT signature encoding")

        if not hmac.compare_digest(expected_sig, actual_sig):
            raise AuthenticationError("Invalid JWT signature")

        # Decode payload
        try:
            payload_json = base64.urlsafe_b64decode(payload_b64 + "==")
            payload = json.loads(payload_json)
        except Exception:
            raise AuthenticationError("Invalid JWT payload")

        # Check expiry — require exp claim to prevent indefinite tokens
        now = time.time()
        if "exp" not in payload:
            raise AuthenticationError(
                "Token missing 'exp' (expiry) claim. Tokens without expiry are not accepted.",
                suggestion="Include an 'exp' claim when generating JWT tokens.",
            )
        if payload["exp"] < now:
            raise AuthenticationError(
                "Token expired",
                suggestion="Request a new authentication token",
            )

        # Check not-before
        if "nbf" in payload and payload["nbf"] > now:
            raise AuthenticationError("Token not yet valid")

        return payload

    def verify_token(self, token: str) -> bool:
        """Check if a token is valid without requiring a request context.

        Useful for transport-level auth gating.
        """
        try:
            self._verify_token(token)
            return True
        except AuthenticationError:
            return False

    def create_token(self, payload: dict[str, Any], *, expires_in: int = 3600) -> str:
        """Create a signed JWT token (utility for testing).

        Args:
            payload: Claims to include in the token.
            expires_in: Token lifetime in seconds.
        """
        header = (
            base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
            .rstrip(b"=")
            .decode()
        )

        full_payload = {**payload, "exp": int(time.time()) + expires_in}
        payload_b64 = (
            base64.urlsafe_b64encode(json.dumps(full_payload).encode()).rstrip(b"=").decode()
        )

        signing_input = f"{header}.{payload_b64}".encode()
        signature = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()

        return f"{header}.{payload_b64}.{sig_b64}"


class AsymmetricJWTAuth:
    """JWT authentication using asymmetric algorithms (RS256, ES256).

    Validates JWT tokens signed with RSA or ECDSA keys.  Requires the
    ``PyJWT`` and ``cryptography`` packages (optional dependencies).

    Args:
        public_key: PEM-encoded public key string, or path to a PEM
            file.  Used for signature verification.
        algorithm: JWT algorithm (``"RS256"`` or ``"ES256"``).
        meta_key: Key in ``ctx.meta`` where the token is expected.
        cache_size: Max cached tokens (0 to disable).

    Example::

        auth = AsymmetricJWTAuth(
            public_key=open("public.pem").read(),
            algorithm="RS256",
        )
        server.add_middleware(AuthMiddleware(auth))
    """

    def __init__(
        self,
        public_key: str,
        *,
        algorithm: str = "RS256",
        meta_key: str = "authorization",
        cache_size: int = 256,
    ) -> None:
        if algorithm not in ("RS256", "ES256"):
            raise ValueError(f"Unsupported algorithm: {algorithm}. Use RS256 or ES256.")

        self._algorithm = algorithm
        self._meta_key = meta_key
        self._cache = _TokenCache(max_size=cache_size) if cache_size > 0 else None

        # Load public key
        import pathlib

        key_str = public_key.strip()
        if not key_str.startswith("-----"):
            path = pathlib.Path(key_str)
            if path.exists():
                key_str = path.read_text().strip()

        self._public_key_pem = key_str

    async def authenticate(self, ctx: RequestContext) -> str:
        """Authenticate using an asymmetric JWT token."""
        token = ctx.meta.get(self._meta_key, "")
        if token.startswith("Bearer "):
            token = token[7:]
        if not token:
            raise AuthenticationError("Missing authentication token")

        if self._cache is not None:
            cached_payload = self._cache.get(token)
            if cached_payload is not None:
                ctx.state["_jwt_payload"] = cached_payload
                return cached_payload.get("sub", cached_payload.get("client_id", "unknown"))

        payload = self._verify_token(token)

        if self._cache is not None:
            self._cache.put(token, payload)

        ctx.state["_jwt_payload"] = payload
        return payload.get("sub", payload.get("client_id", "unknown"))

    def _verify_token(self, token: str) -> dict[str, Any]:
        """Verify and decode a JWT using PyJWT."""
        try:
            import jwt as pyjwt
        except ImportError:
            raise ImportError(
                "PyJWT and cryptography are required for asymmetric JWT. "
                "Install with: pip install PyJWT cryptography"
            )

        try:
            payload = pyjwt.decode(
                token,
                self._public_key_pem,
                algorithms=[self._algorithm],
            )
            return payload
        except pyjwt.ExpiredSignatureError:
            raise AuthenticationError(
                "Token expired",
                suggestion="Request a new authentication token",
            )
        except pyjwt.InvalidTokenError as e:
            raise AuthenticationError(f"Invalid JWT: {e}")

    def verify_token(self, token: str) -> bool:
        """Check if a token is valid without requiring a request context."""
        try:
            self._verify_token(token)
            return True
        except (AuthenticationError, ImportError):
            return False


class APIKeyAuth:
    """API key-based authentication provider.

    Supports two key formats:

    **Simple** — ``{api_key: client_id}``::

        APIKeyAuth(keys={"sk-abc": "agent-1", "sk-xyz": "agent-2"})

    **Rich** — ``{api_key: {"client_id": ..., "roles": [...]}}``::

        APIKeyAuth(keys={
            "sk-abc": {"client_id": "agent-1", "roles": ["admin", "write"]},
            "sk-xyz": {"client_id": "agent-2", "roles": ["read"]},
        })

    Rich keys populate ``ctx.state["roles"]`` so that role-based
    guards (``HasRole``, ``HasAllRoles``) work out of the box.

    Args:
        keys: Mapping of ``{api_key: client_id_or_config}``.
        header: HTTP header name used to transmit the key.
    """

    def __init__(
        self,
        keys: dict[str, str | dict[str, Any]],
        *,
        header: str = "x-api-key",
    ) -> None:
        # Normalise to rich format internally
        self._keys: dict[str, dict[str, Any]] = {}
        for k, v in keys.items():
            if isinstance(v, str):
                self._keys[k] = {"client_id": v, "roles": []}
            else:
                self._keys[k] = {
                    "client_id": v.get("client_id", "unknown"),
                    "roles": list(v.get("roles", [])),
                }
        self._meta_key = header

    async def authenticate(self, ctx: RequestContext) -> str:
        """Authenticate using an API key from request metadata.

        On success, sets ``ctx.state["roles"]`` if the key config
        includes roles.

        Returns:
            The ``client_id`` associated with the key.

        Raises:
            AuthenticationError: If the key is missing or invalid.
        """
        key = ctx.meta.get(self._meta_key, "")
        if not key:
            raise AuthenticationError("Missing API key")
        # Timing-safe key comparison: iterate all registered keys
        # using hmac.compare_digest to prevent timing attacks.
        entry = None
        for registered_key, registered_entry in self._keys.items():
            if hmac.compare_digest(key.encode(), registered_key.encode()):
                entry = registered_entry
                break
        if entry is None:
            raise AuthenticationError("Invalid API key")
        # Populate roles for guard compatibility
        if entry["roles"]:
            ctx.state["roles"] = set(entry["roles"])
        return entry["client_id"]

    def verify_token(self, key: str) -> bool:
        """Check if an API key is valid without requiring a context."""
        # Use timing-safe comparison to prevent key enumeration.
        for registered_key in self._keys:
            if hmac.compare_digest(key.encode(), registered_key.encode()):
                return True
        return False


# =====================================================================
# Helper: build ClientContext from JWT payload
# =====================================================================


def _build_client_context_from_jwt(
    payload: dict[str, Any],
    client_id: str,
    *,
    existing_roles: set[str] | None = None,
    meta: dict[str, Any] | None = None,
) -> ClientContext:
    """Build a :class:`ClientContext` from a verified JWT payload.

    Extracts standard claims (iss, aud, sub, iat, exp) and the
    ``scope`` claim (space-separated string per RFC 8693) into typed
    fields.

    Args:
        payload: Decoded JWT claims dict.
        client_id: Client identifier string (from ``sub`` or provider).
        existing_roles: Roles already extracted by the provider (e.g.
            from API key config).  Merged with JWT ``roles`` claim.
        meta: HTTP headers dict to extract user-agent from.
    """
    # Roles: merge JWT "roles" claim with any provider-supplied roles
    jwt_roles = set(payload.get("roles", []))
    all_roles = (existing_roles or set()) | jwt_roles

    # Scopes: parse space-separated "scope" claim (RFC 8693 / OAuth 2.0)
    scope_claim = payload.get("scope", "")
    scopes: set[str] = set()
    if isinstance(scope_claim, str) and scope_claim.strip():
        scopes = set(scope_claim.split())
    elif isinstance(scope_claim, list):
        scopes = set(scope_claim)

    # IP and User-Agent from transport
    ip_address: str | None = None
    user_agent: str | None = None
    if meta:
        user_agent = meta.get("user-agent")
    client_info = get_request_client_info()
    if client_info:
        ip_address = client_info[0]

    return ClientContext(
        client_id=client_id,
        roles=all_roles,
        scopes=scopes,
        claims=payload,
        issuer=payload.get("iss"),
        audience=payload.get("aud"),
        subject=payload.get("sub"),
        issued_at=payload.get("iat"),
        expires_at=payload.get("exp"),
        ip_address=ip_address,
        user_agent=user_agent,
    )


def _build_client_context_from_api_key(
    client_id: str,
    roles: set[str],
    *,
    meta: dict[str, Any] | None = None,
) -> ClientContext:
    """Build a :class:`ClientContext` for API key auth (no JWT claims)."""
    ip_address: str | None = None
    user_agent: str | None = None
    if meta:
        user_agent = meta.get("user-agent")
    client_info = get_request_client_info()
    if client_info:
        ip_address = client_info[0]

    return ClientContext(
        client_id=client_id,
        roles=roles,
        ip_address=ip_address,
        user_agent=user_agent,
    )


# =====================================================================
# AuthMiddleware
# =====================================================================


class AuthMiddleware:
    """Middleware that enforces authentication on tools with ``auth=True``.

    After successful authentication, populates ``ctx.client`` with a
    fully structured :class:`ClientContext` containing roles, scopes,
    standard JWT claims, client IP, and user-agent.

    Optionally accepts an ``on_authenticate`` hook for custom
    enrichment (e.g. loading org, tenant, or plan info from a database).

    Args:
        provider: An ``AuthProvider`` implementation.
        on_authenticate: Optional async or sync callable that receives
            ``(client_ctx, request_ctx)`` and can mutate ``client_ctx``
            (e.g. populate ``client_ctx.extra``).

    Example::

        async def enrich_client(client: ClientContext, ctx: RequestContext):
            org = await db.get_org_for_client(client.client_id)
            client.extra["org_id"] = org.id
            client.extra["plan"] = org.plan

        server.add_middleware(AuthMiddleware(auth, on_authenticate=enrich_client))
    """

    def __init__(
        self,
        provider: Any,
        *,
        on_authenticate: OnAuthenticateHook | None = None,
    ) -> None:
        self._provider = provider
        self._on_authenticate = on_authenticate

    async def __call__(self, ctx: RequestContext, call_next: Callable[..., Any]) -> Any:
        tool_def = ctx.state.get("tool_def")
        if tool_def and tool_def.auth:
            client_id = await self._provider.authenticate(ctx)
            ctx.client_id = client_id

            # Build structured ClientContext
            jwt_payload = ctx.state.get("_jwt_payload", {})
            existing_roles = ctx.state.get("roles", set())

            if jwt_payload:
                # JWT-based auth — extract standard claims + scopes
                client_ctx = _build_client_context_from_jwt(
                    jwt_payload,
                    client_id,
                    existing_roles=existing_roles,
                    meta=ctx.meta,
                )
            else:
                # API key auth — no JWT claims, just roles
                client_ctx = _build_client_context_from_api_key(
                    client_id,
                    existing_roles,
                    meta=ctx.meta,
                )

            # Merge roles back to ctx.state for backward compatibility
            # with guards that read from ctx.state["roles"]
            jwt_roles = set(jwt_payload.get("roles", []))
            ctx.state["roles"] = existing_roles | jwt_roles

            # Run enrichment hook if configured
            if self._on_authenticate is not None:
                result = self._on_authenticate(client_ctx, ctx)
                if asyncio.iscoroutine(result):
                    await result

            # Attach to request context
            ctx.client = client_ctx

            logger.debug(
                "Authenticated client=%s roles=%s scopes=%s ip=%s",
                client_ctx.client_id,
                client_ctx.roles,
                client_ctx.scopes,
                client_ctx.ip_address,
            )

        return await call_next(ctx)
