"""Unit tests for the Google Cloud credential provider.

The metadata identity endpoint returns the JWT as a plain string, so the
provider must use ``response.text`` and never ``.json()``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from promptise.identity import CredentialAcquisitionError, GcpMetadataProvider
from promptise.identity.providers.gcp import from_gcp

FAKE_JWT = "header.payload.sig"


def _mock_get(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    transport = httpx.MockTransport(handler)

    def mocked_get(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=transport) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr(httpx, "get", mocked_get)


def test_sends_flavor_header_and_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Metadata-Flavor") == "Google"
        assert request.url.params["audience"] == "api://my-mcp"
        assert "service-accounts/default/identity" in str(request.url)
        return httpx.Response(200, text=FAKE_JWT)

    _mock_get(monkeypatch, handler)
    provider = GcpMetadataProvider(audience="api://my-mcp")
    assert provider.provider_name == "gcp-metadata"
    assert provider._acquire_upstream_jwt() == FAKE_JWT


def test_body_returned_verbatim_not_json_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="aaa.bbb.ccc")

    _mock_get(monkeypatch, handler)
    assert GcpMetadataProvider()._acquire_upstream_jwt() == "aaa.bbb.ccc"


def test_custom_service_account_changes_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "service-accounts/agent@p.iam.gserviceaccount.com/identity" in str(request.url)
        return httpx.Response(200, text=FAKE_JWT)

    _mock_get(monkeypatch, handler)
    GcpMetadataProvider(
        service_account_email="agent@p.iam.gserviceaccount.com"
    )._acquire_upstream_jwt()


def test_unreachable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    _mock_get(monkeypatch, handler)
    with pytest.raises(CredentialAcquisitionError, match="could not reach the Google"):
        GcpMetadataProvider()._acquire_upstream_jwt()


def test_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    _mock_get(monkeypatch, handler)
    with pytest.raises(CredentialAcquisitionError, match="timed out"):
        GcpMetadataProvider()._acquire_upstream_jwt()


def test_non_200_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    _mock_get(monkeypatch, handler)
    with pytest.raises(CredentialAcquisitionError, match="HTTP 404"):
        GcpMetadataProvider()._acquire_upstream_jwt()


def test_empty_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="   \n")

    _mock_get(monkeypatch, handler)
    with pytest.raises(CredentialAcquisitionError, match="empty body"):
        GcpMetadataProvider()._acquire_upstream_jwt()


def test_audience_override_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # A per-request audience overrides the provider default (per-resource).
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["audience"] == "api://override"
        return httpx.Response(200, text=FAKE_JWT)

    _mock_get(monkeypatch, handler)
    provider = GcpMetadataProvider(audience="api://default")
    assert provider._acquire_upstream_jwt("api://override") == FAKE_JWT


def test_from_gcp_passes_options() -> None:
    provider = from_gcp(audience="api://aud", service_account_email="x@y.iam.gserviceaccount.com")
    assert isinstance(provider, GcpMetadataProvider)
    assert provider._audience == "api://aud"
    assert provider._service_account_email == "x@y.iam.gserviceaccount.com"
