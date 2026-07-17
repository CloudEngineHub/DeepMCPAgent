"""Unit tests for the dependency-free credential-acquisition retry helpers.

A transient metadata/STS blip must be retried (so a verifiable agent does not
silently degrade to unauthenticated), but an auth/4xx failure must fail fast.
``sleep`` is injected as a no-op so the tests are instant.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from promptise.identity._core.retry import (
    aws_is_transient,
    http_get_with_retry,
    retry_call,
)

_NOSLEEP = lambda _d: None  # noqa: E731


def _seq_get(items: list[Any]) -> tuple[Any, dict[str, int]]:
    """A fake httpx.get returning each item in turn (repeating the last)."""
    calls = {"n": 0}

    def get(url: str, **kwargs: Any) -> httpx.Response:
        item = items[min(calls["n"], len(items) - 1)]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    return get, calls


# ── http_get_with_retry ───────────────────────────────────────────────────
def test_retries_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    get, calls = _seq_get(
        [httpx.Response(503), httpx.Response(503), httpx.Response(200, text="tok")]
    )
    monkeypatch.setattr(httpx, "get", get)
    resp = http_get_with_retry("http://x", params={}, headers={}, timeout=1.0, sleep=_NOSLEEP)
    assert resp.status_code == 200 and calls["n"] == 3


def test_retries_transport_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    get, calls = _seq_get([httpx.ConnectError("boom"), httpx.Response(200, text="tok")])
    monkeypatch.setattr(httpx, "get", get)
    resp = http_get_with_retry("http://x", params={}, headers={}, timeout=1.0, sleep=_NOSLEEP)
    assert resp.status_code == 200 and calls["n"] == 2


def test_4xx_is_returned_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    # 403 is an auth failure — must NOT be retried; returned for the caller to
    # convert into its precise fatal error.
    get, calls = _seq_get([httpx.Response(403), httpx.Response(200)])
    monkeypatch.setattr(httpx, "get", get)
    resp = http_get_with_retry("http://x", params={}, headers={}, timeout=1.0, sleep=_NOSLEEP)
    assert resp.status_code == 403 and calls["n"] == 1


def test_persistent_5xx_exhausts_and_returns_last(monkeypatch: pytest.MonkeyPatch) -> None:
    get, calls = _seq_get([httpx.Response(503)])
    monkeypatch.setattr(httpx, "get", get)
    resp = http_get_with_retry(
        "http://x", params={}, headers={}, timeout=1.0, attempts=3, sleep=_NOSLEEP
    )
    assert resp.status_code == 503 and calls["n"] == 3


def test_persistent_transport_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    get, calls = _seq_get([httpx.ConnectError("down")])
    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(httpx.ConnectError):
        http_get_with_retry(
            "http://x", params={}, headers={}, timeout=1.0, attempts=3, sleep=_NOSLEEP
        )
    assert calls["n"] == 3


# ── retry_call ────────────────────────────────────────────────────────────
def test_retry_call_retries_transient_then_succeeds() -> None:
    n = {"c": 0}

    def fn() -> str:
        n["c"] += 1
        if n["c"] < 3:
            raise ValueError("transient")
        return "ok"

    out = retry_call(fn, is_transient=lambda e: isinstance(e, ValueError), sleep=_NOSLEEP)
    assert out == "ok" and n["c"] == 3


def test_retry_call_fails_fast_on_non_transient() -> None:
    n = {"c": 0}

    def fn() -> str:
        n["c"] += 1
        raise KeyError("auth")

    with pytest.raises(KeyError):
        retry_call(fn, is_transient=lambda e: False, sleep=_NOSLEEP)
    assert n["c"] == 1  # never retried


# ── aws_is_transient ──────────────────────────────────────────────────────
def _client_error(code: str, status: int = 400) -> Exception:
    exc = Exception(code)
    exc.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}  # type: ignore[attr-defined]
    return exc


def test_aws_transient_classification() -> None:
    assert aws_is_transient(_client_error("ThrottlingException")) is True
    assert aws_is_transient(_client_error("ServiceUnavailable", status=503)) is True
    assert aws_is_transient(_client_error("AccessDenied", status=403)) is False
    assert aws_is_transient(type("ConnectTimeoutError", (Exception,), {})()) is True
    assert aws_is_transient(ValueError("nope")) is False
