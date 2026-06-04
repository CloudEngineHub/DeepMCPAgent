"""Unit tests for the generic OIDC credential provider and ``from_oidc``."""

from __future__ import annotations

from pathlib import Path

import pytest

from promptise.identity import (
    CredentialAcquisitionError,
    OidcCallableProvider,
    OidcFileProvider,
    ProviderConfigError,
)
from promptise.identity.providers.oidc import from_oidc

FAKE_JWT = "header.payload.sig"


# -- Source validation ----------------------------------------------------


def test_no_source_raises() -> None:
    with pytest.raises(ProviderConfigError, match="none was supplied"):
        from_oidc("https://example.com")


def test_two_sources_raises() -> None:
    with pytest.raises(ProviderConfigError, match="2 were supplied"):
        from_oidc("https://example.com", token_file="/tmp/a", token_env_var="X")


# -- File / callable / env ------------------------------------------------


def test_file_mode(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    provider = from_oidc("https://example.com", token_file=f)
    assert isinstance(provider, OidcFileProvider)
    assert provider.issuer == "https://example.com"
    assert provider.provider_name == "oidc:https://example.com"
    assert provider._acquire_upstream_jwt() == FAKE_JWT


def test_callable_mode() -> None:
    provider = from_oidc("https://example.com", token_fn=lambda: FAKE_JWT)
    assert isinstance(provider, OidcCallableProvider)
    assert provider.get_credential() == FAKE_JWT


def test_env_var_mode_reads_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_OIDC", "first.jwt.x")
    provider = from_oidc("https://example.com", token_env_var="MY_OIDC")
    assert provider._acquire_upstream_jwt() == "first.jwt.x"
    monkeypatch.setenv("MY_OIDC", "second.jwt.x")
    assert provider._acquire_upstream_jwt() == "second.jwt.x"


def test_env_var_unset_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_OIDC", FAKE_JWT)
    provider = from_oidc("https://example.com", token_env_var="MY_OIDC")
    monkeypatch.delenv("MY_OIDC", raising=False)
    with pytest.raises(CredentialAcquisitionError, match="MY_OIDC"):
        provider._acquire_upstream_jwt()
