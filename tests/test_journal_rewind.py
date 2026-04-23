"""Tests for the multi-granularity RewindEngine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from promptise.runtime.journal import (
    InMemoryJournal,
    JournalEntry,
    RewindEngine,
    RewindMode,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    entry_id: str,
    entry_type: str,
    *,
    process_id: str = "p1",
    seconds_ago: int = 0,
    **data,
) -> JournalEntry:
    return JournalEntry(
        entry_id=entry_id,
        process_id=process_id,
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago),
        entry_type=entry_type,
        data=data,
    )


async def _seed(journal: InMemoryJournal) -> list[JournalEntry]:
    """Seed a journal with a mix of conversation and code entries."""
    entries = [
        _entry("e1", "user_prompt", prompt="hi", seconds_ago=600),
        _entry("e2", "assistant_message", content="hello", seconds_ago=590),
        _entry("e3", "context_update", key="counter", value=1, seconds_ago=580),
        _entry("e4", "user_prompt", prompt="how are you?", seconds_ago=570),
        _entry("e5", "assistant_message", content="great", seconds_ago=560),
        _entry("e6", "tool_call", tool="search", seconds_ago=550),
        _entry("e7", "context_update", key="counter", value=2, seconds_ago=540),
        _entry("e8", "user_prompt", prompt="LATER", seconds_ago=530),
    ]
    for e in entries:
        await journal.append(e)
    return entries


# ---------------------------------------------------------------------------
# plan()
# ---------------------------------------------------------------------------


class TestPlan:
    @pytest.mark.asyncio
    async def test_plan_counts_affected_entries(self):
        journal = InMemoryJournal()
        await _seed(journal)
        rewind = RewindEngine(journal)

        plan = await rewind.plan(
            process_id="p1",
            target_entry_id="e6",  # rewind to before tool_call
            mode=RewindMode.BOTH,
        )
        assert plan.entries_total == 8
        assert plan.entries_affected == 3  # e6, e7, e8
        # e6=tool_call (code), e7=context_update (code), e8=user_prompt (conv)
        assert plan.code_entries_affected == 2
        assert plan.conversation_entries_affected == 1
        assert plan.target_entry_id == "e6"

    @pytest.mark.asyncio
    async def test_plan_target_not_found_raises(self):
        journal = InMemoryJournal()
        await _seed(journal)
        rewind = RewindEngine(journal)

        with pytest.raises(ValueError, match="not found"):
            await rewind.plan(process_id="p1", target_entry_id="missing")

    @pytest.mark.asyncio
    async def test_plan_summary_string(self):
        journal = InMemoryJournal()
        await _seed(journal)
        rewind = RewindEngine(journal)
        plan = await rewind.plan(
            process_id="p1",
            target_entry_id="e8",
            mode=RewindMode.CONVERSATION_ONLY,
        )
        assert "rewind" in plan.summary
        assert "1 entr" in plan.summary  # only e8 affected


# ---------------------------------------------------------------------------
# apply() — modes
# ---------------------------------------------------------------------------


class TestApplyBoth:
    @pytest.mark.asyncio
    async def test_full_rewind_drops_everything_after_target(self):
        journal = InMemoryJournal()
        await _seed(journal)
        rewind = RewindEngine(journal)

        result = await rewind.apply(
            process_id="p1",
            target_entry_id="e4",
            mode=RewindMode.BOTH,
            record=False,
        )

        # Conversation should only contain e1, e2 (e3 is context, e4 dropped)
        assert len(result.conversation) == 2
        assert result.conversation[0]["role"] == "user"
        assert result.conversation[0]["content"] == "hi"
        assert result.conversation[1]["role"] == "assistant"
        # context counter never updated past e3=1
        assert result.context_state == {"counter": 1}


class TestApplyConversationOnly:
    @pytest.mark.asyncio
    async def test_keeps_context_drops_conversation_after_target(self):
        journal = InMemoryJournal()
        await _seed(journal)
        rewind = RewindEngine(journal)

        result = await rewind.apply(
            process_id="p1",
            target_entry_id="e4",
            mode=RewindMode.CONVERSATION_ONLY,
            record=False,
        )

        # Conversation: e1 (user), e2 (assistant) only
        assert len(result.conversation) == 2
        # Context should still reflect e7 update (counter=2) because
        # we kept code entries after target
        assert result.context_state == {"counter": 2}


class TestApplyCodeOnly:
    @pytest.mark.asyncio
    async def test_keeps_conversation_drops_context_after_target(self):
        journal = InMemoryJournal()
        await _seed(journal)
        rewind = RewindEngine(journal)

        result = await rewind.apply(
            process_id="p1",
            target_entry_id="e4",
            mode=RewindMode.CODE_ONLY,
            record=False,
        )

        # Conversation should include e1, e2, e4, e5, e8
        [m["role"] for m in result.conversation]
        contents = [m["content"] for m in result.conversation]
        assert "LATER" in contents
        assert "hi" in contents
        # Counter should be reset to e3 value (1)
        assert result.context_state == {"counter": 1}


class TestApplySummarize:
    @pytest.mark.asyncio
    async def test_summarize_keeps_state_and_appends_summary_note(self):
        journal = InMemoryJournal()
        await _seed(journal)
        rewind = RewindEngine(journal)

        result = await rewind.apply(
            process_id="p1",
            target_entry_id="e6",
            mode=RewindMode.SUMMARIZE,
            record=False,
        )

        # State should still reflect everything (full reconstruct)
        assert result.context_state == {"counter": 2}
        # A summary message should be appended at the end
        assert result.conversation[-1]["role"] == "system"
        assert "rewind summary" in result.conversation[-1]["content"]
        assert result.summary_note is not None


class TestApplyCancel:
    @pytest.mark.asyncio
    async def test_cancel_is_dry_run(self):
        journal = InMemoryJournal()
        await _seed(journal)
        rewind = RewindEngine(journal)

        result = await rewind.apply(
            process_id="p1",
            target_entry_id="e2",
            mode=RewindMode.CANCEL,
        )

        # Cancel returns the plan but no state changes
        assert result.plan is not None
        assert result.plan.entries_affected == 7  # e2..e8
        assert result.context_state == {}
        assert result.conversation == []
        # No rewind entry recorded
        all_entries = await journal.read("p1")
        assert all(e.entry_type != "rewind" for e in all_entries)


# ---------------------------------------------------------------------------
# Recording the rewind itself
# ---------------------------------------------------------------------------


class TestRewindRecord:
    @pytest.mark.asyncio
    async def test_apply_appends_rewind_entry_when_record_true(self):
        journal = InMemoryJournal()
        await _seed(journal)
        rewind = RewindEngine(journal)

        result = await rewind.apply(
            process_id="p1",
            target_entry_id="e4",
            mode=RewindMode.BOTH,
            actor="alice",
            record=True,
        )

        all_entries = await journal.read("p1")
        rewind_entries = [e for e in all_entries if e.entry_type == "rewind"]
        assert len(rewind_entries) == 1
        re = rewind_entries[0]
        assert re.data["actor"] == "alice"
        assert re.data["mode"] == "both"
        assert re.data["target_entry_id"] == "e4"
        assert result.rewind_entry_id == re.entry_id

    @pytest.mark.asyncio
    async def test_apply_record_false_leaves_no_trace(self):
        journal = InMemoryJournal()
        await _seed(journal)
        rewind = RewindEngine(journal)

        await rewind.apply(
            process_id="p1",
            target_entry_id="e4",
            mode=RewindMode.BOTH,
            record=False,
        )

        all_entries = await journal.read("p1")
        assert all(e.entry_type != "rewind" for e in all_entries)
