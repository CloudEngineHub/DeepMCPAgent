"""Journal protocol and entry types.

The journal system records process events for crash recovery and
observability.  Three detail levels are supported:

* ``none`` — no journaling (fire-and-forget processes).
* ``checkpoint`` — snapshot state after each trigger→invoke→result cycle.
* ``full`` — log every side effect (tool calls, LLM responses, state
  mutations).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4


class JournalLevel(str, Enum):
    """Journal detail level."""

    NONE = "none"
    CHECKPOINT = "checkpoint"
    FULL = "full"


@dataclass
class JournalEntry:
    """Single journal record.

    Attributes:
        entry_id: Unique entry ID.
        process_id: Owning process.
        timestamp: When the entry was recorded (timezone.utc).
        entry_type: Type of entry (``state_transition``,
            ``trigger_event``, ``invocation_start``,
            ``invocation_result``, ``checkpoint``, ``error``).
        data: Entry payload.
    """

    entry_id: str = field(default_factory=lambda: str(uuid4()))
    process_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    entry_type: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "entry_id": self.entry_id,
            "process_id": self.process_id,
            "timestamp": self.timestamp.isoformat(),
            "entry_type": self.entry_type,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JournalEntry:
        """Deserialize from a dict."""
        return cls(
            entry_id=data.get("entry_id", str(uuid4())),
            process_id=data.get("process_id", ""),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            entry_type=data.get("entry_type", ""),
            data=data.get("data", {}),
        )


@runtime_checkable
class JournalProvider(Protocol):
    """Protocol for journal backends."""

    async def append(self, entry: JournalEntry) -> None:
        """Append an entry to the journal."""
        ...

    async def read(
        self,
        process_id: str,
        *,
        since: datetime | None = None,
        entry_type: str | None = None,
        limit: int | None = None,
    ) -> list[JournalEntry]:
        """Read entries for a process with optional filters."""
        ...

    async def checkpoint(self, process_id: str, state: dict[str, Any]) -> None:
        """Store a full state checkpoint."""
        ...

    async def last_checkpoint(self, process_id: str) -> dict[str, Any] | None:
        """Return the most recent checkpoint state, or None."""
        ...

    async def close(self) -> None:
        """Release any resources."""
        ...
