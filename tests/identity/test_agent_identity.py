"""Unit tests for the :class:`AgentIdentity` public class.

Covers every factory classmethod (it wraps the right provider type),
attribute forwarding to the underlying provider, the token-minting
surface end-to-end through a mocked Anthropic exchange, the
upstream-JWT accessor, and the identifier-only repr (which must never
contain a token). No network access and no real credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from promptise.identity import (
    AgentIdentity,
    AwsEksProjectedProvider,
    AwsStsProvider,
    EntraManagedIdentityProvider,
    EntraProjectedTokenProvider,
    GcpMetadataProvider,
    IdentityProvider,
    OidcCallableProvider,
    SpiffeFileProvider,
    SpiffeSdkProvider,
)

FAKE_JWT: str = "header.payload.sig"

# Synthetic federation IDs — identifiers, not secrets (build plan 4.1).
_FED_KWARGS: dict[str, str] = {
    "federation_rule_id": "fdrl_test",
    "organization_id": "org_test",
    "service_account_id": "svac_test",
}


def _install_mock_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "sk-ant-oat01-mock", "expires_in": 3600},
        )

    transport = httpx.MockTransport(handler)

    def mocked_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", mocked_post)


def _oidc_identity(**overrides: Any) -> AgentIdentity:
    kwargs: dict[str, Any] = {
        "issuer": "https://example.com",
        "token_fn": lambda: FAKE_JWT,
        **_FED_KWARGS,
    }
    kwargs.update(overrides)
    return AgentIdentity.from_oidc(**kwargs)


# -- Factories wrap the right provider ------------------------------------


def test_from_entra_imds_wraps_managed_identity_provider() -> None:
    identity = AgentIdentity.from_entra(mode="imds", **_FED_KWARGS)
    assert isinstance(identity, AgentIdentity)
    assert isinstance(identity.provider, EntraManagedIdentityProvider)


def test_from_entra_projected_wraps_projected_provider(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    identity = AgentIdentity.from_entra(mode="projected", token_file=f, **_FED_KWARGS)
    assert isinstance(identity.provider, EntraProjectedTokenProvider)


def test_from_aws_sts_wraps_sts_provider() -> None:
    identity = AgentIdentity.from_aws(mode="sts", region="us-east-1", **_FED_KWARGS)
    assert isinstance(identity.provider, AwsStsProvider)


def test_from_aws_projected_wraps_eks_provider(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    identity = AgentIdentity.from_aws(mode="projected", token_file=f, **_FED_KWARGS)
    assert isinstance(identity.provider, AwsEksProjectedProvider)


def test_from_gcp_wraps_metadata_provider() -> None:
    identity = AgentIdentity.from_gcp(**_FED_KWARGS)
    assert isinstance(identity.provider, GcpMetadataProvider)


def test_from_spiffe_sdk_wraps_sdk_provider() -> None:
    identity = AgentIdentity.from_spiffe(mode="sdk", **_FED_KWARGS)
    assert isinstance(identity.provider, SpiffeSdkProvider)


def test_from_spiffe_file_wraps_file_provider(tmp_path: Path) -> None:
    f = tmp_path / "svid.jwt"
    f.write_text(FAKE_JWT, encoding="utf-8")
    identity = AgentIdentity.from_spiffe(token_file=f, **_FED_KWARGS)
    assert isinstance(identity.provider, SpiffeFileProvider)


def test_from_oidc_wraps_callable_provider() -> None:
    identity = _oidc_identity()
    assert isinstance(identity.provider, OidcCallableProvider)


def test_provider_property_returns_identity_provider() -> None:
    identity = _oidc_identity()
    assert isinstance(identity.provider, IdentityProvider)


# -- Per-factory option pass-through --------------------------------------


def test_from_gcp_passes_through_service_account_email() -> None:
    identity = AgentIdentity.from_gcp(
        service_account_email="agent@p.iam.gserviceaccount.com", **_FED_KWARGS
    )
    provider = identity.provider
    assert isinstance(provider, GcpMetadataProvider)
    assert provider._service_account_email == "agent@p.iam.gserviceaccount.com"


def test_from_spiffe_passes_through_socket_and_audience() -> None:
    identity = AgentIdentity.from_spiffe(
        mode="sdk",
        socket_path="unix:///tmp/custom.sock",
        audience="https://aud.example.com",
        **_FED_KWARGS,
    )
    provider = identity.provider
    assert isinstance(provider, SpiffeSdkProvider)
    assert provider._socket_path == "unix:///tmp/custom.sock"
    assert provider._audience == "https://aud.example.com"


def test_from_aws_passes_through_audience_and_algorithm() -> None:
    identity = AgentIdentity.from_aws(
        mode="sts",
        region="eu-west-1",
        audience="https://aud.example.com",
        signing_algorithm="ES256",
        **_FED_KWARGS,
    )
    provider = identity.provider
    assert isinstance(provider, AwsStsProvider)
    assert provider._audience == "https://aud.example.com"
    assert provider._signing_algorithm == "ES256"


# -- Attribute forwarding -------------------------------------------------


def test_identity_attributes_forward_to_provider() -> None:
    identity = _oidc_identity(workspace_id="wrkspc_x")
    assert identity.provider_name == "oidc:https://example.com"
    assert identity.federation_rule_id == "fdrl_test"
    assert identity.organization_id == "org_test"
    assert identity.service_account_id == "svac_test"
    assert identity.workspace_id == "wrkspc_x"


def test_workspace_id_is_none_when_unset() -> None:
    identity = _oidc_identity()
    assert identity.workspace_id is None


def test_get_upstream_jwt_forwards() -> None:
    identity = _oidc_identity()
    assert identity.get_upstream_jwt() == FAKE_JWT


# -- Token-minting surface end-to-end -------------------------------------


def test_get_token_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock_exchange(monkeypatch)
    identity = _oidc_identity()
    assert identity.get_token() == "sk-ant-oat01-mock"


def test_get_auth_header_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock_exchange(monkeypatch)
    identity = _oidc_identity()
    assert identity.get_auth_header() == {
        "Authorization": "Bearer sk-ant-oat01-mock"
    }


# -- repr -----------------------------------------------------------------


def test_repr_contains_identifiers_with_workspace() -> None:
    identity = _oidc_identity(workspace_id="wrkspc_x")
    text = repr(identity)
    assert "AgentIdentity(" in text
    assert "oidc:https://example.com" in text
    assert "svac_test" in text
    assert "wrkspc_x" in text


def test_repr_omits_workspace_when_unset() -> None:
    identity = _oidc_identity()
    text = repr(identity)
    assert "workspace_id" not in text


def test_repr_never_contains_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repr must never leak a credential, even after a token has been
    minted and cached (build plan: never log/print a token)."""
    _install_mock_exchange(monkeypatch)
    identity = _oidc_identity()
    identity.get_token()  # populate the cache
    text = repr(identity)
    assert "sk-ant-oat01-mock" not in text
    assert FAKE_JWT not in text
