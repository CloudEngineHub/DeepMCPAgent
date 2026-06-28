"""Unit tests for the cached-credential primitive."""

from __future__ import annotations

import base64
import json
import time

from promptise.identity import CachedCredential, decode_jwt_claims, decode_jwt_expiry
from promptise.identity._core.cache import CREDENTIAL_REFRESH_BUFFER_SECONDS


def _jwt_with_exp(exp: float | None) -> str:
    """Build an unsigned JWT carrying (or omitting) an ``exp`` claim."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    claims: dict[str, object] = {"sub": "agent"}
    if exp is not None:
        claims["exp"] = exp
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    )
    return f"{header}.{payload}."


# -- CachedCredential.is_stale -------------------------------------------


def test_no_expiry_is_always_stale() -> None:
    cred = CachedCredential(token="x", expires_at_epoch=None)
    assert cred.is_stale() is True


def test_future_expiry_is_not_stale() -> None:
    cred = CachedCredential(token="x", expires_at_epoch=time.time() + 3600)
    assert cred.is_stale() is False


def test_within_buffer_is_stale() -> None:
    # Expires in less than the refresh buffer → re-acquire.
    cred = CachedCredential(
        token="x",
        expires_at_epoch=time.time() + CREDENTIAL_REFRESH_BUFFER_SECONDS - 5,
    )
    assert cred.is_stale() is True


def test_already_expired_is_stale() -> None:
    cred = CachedCredential(token="x", expires_at_epoch=time.time() - 1)
    assert cred.is_stale() is True


# -- decode_jwt_expiry ----------------------------------------------------


def test_decode_expiry_reads_exp_claim() -> None:
    assert decode_jwt_expiry(_jwt_with_exp(1_700_000_000)) == 1_700_000_000.0


def test_decode_expiry_missing_exp_returns_none() -> None:
    assert decode_jwt_expiry(_jwt_with_exp(None)) is None


def test_decode_expiry_malformed_returns_none() -> None:
    assert decode_jwt_expiry("not-a-jwt") is None
    assert decode_jwt_expiry("only.two") is None
    assert decode_jwt_expiry("a.!@#$.c") is None


def test_decode_expiry_non_numeric_exp_returns_none() -> None:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": "soon"}).encode())
        .rstrip(b"=")
        .decode()
    )
    assert decode_jwt_expiry(f"{header}.{payload}.") is None


def test_decode_expiry_boolean_exp_is_rejected() -> None:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": True}).encode())
        .rstrip(b"=")
        .decode()
    )
    assert decode_jwt_expiry(f"{header}.{payload}.") is None


# -- decode_jwt_claims ----------------------------------------------------


def test_decode_claims_returns_payload() -> None:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": "bot", "iss": "idp"}).encode())
        .rstrip(b"=")
        .decode()
    )
    assert decode_jwt_claims(f"{header}.{payload}.") == {"sub": "bot", "iss": "idp"}


def test_decode_claims_malformed_returns_empty() -> None:
    assert decode_jwt_claims("not-a-jwt") == {}
    assert decode_jwt_claims("a.!@#$.c") == {}


def test_decode_claims_non_object_returns_empty() -> None:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps([1, 2]).encode()).rstrip(b"=").decode()
    assert decode_jwt_claims(f"{header}.{payload}.") == {}
