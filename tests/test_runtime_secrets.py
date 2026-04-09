"""Tests for promptise.runtime.secrets — SecretScope and SecretEntry."""

from __future__ import annotations

import time

import pytest

from promptise.runtime.config import SecretScopeConfig
from promptise.runtime.secrets import SecretEntry, SecretScope

# ---------------------------------------------------------------------------
# SecretEntry dataclass
# ---------------------------------------------------------------------------


class TestSecretEntry:
    def test_creation(self):
        entry = SecretEntry(name="API_KEY", value="abc123")
        assert entry.name == "API_KEY"
        assert entry.value == "abc123"

    def test_creation_with_expires(self):
        t = time.monotonic() + 600
        entry = SecretEntry(name="TOKEN", value="xyz", expires_at=t)
        assert entry.expires_at == t


# ---------------------------------------------------------------------------
# SecretScope — resolve_initial
# ---------------------------------------------------------------------------


class TestResolveInitial:
    @pytest.mark.asyncio
    async def test_resolves_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "secret_value")
        config = SecretScopeConfig(secrets={"my_key": "${MY_SECRET}"})
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        assert scope.get("my_key") == "secret_value"

    @pytest.mark.asyncio
    async def test_literal_value_passthrough(self):
        config = SecretScopeConfig(secrets={"literal": "plain_text_value"})
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        assert scope.get("literal") == "plain_text_value"

    @pytest.mark.asyncio
    async def test_missing_env_var_raises(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR_XYZ", raising=False)
        config = SecretScopeConfig(secrets={"bad": "${NONEXISTENT_VAR_XYZ}"})
        scope = SecretScope(config, process_id="test-proc")
        with pytest.raises(KeyError):
            await scope.resolve_initial()

    @pytest.mark.asyncio
    async def test_multiple_secrets(self, monkeypatch):
        monkeypatch.setenv("A_VAR", "aaa")
        monkeypatch.setenv("B_VAR", "bbb")
        config = SecretScopeConfig(
            secrets={
                "a": "${A_VAR}",
                "b": "${B_VAR}",
                "c": "literal",
            }
        )
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        assert scope.get("a") == "aaa"
        assert scope.get("b") == "bbb"
        assert scope.get("c") == "literal"


# ---------------------------------------------------------------------------
# SecretScope — get
# ---------------------------------------------------------------------------


class TestGet:
    @pytest.mark.asyncio
    async def test_returns_value_for_existing(self, monkeypatch):
        monkeypatch.setenv("SEC", "val")
        config = SecretScopeConfig(secrets={"key": "${SEC}"})
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        assert scope.get("key") == "val"

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown(self):
        config = SecretScopeConfig(secrets={})
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        assert scope.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_returns_none_after_ttl_expires(self, monkeypatch):
        monkeypatch.setenv("TTL_SEC", "temporary")
        config = SecretScopeConfig(secrets={"ttl_key": "${TTL_SEC}"})
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        # Verify it exists first
        assert scope.get("ttl_key") == "temporary"
        # Manipulate the entry's expires_at to a past monotonic time
        for entry in scope._secrets.values():
            if entry.name == "ttl_key":
                entry.expires_at = time.monotonic() - 10
                break
        assert scope.get("ttl_key") is None


# ---------------------------------------------------------------------------
# SecretScope — list_names
# ---------------------------------------------------------------------------


class TestListNames:
    @pytest.mark.asyncio
    async def test_returns_all_active(self, monkeypatch):
        monkeypatch.setenv("X", "1")
        monkeypatch.setenv("Y", "2")
        config = SecretScopeConfig(secrets={"x": "${X}", "y": "${Y}"})
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        names = scope.list_names()
        assert sorted(names) == ["x", "y"]

    @pytest.mark.asyncio
    async def test_excludes_expired(self, monkeypatch):
        monkeypatch.setenv("ALIVE", "yes")
        monkeypatch.setenv("DEAD", "no")
        config = SecretScopeConfig(secrets={"alive": "${ALIVE}", "dead": "${DEAD}"})
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        # Expire one entry
        for entry in scope._secrets.values():
            if entry.name == "dead":
                entry.expires_at = time.monotonic() - 10
                break
        names = scope.list_names()
        assert names == ["alive"]


# ---------------------------------------------------------------------------
# SecretScope — rotate
# ---------------------------------------------------------------------------


class TestRotate:
    @pytest.mark.asyncio
    async def test_replaces_value(self, monkeypatch):
        monkeypatch.setenv("ROT", "old")
        config = SecretScopeConfig(secrets={"r": "${ROT}"})
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        assert scope.get("r") == "old"
        await scope.rotate("r", "new")
        assert scope.get("r") == "new"

    @pytest.mark.asyncio
    async def test_unknown_name_raises(self):
        config = SecretScopeConfig(secrets={})
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        with pytest.raises(KeyError):
            await scope.rotate("ghost", "value")


# ---------------------------------------------------------------------------
# SecretScope — revoke_all
# ---------------------------------------------------------------------------


class TestRevokeAll:
    @pytest.mark.asyncio
    async def test_clears_all_secrets(self, monkeypatch):
        monkeypatch.setenv("K1", "v1")
        monkeypatch.setenv("K2", "v2")
        config = SecretScopeConfig(secrets={"k1": "${K1}", "k2": "${K2}"})
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        assert len(scope.list_names()) == 2
        await scope.revoke_all()
        assert scope.list_names() == []


# ---------------------------------------------------------------------------
# SecretScope — sanitize_text
# ---------------------------------------------------------------------------


class TestSanitizeText:
    @pytest.mark.asyncio
    async def test_replaces_secret_values(self, monkeypatch):
        monkeypatch.setenv("TOKEN", "super_secret_token_123")
        config = SecretScopeConfig(secrets={"token": "${TOKEN}"})
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        text = "The token is super_secret_token_123 in the log"
        sanitized = scope.sanitize_text(text)
        assert "super_secret_token_123" not in sanitized
        assert "[REDACTED]" in sanitized

    @pytest.mark.asyncio
    async def test_leaves_text_unchanged_when_no_match(self, monkeypatch):
        monkeypatch.setenv("OTHER", "xyz")
        config = SecretScopeConfig(secrets={"other": "${OTHER}"})
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        text = "Nothing secret here at all"
        assert scope.sanitize_text(text) == text

    @pytest.mark.asyncio
    async def test_sanitizes_multiple_secrets(self, monkeypatch):
        monkeypatch.setenv("A", "alpha_secret")
        monkeypatch.setenv("B", "beta_secret")
        config = SecretScopeConfig(secrets={"a": "${A}", "b": "${B}"})
        scope = SecretScope(config, process_id="test-proc")
        await scope.resolve_initial()
        text = "Found alpha_secret and beta_secret in output"
        sanitized = scope.sanitize_text(text)
        assert "alpha_secret" not in sanitized
        assert "beta_secret" not in sanitized
        assert sanitized.count("[REDACTED]") == 2
