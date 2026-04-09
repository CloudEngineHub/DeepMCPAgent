"""Journal system for durable process audit logging.

The journal records process events (state transitions, trigger firings,
invocation results, checkpoints) for crash recovery and observability.

Available backends:

* :class:`InMemoryJournal` — in-memory, for testing
* :class:`FileJournal` — append-only JSONL files, one per process
"""

from __future__ import annotations

from .base import JournalEntry, JournalLevel, JournalProvider
from .file import FileJournal
from .memory import InMemoryJournal
from .replay import ReplayEngine
from .rewind import RewindEngine, RewindMode, RewindPlan, RewindResult

__all__ = [
    "JournalProvider",
    "JournalEntry",
    "JournalLevel",
    "FileJournal",
    "InMemoryJournal",
    "ReplayEngine",
    "RewindEngine",
    "RewindMode",
    "RewindPlan",
    "RewindResult",
]
