"""Test framework for prompt engineering.

Provides :class:`PromptTestCase` as a base class for testing prompts
with mocked LLM calls, context providers, and assertions.

Example::

    import pytest
    from promptise.prompts.testing import PromptTestCase

    class TestSentiment(PromptTestCase):
        prompt = analyze_sentiment

        async def test_positive(self):
            with self.mock_llm("positive"):
                result = await self.run_prompt("I love this!")
                self.assert_contains(result, "positive")

        async def test_with_context(self):
            with self.mock_context(
                user=UserContext(expertise_level="expert"),
            ):
                with self.mock_llm("expert analysis: positive"):
                    result = await self.run_prompt("Great product")
                    self.assert_contains(result, "expert")
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from .context import BaseContext
from .core import Prompt, PromptStats

__all__ = ["PromptTestCase"]


class PromptTestCase:
    """Base class for prompt tests.

    Set the ``prompt`` class attribute to the :class:`Prompt` under test.
    Use :meth:`mock_llm` and :meth:`mock_context` to isolate tests
    from real LLM calls and external context sources.

    Works with both ``unittest.TestCase`` and ``pytest`` patterns.

    Attributes:
        prompt: The :class:`Prompt` to test.
    """

    prompt: Prompt | None = None

    # ------------------------------------------------------------------
    # Running prompts
    # ------------------------------------------------------------------

    async def run_prompt(self, *args: Any, **kwargs: Any) -> Any:
        """Execute the prompt under test.

        Args:
            *args: Positional arguments passed to the prompt.
            **kwargs: Keyword arguments passed to the prompt.

        Returns:
            The prompt's output.

        Raises:
            ValueError: No prompt configured.
        """
        if self.prompt is None:
            raise ValueError("Set the 'prompt' class attribute to test")
        return await self.prompt(*args, **kwargs)

    async def run_with_stats(self, *args: Any, **kwargs: Any) -> tuple[Any, PromptStats | None]:
        """Execute the prompt and return both result and stats.

        Returns:
            Tuple of (result, PromptStats).
        """
        result = await self.run_prompt(*args, **kwargs)
        stats = self.prompt.last_stats if self.prompt else None
        return result, stats

    # ------------------------------------------------------------------
    # Mocking
    # ------------------------------------------------------------------

    @contextmanager
    def mock_llm(self, response: str) -> Generator[None, None, None]:
        """Mock the LLM call to return a fixed response.

        Args:
            response: The string response the LLM should return.
        """
        mock_msg = MagicMock()
        mock_msg.content = response

        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_msg)

        with patch(
            "promptise.prompts.core.init_chat_model",
            return_value=mock_model,
        ):
            yield

    @contextmanager
    def mock_context(
        self,
        **contexts: BaseContext,
    ) -> Generator[None, None, None]:
        """Temporarily add world contexts to the prompt.

        Args:
            **contexts: World contexts to inject (e.g.
                ``user=UserContext(...)``).
        """
        if self.prompt is None:
            raise ValueError("Set the 'prompt' class attribute to test")

        original_world = dict(self.prompt._world)
        self.prompt._world.update(contexts)
        try:
            yield
        finally:
            self.prompt._world = original_world

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------

    def assert_schema(self, result: Any, expected_type: type) -> None:
        """Assert the result matches the expected type.

        Works with dataclasses, Pydantic models, and basic types.
        """
        if not isinstance(result, expected_type):
            raise AssertionError(
                f"Expected {expected_type.__name__}, got {type(result).__name__}: {result!r}"
            )

    def assert_contains(self, result: Any, substring: str) -> None:
        """Assert the result (as string) contains a substring."""
        text = str(result)
        if substring not in text:
            raise AssertionError(f"Expected result to contain {substring!r}, got: {text[:200]!r}")

    def assert_not_contains(self, result: Any, substring: str) -> None:
        """Assert the result does NOT contain a substring."""
        text = str(result)
        if substring in text:
            raise AssertionError(
                f"Expected result NOT to contain {substring!r}, but it was found in: {text[:200]!r}"
            )

    def assert_latency(
        self,
        stats: PromptStats | None,
        max_ms: float,
    ) -> None:
        """Assert the call latency is within limit."""
        if stats is None:
            raise AssertionError("No stats available")
        if stats.latency_ms > max_ms:
            raise AssertionError(f"Latency {stats.latency_ms:.0f}ms exceeds limit {max_ms:.0f}ms")

    def assert_context_provided(
        self,
        stats: PromptStats | None,
        provider_name: str,
    ) -> None:
        """Assert a specific context provider was used."""
        if stats is None:
            raise AssertionError("No stats available")
        providers = stats.context_providers_used or []
        if provider_name not in providers:
            raise AssertionError(f"Provider {provider_name!r} not in used providers: {providers}")

    def assert_guard_passed(
        self,
        result: Any,
    ) -> None:
        """Assert the result was not blocked by a guard.

        Checks that the result is not None (guards raise exceptions,
        so a non-None result means all guards passed).
        """
        if result is None:
            raise AssertionError("Result is None — may have been blocked")
