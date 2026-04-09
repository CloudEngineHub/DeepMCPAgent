"""Prompt guardrails — input/output validation for LLM calls.

Guards are runtime enforcers applied before and after LLM calls.
Implement the :class:`Guard` protocol to create custom guardrails.

Example::

    from promptise.prompts import prompt, guard
    from promptise.prompts.guards import content_filter, length

    @prompt(model="openai:gpt-5-mini")
    @guard(content_filter(blocked=["secret"]), length(max_length=2000))
    async def analyze(text: str) -> str:
        \"\"\"Analyze: {text}\"\"\"
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Guard",
    "GuardError",
    "ContentFilterGuard",
    "SchemaStrictGuard",
    "LengthGuard",
    "InputValidatorGuard",
    "OutputValidatorGuard",
    "guard",
    "content_filter",
    "schema_strict",
    "length",
    "input_validator",
    "output_validator",
]


# ---------------------------------------------------------------------------
# Guard protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Guard(Protocol):
    """Protocol for prompt input/output guards.

    Implement ``check_input`` and ``check_output`` to create a custom
    guardrail.  Return the (possibly transformed) value to pass, or
    raise :class:`GuardError` to reject.
    """

    async def check_input(self, input_text: str) -> str:
        """Validate/transform input text before the LLM call."""
        ...

    async def check_output(self, output: Any) -> Any:
        """Validate/transform output after the LLM call."""
        ...


class GuardError(Exception):
    """Raised when a guard rejects input or output."""

    def __init__(self, message: str, guard_name: str = "") -> None:
        super().__init__(message)
        self.guard_name = guard_name
        self.reason = message


# ---------------------------------------------------------------------------
# Built-in guards
# ---------------------------------------------------------------------------


class ContentFilterGuard:
    """Block or require specific words in input/output.

    Args:
        blocked: Words that must NOT appear (case-insensitive).
        required: Words that MUST appear in output (case-insensitive).
    """

    def __init__(
        self,
        blocked: list[str] | None = None,
        required: list[str] | None = None,
    ) -> None:
        self._blocked = [w.lower() for w in (blocked or [])]
        self._required = [w.lower() for w in (required or [])]

    async def check_input(self, input_text: str) -> str:
        lower = input_text.lower()
        for word in self._blocked:
            if word in lower:
                raise GuardError(
                    f"Input contains blocked word: {word!r}",
                    guard_name="content_filter",
                )
        return input_text

    async def check_output(self, output: Any) -> Any:
        if not isinstance(output, str):
            return output
        lower = output.lower()
        for word in self._blocked:
            if word in lower:
                raise GuardError(
                    f"Output contains blocked word: {word!r}",
                    guard_name="content_filter",
                )
        for word in self._required:
            if word not in lower:
                raise GuardError(
                    f"Output missing required word: {word!r}",
                    guard_name="content_filter",
                )
        return output


class SchemaStrictGuard:
    """Validate that LLM output is well-formed JSON.

    Parses the output as JSON and raises :class:`GuardError` if
    parsing fails.  Non-string outputs are passed through unchanged.
    """

    async def check_input(self, input_text: str) -> str:
        return input_text

    async def check_output(self, output: Any) -> Any:
        if not isinstance(output, str):
            return output
        import json

        try:
            json.loads(output)
        except (json.JSONDecodeError, ValueError) as exc:
            raise GuardError(
                f"Output is not valid JSON: {exc}",
                guard_name="schema_strict",
            ) from exc
        return output


class LengthGuard:
    """Enforce output character length bounds."""

    def __init__(
        self,
        min_length: int | None = None,
        max_length: int | None = None,
    ) -> None:
        self._min = min_length
        self._max = max_length

    async def check_input(self, input_text: str) -> str:
        return input_text

    async def check_output(self, output: Any) -> Any:
        if not isinstance(output, str):
            return output
        length = len(output)
        if self._min is not None and length < self._min:
            raise GuardError(
                f"Output too short: {length} chars (min {self._min})",
                guard_name="length",
            )
        if self._max is not None and length > self._max:
            raise GuardError(
                f"Output too long: {length} chars (max {self._max})",
                guard_name="length",
            )
        return output


class InputValidatorGuard:
    """Wrap any callable as an input validator.

    The callable receives the input text and must return the
    (possibly transformed) text, or raise an exception to reject.
    """

    def __init__(self, fn: Callable[..., Any]) -> None:
        self._fn = fn
        self._is_async = inspect.iscoroutinefunction(fn)

    async def check_input(self, input_text: str) -> str:
        if self._is_async:
            return await self._fn(input_text)
        return self._fn(input_text)

    async def check_output(self, output: Any) -> Any:
        return output


class OutputValidatorGuard:
    """Wrap any callable as an output validator.

    The callable receives the output and must return the
    (possibly transformed) value, or raise an exception to reject.
    """

    def __init__(self, fn: Callable[..., Any]) -> None:
        self._fn = fn
        self._is_async = inspect.iscoroutinefunction(fn)

    async def check_input(self, input_text: str) -> str:
        return input_text

    async def check_output(self, output: Any) -> Any:
        if self._is_async:
            return await self._fn(output)
        return self._fn(output)


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


def content_filter(
    blocked: list[str] | None = None,
    required: list[str] | None = None,
) -> ContentFilterGuard:
    """Create a :class:`ContentFilterGuard`."""
    return ContentFilterGuard(blocked=blocked, required=required)


def schema_strict() -> SchemaStrictGuard:
    """Create a :class:`SchemaStrictGuard`."""
    return SchemaStrictGuard()


def length(
    min_length: int | None = None,
    max_length: int | None = None,
) -> LengthGuard:
    """Create a :class:`LengthGuard`."""
    return LengthGuard(min_length=min_length, max_length=max_length)


def input_validator(fn: Callable[..., Any]) -> InputValidatorGuard:
    """Create an :class:`InputValidatorGuard` from any callable."""
    return InputValidatorGuard(fn)


def output_validator(fn: Callable[..., Any]) -> OutputValidatorGuard:
    """Create an :class:`OutputValidatorGuard` from any callable."""
    return OutputValidatorGuard(fn)


# ---------------------------------------------------------------------------
# @guard decorator
# ---------------------------------------------------------------------------


def guard(*guards: Guard) -> Callable[..., Any]:
    """Decorator that attaches guards to a :class:`Prompt`.

    Usage::

        @prompt(model="openai:gpt-5-mini")
        @guard(content_filter(blocked=["secret"]), length(max_length=2000))
        async def analyze(text: str) -> str:
            \"\"\"Analyze: {text}\"\"\"
    """

    def decorator(prompt_or_func: Any) -> Any:
        # If applied to a Prompt object, attach directly
        if hasattr(prompt_or_func, "_input_guards"):
            for g in guards:
                prompt_or_func._input_guards.append(g)
                prompt_or_func._output_guards.append(g)
            return prompt_or_func
        # If applied to a plain function (before @prompt), store as attr
        if not hasattr(prompt_or_func, "_pending_guards"):
            prompt_or_func._pending_guards = []
        prompt_or_func._pending_guards.extend(guards)
        return prompt_or_func

    return decorator
