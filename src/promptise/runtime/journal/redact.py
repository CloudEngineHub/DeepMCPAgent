"""Journal wrapper that keeps secret values out of every entry.

Wrap the process journal with :class:`RedactingJournal` when secret
scoping is enabled: every string in an entry's ``data`` and in a
checkpoint's state passes through the redactor (normally
:meth:`~promptise.runtime.secrets.SecretScope.sanitize`) before it reaches
the backend, so tool results, invocation results and the conversation
snapshot never store a secret in plain text.

Example::

    journal = RedactingJournal(FileJournal(".promptise/journal"), scope.sanitize)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

from .base import JournalEntry, JournalProvider


class RedactingJournal:
    """A :class:`JournalProvider` that redacts before it writes.

    Args:
        inner: The journal that stores the entries.
        redact: Callable that returns a redacted copy of a (nested) value,
            such as :meth:`SecretScope.sanitize`.  It must not modify its
            input.
    """

    def __init__(self, inner: JournalProvider, redact: Callable[[Any], Any]) -> None:
        self._inner = inner
        self._redact = redact

    @property
    def inner(self) -> JournalProvider:
        """The wrapped journal."""
        return self._inner

    async def append(self, entry: JournalEntry) -> None:
        """Append a redacted copy of *entry*."""
        await self._inner.append(replace(entry, data=self._redact(entry.data)))

    async def read(
        self,
        process_id: str,
        *,
        since: datetime | None = None,
        entry_type: str | None = None,
        limit: int | None = None,
    ) -> list[JournalEntry]:
        """Read entries from the wrapped journal."""
        return await self._inner.read(process_id, since=since, entry_type=entry_type, limit=limit)

    async def checkpoint(self, process_id: str, state: dict[str, Any]) -> None:
        """Store a redacted copy of *state*."""
        await self._inner.checkpoint(process_id, self._redact(state))

    async def last_checkpoint(self, process_id: str) -> dict[str, Any] | None:
        """Return the latest checkpoint of the wrapped journal."""
        return await self._inner.last_checkpoint(process_id)

    async def close(self) -> None:
        """Close the wrapped journal."""
        await self._inner.close()

    def __getattr__(self, name: str) -> Any:
        if name == "_inner":
            raise AttributeError(name)
        # Backend extras (e.g. InMemoryJournal._entries in tests).
        return getattr(self._inner, name)

    def __repr__(self) -> str:
        return f"RedactingJournal({self._inner!r})"
