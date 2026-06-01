"""Unit tests for the Microsoft Entra ID provider and ``from_entra``.

Mocks the IMDS HTTP GET and the Anthropic exchange POST with
:class:`httpx.MockTransport`. The projected-token path uses a real
temporary file. No network access and no real credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from promptise.identity import (
    EntraManagedIdentityProvider,
    EntraProjectedTokenProvider,
    ProviderConfigError,
    TokenAcquisitionError,
)
from promptise.identity.providers.entra import (
    ENV_AZURE_FEDERATED_TOKEN_FILE,
    from_entra,
)

FAKE_JWT: str = "header.payload.sig"

_FED_KWARGS: dict[str, str] = {
    "federation_rule_id": "fdrl_test",
    "organization_id": "org_test",
    "service_account_id": "svac_test",
}


def _mock_imds(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> list[httpx.Request]:
    """Replace httpx.get with a mock transport for the IMDS endpoint."""
    captured: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def mocked_get(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=transport) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr(httpx, "get", mocked_get)
    return captured


def _mock_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace httpx.post so the Anthropic exchange returns a mock token."""

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


# -- IMDS path ------------------------------------------------------------


def test_imds_extracts_id_token_and_sends_metadata_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # IMDS requires the Metadata: true header.
        assert request.headers.get("Metadata") == "true"
        assert "api-version" in request.url.params
        assert request.url.params["resource"] == "https://api.anthropic.com"
        return httpx.Response(200, json={"id_token": FAKE_JWT})

    captured = _mock_imds(monkeypatch, handler)
    provider = EntraManagedIdentityProvider(**_FED_KWARGS)
    assert provider._acquire_upstream_jwt() == FAKE_JWT
    assert len(captured) == 1
    assert provider.provider_name == "entra-imds"


def test_imds_client_id_added_when_user_assigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["client_id"] == "00000000-1111-2222-3333-444444444444"
        return httpx.Response(200, json={"id_token": FAKE_JWT})

    _mock_imds(monkeypatch, handler)
    provider = EntraManagedIdentityProvider(
        client_id="00000000-1111-2222-3333-444444444444",
        **_FED_KWARGS,
    )
    assert provider._acquire_upstream_jwt() == FAKE_JWT


def test_imds_unreachable_raises_token_acquisition_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    _mock_imds(monkeypatch, handler)
    provider = EntraManagedIdentityProvider(**_FED_KWARGS)
    with pytest.raises(TokenAcquisitionError, match="could not reach the Azure"):
        provider._acquire_upstream_jwt()


def test_imds_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    _mock_imds(monkeypatch, handler)
    provider = EntraManagedIdentityProvider(**_FED_KWARGS)
    with pytest.raises(TokenAcquisitionError, match="timed out"):
        provider._acquire_upstream_jwt()


def test_imds_non_200_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="no identity assigned")

    _mock_imds(monkeypatch, handler)
    provider = EntraManagedIdentityProvider(**_FED_KWARGS)
    with pytest.raises(TokenAcquisitionError, match="HTTP 400"):
        provider._acquire_upstream_jwt()


def test_imds_non_json_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    _mock_imds(monkeypatch, handler)
    provider = EntraManagedIdentityProvider(**_FED_KWARGS)
    with pytest.raises(TokenAcquisitionError, match="non-JSON body"):
        provider._acquire_upstream_jwt()


def test_imds_response_without_id_token_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # IMDS returns an access_token but no id_token — the federation
        # flow needs the id_token.
        return httpx.Response(200, json={"access_token": "some-access-token"})

    _mock_imds(monkeypatch, handler)
    provider = EntraManagedIdentityProvider(**_FED_KWARGS)
    with pytest.raises(TokenAcquisitionError, match="did not contain an 'id_token'"):
        provider._acquire_upstream_jwt()


# -- Projected-token path -------------------------------------------------


def test_projected_reads_default_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "azure-identity-token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    monkeypatch.setenv(ENV_AZURE_FEDERATED_TOKEN_FILE, str(f))
    provider = EntraProjectedTokenProvider(**_FED_KWARGS)
    assert provider.token_file == f
    assert provider._acquire_upstream_jwt() == FAKE_JWT
    assert provider.provider_name == "entra-projected"


def test_projected_explicit_token_file_overrides_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "from-env"
    env_file.write_text("env.jwt.value", encoding="utf-8")
    explicit = tmp_path / "explicit"
    explicit.write_text("explicit.jwt.value", encoding="utf-8")
    monkeypatch.setenv(ENV_AZURE_FEDERATED_TOKEN_FILE, str(env_file))
    provider = EntraProjectedTokenProvider(token_file=explicit, **_FED_KWARGS)
    assert provider.token_file == explicit
    assert provider._acquire_upstream_jwt() == "explicit.jwt.value"


def test_projected_falls_back_to_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_AZURE_FEDERATED_TOKEN_FILE, raising=False)
    provider = EntraProjectedTokenProvider(**_FED_KWARGS)
    # The default path won't exist in the test environment; we only
    # assert it was selected, not that it reads.
    assert str(provider.token_file).endswith("azure-identity-token")


# -- Factory + mode handling ----------------------------------------------


def test_from_entra_imds_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    def imds_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id_token": FAKE_JWT})

    _mock_imds(monkeypatch, imds_handler)
    _mock_exchange(monkeypatch)
    provider = from_entra(mode="imds", **_FED_KWARGS)
    assert isinstance(provider, EntraManagedIdentityProvider)
    assert provider.get_token() == "sk-ant-oat01-mock"


def test_from_entra_projected_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    _mock_exchange(monkeypatch)
    provider = from_entra(mode="projected", token_file=f, **_FED_KWARGS)
    assert isinstance(provider, EntraProjectedTokenProvider)
    assert provider.get_token() == "sk-ant-oat01-mock"


def test_from_entra_auto_picks_projected_when_env_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    monkeypatch.setenv(ENV_AZURE_FEDERATED_TOKEN_FILE, str(f))
    provider = from_entra(mode="auto", **_FED_KWARGS)
    assert isinstance(provider, EntraProjectedTokenProvider)


def test_from_entra_auto_picks_imds_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_AZURE_FEDERATED_TOKEN_FILE, raising=False)
    provider = from_entra(mode="auto", **_FED_KWARGS)
    assert isinstance(provider, EntraManagedIdentityProvider)


def test_from_entra_unknown_mode_raises() -> None:
    with pytest.raises(ProviderConfigError, match="Unknown Entra mode"):
        from_entra(mode="bogus", **_FED_KWARGS)  # type: ignore[arg-type]


def test_from_entra_missing_federation_id_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_FEDERATION_RULE_ID", raising=False)
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org_env")
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_env")
    monkeypatch.delenv(ENV_AZURE_FEDERATED_TOKEN_FILE, raising=False)
    with pytest.raises(ProviderConfigError, match="ANTHROPIC_FEDERATION_RULE_ID"):
        from_entra(mode="imds")
