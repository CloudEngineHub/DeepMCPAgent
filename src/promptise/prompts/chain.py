"""Prompt composition operators.

Combinators for building multi-step prompt pipelines:

- :func:`chain` — sequential execution, output feeds next input
- :func:`parallel` — concurrent execution, returns dict of results
- :func:`branch` — conditional routing based on a predicate
- :func:`retry` — exponential backoff retry
- :func:`fallback` — try alternatives on failure

Context propagation: ``PromptContext.state`` carries data between steps.

Example::

    from promptise.prompts.chain import chain, parallel, retry

    pipeline = chain(extract_facts, analyze_facts, write_summary)
    result = await pipeline("raw text...")

    multi = parallel(sentiment=analyze_sentiment, topics=extract_topics)
    results = await multi("article text...")
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from .core import Prompt

__all__ = ["chain", "parallel", "branch", "retry", "fallback"]


# ---------------------------------------------------------------------------
# Chain — sequential composition
# ---------------------------------------------------------------------------


class _Chain:
    """Sequential prompt chain: output of each step feeds the next."""

    def __init__(self, prompts: list[Prompt]) -> None:
        self._prompts = prompts

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # First prompt gets the original input
        result = await self._prompts[0](*args, **kwargs)
        # Subsequent prompts receive previous output as first arg
        for p in self._prompts[1:]:
            if isinstance(result, str):
                result = await p(result)
            elif isinstance(result, dict):
                result = await p(**result)
            else:
                result = await p(str(result))
        return result

    def __repr__(self) -> str:
        names = " → ".join(p.name for p in self._prompts)
        return f"<Chain {names}>"


def chain(*prompts: Prompt) -> _Chain:
    """Create a sequential chain of prompts.

    Each prompt's output is passed as input to the next prompt.
    The final prompt's output is returned.

    Args:
        *prompts: Two or more :class:`Prompt` instances.

    Returns:
        Callable chain that executes prompts sequentially.
    """
    if len(prompts) < 2:
        raise ValueError("chain() requires at least 2 prompts")
    return _Chain(list(prompts))


# ---------------------------------------------------------------------------
# Parallel — concurrent composition
# ---------------------------------------------------------------------------


class _Parallel:
    """Concurrent prompt execution, returns dict of results."""

    def __init__(self, prompts: dict[str, Prompt]) -> None:
        self._prompts = prompts

    async def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        async def _run(name: str, p: Prompt) -> tuple[str, Any]:
            result = await p(*args, **kwargs)
            return name, result

        tasks = [_run(name, p) for name, p in self._prompts.items()]
        results = await asyncio.gather(*tasks)
        return dict(results)

    def __repr__(self) -> str:
        names = ", ".join(self._prompts.keys())
        return f"<Parallel {names}>"


def parallel(**prompts: Prompt) -> _Parallel:
    """Execute multiple prompts concurrently.

    All prompts receive the same input.  Returns a dict mapping
    prompt names to their results.

    Args:
        **prompts: Named :class:`Prompt` instances.

    Returns:
        Callable that executes all prompts concurrently.
    """
    if len(prompts) < 2:
        raise ValueError("parallel() requires at least 2 prompts")
    return _Parallel(prompts)


# ---------------------------------------------------------------------------
# Branch — conditional routing
# ---------------------------------------------------------------------------


class _Branch:
    """Conditional prompt routing."""

    def __init__(
        self,
        condition: Callable[..., str],
        routes: dict[str, Prompt],
        default: Prompt | None = None,
    ) -> None:
        self._condition = condition
        self._routes = routes
        self._default = default

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        key = self._condition(*args, **kwargs)
        target = self._routes.get(key, self._default)
        if target is None:
            raise ValueError(
                f"Branch condition returned {key!r} but no matching route "
                f"and no default. Available: {list(self._routes.keys())}"
            )
        return await target(*args, **kwargs)

    def __repr__(self) -> str:
        routes = ", ".join(self._routes.keys())
        return f"<Branch routes=[{routes}]>"


def branch(
    condition: Callable[..., str],
    routes: dict[str, Prompt],
    default: Prompt | None = None,
) -> _Branch:
    """Route to different prompts based on a condition.

    The *condition* callable receives the same arguments as the prompts
    and must return a string key matching one of the *routes*.

    Args:
        condition: Function that returns a route key.
        routes: Mapping of route keys to :class:`Prompt` instances.
        default: Fallback prompt when no route matches.

    Returns:
        Callable that routes to the appropriate prompt.
    """
    return _Branch(condition, routes, default)


# ---------------------------------------------------------------------------
# Retry — exponential backoff
# ---------------------------------------------------------------------------


class _Retry:
    """Retry a prompt with exponential backoff."""

    def __init__(
        self,
        target: Prompt,
        max_retries: int = 3,
        backoff: float = 1.0,
    ) -> None:
        self._target = target
        self._max_retries = max_retries
        self._backoff = backoff

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._target(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    wait = self._backoff * (2**attempt)
                    await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]

    def __repr__(self) -> str:
        return f"<Retry {self._target.name} max={self._max_retries} backoff={self._backoff}>"


def retry(
    target: Prompt,
    max_retries: int = 3,
    backoff: float = 1.0,
) -> _Retry:
    """Wrap a prompt with exponential backoff retry.

    Args:
        target: The :class:`Prompt` to retry.
        max_retries: Maximum number of retry attempts.
        backoff: Base backoff duration in seconds (doubles each attempt).

    Returns:
        Callable that retries on failure.
    """
    return _Retry(target, max_retries=max_retries, backoff=backoff)


# ---------------------------------------------------------------------------
# Fallback — try alternatives
# ---------------------------------------------------------------------------


class _Fallback:
    """Try prompts in order until one succeeds."""

    def __init__(self, prompts: list[Prompt]) -> None:
        self._prompts = prompts

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for p in self._prompts:
            try:
                return await p(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
        raise last_exc  # type: ignore[misc]

    def __repr__(self) -> str:
        names = " | ".join(p.name for p in self._prompts)
        return f"<Fallback {names}>"


def fallback(primary: Prompt, *alternatives: Prompt) -> _Fallback:
    """Try prompts in order until one succeeds.

    If the primary prompt fails, each alternative is tried in order.
    The first successful result is returned.

    Args:
        primary: The preferred :class:`Prompt`.
        *alternatives: Fallback prompts tried in order.

    Returns:
        Callable that tries prompts until success.
    """
    return _Fallback([primary, *alternatives])
