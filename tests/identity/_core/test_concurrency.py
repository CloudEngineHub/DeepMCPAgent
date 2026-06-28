"""Concurrency test for the credential cache's documented thread-safety.

provider.py promises: "A single instance is safe to share across threads.
Concurrent calls to get_credential collapse into one acquisition." This proves
it — N threads hitting a cold cache simultaneously must trigger exactly one
upstream fetch, and all threads must get the same token.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import jwt

from promptise.identity._core.callable_provider import CallableTokenProvider


def _make_jwt(exp_offset: int = 3600) -> str:
    return jwt.encode(
        {"sub": "billing-bot", "exp": int(time.time()) + exp_offset},
        "test-secret",
        algorithm="HS256",
    )


def test_concurrent_get_credential_collapses_to_one_acquisition() -> None:
    calls = {"n": 0}
    lock = threading.Lock()
    token = _make_jwt()

    def token_fn(audience: str | None = None) -> str:
        with lock:
            calls["n"] += 1
        time.sleep(0.05)  # widen the race window so a non-locking impl would double-fetch
        return token

    provider = CallableTokenProvider(token_fn=token_fn, provider_label="test")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _i: provider.get_credential(), range(16)))

    assert calls["n"] == 1, f"expected exactly one upstream acquisition, got {calls['n']}"
    assert all(r == token for r in results)


def test_concurrent_per_audience_isolation() -> None:
    calls: dict[str, int] = {}
    lock = threading.Lock()

    def token_fn(audience: str | None = None) -> str:
        key = audience or "<default>"
        with lock:
            calls[key] = calls.get(key, 0) + 1
        time.sleep(0.03)
        return _make_jwt()

    provider = CallableTokenProvider(token_fn=token_fn, provider_label="test")
    audiences = ["api://a", "api://b", "api://a", "api://b"] * 4

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda aud: provider.get_credential(aud), audiences))

    # Exactly one acquisition per distinct audience, regardless of concurrency.
    assert calls == {"api://a": 1, "api://b": 1}, calls
