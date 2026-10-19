"""Typed server specifications and conversion helpers.

Supports the Promptise MCP Client with token-based authentication
(Bearer token or API key).
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FieldSerializationInfo,
    SecretStr,
    field_serializer,
    field_validator,
)

#: Shown instead of a credential header's value in dumps and reprs.
MASKED_HEADER_VALUE = "**********"

#: Header names whose values are credentials (matched case-insensitively,
#: anywhere in the name): masked in JSON dumps and reprs.
_CREDENTIAL_HEADER = re.compile(
    r"authorization|cookie|api[-_]?key|token|secret|password|passwd|session|signature|credential",
    re.IGNORECASE,
)


def is_credential_header(name: str) -> bool:
    """Whether header *name* carries a credential (its value is masked)."""
    return bool(_CREDENTIAL_HEADER.search(name))


def mask_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """A copy of *headers* with credential values replaced by ``**********``."""
    return {
        k: (MASKED_HEADER_VALUE if is_credential_header(k) and v else v) for k, v in headers.items()
    }


class _BaseServer(BaseModel):
    """Base model for server specs."""

    model_config = ConfigDict(extra="forbid")


class StdioServerSpec(_BaseServer):
    """Specification for a local MCP server launched via stdio.

    Attributes:
        command: Executable to launch (e.g., ``"python"``).
        args: Positional arguments for the process.
        env: Environment variables to set for the process.
        cwd: Optional working directory.
        keep_alive: Whether the client should try to keep a persistent session.
    """

    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    keep_alive: bool = True


class HTTPServerSpec(_BaseServer):
    """Specification for a remote MCP server reachable via HTTP/SSE.

    Supports two authentication methods:

    1. **Bearer token** (JWT): pass a pre-issued token via ``bearer_token``
       and it is automatically injected as ``Authorization: Bearer <token>``.
    2. **API key** (simple secret): pass a pre-shared key via ``api_key``
       and it is automatically injected as ``x-api-key: <key>``.

    Tokens should be obtained from your Identity Provider (Auth0, Keycloak,
    Okta, etc.) or from the MCP server's built-in token endpoint (for
    development/testing).  The agent **never** generates tokens itself.

    Attributes:
        url: Full endpoint URL (e.g., ``http://127.0.0.1:8080/mcp``).
        transport: ``"http"``, ``"streamable-http"``, or ``"sse"``.
        headers: Extra HTTP headers sent on every request.  Values of
            credential headers (``Authorization``, ``Cookie``,
            ``x-api-key``, names containing ``token``, ``secret``, ...)
            are sent as given but shown as ``**********`` in ``repr()``
            and in ``model_dump(mode="json")`` (a ``${VAR}`` resolved
            from a manifest never shows up in a dump or a log line).
            ``model_dump()`` in Python mode keeps them, so a spec can be
            rebuilt from it.
        auth: Deprecated and ignored — it was never sent to the server.
            Setting it emits a ``FutureWarning``; use ``bearer_token`` or
            ``api_key``.
        bearer_token: Pre-issued Bearer token.  When set, an
            ``Authorization: Bearer <token>`` header is created
            automatically.
        api_key: Pre-shared API key.  When set, an ``x-api-key``
            header is created automatically.  Use this for simple
            secret-based auth when JWT is overkill.
        forward_caller_token: Whether an invocation's caller token
            reaches this server.  When it does and the invocation carries
            a :class:`~promptise.CallerContext` with a ``bearer_token``,
            this server's tools are called with
            ``Authorization: Bearer <caller token>`` over a session
            opened for that caller, so the server sees the user, not
            whoever built the agent.  The caller's token replaces
            ``bearer_token`` (and any ``Authorization`` header), which,
            like the agent identity, is still used for tool discovery and
            for calls without a caller token.

            ``None`` (default): forward, unless the server is configured
            with an API key (``api_key`` or an ``x-api-key`` header).  An
            API key is the server's explicit credential and wins: the
            caller's bearer would be sent *next to* it, and a gate that
            checks the bearer first (a Promptise server does) rejects the
            call with ``401``.  ``True``: always forward, also to an API-key
            server; the key is still sent, so the server receives both and
            decides which one counts (a Promptise server checks the bearer
            first).  ``False``: never forward; use it for a third-party
            server that must not receive your users' tokens, or one that
            should always see the agent's own credential.  See
            :attr:`forwards_caller_token` for the resolved value.

    Example — Bearer token::

        HTTPServerSpec(
            url="http://localhost:8080/mcp",
            bearer_token="eyJhbGciOiJIUzI1NiIs...",
        )

    Example — API key::

        HTTPServerSpec(
            url="http://localhost:8080/mcp",
            api_key="my-secret-key",
        )

    Example — manual header::

        HTTPServerSpec(
            url="http://localhost:8080/mcp",
            headers={"authorization": "Bearer <token>"},
        )
    """

    url: str
    transport: Literal["http", "streamable-http", "sse"] = "http"
    headers: dict[str, str] = Field(default_factory=dict)
    auth: str | None = None

    # Resource audience this server expects. When an agent identity is
    # attached and the server has no credential of its own (bearer_token,
    # api_key, or an Authorization / x-api-key header), build_agent presents
    # a credential minted for this audience (per-resource identity).
    audience: str | None = Field(
        default=None,
        description="Resource audience this server expects. When an agent "
        "identity is attached, a credential scoped to this audience is "
        "presented automatically.",
    )

    # Token auth — pass a pre-issued Bearer token (from an IdP or server token endpoint)
    bearer_token: SecretStr | None = Field(
        default=None,
        description="Pre-issued Bearer token for authentication. "
        "Obtain from your Identity Provider or the server's token endpoint.",
    )

    # API key auth — simple pre-shared secret (injected as x-api-key header)
    api_key: SecretStr | None = Field(
        default=None,
        description="Pre-shared API key for simple secret-based authentication. "
        "Injected as an x-api-key header.",
    )

    # Per-invocation identity: send the invoking user's token, not the agent's.
    forward_caller_token: bool | None = Field(
        default=None,
        description="When an invocation carries a CallerContext with a "
        "bearer_token, call this server's tools with that token (one session "
        "per caller) instead of bearer_token / the agent identity. None "
        "(default) forwards unless the server has an API key (api_key or an "
        "x-api-key header), which then wins. True always forwards (the key is "
        "still sent next to the caller's bearer). False never forwards: use it "
        "for servers outside your trust boundary, or whose credential is the "
        "agent's own rather than the user's.",
    )

    @property
    def has_api_key(self) -> bool:
        """Whether an API key is configured (``api_key`` or an ``x-api-key`` header)."""
        return bool(self.api_key) or any(k.lower() == "x-api-key" for k in (self.headers or {}))

    @property
    def forwards_caller_token(self) -> bool:
        """Whether tool calls send the invoking caller's bearer token here.

        ``forward_caller_token`` when it is set; otherwise ``True`` unless
        the server has an API key (:attr:`has_api_key`), which then wins
        over the caller's token.
        """
        if self.forward_caller_token is not None:
            return self.forward_caller_token
        return not self.has_api_key

    @field_serializer("headers")
    def _mask_credential_headers(
        self, headers: dict[str, str], info: FieldSerializationInfo
    ) -> dict[str, str]:
        return mask_headers(headers) if info.mode == "json" else dict(headers)

    def __repr_args__(self) -> Any:
        for name, value in super().__repr_args__():
            if name == "headers" and isinstance(value, Mapping):
                yield name, mask_headers(value)
            else:
                yield name, value

    @field_validator("auth")
    @classmethod
    def _warn_auth_ignored(cls, v: str | None) -> str | None:
        if v is not None:
            warnings.warn(
                "HTTPServerSpec.auth is ignored (it is not sent to the server) and will be "
                "removed. Use bearer_token= (sent as 'Authorization: Bearer <token>') or "
                "api_key= (sent as 'x-api-key: <key>').",
                FutureWarning,
                stacklevel=2,
            )
        return v


ServerSpec = StdioServerSpec | HTTPServerSpec
"""Union of supported server specifications."""


def servers_to_mcp_config(servers: Mapping[str, ServerSpec]) -> dict[str, dict[str, object]]:
    """Convert programmatic server specs to a configuration dict.

    Args:
        servers: Mapping of server name to specification.

    Returns:
        Dict with server configurations keyed by name.
    """
    cfg: dict[str, dict[str, object]] = {}
    for name, s in servers.items():
        if isinstance(s, StdioServerSpec):
            cfg[name] = {
                "transport": "stdio",
                "command": s.command,
                "args": s.args,
                "env": s.env,  # Always pass dict (empty dict is valid)
                "cwd": s.cwd,  # None is acceptable for optional fields
                "keep_alive": s.keep_alive,
            }
        else:
            entry: dict[str, object] = {
                "transport": s.transport,
                "url": s.url,
            }
            if s.headers:
                entry["headers"] = s.headers
            if s.auth is not None:
                entry["auth"] = s.auth
            if s.bearer_token is not None:
                entry["bearer_token"] = s.bearer_token.get_secret_value()
            if s.api_key is not None:
                entry["api_key"] = s.api_key.get_secret_value()
            cfg[name] = entry
    return cfg
