"""File-based journal: append-only JSONL.

Each process gets its own file: ``{base_path}/{process_id}.jsonl``.
Checkpoints are stored in a separate file:
``{base_path}/{process_id}.checkpoint.json``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..exceptions import JournalError
from .base import JournalEntry

logger = logging.getLogger(__name__)


class FileJournal:
    """JSONL-based journal for durable process audit logging.

    One JSONL file per process, append-only writes.  Checkpoints are
    stored separately in a JSON file.

    Args:
        base_path: Directory for journal files (created if missing).

    Example::

        journal = FileJournal("/tmp/journals")
        await journal.append(JournalEntry(
            process_id="p-1",
            entry_type="trigger_event",
            data={"trigger_type": "cron"},
        ))
        entries = await journal.read("p-1")
    """

    def __init__(self, base_path: str = ".promptise/journal") -> None:
        self._base_path = Path(base_path)
        self._base_path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sanitize_id(process_id: str) -> str:
        """Sanitize process_id for safe filesystem use."""
        # Reject null bytes and restrict to safe characters
        if "\x00" in process_id:
            raise ValueError("Null bytes not allowed in process_id")
        safe_id = process_id.replace("/", "_").replace("\\", "_")
        # Enforce max length to prevent filesystem issues
        if len(safe_id) > 200:
            safe_id = safe_id[:200]
        return safe_id

    def _journal_path(self, process_id: str) -> Path:
        """Return the JSONL file path for a process."""
        safe_id = self._sanitize_id(process_id)
        return self._base_path / f"{safe_id}.jsonl"

    def _checkpoint_path(self, process_id: str) -> Path:
        """Return the checkpoint file path for a process."""
        safe_id = self._sanitize_id(process_id)
        return self._base_path / f"{safe_id}.checkpoint.json"

    async def append(self, entry: JournalEntry) -> None:
        """Append an entry to the process's JSONL file."""
        path = self._journal_path(entry.process_id)
        try:
            line = json.dumps(entry.to_dict(), default=str)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            raise JournalError(f"Failed to write journal entry: {exc}") from exc

    async def read(
        self,
        process_id: str,
        *,
        since: datetime | None = None,
        entry_type: str | None = None,
        limit: int | None = None,
    ) -> list[JournalEntry]:
        """Read entries from the process's JSONL file."""
        path = self._journal_path(process_id)
        if not path.exists():
            return []

        results: list[JournalEntry] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        entry = JournalEntry.from_dict(data)
                    except (json.JSONDecodeError, KeyError):
                        logger.warning("Skipping malformed journal line in %s", path)
                        continue

                    if since is not None and entry.timestamp < since:
                        continue
                    if entry_type is not None and entry.entry_type != entry_type:
                        continue
                    results.append(entry)
                    if limit is not None and len(results) >= limit:
                        break
        except OSError as exc:
            raise JournalError(f"Failed to read journal: {exc}") from exc

        return results

    async def checkpoint(self, process_id: str, state: dict[str, Any]) -> None:
        """Store a checkpoint and record it in the journal."""
        path = self._checkpoint_path(process_id)
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(state, f, default=str, indent=2)
        except OSError as exc:
            raise JournalError(f"Failed to write checkpoint: {exc}") from exc

        # Also append to journal
        await self.append(
            JournalEntry(
                process_id=process_id,
                entry_type="checkpoint",
                data={"state": state},
            )
        )

    async def last_checkpoint(self, process_id: str) -> dict[str, Any] | None:
        """Return the latest checkpoint from the checkpoint file."""
        path = self._checkpoint_path(process_id)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read checkpoint %s: %s", path, exc)
            return None

    async def close(self) -> None:
        """No-op — file handles are opened/closed per operation."""

    def __repr__(self) -> str:
        return f"FileJournal(path={self._base_path})"
