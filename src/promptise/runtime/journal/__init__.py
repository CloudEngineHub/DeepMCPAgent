"""Journal system for durable process audit logging.

The journal records process events (state transitions, trigger firings,
invocation results, checkpoints) for crash recovery and observability.

Available backends:

* :class:`InMemoryJournal` — in-memory, for testing
* :class:`FileJournal` — append-only JSONL files, one per process

:class:`RedactingJournal` wraps either one and redacts secret values
before anything is written.
"""

from __future__ import annotations

from .base import JournalEntry, JournalLevel, JournalProvider
from .file import FileJournal
from .memory import InMemoryJournal
from .redact import RedactingJournal
from .replay import ReplayEngine
from .rewind import RewindEngine, RewindMode, RewindPlan, RewindResult

__all__ = [
    "JournalProvider",
    "JournalEntry",
    "JournalLevel",
    "FileJournal",
    "InMemoryJournal",
    "RedactingJournal",
    "ReplayEngine",
    "RewindEngine",
    "RewindMode",
    "RewindPlan",
    "RewindResult",
]
