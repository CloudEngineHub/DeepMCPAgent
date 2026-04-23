"""RewindEngine — multi-granularity rollback over the journal.

Where :class:`ReplayEngine` reconstructs *current* state by replaying
forward from the last checkpoint, :class:`RewindEngine` lets the user
go *backward*: pick a point in journal history and rebuild the process
state as it was at that point. The rewind can be selective:

* ``RewindMode.BOTH`` — rewind the conversation buffer **and** any
  context state changes (full rollback).
* ``RewindMode.CONVERSATION_ONLY`` — restore the conversation buffer
  but keep current context state. Useful when an agent went off-rails
  in chat but you want to keep accumulated tool results / files.
* ``RewindMode.CODE_ONLY`` — restore context state and any "code" /
  "tool result" / "file" entries but keep the conversation buffer.
  Useful when you want to "undo" a tool call without losing the
  reasoning that led to it.
* ``RewindMode.SUMMARIZE`` — keep current state but inject a summary
  of the rewound interval into the conversation. Lets the agent
  remember it tried something without re-running it.
* ``RewindMode.CANCEL`` — preview what would happen and return without
  changing anything. Always safe.

A rewind is **non-destructive**: the original journal entries stay on
disk. The rewind itself is recorded as a ``rewind`` entry so the
history shows the rollback point and the choice that was made.

Example::

    rewind = RewindEngine(journal)

    # Preview first
    plan = await rewind.plan(
        process_id="support-bot",
        target_entry_id="entry-2025-10-23T15:42:11Z",
        mode=RewindMode.CANCEL,
    )
    print(plan.entries_affected, plan.summary)

    # Apply
    state = await rewind.apply(
        process_id="support-bot",
        target_entry_id="entry-2025-10-23T15:42:11Z",
        mode=RewindMode.CONVERSATION_ONLY,
        actor="user@acme.com",
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .base import JournalEntry, JournalProvider

logger = logging.getLogger(__name__)


class RewindMode(str, Enum):
    """Selectivity of a :meth:`RewindEngine.apply` operation."""

    #: Rewind both conversation history and context state. Full rollback.
    BOTH = "both"
    #: Rewind only the conversation buffer; keep tool results / state.
    CONVERSATION_ONLY = "conversation_only"
    #: Rewind only state / tool / code entries; keep conversation.
    CODE_ONLY = "code_only"
    #: Don't rewind — instead, inject a summary of the interval into
    #: the conversation as a system note.
    SUMMARIZE = "summarize"
    #: Dry-run: return the plan without changing anything.
    CANCEL = "cancel"


# Entry-type buckets the rewind engine recognizes. Anything not listed
# here is treated as "other" and rewound by BOTH only.
_CONVERSATION_TYPES = frozenset(
    {
        "user_prompt",
        "assistant_message",
        "conversation_turn",
    }
)

_CODE_TYPES = frozenset(
    {
        "tool_call",
        "tool_result",
        "file_write",
        "file_change",
        "code_execution",
        "context_update",
        "state_transition",
    }
)


@dataclass
class RewindPlan:
    """Result of :meth:`RewindEngine.plan` — preview without committing.

    Attributes:
        process_id: Process being rewound.
        target_entry_id: Journal entry the rewind targets (state will
            be reconstructed *as of just before* this entry).
        mode: Selected mode (echoed for clarity).
        entries_total: Number of entries in the full journal.
        entries_affected: Number of entries that *would be ignored* by
            the rewind (those after the target).
        conversation_entries_affected: Of those, how many are
            conversation-style.
        code_entries_affected: Of those, how many are code/state-style.
        target_timestamp: When the target entry was written.
        summary: Human-readable summary of the rewind plan.
    """

    process_id: str
    target_entry_id: str
    mode: RewindMode
    entries_total: int
    entries_affected: int
    conversation_entries_affected: int
    code_entries_affected: int
    target_timestamp: datetime | None
    summary: str = ""


@dataclass
class RewindResult:
    """State produced by :meth:`RewindEngine.apply`."""

    process_id: str
    target_entry_id: str
    mode: RewindMode
    context_state: dict[str, Any] = field(default_factory=dict)
    lifecycle_state: str = "running"
    conversation: list[dict[str, Any]] = field(default_factory=list)
    summary_note: str | None = None
    plan: RewindPlan | None = None
    rewind_entry_id: str | None = None
    applied_at: datetime | None = None


class RewindEngine:
    """Multi-granularity rewind over a journal.

    Build one of these per process or share a single instance across
    processes — it's stateless besides holding a journal reference.

    Args:
        journal: The :class:`JournalProvider` to read and write rewind
            markers to. The same journal used by the process.
    """

    def __init__(self, journal: JournalProvider) -> None:
        self._journal = journal

    # ------------------------------------------------------------------
    # Plan (dry-run)
    # ------------------------------------------------------------------

    async def plan(
        self,
        *,
        process_id: str,
        target_entry_id: str,
        mode: RewindMode = RewindMode.CANCEL,
    ) -> RewindPlan:
        """Compute what would happen for a given rewind, without applying.

        This is the preferred way to surface a rewind to a user before
        they confirm: build the plan, show entries_affected, then
        invoke :meth:`apply` once they pick a mode.

        Args:
            process_id: Which process to inspect.
            target_entry_id: Entry ID to roll back to. State will be
                rebuilt as of *just before* this entry.
            mode: Mode echoed back into the plan; defaults to
                ``CANCEL``.

        Returns:
            A :class:`RewindPlan` summarizing the impact.

        Raises:
            ValueError: If ``target_entry_id`` is not present in the
                journal for ``process_id``.
        """
        all_entries = await self._journal.read(process_id)
        target_idx = self._find_target_index(all_entries, target_entry_id)
        affected = all_entries[target_idx:]

        conv = sum(1 for e in affected if e.entry_type in _CONVERSATION_TYPES)
        code = sum(1 for e in affected if e.entry_type in _CODE_TYPES)
        target_ts = all_entries[target_idx].timestamp if affected else None

        if mode == RewindMode.SUMMARIZE:
            verb = "summarize without removing"
        elif mode == RewindMode.CANCEL:
            verb = "preview only"
        else:
            verb = f"rewind ({mode.value})"

        summary = (
            f"{verb}: {len(affected)} entr{'y' if len(affected) == 1 else 'ies'} "
            f"affected ({conv} conversation, {code} code/state)."
        )

        return RewindPlan(
            process_id=process_id,
            target_entry_id=target_entry_id,
            mode=mode,
            entries_total=len(all_entries),
            entries_affected=len(affected),
            conversation_entries_affected=conv,
            code_entries_affected=code,
            target_timestamp=target_ts,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    async def apply(
        self,
        *,
        process_id: str,
        target_entry_id: str,
        mode: RewindMode,
        actor: str = "system",
        record: bool = True,
    ) -> RewindResult:
        """Execute a rewind and return the new process state.

        The journal is **never edited or deleted** by this method. The
        original entries remain. A new ``rewind`` entry is appended
        recording the operation (unless ``record=False``).

        Args:
            process_id: Which process to rewind.
            target_entry_id: Entry to roll back to. The state is
                rebuilt by replaying entries up to but not including
                this one (according to ``mode``).
            mode: Selectivity of the rewind. See :class:`RewindMode`.
            actor: Who initiated the rewind — recorded in the rewind
                entry. Defaults to ``"system"``.
            record: If True (default), append a journal entry recording
                the rewind. Set False for tests / dry-runs that should
                leave no trace.

        Returns:
            A :class:`RewindResult` with the rebuilt context state,
            lifecycle state, conversation, and an optional summary
            note injected (for SUMMARIZE mode).
        """
        all_entries = await self._journal.read(process_id)
        target_idx = self._find_target_index(all_entries, target_entry_id)

        plan = await self.plan(
            process_id=process_id,
            target_entry_id=target_entry_id,
            mode=mode,
        )

        # CANCEL: dry-run only
        if mode == RewindMode.CANCEL:
            return RewindResult(
                process_id=process_id,
                target_entry_id=target_entry_id,
                mode=mode,
                plan=plan,
            )

        # Decide which historical entries to keep when rebuilding.
        kept = self._select_kept_entries(all_entries, target_idx, mode)

        context_state, lifecycle_state, conversation = self._reconstruct(kept)

        summary_note: str | None = None
        if mode == RewindMode.SUMMARIZE:
            summary_note = self._build_summary_note(all_entries[target_idx:])
            # Don't actually drop anything; rebuild from full history.
            kept = all_entries
            context_state, lifecycle_state, conversation = self._reconstruct(kept)
            conversation.append(
                {
                    "role": "system",
                    "content": (
                        "[rewind summary] " + summary_note
                        if summary_note
                        else "[rewind summary] (empty interval)"
                    ),
                }
            )

        applied_at = datetime.now(UTC)
        rewind_entry_id: str | None = None
        if record:
            rewind_entry_id = f"rewind-{int(applied_at.timestamp() * 1000)}"
            await self._journal.append(
                JournalEntry(
                    entry_id=rewind_entry_id,
                    process_id=process_id,
                    timestamp=applied_at,
                    entry_type="rewind",
                    data={
                        "target_entry_id": target_entry_id,
                        "mode": mode.value,
                        "actor": actor,
                        "entries_affected": plan.entries_affected,
                        "conversation_affected": plan.conversation_entries_affected,
                        "code_affected": plan.code_entries_affected,
                        "summary_note": summary_note,
                    },
                )
            )

        logger.info(
            "Rewind applied: process=%s mode=%s target=%s affected=%d actor=%s",
            process_id,
            mode.value,
            target_entry_id,
            plan.entries_affected,
            actor,
        )

        return RewindResult(
            process_id=process_id,
            target_entry_id=target_entry_id,
            mode=mode,
            context_state=context_state,
            lifecycle_state=lifecycle_state,
            conversation=conversation,
            summary_note=summary_note,
            plan=plan,
            rewind_entry_id=rewind_entry_id,
            applied_at=applied_at,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _find_target_index(entries: list[JournalEntry], target_entry_id: str) -> int:
        for i, e in enumerate(entries):
            if e.entry_id == target_entry_id:
                return i
        raise ValueError(f"target entry id {target_entry_id!r} not found in journal")

    @staticmethod
    def _select_kept_entries(
        entries: list[JournalEntry],
        target_idx: int,
        mode: RewindMode,
    ) -> list[JournalEntry]:
        before = entries[:target_idx]
        after = entries[target_idx:]

        if mode == RewindMode.BOTH:
            return list(before)

        if mode == RewindMode.CONVERSATION_ONLY:
            # Drop conversation entries after target; keep code entries.
            kept = list(before)
            kept.extend(e for e in after if e.entry_type not in _CONVERSATION_TYPES)
            return kept

        if mode == RewindMode.CODE_ONLY:
            # Drop code/state entries after target; keep conversation.
            kept = list(before)
            kept.extend(e for e in after if e.entry_type not in _CODE_TYPES)
            return kept

        # SUMMARIZE rebuilds from full history; CANCEL never reaches here.
        return list(entries)

    @staticmethod
    def _reconstruct(
        entries: list[JournalEntry],
    ) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
        """Replay a filtered entry list into (context_state, lifecycle, conversation)."""
        context_state: dict[str, Any] = {}
        lifecycle_state: str = "created"
        conversation: list[dict[str, Any]] = []

        for entry in entries:
            t = entry.entry_type
            d = entry.data

            if t == "checkpoint":
                # Checkpoints are full snapshots — they overwrite.
                if isinstance(d.get("context_state"), dict):
                    context_state = dict(d["context_state"])
                if isinstance(d.get("lifecycle_state"), str):
                    lifecycle_state = d["lifecycle_state"]
                if isinstance(d.get("conversation"), list):
                    conversation = list(d["conversation"])
                continue

            if t == "state_transition":
                new_state = d.get("to_state")
                if isinstance(new_state, str):
                    lifecycle_state = new_state
                continue

            if t == "context_update":
                key = d.get("key")
                if key is not None:
                    context_state[key] = d.get("value")
                continue

            if t == "user_prompt":
                content = d.get("prompt") or d.get("content") or ""
                conversation.append({"role": "user", "content": content})
                continue

            if t == "assistant_message":
                content = d.get("content") or ""
                conversation.append({"role": "assistant", "content": content})
                continue

            if t == "conversation_turn":
                role = d.get("role", "user")
                content = d.get("content", "")
                conversation.append({"role": role, "content": content})
                continue

            # Anything else (rewind markers, tool_call, lifecycle) we
            # leave alone — those are observability, not state.

        return context_state, lifecycle_state, conversation

    @staticmethod
    def _build_summary_note(after_entries: list[JournalEntry]) -> str:
        """Build a short human-readable summary of the rewound interval."""
        if not after_entries:
            return ""
        counts: dict[str, int] = {}
        for e in after_entries:
            counts[e.entry_type] = counts.get(e.entry_type, 0) + 1
        parts = ", ".join(f"{n} {t}" for t, n in sorted(counts.items()))
        first_ts = after_entries[0].timestamp.isoformat()
        last_ts = after_entries[-1].timestamp.isoformat()
        return (
            f"Skipped interval {first_ts} -> {last_ts} containing {parts}. "
            f"State and conversation were preserved as-is."
        )


__all__ = ["RewindEngine", "RewindMode", "RewindPlan", "RewindResult"]
