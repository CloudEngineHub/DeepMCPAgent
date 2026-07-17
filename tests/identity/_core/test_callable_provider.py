"""Unit tests for :class:`CallableTokenProvider`.

The token callable is audience-aware (``Callable[[str | None], str]``):
active providers honour the requested audience; passive ones ignore it.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from promptise.identity import (
    CallableTokenProvider,
    CredentialAcquisitionError,
    ProviderConfigError,
)

FAKE_JWT = "header.payload.sig"


def _jwt(aud: str, *, exp: float | None = None) -> str:
    claims: dict[str, object] = {"aud": aud}
    if exp is not None:
        claims["exp"] = exp
    h = base64.urlsafe_b64encode(b"{}").rstrip(b"=").decode()
    p = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{h}.{p}."


def test_invokes_callable_and_returns_jwt() -> None:
    provider = CallableTokenProvider(token_fn=lambda audience=None: FAKE_JWT)
    assert provider.provider_name == "callable"
    assert provider._acquire_upstream_jwt() == FAKE_JWT
    assert provider.get_credential() == FAKE_JWT


def test_strips_whitespace() -> None:
    provider = CallableTokenProvider(token_fn=lambda audience=None: f"  {FAKE_JWT}\n")
    assert provider.get_credential() == FAKE_JWT


def test_typed_identity_error_propagates_unchanged() -> None:
    def boom(audience: str | None = None) -> str:
        raise ProviderConfigError("missing optional dependency")

    provider = CallableTokenProvider(token_fn=boom)
    with pytest.raises(ProviderConfigError, match="missing optional dependency"):
        provider.get_credential()


def test_generic_exception_is_wrapped() -> None:
    def boom(audience: str | None = None) -> str:
        raise RuntimeError("metadata down")

    provider = CallableTokenProvider(token_fn=boom, provider_label="gcp-metadata")
    with pytest.raises(CredentialAcquisitionError, match="gcp-metadata") as exc:
        provider.get_credential()
    assert isinstance(exc.value.__cause__, RuntimeError)


def test_non_string_return_raises() -> None:
    provider = CallableTokenProvider(token_fn=lambda audience=None: 123)  # type: ignore[arg-type,return-value]
    with pytest.raises(CredentialAcquisitionError, match="expected str"):
        provider.get_credential()


def test_empty_return_raises() -> None:
    provider = CallableTokenProvider(token_fn=lambda audience=None: "   ")
    with pytest.raises(CredentialAcquisitionError, match="empty string"):
        provider.get_credential()


def test_auth_header() -> None:
    provider = CallableTokenProvider(token_fn=lambda audience=None: FAKE_JWT)
    assert provider.auth_header() == {"Authorization": f"Bearer {FAKE_JWT}"}


# -- Per-resource (per-audience) credentials ------------------------------


def test_audience_is_passed_to_callable() -> None:
    seen: list[str | None] = []

    def mint(audience: str | None = None) -> str:
        seen.append(audience)
        return _jwt(audience or "default", exp=time.time() + 3600)

    provider = CallableTokenProvider(token_fn=mint)
    provider.get_credential("api://A")
    provider.get_credential()
    assert seen == ["api://A", None]


def test_credentials_cached_per_audience() -> None:
    calls = {"n": 0}

    def mint(audience: str | None = None) -> str:
        calls["n"] += 1
        return _jwt(audience or "default", exp=time.time() + 3600)

    provider = CallableTokenProvider(token_fn=mint)
    a1 = provider.get_credential("api://A")
    b1 = provider.get_credential("api://B")
    a2 = provider.get_credential("api://A")  # served from cache
    assert a1 == a2
    assert a1 != b1
    assert calls["n"] == 2  # one mint per distinct audience


def test_auth_header_with_audience() -> None:
    provider = CallableTokenProvider(
        token_fn=lambda audience=None: _jwt(audience or "default", exp=time.time() + 3600)
    )
    header = provider.auth_header("api://A")
    assert header["Authorization"].startswith("Bearer ")
