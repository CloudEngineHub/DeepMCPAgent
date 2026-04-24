"""Comprehensive tests for the unified PromptiseAgent class.

Tests memory integration, observability + memory combined, shutdown
lifecycle, and the builder return type.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from promptise.agent import PromptiseAgent
from promptise.observability import ObservabilityCollector
from promptise.observability_config import ObservabilityConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_inner() -> MagicMock:
    """Return a mock object that quacks like a LangGraph Runnable."""
    mock = MagicMock()
    mock.ainvoke = AsyncMock(return_value={"messages": [{"role": "assistant", "content": "ok"}]})
    mock.invoke = MagicMock(return_value={"messages": [{"role": "assistant", "content": "ok"}]})
    mock.some_custom_attr = "hello"
    return mock


class FakeMemoryResult:
    """Minimal MemoryResult stand-in for testing."""

    def __init__(self, content: str, score: float = 1.0) -> None:
        self.content = content
        self.score = score


class FakeMemoryProvider:
    """Minimal in-memory provider for testing PromptiseAgent memory integration."""

    def __init__(self, results: list[FakeMemoryResult] | None = None) -> None:
        self._results = results or []
        self.stored: list[str] = []
        self.search_calls: list[str] = []

    async def search(
        self, query: str, *, limit: int = 5, user_id: str | None = None
    ) -> list[FakeMemoryResult]:
        self.search_calls.append(query)
        return self._results[:limit]

    async def add(
        self,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> None:
        self.stored.append(content)


# ===========================================================================
# 1. Memory integration via PromptiseAgent
# ===========================================================================


class TestPromptiseAgentMemory:
    """Test memory search + inject + auto-store through PromptiseAgent."""

    @pytest.mark.asyncio
    async def test_ainvoke_injects_memory_context(self) -> None:
        """When memory returns results, they should be injected as SystemMessage."""
        provider = FakeMemoryProvider(
            [
                FakeMemoryResult("The user likes Python", score=0.9),
            ]
        )
        inner = _make_mock_inner()
        agent = PromptiseAgent(inner=inner, memory_provider=provider)

        await agent.ainvoke(
            {
                "messages": [{"role": "user", "content": "Hello!"}],
            }
        )

        # Memory should have been searched
        assert len(provider.search_calls) == 1
        assert "Hello!" in provider.search_calls[0]

        # Inner graph should have been called with injected memory
        inner.ainvoke.assert_awaited_once()
        call_args = inner.ainvoke.call_args
        messages = call_args[0][0]["messages"]
        # Should have the original user message + injected system message
        assert len(messages) == 2
        assert any(hasattr(m, "content") and "Python" in m.content for m in messages)

    @pytest.mark.asyncio
    async def test_ainvoke_no_results_no_injection(self) -> None:
        """When memory returns no results, input should pass through unchanged."""
        provider = FakeMemoryProvider([])
        inner = _make_mock_inner()
        agent = PromptiseAgent(inner=inner, memory_provider=provider)

        original_input = {"messages": [{"role": "user", "content": "Hi"}]}
        await agent.ainvoke(original_input)

        call_args = inner.ainvoke.call_args
        messages = call_args[0][0]["messages"]
        assert len(messages) == 1  # No injection

    @pytest.mark.asyncio
    async def test_auto_store_after_invocation(self) -> None:
        """When memory_auto_store=True, exchange should be stored."""
        provider = FakeMemoryProvider([])
        inner = _make_mock_inner()
        agent = PromptiseAgent(
            inner=inner,
            memory_provider=provider,
            memory_auto_store=True,
        )

        await agent.ainvoke(
            {
                "messages": [{"role": "user", "content": "What is Python?"}],
            }
        )

        assert len(provider.stored) == 1
        assert "User: What is Python?" in provider.stored[0]

    @pytest.mark.asyncio
    async def test_no_auto_store_by_default(self) -> None:
        """When memory_auto_store=False (default), nothing should be stored."""
        provider = FakeMemoryProvider([])
        inner = _make_mock_inner()
        agent = PromptiseAgent(inner=inner, memory_provider=provider)

        await agent.ainvoke(
            {
                "messages": [{"role": "user", "content": "Hello"}],
            }
        )

        assert len(provider.stored) == 0

    @pytest.mark.asyncio
    async def test_memory_search_timeout_graceful(self) -> None:
        """Memory search timeout should not crash the agent."""
        provider = FakeMemoryProvider([])

        # Make search hang
        async def slow_search(query, *, limit=5):
            await asyncio.sleep(10)
            return []

        provider.search = slow_search  # type: ignore[assignment]

        inner = _make_mock_inner()
        agent = PromptiseAgent(
            inner=inner,
            memory_provider=provider,
            memory_timeout=0.01,  # Very short timeout
        )

        # Should not raise — memory degrades gracefully
        await agent.ainvoke(
            {
                "messages": [{"role": "user", "content": "Hello"}],
            }
        )

        inner.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_memory_search_error_graceful(self) -> None:
        """Memory search error should not crash the agent."""
        provider = FakeMemoryProvider([])

        async def failing_search(query, *, limit=5):
            raise RuntimeError("DB connection failed")

        provider.search = failing_search  # type: ignore[assignment]

        inner = _make_mock_inner()
        agent = PromptiseAgent(inner=inner, memory_provider=provider)

        # Should not raise
        await agent.ainvoke(
            {
                "messages": [{"role": "user", "content": "Hello"}],
            }
        )

        inner.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_min_score_filtering(self) -> None:
        """Results below min_score should be filtered out."""
        provider = FakeMemoryProvider(
            [
                FakeMemoryResult("high relevance", score=0.9),
                FakeMemoryResult("low relevance", score=0.1),
            ]
        )
        inner = _make_mock_inner()
        agent = PromptiseAgent(
            inner=inner,
            memory_provider=provider,
            memory_min_score=0.5,
        )

        await agent.ainvoke(
            {
                "messages": [{"role": "user", "content": "test"}],
            }
        )

        # Should inject only the high-relevance result
        call_args = inner.ainvoke.call_args
        messages = call_args[0][0]["messages"]
        injected = [m for m in messages if hasattr(m, "content") and "relevance" in m.content]
        assert len(injected) == 1
        assert "high relevance" in injected[0].content
        assert "low relevance" not in injected[0].content


# ===========================================================================
# 2. Both observability + memory
# ===========================================================================


class TestPromptiseAgentBothFeatures:
    """Test observability and memory working together."""

    @pytest.mark.asyncio
    async def test_both_observe_and_memory(self) -> None:
        """Agent with both features should inject memory AND callbacks."""
        provider = FakeMemoryProvider(
            [
                FakeMemoryResult("remembered fact", score=0.8),
            ]
        )
        inner = _make_mock_inner()
        collector = ObservabilityCollector(session_name="both-test")
        handler = MagicMock(name="PromptiseCallbackHandler")

        agent = PromptiseAgent(
            inner=inner,
            handler=handler,
            collector=collector,
            observe_config=ObservabilityConfig(),
            memory_provider=provider,
        )

        await agent.ainvoke(
            {
                "messages": [{"role": "user", "content": "What do you remember?"}],
            }
        )

        # Memory was searched
        assert len(provider.search_calls) == 1

        # Callback was injected
        call_kwargs = inner.ainvoke.call_args[1]
        assert handler in call_kwargs["config"]["callbacks"]

        # Memory was injected into messages
        call_input = inner.ainvoke.call_args[0][0]
        assert len(call_input["messages"]) == 2  # user + system

        # Stats work
        assert isinstance(agent.get_stats(), dict)

    @pytest.mark.asyncio
    async def test_shutdown_cleans_all(self) -> None:
        """shutdown() should clean up both MCP, transporters, and memory."""
        inner = _make_mock_inner()
        mcp_multi = MagicMock()
        mcp_multi.__aexit__ = AsyncMock()
        transporter = MagicMock()
        transporter.flush = MagicMock()
        provider = FakeMemoryProvider([])
        provider.close = AsyncMock()

        agent = PromptiseAgent(
            inner=inner,
            mcp_multi=mcp_multi,
            transporters=[transporter],
            handler=MagicMock(),
            collector=ObservabilityCollector(session_name="shutdown-test"),
            memory_provider=provider,
        )

        await agent.shutdown()

        mcp_multi.__aexit__.assert_awaited_once()
        transporter.flush.assert_called_once()
        provider.close.assert_awaited_once()


# ===========================================================================
# 3. Shutdown lifecycle
# ===========================================================================


class TestPromptiseAgentShutdown:
    """Test shutdown for various feature combinations."""

    @pytest.mark.asyncio
    async def test_shutdown_mcp_only(self) -> None:
        inner = _make_mock_inner()
        mcp_multi = MagicMock()
        mcp_multi.__aexit__ = AsyncMock()
        agent = PromptiseAgent(inner=inner, mcp_multi=mcp_multi)

        await agent.shutdown()

        mcp_multi.__aexit__.assert_awaited_once()
        assert agent._mcp_multi is None

    @pytest.mark.asyncio
    async def test_shutdown_transporters_only(self) -> None:
        inner = _make_mock_inner()
        t1 = MagicMock()
        t1.flush = MagicMock()
        agent = PromptiseAgent(inner=inner, transporters=[t1])

        await agent.shutdown()

        t1.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_tolerates_mcp_error(self) -> None:
        inner = _make_mock_inner()
        mcp_multi = MagicMock()
        mcp_multi.__aexit__ = AsyncMock(side_effect=RuntimeError("connection lost"))
        agent = PromptiseAgent(inner=inner, mcp_multi=mcp_multi)

        await agent.shutdown()  # Should not raise
        assert agent._mcp_multi is None

    @pytest.mark.asyncio
    async def test_shutdown_tolerates_transporter_error(self) -> None:
        inner = _make_mock_inner()
        t1 = MagicMock()
        t1.flush = MagicMock(side_effect=RuntimeError("flush failed"))
        agent = PromptiseAgent(inner=inner, transporters=[t1])

        await agent.shutdown()  # Should not raise

    @pytest.mark.asyncio
    async def test_shutdown_async_flush(self) -> None:
        inner = _make_mock_inner()
        t1 = MagicMock()
        t1.flush = AsyncMock()
        agent = PromptiseAgent(inner=inner, transporters=[t1])

        await agent.shutdown()

        t1.flush.assert_awaited_once()


# ===========================================================================
# 4. Getattr passthrough
# ===========================================================================


class TestPromptiseAgentGetattr:
    """PromptiseAgent.__getattr__ proxies to inner graph."""

    def test_proxies_to_inner(self) -> None:
        inner = _make_mock_inner()
        agent = PromptiseAgent(inner=inner)
        assert agent.some_custom_attr == "hello"

    def test_raises_for_missing(self) -> None:
        inner = MagicMock(spec=[])
        agent = PromptiseAgent(inner=inner)
        with pytest.raises(AttributeError):
            _ = agent.nonexistent_attribute
