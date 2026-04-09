"""Observability bridge for the prompt framework.

:class:`PromptObserver` wraps :class:`~promptise.observability.ObservabilityCollector`
to record prompt-specific events: start, end, error, guard blocks,
and context provider usage.

Example::

    from promptise.prompts.observe import PromptObserver
    from promptise.observability import ObservabilityCollector

    collector = ObservabilityCollector()
    observer = PromptObserver(collector)

    # Observer is attached to prompts automatically when observe=True
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["PromptObserver"]


class PromptObserver:
    """Bridge between prompt execution and ObservabilityCollector.

    Records timeline events for prompt start, end, error, guard
    blocks, and context provider execution.

    Args:
        collector: An :class:`ObservabilityCollector` instance.
    """

    def __init__(self, collector: Any) -> None:
        self._collector = collector

    def record_start(
        self,
        prompt_name: str,
        model: str,
        input_text: str,
    ) -> None:
        """Record prompt execution start."""
        try:
            from ..observability import TimelineEventType

            self._collector.record_event(
                event_type=TimelineEventType.PROMPT_START,
                agent_id=prompt_name,
                data={
                    "prompt_name": prompt_name,
                    "model": model,
                    "input_length": len(input_text),
                },
            )
        except (ImportError, AttributeError) as exc:
            logger.debug("PromptObserver: failed to record event: %s", exc)

    def record_end(
        self,
        prompt_name: str,
        model: str,
        latency_ms: float,
        output_length: int,
    ) -> None:
        """Record prompt execution end."""
        try:
            from ..observability import TimelineEventType

            self._collector.record_event(
                event_type=TimelineEventType.PROMPT_END,
                agent_id=prompt_name,
                data={
                    "prompt_name": prompt_name,
                    "model": model,
                    "latency_ms": latency_ms,
                    "output_length": output_length,
                },
            )
        except (ImportError, AttributeError) as exc:
            logger.debug("PromptObserver: failed to record event: %s", exc)

    def record_error(
        self,
        prompt_name: str,
        error: Exception,
    ) -> None:
        """Record prompt execution error."""
        try:
            from ..observability import TimelineEventType

            self._collector.record_event(
                event_type=TimelineEventType.PROMPT_ERROR,
                agent_id=prompt_name,
                data={
                    "prompt_name": prompt_name,
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
            )
        except (ImportError, AttributeError) as exc:
            logger.debug("PromptObserver: failed to record event: %s", exc)

    def record_guard_block(
        self,
        prompt_name: str,
        guard_name: str,
        reason: str,
    ) -> None:
        """Record a guard blocking execution."""
        try:
            from ..observability import TimelineEventType

            self._collector.record_event(
                event_type=TimelineEventType.PROMPT_GUARD_BLOCK,
                agent_id=prompt_name,
                data={
                    "prompt_name": prompt_name,
                    "guard_name": guard_name,
                    "reason": reason,
                },
            )
        except (ImportError, AttributeError) as exc:
            logger.debug("PromptObserver: failed to record event: %s", exc)

    def record_context(
        self,
        prompt_name: str,
        provider_name: str,
        chars_injected: int,
    ) -> None:
        """Record context provider execution."""
        try:
            from ..observability import TimelineEventType

            self._collector.record_event(
                event_type=TimelineEventType.PROMPT_CONTEXT,
                agent_id=prompt_name,
                data={
                    "prompt_name": prompt_name,
                    "provider": provider_name,
                    "chars_injected": chars_injected,
                },
            )
        except (ImportError, AttributeError) as exc:
            logger.debug("PromptObserver: failed to record event: %s", exc)

    def __repr__(self) -> str:
        return f"<PromptObserver collector={self._collector!r}>"
