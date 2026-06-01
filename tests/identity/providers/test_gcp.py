"""Unit tests for the Google Cloud provider and ``from_gcp``.

Mocks the metadata-server HTTP GET and the Anthropic exchange POST.
A dedicated test proves the response body is used verbatim and never
JSON-parsed (the key behaviour from section 5.9). No network access
and no real credentials.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from promptise.identity import (
    GcpMetadataProvider,
    ProviderConfigError,
    TokenAcquisitionError,
)
from promptise.identity.providers.gcp import from_gcp

FAKE_JWT: str = "header.payload.sig"

_FED_KWARGS: dict[str, str] = {
    "federation_rule_id": "fdrl_test",
    "organization_id": "org_test",
    "service_account_id": "svac_test",
}


def _mock_metadata(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> list[httpx.Request]:
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


# -- Metadata path --------------------------------------------------------


def test_sends_metadata_flavor_header_and_audience_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Metadata-Flavor") == "Google"
        assert request.url.params["audience"] == "https://api.anthropic.com"
        assert "service-accounts/default/identity" in str(request.url)
        return httpx.Response(200, text=FAKE_JWT)

    captured = _mock_metadata(monkeypatch, handler)
    provider = GcpMetadataProvider(**_FED_KWARGS)
    assert provider._acquire_upstream_jwt() == FAKE_JWT
    assert provider.provider_name == "gcp-metadata"
    assert len(captured) == 1


def test_body_returned_verbatim_not_json_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 5.9: the metadata identity endpoint returns the JWT as a
    plain string. The provider must use response.text directly and never
    call .json(). Here the body is a bare JWT — if the code tried to
    JSON-parse it the call would fail; instead it is returned verbatim."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="aaa.bbb.ccc")

    _mock_metadata(monkeypatch, handler)
    provider = GcpMetadataProvider(**_FED_KWARGS)
    assert provider._acquire_upstream_jwt() == "aaa.bbb.ccc"


def test_body_whitespace_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"  {FAKE_JWT}\n")

    _mock_metadata(monkeypatch, handler)
    provider = GcpMetadataProvider(**_FED_KWARGS)
    assert provider._acquire_upstream_jwt() == FAKE_JWT


def test_custom_service_account_email_changes_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "service-accounts/agent@project.iam.gserviceaccount.com/identity" in str(
            request.url
        )
        return httpx.Response(200, text=FAKE_JWT)

    _mock_metadata(monkeypatch, handler)
    provider = GcpMetadataProvider(
        service_account_email="agent@project.iam.gserviceaccount.com",
        **_FED_KWARGS,
    )
    assert provider._acquire_upstream_jwt() == FAKE_JWT


def test_custom_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["audience"] == "https://custom.example.com"
        return httpx.Response(200, text=FAKE_JWT)

    _mock_metadata(monkeypatch, handler)
    provider = GcpMetadataProvider(audience="https://custom.example.com", **_FED_KWARGS)
    provider._acquire_upstream_jwt()


def test_metadata_unreachable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    _mock_metadata(monkeypatch, handler)
    provider = GcpMetadataProvider(**_FED_KWARGS)
    with pytest.raises(TokenAcquisitionError, match="could not reach the Google"):
        provider._acquire_upstream_jwt()


def test_metadata_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    _mock_metadata(monkeypatch, handler)
    provider = GcpMetadataProvider(**_FED_KWARGS)
    with pytest.raises(TokenAcquisitionError, match="timed out"):
        provider._acquire_upstream_jwt()


def test_metadata_non_200_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="service account not found")

    _mock_metadata(monkeypatch, handler)
    provider = GcpMetadataProvider(**_FED_KWARGS)
    with pytest.raises(TokenAcquisitionError, match="HTTP 404"):
        provider._acquire_upstream_jwt()


def test_empty_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="   \n")

    _mock_metadata(monkeypatch, handler)
    provider = GcpMetadataProvider(**_FED_KWARGS)
    with pytest.raises(TokenAcquisitionError, match="empty body"):
        provider._acquire_upstream_jwt()


# -- Factory --------------------------------------------------------------


def test_from_gcp_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    def metadata_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=FAKE_JWT)

    _mock_metadata(monkeypatch, metadata_handler)
    _mock_exchange(monkeypatch)
    provider = from_gcp(**_FED_KWARGS)
    assert isinstance(provider, GcpMetadataProvider)
    assert provider.get_token() == "sk-ant-oat01-mock"


def test_from_gcp_passes_through_options(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = from_gcp(
        audience="https://aud.example.com",
        service_account_email="x@y.iam.gserviceaccount.com",
        workspace_id="wrkspc_explicit",
        **_FED_KWARGS,
    )
    assert provider._audience == "https://aud.example.com"
    assert provider._service_account_email == "x@y.iam.gserviceaccount.com"
    assert provider.workspace_id == "wrkspc_explicit"


def test_from_gcp_missing_federation_id_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_FEDERATION_RULE_ID", raising=False)
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org_env")
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_env")
    with pytest.raises(ProviderConfigError, match="ANTHROPIC_FEDERATION_RULE_ID"):
        from_gcp()
