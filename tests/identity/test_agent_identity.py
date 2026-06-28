"""Unit tests for the :class:`AgentIdentity` public class."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from promptise.identity import (
    AgentIdentity,
    AwsEksProjectedProvider,
    CallableTokenProvider,
    CredentialAcquisitionError,
    EntraManagedIdentityProvider,
    GcpMetadataProvider,
    OidcCallableProvider,
    ProviderConfigError,
    SpiffeSdkProvider,
)

FAKE_JWT = "header.payload.sig"


def _jwt(claims: dict[str, Any]) -> str:
    h = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    p = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{h}.{p}."


def _verifiable() -> AgentIdentity:
    return AgentIdentity.from_oidc(
        "release-bot", issuer="https://gitlab.com", token_fn=lambda: FAKE_JWT
    )


def _verifiable_with(claims: dict[str, Any], *, agent_id: str | None = None) -> AgentIdentity:
    return AgentIdentity.from_oidc(agent_id, issuer="https://idp", token_fn=lambda: _jwt(claims))


# -- Local identity -------------------------------------------------------


def test_local_identity_fields() -> None:
    ident = AgentIdentity(
        "  billing-bot  ", name="Billing Bot", owner="payments", labels={"env": "prod"}
    )
    assert ident.agent_id == "billing-bot"  # stripped
    assert ident.name == "Billing Bot"
    assert ident.owner == "payments"
    assert ident.labels == {"env": "prod"}
    assert ident.is_verifiable is False
    assert ident.credential_provider is None
    assert ident.credential is None


def test_no_id_and_no_credential_raises() -> None:
    with pytest.raises(ProviderConfigError, match="either an agent_id or a credential"):
        AgentIdentity("   ")
    with pytest.raises(ProviderConfigError, match="either an agent_id or a credential"):
        AgentIdentity()


# -- Identity from the IdP (sub/oid) -------------------------------------


def test_subject_reads_sub_claim() -> None:
    ident = _verifiable_with({"sub": "spiffe://acme/billing-bot", "iss": "https://idp"})
    assert ident.agent_id is None  # no local handle passed
    assert ident.subject() == "spiffe://acme/billing-bot"
    assert ident.resolve_identifier() == "spiffe://acme/billing-bot"


def test_subject_falls_back_to_oid() -> None:
    ident = _verifiable_with({"oid": "entra-object-id", "appid": "app-1"})
    assert ident.subject() == "entra-object-id"


def test_idp_claims_subset() -> None:
    ident = _verifiable_with(
        {"sub": "s", "oid": "o", "iss": "i", "aud": "a", "azp": "z", "nonce": "x"}
    )
    assert ident.idp_claims() == {
        "sub": "s",
        "oid": "o",
        "iss": "i",
        "aud": "a",
        "azp": "z",
    }


def test_subject_without_sub_or_oid_raises() -> None:
    ident = _verifiable_with({"iss": "https://idp"})
    with pytest.raises(CredentialAcquisitionError, match="neither a 'sub' nor an 'oid'"):
        ident.subject()


def test_subject_on_local_identity_raises() -> None:
    ident = AgentIdentity("local-bot")
    with pytest.raises(ProviderConfigError, match="local identity with no"):
        ident.subject()
    with pytest.raises(ProviderConfigError):
        ident.idp_claims()


def test_explicit_agent_id_wins_over_subject() -> None:
    ident = _verifiable_with({"sub": "idp-subject"}, agent_id="friendly-name")
    assert ident.agent_id == "friendly-name"
    assert ident.resolve_identifier() == "friendly-name"  # explicit label wins
    assert ident.subject() == "idp-subject"  # IdP id still readable


def test_claims_structure() -> None:
    ident = AgentIdentity("bot", name="Bot", owner="team", labels={"v": "1"})
    claims = ident.claims()
    assert claims == {
        "agent_id": "bot",
        "verifiable": False,
        "name": "Bot",
        "owner": "team",
        "labels": {"v": "1"},
    }


def test_minimal_claims() -> None:
    assert AgentIdentity("bot").claims() == {"agent_id": "bot", "verifiable": False}


def test_claims_omit_agent_id_when_unset() -> None:
    # A verifiable identity with no local handle: claims carry no agent_id
    # (the authoritative id is the IdP subject, read via subject()).
    ident = _verifiable_with({"sub": "idp-subject"})
    claims = ident.claims()
    assert "agent_id" not in claims
    assert claims["verifiable"] is True


def test_local_get_credential_raises() -> None:
    ident = AgentIdentity("bot")
    with pytest.raises(ProviderConfigError, match="local identity with no"):
        ident.get_credential()
    with pytest.raises(ProviderConfigError):
        ident.auth_header()


def test_repr_is_identifier_only() -> None:
    ident = AgentIdentity("bot", name="Bot", owner="team")
    text = repr(ident)
    assert "bot" in text and "Bot" in text and "team" in text
    assert "verifiable=False" in text


# -- Verifiable identity --------------------------------------------------


def test_verifiable_identity() -> None:
    ident = _verifiable()
    assert ident.is_verifiable is True
    assert ident.credential_provider == "oidc:https://gitlab.com"
    assert isinstance(ident.credential, OidcCallableProvider)
    assert ident.get_credential() == FAKE_JWT
    assert ident.auth_header() == {"Authorization": f"Bearer {FAKE_JWT}"}


def test_verifiable_claims_include_provider() -> None:
    claims = _verifiable().claims()
    assert claims["verifiable"] is True
    assert claims["credential_provider"] == "oidc:https://gitlab.com"


def test_repr_never_contains_credential() -> None:
    text = repr(_verifiable())
    assert FAKE_JWT not in text
    assert "verifiable=True" in text


# -- Per-resource credentials (audience forwarding) -----------------------


def test_get_credential_forwards_audience() -> None:
    # An active provider re-mints per resource: the requested audience must
    # reach the backing provider so one identity can serve several resources.
    seen: list[str | None] = []

    def mint(audience: str | None = None) -> str:
        seen.append(audience)
        return _jwt({"aud": audience or "default"})

    ident = AgentIdentity("bot", credential=CallableTokenProvider(token_fn=mint))
    ident.get_credential("api://A")
    ident.get_credential("api://B")
    ident.get_credential()
    assert seen == ["api://A", "api://B", None]


def test_auth_header_forwards_audience() -> None:
    seen: list[str | None] = []

    def mint(audience: str | None = None) -> str:
        seen.append(audience)
        return _jwt({"aud": audience or "default"})

    ident = AgentIdentity("bot", credential=CallableTokenProvider(token_fn=mint))
    header = ident.auth_header("api://A")
    assert header["Authorization"].startswith("Bearer ")
    assert seen == ["api://A"]


def test_oidc_passive_ignores_audience() -> None:
    # OIDC file/env/callable identities have a fixed audience: the per-request
    # audience is accepted but ignored (the same token is returned).
    ident = _verifiable()  # from_oidc with a zero-arg token_fn
    assert ident.get_credential("api://A") == FAKE_JWT
    assert ident.get_credential() == FAKE_JWT


def test_local_get_credential_with_audience_raises() -> None:
    ident = AgentIdentity("bot")
    with pytest.raises(ProviderConfigError, match="local identity with no"):
        ident.get_credential("api://A")


# -- Factories wrap the right provider ------------------------------------


def test_from_entra_imds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
    ident = AgentIdentity.from_entra("bot", mode="imds")
    assert isinstance(ident.credential, EntraManagedIdentityProvider)


def test_from_aws_projected(tmp_path: Path) -> None:
    f = tmp_path / "t"
    f.write_text(FAKE_JWT, encoding="utf-8")
    ident = AgentIdentity.from_aws("bot", mode="projected", token_file=f)
    assert isinstance(ident.credential, AwsEksProjectedProvider)


def test_from_gcp() -> None:
    ident = AgentIdentity.from_gcp("bot", audience="api://m")
    assert isinstance(ident.credential, GcpMetadataProvider)
    assert ident.credential._audience == "api://m"


def test_from_spiffe_sdk() -> None:
    ident = AgentIdentity.from_spiffe("bot", mode="sdk")
    assert isinstance(ident.credential, SpiffeSdkProvider)


def test_factory_carries_metadata() -> None:
    ident = AgentIdentity.from_gcp("bot", name="Bot", owner="team", labels={"a": "b"})
    assert ident.agent_id == "bot"
    assert ident.name == "Bot"
    assert ident.owner == "team"
    assert ident.labels == {"a": "b"}
