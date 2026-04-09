"""Replay engine: reconstruct process state from journal.

Used for crash recovery — reads the journal, finds the last checkpoint,
and replays subsequent entries to rebuild the process state.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import JournalEntry, JournalProvider

logger = logging.getLogger(__name__)


class ReplayEngine:
    """Replays journal entries to reconstruct process state.

    Args:
        journal: :class:`JournalProvider` to read from.

    Example::

        engine = ReplayEngine(journal)
        recovered = await engine.recover("process-1")
        # recovered = {
        #     "context_state": {...},
        #     "lifecycle_state": "running",
        #     "last_entry_type": "invocation_result",
        # }
    """

    def __init__(self, journal: JournalProvider) -> None:
        self._journal = journal

    async def recover(self, process_id: str) -> dict[str, Any]:
        """Recover process state from journal.

        1. Load the last checkpoint (if any).
        2. Read all entries after the checkpoint.
        3. Replay state transitions and context mutations.

        Args:
            process_id: Process to recover.

        Returns:
            Dict with ``context_state``, ``lifecycle_state``,
            ``last_entry_type``, and ``entries_replayed``.
        """
        # 1. Get last checkpoint
        checkpoint = await self._journal.last_checkpoint(process_id)
        context_state: dict[str, Any] = {}
        lifecycle_state: str = "created"

        if checkpoint:
            context_state = checkpoint.get("context_state", {})
            lifecycle_state = checkpoint.get("lifecycle_state", "running")
            logger.info(
                "Replay: loaded checkpoint for %s (state=%s)",
                process_id,
                lifecycle_state,
            )

        # 2. Read entries after checkpoint
        all_entries = await self._journal.read(process_id)

        # Find entries after the last checkpoint
        entries_to_replay: list[JournalEntry] = []
        found_checkpoint = checkpoint is None  # If no checkpoint, replay all
        for entry in all_entries:
            if entry.entry_type == "checkpoint" and not found_checkpoint:
                found_checkpoint = True
                entries_to_replay.clear()
                continue
            if found_checkpoint:
                entries_to_replay.append(entry)

        # 3. Replay
        last_entry_type = ""
        for entry in entries_to_replay:
            last_entry_type = entry.entry_type

            if entry.entry_type == "state_transition":
                new_state = entry.data.get("to_state", "")
                if new_state:
                    lifecycle_state = new_state

            elif entry.entry_type == "context_update":
                key = entry.data.get("key")
                value = entry.data.get("value")
                if key is not None:
                    context_state[key] = value

        logger.info(
            "Replay: replayed %d entries for %s (final state=%s)",
            len(entries_to_replay),
            process_id,
            lifecycle_state,
        )

        return {
            "context_state": context_state,
            "lifecycle_state": lifecycle_state,
            "last_entry_type": last_entry_type,
            "entries_replayed": len(entries_to_replay),
        }
