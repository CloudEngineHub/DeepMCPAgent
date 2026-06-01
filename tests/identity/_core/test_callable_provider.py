"""Unit tests for :class:`CallableTokenProvider`.

Covers success, exception wrapping, type validation, empty-result
handling, and the cross-thread concurrency contract from section 4.4
of the build plan.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from promptise.identity import CallableTokenProvider, TokenAcquisitionError


def _make_provider(token_fn: Callable[[], str]) -> CallableTokenProvider:
    return CallableTokenProvider(
        token_fn=token_fn,
        provider_label="test-callable",
        federation_rule_id="fdrl_test",
        organization_id="org_test",
        service_account_id="svac_test",
    )


def test_returns_value_from_callable() -> None:
    provider = _make_provider(lambda: "header.payload.sig")
    assert provider._acquire_upstream_jwt() == "header.payload.sig"


def test_strips_whitespace_around_callable_result() -> None:
    provider = _make_provider(lambda: "  header.payload.sig\n")
    assert provider._acquire_upstream_jwt() == "header.payload.sig"


def test_callable_exception_is_wrapped_with_cause() -> None:
    class _Boom(Exception):
        """Synthetic upstream failure."""

    def fn() -> str:
        raise _Boom("simulated metadata service failure")

    provider = _make_provider(fn)
    with pytest.raises(TokenAcquisitionError, match="_Boom") as exc_info:
        provider._acquire_upstream_jwt()
    assert isinstance(exc_info.value.__cause__, _Boom)


def test_callable_returning_non_string_raises() -> None:
    def bad() -> Any:
        return 12345

    provider = _make_provider(bad)
    with pytest.raises(TokenAcquisitionError, match="expected str"):
        provider._acquire_upstream_jwt()


def test_callable_returning_empty_string_raises() -> None:
    provider = _make_provider(lambda: "   \n")
    with pytest.raises(TokenAcquisitionError, match="empty"):
        provider._acquire_upstream_jwt()


def test_provider_name_returns_label() -> None:
    provider = _make_provider(lambda: "x.y.z")
    assert provider.provider_name == "test-callable"


def test_concurrent_get_token_calls_collapse_to_one_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 4.4: a single :class:`IdentityProvider` shared across
    threads must serialise concurrent ``get_token`` calls so the
    upstream IdP is hit exactly once."""

    upstream_call_count = [0]
    exchange_call_count = [0]

    def upstream() -> str:
        upstream_call_count[0] += 1
        # Sleep so the second thread enters the critical section
        # while the first thread is still inside it.
        time.sleep(0.05)
        return "header.payload.sig"

    def transport_handler(request: httpx.Request) -> httpx.Response:
        exchange_call_count[0] += 1
        return httpx.Response(
            200,
            json={"access_token": "sk-ant-oat01-mock", "expires_in": 3600},
        )

    transport = httpx.MockTransport(transport_handler)

    def mocked_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", mocked_post)

    provider = _make_provider(upstream)
    tokens: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            tokens.append(provider.get_token())
        except BaseException as exc:  # noqa: BLE001 — capture and re-raise from main
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"workers raised: {errors!r}"
    assert len(set(tokens)) == 1, "all five threads must receive the same token"
    assert upstream_call_count[0] == 1, "upstream IdP must be hit exactly once"
    assert exchange_call_count[0] == 1, "Anthropic exchange must be hit exactly once"


def test_get_auth_header_returns_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_auth_header() is the shortcut for downstream HTTP calls."""

    def transport_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "sk-ant-oat01-mock", "expires_in": 3600},
        )

    transport = httpx.MockTransport(transport_handler)

    def mocked_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", mocked_post)

    provider = _make_provider(lambda: "header.payload.sig")
    header = provider.get_auth_header()
    assert header == {"Authorization": "Bearer sk-ant-oat01-mock"}


def test_advisory_refresh_success_replaces_cached_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 4.5: when a token is inside the advisory window and the
    refresh succeeds, the new token replaces the cached one."""

    upstream_calls = [0]
    expires_values = [90, 3600]  # first token already in advisory window

    def upstream() -> str:
        upstream_calls[0] += 1
        return f"jwt.{upstream_calls[0]}.sig"

    def transport_handler(request: httpx.Request) -> httpx.Response:
        idx = min(upstream_calls[0] - 1, len(expires_values) - 1)
        return httpx.Response(
            200,
            json={
                "access_token": f"sk-ant-oat01-call{upstream_calls[0]}",
                "expires_in": expires_values[idx],
            },
        )

    transport = httpx.MockTransport(transport_handler)

    def mocked_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", mocked_post)

    provider = _make_provider(upstream)
    first = provider.get_token()
    assert first == "sk-ant-oat01-call1"
    # First token is inside the advisory window (90 s); the next call
    # triggers a successful refresh that returns a fresh, longer-lived
    # token.
    second = provider.get_token()
    assert second == "sk-ant-oat01-call2"
    assert upstream_calls[0] == 2


def test_advisory_refresh_failure_keeps_cached_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 4.5: if the advisory refresh fails, the cached token
    continues to be served and a warning is logged."""

    upstream_calls = [0]
    transport_calls = [0]

    def upstream() -> str:
        upstream_calls[0] += 1
        if upstream_calls[0] == 1:
            return "first.jwt.value"
        raise RuntimeError("simulated upstream failure on refresh")

    def transport_handler(request: httpx.Request) -> httpx.Response:
        transport_calls[0] += 1
        # First call returns a token already inside the advisory
        # window (90 s remaining < 120 s advisory buffer).
        return httpx.Response(
            200,
            json={"access_token": "sk-ant-oat01-first", "expires_in": 90},
        )

    transport = httpx.MockTransport(transport_handler)

    def mocked_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", mocked_post)

    provider = _make_provider(upstream)
    first = provider.get_token()
    assert first == "sk-ant-oat01-first"

    # Second call: the cached token is inside the advisory window, so
    # the provider attempts a refresh. The upstream callable raises.
    # The cached token must still be returned and no exception leaks.
    second = provider.get_token()
    assert second == "sk-ant-oat01-first"
    assert upstream_calls[0] == 2, "upstream was called for the refresh attempt"
