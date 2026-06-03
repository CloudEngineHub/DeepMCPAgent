"""Tests for ``build_agent(identity=...)`` — Phase 8 identity integration.

Covers the credential-precedence guard (a static ANTHROPIC_API_KEY must
not silently shadow a federated identity), the ``agent.identity``
attribute, and the federated-token injection in ``_normalize_model``
(an Anthropic OAuth bearer token reaches the model's Authorization
header, with no static key required). The heavy graph/model internals
are mocked exactly as the existing core-agent E2E tests do — no network
and no real credentials.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from promptise.agent import PromptiseAgent, _normalize_model, build_agent
from promptise.identity import AgentIdentity, CredentialPrecedenceError

FAKE_JWT: str = "header.payload.sig"

# Synthetic federation IDs — identifiers, not secrets (build plan 4.1).
_FED_KWARGS: dict[str, str] = {
    "federation_rule_id": "fdrl_test",
    "organization_id": "org_test",
    "service_account_id": "svac_test",
}


def _identity() -> AgentIdentity:
    """An OIDC identity whose upstream JWT needs no network."""
    return AgentIdentity.from_oidc(
        issuer="https://example.com",
        token_fn=lambda: FAKE_JWT,
        **_FED_KWARGS,
    )


def _mock_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "sk-ant-oat01-mock", "expires_in": 3600},
        )

    transport = httpx.MockTransport(handler)

    def mocked_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", mocked_post)


def _make_mock_inner() -> MagicMock:
    mock = MagicMock()
    mock.ainvoke = AsyncMock(return_value={"messages": []})
    mock.invoke = MagicMock(return_value={"messages": []})
    return mock


# -- Credential-precedence guard ------------------------------------------


@pytest.mark.asyncio
async def test_identity_with_api_key_env_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A static ANTHROPIC_API_KEY alongside an identity must hard-fail."""
    # Sentinel value — presence is what the guard checks, not the content.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "present-for-test")
    with pytest.raises(CredentialPrecedenceError, match="silently shadow"):
        await build_agent(
            servers={},
            model="anthropic:claude-3-5-sonnet-latest",
            identity=_identity(),
        )


# -- agent.identity attribute ---------------------------------------------


@pytest.mark.asyncio
async def test_build_agent_stores_identity_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ANTHROPIC_API_KEY set, an agent builds and exposes the
    identity (build-plan phase-8 acceptance criterion)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    identity = _identity()
    mock_graph = _make_mock_inner()

    with (
        patch("promptise.agent._normalize_model") as mock_norm,
        patch("promptise.agent.PromptGraphEngine", return_value=mock_graph),
        patch.dict("sys.modules", {"deepagents": None}),
    ):
        mock_norm.return_value = MagicMock()
        agent = await build_agent(
            servers={},
            model="anthropic:claude-3-5-sonnet-latest",
            identity=identity,
        )

    assert isinstance(agent, PromptiseAgent)
    assert agent.identity is identity
    # build_agent forwards the identity into model normalization.
    mock_norm.assert_called_once()
    assert mock_norm.call_args.args[1] is identity


@pytest.mark.asyncio
async def test_build_agent_identity_defaults_to_none() -> None:
    mock_graph = _make_mock_inner()
    with (
        patch("promptise.agent._normalize_model", return_value=MagicMock()),
        patch("promptise.agent.PromptGraphEngine", return_value=mock_graph),
        patch.dict("sys.modules", {"deepagents": None}),
    ):
        agent = await build_agent(servers={}, model="openai:gpt-5-mini")
    assert agent.identity is None


# -- Federated-token injection in _normalize_model ------------------------


def test_normalize_model_injects_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A string Anthropic model + identity => the minted OAuth token is
    placed in the Authorization header (no static key needed)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _mock_exchange(monkeypatch)
    model = _normalize_model("anthropic:claude-3-5-sonnet-latest", _identity())
    headers = model.default_headers  # type: ignore[attr-defined]
    assert headers is not None
    assert headers["Authorization"] == "Bearer sk-ant-oat01-mock"


def test_normalize_model_without_identity_has_no_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A sentinel api key lets ChatAnthropic construct; presence/content is
    # irrelevant to the assertion (we only check no Authorization header).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "present-for-test")
    model = _normalize_model("anthropic:claude-3-5-sonnet-latest")
    assert not getattr(model, "default_headers", None)


def test_normalize_model_passes_through_prebuilt_model() -> None:
    """A pre-built chat model instance is returned untouched even when an
    identity is supplied — there is nothing to inject into."""
    sentinel = MagicMock()
    result = _normalize_model(sentinel, _identity())
    assert result is sentinel
