"""Unit tests for the Microsoft Entra ID credential provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from promptise.identity import (
    CredentialAcquisitionError,
    EntraManagedIdentityProvider,
    EntraProjectedTokenProvider,
    ProviderConfigError,
)
from promptise.identity.providers.entra import (
    ENV_AZURE_FEDERATED_TOKEN_FILE,
    from_entra,
)

FAKE_JWT = "header.payload.sig"


def _mock_get(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
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


# -- IMDS -----------------------------------------------------------------


def test_imds_sends_metadata_header_and_returns_id_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Metadata") == "true"
        assert request.url.params["resource"] == "api://my-mcp"
        return httpx.Response(200, json={"id_token": FAKE_JWT})

    _mock_get(monkeypatch, handler)
    provider = EntraManagedIdentityProvider(resource="api://my-mcp")
    assert provider.provider_name == "entra-imds"
    assert provider._acquire_upstream_jwt() == FAKE_JWT


def test_imds_includes_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["client_id"] == "mi-123"
        return httpx.Response(200, json={"id_token": FAKE_JWT})

    _mock_get(monkeypatch, handler)
    provider = EntraManagedIdentityProvider(client_id="mi-123")
    provider._acquire_upstream_jwt()


def test_imds_unreachable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    _mock_get(monkeypatch, handler)
    provider = EntraManagedIdentityProvider()
    with pytest.raises(CredentialAcquisitionError, match="could not reach the Azure"):
        provider._acquire_upstream_jwt()


def test_imds_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    _mock_get(monkeypatch, handler)
    provider = EntraManagedIdentityProvider()
    with pytest.raises(CredentialAcquisitionError, match="timed out"):
        provider._acquire_upstream_jwt()


def test_imds_non_200_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="no identity")

    _mock_get(monkeypatch, handler)
    provider = EntraManagedIdentityProvider()
    with pytest.raises(CredentialAcquisitionError, match="HTTP 400"):
        provider._acquire_upstream_jwt()


def test_imds_non_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    _mock_get(monkeypatch, handler)
    provider = EntraManagedIdentityProvider()
    with pytest.raises(CredentialAcquisitionError, match="non-JSON"):
        provider._acquire_upstream_jwt()


def test_imds_missing_id_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "only-access"})

    _mock_get(monkeypatch, handler)
    provider = EntraManagedIdentityProvider()
    with pytest.raises(CredentialAcquisitionError, match="id_token"):
        provider._acquire_upstream_jwt()


# -- Projected ------------------------------------------------------------


def test_projected_reads_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    monkeypatch.setenv(ENV_AZURE_FEDERATED_TOKEN_FILE, str(f))
    provider = EntraProjectedTokenProvider()
    assert provider.provider_name == "entra-projected"
    assert provider._acquire_upstream_jwt() == FAKE_JWT


def test_projected_explicit_override(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    provider = EntraProjectedTokenProvider(token_file=f)
    assert provider.token_file == f


# -- Factory --------------------------------------------------------------


def test_from_entra_auto_picks_projected_when_env_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    monkeypatch.setenv(ENV_AZURE_FEDERATED_TOKEN_FILE, str(f))
    assert isinstance(from_entra(), EntraProjectedTokenProvider)


def test_from_entra_auto_picks_imds_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_AZURE_FEDERATED_TOKEN_FILE, raising=False)
    assert isinstance(from_entra(), EntraManagedIdentityProvider)


def test_from_entra_explicit_modes() -> None:
    assert isinstance(from_entra(mode="imds"), EntraManagedIdentityProvider)
    assert isinstance(from_entra(mode="projected"), EntraProjectedTokenProvider)


def test_from_entra_unknown_mode_raises() -> None:
    with pytest.raises(ProviderConfigError, match="Unknown Entra mode"):
        from_entra(mode="bogus")  # type: ignore[arg-type]
