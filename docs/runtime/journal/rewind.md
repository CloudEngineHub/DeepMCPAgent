# Multi-Granularity Rewind

Roll back agent state to a previous point in the journal. Unlike [ReplayEngine](replay.md) (which reconstructs *current* state by replaying forward), `RewindEngine` lets you go **backward** — pick a point in history and selectively undo conversation, code/state changes, or both.

```python
from promptise.runtime.journal import InMemoryJournal, RewindEngine, RewindMode

rewind = RewindEngine(journal)

# Preview first (dry-run)
plan = await rewind.plan(
    process_id="support-bot",
    target_entry_id="entry-42",
    mode=RewindMode.CANCEL,
)
print(plan.entries_affected, plan.summary)

# Apply
result = await rewind.apply(
    process_id="support-bot",
    target_entry_id="entry-42",
    mode=RewindMode.CONVERSATION_ONLY,
    actor="alice@acme.com",
)
```

---

## 5 Rewind Modes

| Mode | What it does |
|---|---|
| `BOTH` | Full rollback — rewind conversation *and* context state to the target point. |
| `CONVERSATION_ONLY` | Restore the conversation buffer but keep tool results and state changes. Useful when the agent went off-rails in chat but accumulated useful work. |
| `CODE_ONLY` | Restore context state and tool results but keep the conversation. Useful to "undo" a tool call without losing the reasoning that led to it. |
| `SUMMARIZE` | Keep everything as-is but inject a summary of the skipped interval into the conversation as a system note. The agent remembers it tried something without re-running it. |
| `CANCEL` | Dry-run: return the plan without changing anything. Always safe. |

---

## Non-destructive

Original journal entries **never get deleted or edited**. A rewind is recorded as a new `rewind` entry in the journal, so the history shows when a rollback happened, who triggered it, and which mode was used.

```python
# The rewind entry data looks like:
{
    "target_entry_id": "entry-42",
    "mode": "conversation_only",
    "actor": "alice@acme.com",
    "entries_affected": 5,
    "conversation_affected": 3,
    "code_affected": 2,
}
```

---

## plan() — preview before committing

Always preview first:

```python
plan = await rewind.plan(
    process_id="support-bot",
    target_entry_id="entry-42",
)
print(plan.entries_affected)                # 5
print(plan.conversation_entries_affected)   # 3
print(plan.code_entries_affected)           # 2
print(plan.summary)                        # human-readable summary
```

Show the plan to a user or operator, let them pick a mode, then apply.

---

## Entry type classification

The rewind engine classifies journal entries into two buckets:

**Conversation entries:** `user_prompt`, `assistant_message`, `conversation_turn`

**Code/state entries:** `tool_call`, `tool_result`, `file_write`, `file_change`, `code_execution`, `context_update`, `state_transition`

`CONVERSATION_ONLY` drops conversation entries after the target but keeps code entries. `CODE_ONLY` does the reverse. `BOTH` drops everything after the target.

---

## Related

- [Journal Backends](backends.md) — InMemoryJournal, FileJournal
- [Replay Engine](replay.md) — forward recovery from checkpoints
- [Agent Processes](../processes.md) — lifecycle and state management
