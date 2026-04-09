"""In-memory journal backend for testing.

Stores all entries in a list — no persistence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import JournalEntry


class InMemoryJournal:
    """In-memory journal for testing and development.

    All entries are stored in a Python list — nothing is persisted.

    Example::

        journal = InMemoryJournal()
        await journal.append(JournalEntry(process_id="p-1", entry_type="test"))
        entries = await journal.read("p-1")
        assert len(entries) == 1
    """

    def __init__(self) -> None:
        self._entries: list[JournalEntry] = []
        self._checkpoints: dict[str, dict[str, Any]] = {}

    async def append(self, entry: JournalEntry) -> None:
        """Append an entry."""
        self._entries.append(entry)

    async def read(
        self,
        process_id: str,
        *,
        since: datetime | None = None,
        entry_type: str | None = None,
        limit: int | None = None,
    ) -> list[JournalEntry]:
        """Read entries with optional filters."""
        results = [e for e in self._entries if e.process_id == process_id]
        if since is not None:
            results = [e for e in results if e.timestamp >= since]
        if entry_type is not None:
            results = [e for e in results if e.entry_type == entry_type]
        if limit is not None:
            results = results[:limit]
        return results

    async def checkpoint(self, process_id: str, state: dict[str, Any]) -> None:
        """Store a checkpoint."""
        self._checkpoints[process_id] = dict(state)
        # Also record as a journal entry
        await self.append(
            JournalEntry(
                process_id=process_id,
                entry_type="checkpoint",
                data={"state": state},
            )
        )

    async def last_checkpoint(self, process_id: str) -> dict[str, Any] | None:
        """Return the latest checkpoint."""
        return self._checkpoints.get(process_id)

    async def close(self) -> None:
        """No-op for in-memory backend."""

    def __repr__(self) -> str:
        return f"InMemoryJournal(entries={len(self._entries)})"
