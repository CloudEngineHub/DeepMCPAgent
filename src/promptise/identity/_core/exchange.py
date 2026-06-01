"""RFC 7523 JWT-bearer exchange against Anthropic's OAuth endpoint.

This is the **only** module that knows the Anthropic OAuth URL and the
RFC 7523 grant-type string. If Anthropic ever changes the protocol —
new URL, new grant type, new response schema — this file is the only
one that needs to be edited (build plan section 5.6).

The exchange flow:

1. The caller (an :class:`~promptise.identity.IdentityProvider`)
   already holds a fresh upstream JWT obtained from the cloud
   platform's metadata service, STS endpoint, or token file.
2. :func:`exchange_jwt_for_anthropic_token` POSTs that JWT to
   ``https://api.anthropic.com/v1/oauth/token`` using the grant type
   ``urn:ietf:params:oauth:grant-type:jwt-bearer``.
3. Anthropic returns a short-lived bearer token whose value starts
   with ``sk-ant-oat01-``.
4. The function returns a :class:`MintedToken` whose monotonic-clock
   expiry the caller stores in its cache.

The function uses :mod:`httpx` for the HTTP transport. ``httpx`` is
already pulled in transitively by the Anthropic SDK, the OpenAI SDK,
MCP, LangGraph, and roughly fifteen other packages Promptise depends
on, so this is not a new top-level dependency (build plan section
4.10).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .._internal.logging import logger
from .cache import MintedToken
from .errors import TokenAcquisitionError, TokenExchangeError

#: Anthropic's OAuth token endpoint. Hard-coded — overrides are not
#: part of the v1.0 public API.
_ANTHROPIC_OAUTH_TOKEN_ENDPOINT: str = "https://api.anthropic.com/v1/oauth/token"

#: The RFC 7523 grant type the endpoint expects.
_RFC_7523_GRANT_TYPE: str = "urn:ietf:params:oauth:grant-type:jwt-bearer"

#: Every workload-issued Anthropic access token must start with this
#: prefix. A response that does not is treated as a protocol change
#: and rejected — the framework refuses to handle an unrecognised
#: credential format.
_EXPECTED_ACCESS_TOKEN_PREFIX: str = "sk-ant-oat01-"


def exchange_jwt_for_anthropic_token(
    upstream_jwt: str,
    *,
    federation_rule_id: str,
    organization_id: str,
    service_account_id: str,
    workspace_id: str | None,
    timeout: float = 10.0,
    provider_name: str = "identity",
) -> MintedToken:
    """Exchange an upstream JWT for a short-lived Anthropic access token.

    Args:
        upstream_jwt: The OIDC JWT obtained from the upstream identity
            provider. The token's ``iss`` claim must match the
            federation issuer URL registered in the Anthropic Console.
        federation_rule_id: The ``fdrl_*`` identifier registered in the
            Anthropic Console for this workload's federation.
        organization_id: The Anthropic organization UUID.
        service_account_id: The ``svac_*`` identifier of the workload's
            service account.
        workspace_id: Optional ``wrkspc_*`` identifier. When supplied
            the minted token is scoped to a single workspace; when
            omitted the token covers every workspace the service
            account is authorised for.
        timeout: Maximum seconds to wait for the exchange. Default
            ten seconds — short enough to surface degraded networks
            quickly, long enough to tolerate normal TLS handshakes
            against ``api.anthropic.com``.
        provider_name: Free-form short identifier used in log records
            and error messages so operators can tell which provider
            triggered an exchange.

    Returns:
        A :class:`MintedToken` whose ``expires_at_monotonic`` field is
        derived from a :func:`time.monotonic` snapshot taken **before**
        the HTTP request. This is intentionally conservative — the
        token will be considered "expiring" slightly sooner than its
        nominal lifetime, which is safer than the opposite mistake.

    Raises:
        TokenAcquisitionError: When the request times out or the
            transport fails before a response is received.
        TokenExchangeError: When Anthropic returns a non-2xx status,
            a body without ``access_token``, an ``expires_in`` value
            that is not an integer, or an access token that does not
            start with the expected prefix.
    """
    payload: dict[str, Any] = {
        "grant_type": _RFC_7523_GRANT_TYPE,
        "assertion": upstream_jwt,
        "federation_rule_id": federation_rule_id,
        "organization_id": organization_id,
        "service_account_id": service_account_id,
    }
    if workspace_id is not None:
        payload["workspace_id"] = workspace_id

    minted_at_monotonic = time.monotonic()
    try:
        response = httpx.post(
            _ANTHROPIC_OAUTH_TOKEN_ENDPOINT,
            json=payload,
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise TokenAcquisitionError(
            f"[{provider_name}] JWT-bearer exchange to "
            f"{_ANTHROPIC_OAUTH_TOKEN_ENDPOINT} timed out after {timeout}s. "
            f"Most common cause: outbound network access from this workload "
            f"to api.anthropic.com is blocked. Verify the egress policy."
        ) from exc
    except httpx.HTTPError as exc:
        raise TokenAcquisitionError(
            f"[{provider_name}] JWT-bearer exchange to "
            f"{_ANTHROPIC_OAUTH_TOKEN_ENDPOINT} failed before a response was "
            f"received ({type(exc).__name__}). Most common cause: TLS "
            f"handshake or DNS failure. Underlying error: {exc}"
        ) from exc

    if response.status_code != 200:
        raise TokenExchangeError(
            f"[{provider_name}] Anthropic rejected the JWT-bearer exchange "
            f"(HTTP {response.status_code}). Most common cause: the JWT's "
            f"'iss' claim does not match the federation issuer URL "
            f"registered in the Anthropic Console. Response body: "
            f"{response.text}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise TokenExchangeError(
            f"[{provider_name}] Anthropic returned a non-JSON body from "
            f"{_ANTHROPIC_OAUTH_TOKEN_ENDPOINT} (HTTP "
            f"{response.status_code}). Body preview: {response.text[:200]!r}"
        ) from exc

    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise TokenExchangeError(
            f"[{provider_name}] Exchange response is missing the "
            f"'access_token' field. Most common cause: Anthropic changed "
            f"the OAuth response schema. Body keys: {list(body.keys())}"
        )
    if not access_token.startswith(_EXPECTED_ACCESS_TOKEN_PREFIX):
        raise TokenExchangeError(
            f"[{provider_name}] Anthropic access token does not start with "
            f"{_EXPECTED_ACCESS_TOKEN_PREFIX!r}. Most common cause: "
            f"Anthropic has changed the access-token format. If you are "
            f"seeing this, the framework needs an update."
        )

    expires_in_raw = body.get("expires_in", 3600)
    try:
        expires_in_seconds = int(expires_in_raw)
    except (TypeError, ValueError) as exc:
        raise TokenExchangeError(
            f"[{provider_name}] Exchange response 'expires_in' is not an "
            f"integer: {expires_in_raw!r}. Most common cause: Anthropic "
            f"changed the response schema."
        ) from exc

    token_type_raw = body.get("token_type", "Bearer")
    token_type = token_type_raw if isinstance(token_type_raw, str) else "Bearer"

    minted = MintedToken(
        access_token=access_token,
        token_type=token_type,
        expires_at_monotonic=minted_at_monotonic + expires_in_seconds,
        expires_in_seconds=expires_in_seconds,
    )

    scope = body.get("scope")
    if isinstance(scope, str):
        logger.info(
            "Anthropic exchange ok provider=%s scope=%s expires_in=%d",
            provider_name,
            scope,
            expires_in_seconds,
        )
    else:
        logger.info(
            "Anthropic exchange ok provider=%s expires_in=%d",
            provider_name,
            expires_in_seconds,
        )
    return minted
