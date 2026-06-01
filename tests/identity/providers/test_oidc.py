"""Unit tests for the generic OIDC provider and its ``from_oidc`` factory.

Covers all three token sources (file, callable, env var), the
mutual-exclusivity validation, issuer recording, federation-credential
resolution, fresh env-var reads on rotation, and an end-to-end token
acquisition through a mocked Anthropic exchange.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from promptise.identity import (
    OidcCallableProvider,
    OidcFileProvider,
    ProviderConfigError,
    TokenAcquisitionError,
)
from promptise.identity.providers.oidc import from_oidc

FAKE_JWT: str = "header.payload.sig"

# Synthetic federation IDs reused across tests — identifiers, not
# secrets (build plan section 4.1).
_FED_KWARGS: dict[str, str] = {
    "federation_rule_id": "fdrl_test",
    "organization_id": "org_test",
    "service_account_id": "svac_test",
}


def _install_mock_exchange(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Mock the Anthropic exchange. Returns a counter dict the test can
    assert on (``{"calls": N}``)."""
    counter = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["calls"] += 1
        return httpx.Response(
            200,
            json={"access_token": "sk-ant-oat01-mock", "expires_in": 3600},
        )

    transport = httpx.MockTransport(handler)

    def mocked_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", mocked_post)
    return counter


# -- Source selection / validation ---------------------------------------


def test_no_source_raises() -> None:
    with pytest.raises(ProviderConfigError, match="none was supplied"):
        from_oidc(issuer="https://example.com", **_FED_KWARGS)


def test_two_sources_raises() -> None:
    with pytest.raises(ProviderConfigError, match="2 were supplied"):
        from_oidc(
            issuer="https://example.com",
            token_file="/tmp/a",
            token_env_var="X",
            **_FED_KWARGS,
        )


def test_three_sources_raises() -> None:
    with pytest.raises(ProviderConfigError, match="3 were supplied"):
        from_oidc(
            issuer="https://example.com",
            token_file="/tmp/a",
            token_fn=lambda: "x",
            token_env_var="X",
            **_FED_KWARGS,
        )


# -- File mode ------------------------------------------------------------


def test_file_mode_returns_file_provider(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    provider = from_oidc(issuer="https://example.com", token_file=f, **_FED_KWARGS)
    assert isinstance(provider, OidcFileProvider)
    assert provider.issuer == "https://example.com"
    assert provider.provider_name == "oidc:https://example.com"
    assert provider._acquire_upstream_jwt() == FAKE_JWT


# -- Callable mode --------------------------------------------------------


def test_callable_mode_returns_callable_provider() -> None:
    provider = from_oidc(
        issuer="https://example.com",
        token_fn=lambda: FAKE_JWT,
        **_FED_KWARGS,
    )
    assert isinstance(provider, OidcCallableProvider)
    assert provider.issuer == "https://example.com"
    assert provider._acquire_upstream_jwt() == FAKE_JWT


# -- Env-var mode ---------------------------------------------------------


def test_env_var_mode_returns_callable_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_OIDC_TOKEN", FAKE_JWT)
    provider = from_oidc(
        issuer="https://example.com",
        token_env_var="MY_OIDC_TOKEN",
        **_FED_KWARGS,
    )
    assert isinstance(provider, OidcCallableProvider)
    assert provider._acquire_upstream_jwt() == FAKE_JWT


def test_env_var_mode_reads_fresh_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env-var mode must re-read the variable on every refresh so token
    rotation in CI is observed (build plan section 4.6)."""
    monkeypatch.setenv("MY_OIDC_TOKEN", "first.jwt.value")
    provider = from_oidc(
        issuer="https://example.com",
        token_env_var="MY_OIDC_TOKEN",
        **_FED_KWARGS,
    )
    assert provider._acquire_upstream_jwt() == "first.jwt.value"
    monkeypatch.setenv("MY_OIDC_TOKEN", "second.jwt.value")
    assert provider._acquire_upstream_jwt() == "second.jwt.value"


def test_env_var_unset_at_refresh_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_OIDC_TOKEN", FAKE_JWT)
    provider = from_oidc(
        issuer="https://example.com",
        token_env_var="MY_OIDC_TOKEN",
        **_FED_KWARGS,
    )
    # The variable disappears between construction and refresh.
    monkeypatch.delenv("MY_OIDC_TOKEN", raising=False)
    with pytest.raises(TokenAcquisitionError, match="MY_OIDC_TOKEN"):
        provider._acquire_upstream_jwt()


# -- Federation-credential resolution ------------------------------------


def test_federation_ids_resolved_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "fdrl_env")
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org_env")
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_env")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    provider = from_oidc(
        issuer="https://example.com",
        token_fn=lambda: FAKE_JWT,
    )
    assert provider.federation_rule_id == "fdrl_env"
    assert provider.organization_id == "org_env"
    assert provider.service_account_id == "svac_env"
    assert provider.workspace_id is None


def test_missing_federation_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_FEDERATION_RULE_ID", raising=False)
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org_env")
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_env")
    with pytest.raises(ProviderConfigError, match="ANTHROPIC_FEDERATION_RULE_ID"):
        from_oidc(
            issuer="https://example.com",
            token_fn=lambda: FAKE_JWT,
        )


def test_workspace_id_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = from_oidc(
        issuer="https://example.com",
        token_fn=lambda: FAKE_JWT,
        workspace_id="wrkspc_explicit",
        **_FED_KWARGS,
    )
    assert provider.workspace_id == "wrkspc_explicit"


# -- End-to-end through the mocked exchange ------------------------------


def test_env_var_mode_get_token_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build-plan phase-2 acceptance criterion: the docstring example
    (``from_oidc(issuer=..., token_env_var=...)``) works end-to-end with
    a fake JWT in an env var and a mocked Anthropic exchange."""
    monkeypatch.setenv("MY_OIDC_TOKEN", FAKE_JWT)
    counter = _install_mock_exchange(monkeypatch)

    provider = from_oidc(
        issuer="https://gitlab.com",
        token_env_var="MY_OIDC_TOKEN",
        **_FED_KWARGS,
    )
    token = provider.get_token()
    assert token == "sk-ant-oat01-mock"
    assert counter["calls"] == 1
    # Second call uses the cache — no second exchange.
    assert provider.get_token() == "sk-ant-oat01-mock"
    assert counter["calls"] == 1


def test_file_mode_get_token_smoke(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    counter = _install_mock_exchange(monkeypatch)

    provider = from_oidc(issuer="https://example.com", token_file=f, **_FED_KWARGS)
    assert provider.get_token() == "sk-ant-oat01-mock"
    assert counter["calls"] == 1


def test_callable_mode_get_auth_header_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock_exchange(monkeypatch)
    provider = from_oidc(
        issuer="https://example.com",
        token_fn=lambda: FAKE_JWT,
        **_FED_KWARGS,
    )
    assert provider.get_auth_header() == {"Authorization": "Bearer sk-ant-oat01-mock"}
