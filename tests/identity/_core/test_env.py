"""Unit tests for the federation-credential environment helpers.

``_internal/env.py`` is a Phase 1 deliverable, so it ships with its
own tests per the build plan's "tests are not optional" principle.
"""

from __future__ import annotations

import pytest

from promptise.identity import ProviderConfigError
from promptise.identity._internal import env as env_mod


def test_require_env_returns_stripped_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_VAR", "  value  ")
    assert env_mod._require_env("SOME_VAR") == "value"


def test_require_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSENT_VAR", raising=False)
    with pytest.raises(ProviderConfigError, match="ABSENT_VAR is not set"):
        env_mod._require_env("ABSENT_VAR")


def test_require_env_whitespace_only_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLANK_VAR", "   ")
    with pytest.raises(ProviderConfigError, match="BLANK_VAR is not set"):
        env_mod._require_env("BLANK_VAR")


def test_resolve_uses_overrides_when_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with env vars set, explicit overrides win.
    monkeypatch.setenv(env_mod.ENV_FEDERATION_RULE_ID, "fdrl_env")
    monkeypatch.setenv(env_mod.ENV_ORGANIZATION_ID, "org_env")
    monkeypatch.setenv(env_mod.ENV_SERVICE_ACCOUNT_ID, "svac_env")
    creds = env_mod._resolve_anthropic_credentials(
        federation_rule_id="fdrl_override",
        organization_id="org_override",
        service_account_id="svac_override",
        workspace_id="wrkspc_override",
    )
    assert creds == {
        "federation_rule_id": "fdrl_override",
        "organization_id": "org_override",
        "service_account_id": "svac_override",
        "workspace_id": "wrkspc_override",
    }


def test_resolve_reads_env_when_no_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(env_mod.ENV_FEDERATION_RULE_ID, "fdrl_env")
    monkeypatch.setenv(env_mod.ENV_ORGANIZATION_ID, "org_env")
    monkeypatch.setenv(env_mod.ENV_SERVICE_ACCOUNT_ID, "svac_env")
    monkeypatch.delenv(env_mod.ENV_WORKSPACE_ID, raising=False)
    creds = env_mod._resolve_anthropic_credentials()
    assert creds == {
        "federation_rule_id": "fdrl_env",
        "organization_id": "org_env",
        "service_account_id": "svac_env",
        "workspace_id": None,
    }


def test_resolve_reads_workspace_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(env_mod.ENV_FEDERATION_RULE_ID, "fdrl_env")
    monkeypatch.setenv(env_mod.ENV_ORGANIZATION_ID, "org_env")
    monkeypatch.setenv(env_mod.ENV_SERVICE_ACCOUNT_ID, "svac_env")
    monkeypatch.setenv(env_mod.ENV_WORKSPACE_ID, "wrkspc_env")
    creds = env_mod._resolve_anthropic_credentials()
    assert creds["workspace_id"] == "wrkspc_env"


def test_resolve_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(env_mod.ENV_FEDERATION_RULE_ID, raising=False)
    monkeypatch.setenv(env_mod.ENV_ORGANIZATION_ID, "org_env")
    monkeypatch.setenv(env_mod.ENV_SERVICE_ACCOUNT_ID, "svac_env")
    with pytest.raises(ProviderConfigError, match=env_mod.ENV_FEDERATION_RULE_ID):
        env_mod._resolve_anthropic_credentials()


def test_resolve_blank_workspace_env_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(env_mod.ENV_FEDERATION_RULE_ID, "fdrl_env")
    monkeypatch.setenv(env_mod.ENV_ORGANIZATION_ID, "org_env")
    monkeypatch.setenv(env_mod.ENV_SERVICE_ACCOUNT_ID, "svac_env")
    monkeypatch.setenv(env_mod.ENV_WORKSPACE_ID, "   ")
    creds = env_mod._resolve_anthropic_credentials()
    assert creds["workspace_id"] is None
