"""Live-cloud smoke tests for the identity providers — the real round-trip.

These mint an ACTUAL token from the real platform (Azure IMDS, GCP metadata,
AWS STS, a SPIRE Workload API socket, or a live OIDC issuer). They are:

  * marked ``@pytest.mark.integration`` → deselected by default
    (``addopts = -m 'not integration'``), and
  * each ``skipif``-gated on an explicit opt-in env var, so they only run when
    an operator intentionally runs them *inside* the target platform.

Run one in the matching environment, e.g. on an Azure VM with a managed
identity::

    PROMPTISE_IT_ENTRA=1 pytest tests/identity/integration -m integration -v

This is how you confirm a provider works against the real cloud before you
depend on it — the offline suite proves the logic, this proves the live path.
"""

from __future__ import annotations

import os

import jwt
import pytest

from promptise import AgentIdentity

pytestmark = pytest.mark.integration


def _assert_is_jwt(token: str, *, expected_audience: str | None = None) -> dict:
    """A minted credential must be a non-empty, decodable JWT."""
    assert isinstance(token, str) and token.count(".") == 2, "not a JWT-shaped token"
    claims = jwt.decode(token, options={"verify_signature": False})  # smoke: inspect only
    assert claims.get("exp"), "token has no exp claim"
    if expected_audience is not None:
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        assert expected_audience in auds, f"audience {expected_audience!r} not in {auds}"
    return claims


@pytest.mark.skipif(
    not os.environ.get("PROMPTISE_IT_ENTRA"), reason="set PROMPTISE_IT_ENTRA=1 on Azure"
)
def test_entra_live() -> None:
    resource = os.environ.get("PROMPTISE_IT_ENTRA_RESOURCE", "api://promptise-agent")
    identity = AgentIdentity.from_entra("it-bot", resource=resource)
    _assert_is_jwt(identity.get_credential(), expected_audience=resource)


@pytest.mark.skipif(
    not os.environ.get("PROMPTISE_IT_AWS"), reason="set PROMPTISE_IT_AWS=1 with an IAM role"
)
def test_aws_live() -> None:
    audience = os.environ.get("PROMPTISE_IT_AWS_AUDIENCE", "api://promptise-agent")
    identity = AgentIdentity.from_aws("it-bot", audience=audience)
    _assert_is_jwt(identity.get_credential(), expected_audience=audience)


@pytest.mark.skipif(
    not os.environ.get("PROMPTISE_IT_GCP"), reason="set PROMPTISE_IT_GCP=1 on GCE/GKE/Cloud Run"
)
def test_gcp_live() -> None:
    audience = os.environ.get("PROMPTISE_IT_GCP_AUDIENCE", "api://promptise-agent")
    identity = AgentIdentity.from_gcp("it-bot", audience=audience)
    _assert_is_jwt(identity.get_credential(), expected_audience=audience)


@pytest.mark.skipif(
    not os.environ.get("SPIFFE_ENDPOINT_SOCKET"), reason="set SPIFFE_ENDPOINT_SOCKET (SPIRE)"
)
def test_spiffe_live() -> None:
    audience = os.environ.get("PROMPTISE_IT_SPIFFE_AUDIENCE", "spiffe://promptise/mcp")
    identity = AgentIdentity.from_spiffe("it-bot", audience=audience)
    _assert_is_jwt(identity.get_credential(), expected_audience=audience)


@pytest.mark.skipif(
    not (
        os.environ.get("PROMPTISE_IT_OIDC_ISSUER") and os.environ.get("PROMPTISE_IT_OIDC_TOKEN_ENV")
    ),
    reason="set PROMPTISE_IT_OIDC_ISSUER + PROMPTISE_IT_OIDC_TOKEN_ENV (e.g. ACTIONS_ID_TOKEN...)",
)
def test_oidc_live() -> None:
    issuer = os.environ["PROMPTISE_IT_OIDC_ISSUER"]
    token_env = os.environ["PROMPTISE_IT_OIDC_TOKEN_ENV"]
    identity = AgentIdentity.from_oidc("it-bot", issuer=issuer, token_env_var=token_env)
    _assert_is_jwt(identity.get_credential())
