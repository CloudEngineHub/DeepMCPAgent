"""Prompt version registry.

Provides a singleton :class:`PromptRegistry` for registering, retrieving,
and rolling back prompt versions.

Example::

    from promptise.prompts.registry import registry, version

    @version("1.0.0")
    @prompt(model="openai:gpt-5-mini")
    async def summarize(text: str) -> str:
        \"""Summarize: {text}\"""

    # Later, register a new version
    @version("2.0.0")
    @prompt(model="openai:gpt-4o")
    async def summarize(text: str) -> str:
        \"""Provide a concise summary: {text}\"""

    # Retrieve a specific version
    v1 = registry.get("summarize", "1.0.0")

    # Rollback to previous
    registry.rollback("summarize")
"""

from __future__ import annotations

from typing import Any

from .core import Prompt

__all__ = ["PromptRegistry", "registry", "version"]


class PromptRegistry:
    """Singleton registry for versioned prompts.

    Stores prompts keyed by ``(name, version)`` with a latest pointer
    for each name.  Supports rollback to previous versions.
    """

    def __init__(self) -> None:
        # {name: [(version, Prompt), ...]} — ordered by registration
        self._store: dict[str, list[tuple[str, Prompt]]] = {}

    def register(self, name: str, ver: str, p: Prompt) -> None:
        """Register a prompt under *name* at version *ver*.

        Args:
            name: Prompt name.
            ver: Semantic version string (e.g. ``"1.0.0"``).
            p: The :class:`Prompt` instance.
        """
        if name not in self._store:
            self._store[name] = []
        # Check for duplicate version
        for existing_ver, _ in self._store[name]:
            if existing_ver == ver:
                raise ValueError(f"Prompt {name!r} version {ver!r} already registered")
        self._store[name].append((ver, p))

    def get(self, name: str, ver: str | None = None) -> Prompt:
        """Retrieve a prompt by name and optional version.

        Args:
            name: Prompt name.
            ver: Version string.  When ``None``, returns the latest.

        Returns:
            The registered :class:`Prompt`.

        Raises:
            KeyError: Prompt or version not found.
        """
        if name not in self._store or not self._store[name]:
            raise KeyError(f"Prompt {name!r} not found in registry")

        entries = self._store[name]
        if ver is None:
            return entries[-1][1]

        for v, p in entries:
            if v == ver:
                return p
        available = [v for v, _ in entries]
        raise KeyError(f"Version {ver!r} not found for prompt {name!r}. Available: {available}")

    def latest_version(self, name: str) -> str:
        """Return the latest version string for a prompt.

        Raises:
            KeyError: Prompt not found.
        """
        if name not in self._store or not self._store[name]:
            raise KeyError(f"Prompt {name!r} not found in registry")
        return self._store[name][-1][0]

    def rollback(self, name: str) -> Prompt:
        """Remove the latest version and return the new latest.

        Raises:
            KeyError: Prompt not found or only one version exists.
        """
        if name not in self._store or not self._store[name]:
            raise KeyError(f"Prompt {name!r} not found in registry")
        if len(self._store[name]) < 2:
            raise KeyError(f"Cannot rollback {name!r}: only one version registered")
        self._store[name].pop()
        return self._store[name][-1][1]

    def list(self) -> dict[str, list[str]]:
        """List all registered prompts and their versions.

        Returns:
            Dict mapping prompt names to lists of version strings.
        """
        return {name: [v for v, _ in entries] for name, entries in self._store.items()}

    def clear(self) -> None:
        """Remove all registered prompts."""
        self._store.clear()

    def __repr__(self) -> str:
        count = sum(len(entries) for entries in self._store.values())
        return f"<PromptRegistry prompts={len(self._store)} versions={count}>"


# Module-level singleton
registry = PromptRegistry()


def version(ver: str) -> Any:
    """Decorator that registers a :class:`Prompt` in the global registry.

    Usage::

        @version("1.0.0")
        @prompt(model="openai:gpt-5-mini")
        async def summarize(text: str) -> str:
            \"""Summarize: {text}\"""

    Args:
        ver: Semantic version string.

    Returns:
        Decorator that registers and returns the Prompt.
    """

    def decorator(prompt_or_func: Any) -> Any:
        if isinstance(prompt_or_func, Prompt):
            registry.register(prompt_or_func.name, ver, prompt_or_func)
            return prompt_or_func
        # Applied before @prompt — store version for later
        if not hasattr(prompt_or_func, "_pending_version"):
            prompt_or_func._pending_version = ver
        return prompt_or_func

    return decorator
