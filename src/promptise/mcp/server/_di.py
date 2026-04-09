"""Dependency injection for MCP server handlers.

Provides FastAPI-style ``Depends()`` for injecting shared resources
(database connections, HTTP clients, config) into tool handlers.

Example::

    async def get_db():
        db = await Database.connect()
        try:
            yield db
        finally:
            await db.close()

    @server.tool()
    async def query(sql: str, db: Database = Depends(get_db)) -> list:
        return await db.execute(sql)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncGenerator, Generator
from typing import Any

logger = logging.getLogger(__name__)

from ._decorators import _DependsMarker


def Depends(dependency: Any, *, use_cache: bool = True) -> Any:
    """Mark a parameter for dependency injection.

    Args:
        dependency: A callable (sync or async) or generator that provides
            the dependency value.
        use_cache: If ``True``, cache the resolved value per-request so
            the same dependency is only created once.

    Returns:
        A marker that the framework detects at call time.
    """
    return _DependsMarker(dependency, use_cache=use_cache)


class DependencyResolver:
    """Resolve ``Depends()`` markers in a handler's signature at call time.

    Supports:
    - Plain callables (sync/async)
    - Sync generators (``yield`` — cleanup after handler)
    - Async generators (``async yield`` — cleanup after handler)
    - Request-scoped caching (same dep resolved once per request)
    """

    def __init__(self) -> None:
        self._cache: dict[int, Any] = {}
        self._generators: list[Any] = []

    async def resolve(
        self,
        func: Any,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Return *arguments* with ``Depends()`` markers resolved to values."""
        sig = inspect.signature(func)
        resolved = dict(arguments)

        for name, param in sig.parameters.items():
            if not isinstance(param.default, _DependsMarker):
                continue
            marker = param.default
            value = await self._resolve_one(marker)
            resolved[name] = value

        return resolved

    async def cleanup(self) -> None:
        """Run cleanup for all generator-based dependencies (in reverse order)."""
        for gen in reversed(self._generators):
            try:
                if isinstance(gen, AsyncGenerator):
                    try:
                        await gen.__anext__()
                    except StopAsyncIteration:
                        pass
                elif isinstance(gen, Generator):
                    try:
                        next(gen)
                    except StopIteration:
                        pass
            except Exception:
                logger.debug("Dependency cleanup error", exc_info=True)
        self._generators.clear()
        self._cache.clear()

    async def _resolve_one(self, marker: _DependsMarker) -> Any:
        """Resolve a single dependency."""
        cache_key = id(marker.dependency)
        if marker.use_cache and cache_key in self._cache:
            return self._cache[cache_key]

        dep = marker.dependency
        value: Any

        if inspect.isasyncgenfunction(dep):
            gen = dep()
            value = await gen.__anext__()
            self._generators.append(gen)
        elif inspect.isgeneratorfunction(dep):
            gen = dep()
            value = next(gen)
            self._generators.append(gen)
        elif asyncio.iscoroutinefunction(dep):
            value = await dep()
        elif callable(dep):
            value = dep()
        else:
            value = dep

        if marker.use_cache:
            self._cache[cache_key] = value
        return value
