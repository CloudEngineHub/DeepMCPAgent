"""Unit tests for the RFC 7523 JWT-bearer exchange.

Mocks :func:`httpx.post` with an :class:`httpx.MockTransport` so the
tests run with no network access and no real credentials. Every
fixture below produces synthetic data — the ``access_token`` returned
by the mock always starts with ``sk-ant-oat01-`` followed by literal
text like ``mock`` so it is unmistakeable as a non-credential.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from promptise.identity import (
    MintedToken,
    TokenAcquisitionError,
    TokenExchangeError,
)
from promptise.identity._core import exchange as exchange_mod

# Synthetic JWT — three dot-separated literals so it lexes as JWT-shaped
# but contains nothing exchangeable. The signature is the literal word
# 'sig', not a real signature.
FAKE_JWT: str = "header.payload.sig"


def _install_mock_post(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Replace :func:`httpx.post` with one that uses ``handler``.

    Returns a list of every request the handler observed so tests can
    assert on payload contents.
    """
    captured: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def mocked_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", mocked_post)
    return captured


def test_payload_matches_rfc_7523_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "sk-ant-oat01-mock",
                "expires_in": 1800,
                "token_type": "Bearer",
            },
        )

    captured = _install_mock_post(monkeypatch, handler)

    minted = exchange_mod.exchange_jwt_for_anthropic_token(
        FAKE_JWT,
        federation_rule_id="fdrl_test",
        organization_id="org_test",
        service_account_id="svac_test",
        workspace_id=None,
        provider_name="unit",
    )

    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://api.anthropic.com/v1/oauth/token"
    body = json.loads(request.content)
    assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert body["assertion"] == FAKE_JWT
    assert body["federation_rule_id"] == "fdrl_test"
    assert body["organization_id"] == "org_test"
    assert body["service_account_id"] == "svac_test"
    # workspace_id omitted from payload, not sent as null.
    assert "workspace_id" not in body

    assert isinstance(minted, MintedToken)
    assert minted.access_token == "sk-ant-oat01-mock"
    assert minted.expires_in_seconds == 1800
    assert minted.token_type == "Bearer"


def test_workspace_id_included_when_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "sk-ant-oat01-mock", "expires_in": 3600},
        )

    captured = _install_mock_post(monkeypatch, handler)
    exchange_mod.exchange_jwt_for_anthropic_token(
        FAKE_JWT,
        federation_rule_id="fdrl_test",
        organization_id="org_test",
        service_account_id="svac_test",
        workspace_id="wrkspc_test",
        provider_name="unit",
    )
    body = json.loads(captured[0].content)
    assert body["workspace_id"] == "wrkspc_test"


def test_http_400_raises_token_exchange_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid issuer claim")

    _install_mock_post(monkeypatch, handler)
    with pytest.raises(TokenExchangeError, match="HTTP 400"):
        exchange_mod.exchange_jwt_for_anthropic_token(
            FAKE_JWT,
            federation_rule_id="fdrl_test",
            organization_id="org_test",
            service_account_id="svac_test",
            workspace_id=None,
            provider_name="unit",
        )


def test_missing_access_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"expires_in": 3600})

    _install_mock_post(monkeypatch, handler)
    with pytest.raises(TokenExchangeError, match="missing the 'access_token'"):
        exchange_mod.exchange_jwt_for_anthropic_token(
            FAKE_JWT,
            federation_rule_id="fdrl_test",
            organization_id="org_test",
            service_account_id="svac_test",
            workspace_id=None,
            provider_name="unit",
        )


def test_wrong_access_token_prefix_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # An access-token-shaped string with the wrong prefix; this
        # would indicate Anthropic changed the protocol.
        return httpx.Response(
            200,
            json={"access_token": "sk-ant-api03-wrongkind", "expires_in": 3600},
        )

    _install_mock_post(monkeypatch, handler)
    with pytest.raises(TokenExchangeError, match="does not start with"):
        exchange_mod.exchange_jwt_for_anthropic_token(
            FAKE_JWT,
            federation_rule_id="fdrl_test",
            organization_id="org_test",
            service_account_id="svac_test",
            workspace_id=None,
            provider_name="unit",
        )


def test_non_integer_expires_in_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "sk-ant-oat01-mock", "expires_in": "not-a-number"},
        )

    _install_mock_post(monkeypatch, handler)
    with pytest.raises(TokenExchangeError, match="'expires_in' is not an integer"):
        exchange_mod.exchange_jwt_for_anthropic_token(
            FAKE_JWT,
            federation_rule_id="fdrl_test",
            organization_id="org_test",
            service_account_id="svac_test",
            workspace_id=None,
            provider_name="unit",
        )


def test_non_json_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # HTTP 200 but the body is not JSON — e.g. an upstream proxy
        # returned an HTML error page with a 200 status.
        return httpx.Response(200, text="<html>not json</html>")

    _install_mock_post(monkeypatch, handler)
    with pytest.raises(TokenExchangeError, match="non-JSON body"):
        exchange_mod.exchange_jwt_for_anthropic_token(
            FAKE_JWT,
            federation_rule_id="fdrl_test",
            organization_id="org_test",
            service_account_id="svac_test",
            workspace_id=None,
            provider_name="unit",
        )


def test_scope_in_response_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When Anthropic returns a ``scope`` field it is included in the
    INFO log line (section 4.3)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "sk-ant-oat01-mock",
                "expires_in": 1800,
                "scope": "messages:write",
            },
        )

    _install_mock_post(monkeypatch, handler)
    with caplog.at_level("INFO", logger="promptise.identity"):
        exchange_mod.exchange_jwt_for_anthropic_token(
            FAKE_JWT,
            federation_rule_id="fdrl_test",
            organization_id="org_test",
            service_account_id="svac_test",
            workspace_id=None,
            provider_name="unit",
        )
    rendered = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "INFO")
    assert "scope=messages:write" in rendered


def test_timeout_raises_token_acquisition_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated", request=request)

    _install_mock_post(monkeypatch, handler)
    with pytest.raises(TokenAcquisitionError, match="timed out"):
        exchange_mod.exchange_jwt_for_anthropic_token(
            FAKE_JWT,
            federation_rule_id="fdrl_test",
            organization_id="org_test",
            service_account_id="svac_test",
            workspace_id=None,
            provider_name="unit",
        )


def test_transport_error_raises_token_acquisition_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure", request=request)

    _install_mock_post(monkeypatch, handler)
    with pytest.raises(TokenAcquisitionError, match="before a response was received"):
        exchange_mod.exchange_jwt_for_anthropic_token(
            FAKE_JWT,
            federation_rule_id="fdrl_test",
            organization_id="org_test",
            service_account_id="svac_test",
            workspace_id=None,
            provider_name="unit",
        )


def test_minted_token_expiry_anchored_to_request_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """``expires_at_monotonic`` must be derived from a snapshot taken
    **before** the POST, so the cache treats the token as expiring
    slightly earlier than its nominal lifetime."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "sk-ant-oat01-mock", "expires_in": 600},
        )

    _install_mock_post(monkeypatch, handler)
    before = time.monotonic()
    minted = exchange_mod.exchange_jwt_for_anthropic_token(
        FAKE_JWT,
        federation_rule_id="fdrl_test",
        organization_id="org_test",
        service_account_id="svac_test",
        workspace_id=None,
        provider_name="unit",
    )
    after = time.monotonic()
    assert before + 600 <= minted.expires_at_monotonic <= after + 600 + 0.5


def test_successful_exchange_logs_at_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Section 4.3 / 9.1: every successful exchange logs at INFO with
    provider name and expires_in, but never with the access token."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "sk-ant-oat01-mock-secret", "expires_in": 1800},
        )

    _install_mock_post(monkeypatch, handler)
    with caplog.at_level("INFO", logger="promptise.identity"):
        exchange_mod.exchange_jwt_for_anthropic_token(
            FAKE_JWT,
            federation_rule_id="fdrl_test",
            organization_id="org_test",
            service_account_id="svac_test",
            workspace_id=None,
            provider_name="unit",
        )
    # Find the success log line.
    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(info_records) >= 1
    rendered = "\n".join(r.getMessage() for r in info_records)
    assert "provider=unit" in rendered
    assert "expires_in=1800" in rendered
    # Critical: the access token MUST NOT appear in any log message
    # (section 9.1). This is a security regression test.
    for record in caplog.records:
        assert "sk-ant-oat01-mock-secret" not in record.getMessage()
        assert "header.payload.sig" not in record.getMessage()
