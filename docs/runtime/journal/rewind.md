# Rewind

Rewind rolls a live process back to how it was just before a journal entry: its context state, its conversation history, or both. Use it when an agent went off track -- a bad tool result written into its state, a conversation that drifted -- and you want it to continue from an earlier point instead of starting over. It is non-destructive: the journal is only appended to, so a rewind can itself be rewound.

```python
from promptise.runtime import AgentProcess, JournalConfig, ProcessConfig
from promptise.runtime.journal import FileJournal

process = AgentProcess("notes", ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="You write release notes.",
    journal=JournalConfig(level="checkpoint"),     # rewind needs a journal
))
await process.start()
process.context.put("stage", "draft")
# ... a run ...
process.context.put("stage", "published")         # this turns out to be wrong
# ... another run ...

# Find the entry to go back to (or: GET /journal, `promptise runtime logs notes`)
entries = await FileJournal(".promptise/journal").read("notes")
target = next(e for e in entries
              if e.entry_type == "context_update" and e.data["value"] == "published")

plan = await process.rewind_plan(target.entry_id, "both")  # preview, changes nothing
print(plan.summary)    # rewind (both): 3 entries affected (1 conversation, 1 code/state).

result = await process.rewind(target.entry_id, "both", actor="alice")
print(process.context.get("stage"))   # 'draft'
print(result.rewind_entry_id)         # rewind-8e4617a...
```

From the terminal, against a runtime started with `promptise runtime start`:

```bash
promptise runtime logs notes                         # find the entry ID
promptise runtime rewind notes <entry_id> --plan     # preview
promptise runtime rewind notes <entry_id>            # preview, confirm, apply
```

---

## Modes

| Mode | Context state | Conversation history |
|---|---|---|
| `both` (default) | Rewound | Rewound |
| `conversation_only` | Kept as it is now | Rewound |
| `code_only` | Rewound | Kept as it is now |
| `summarize` | Kept | Kept, plus a system note summarizing what happened since the target |
| `cancel` | Kept | Kept (a dry run that returns the plan; `rewind_plan()` does the same) |

"Rewound" means rebuilt as it was **just before** the target entry; the target itself is undone too. To get back the state a checkpoint recorded, target the entry *after* that checkpoint.

The `summarize` note is a `system` message like `[rewind summary] Skipped interval 2026-10-19T20:23:11+00:00 -> 2026-10-19T20:23:12+00:00 containing 1 checkpoint, 1 context_update, 1 invocation_result. State and conversation were preserved as-is.` It lets the agent know what it already tried without undoing anything.

**Never rewound:** long-term memory, the budget counters (a rewind doesn't refund spent budget), the mission's progress, secrets, the invocation count, and anything a tool did outside the process (an email sent stays sent). Open mode's `process.rollback()` is a different thing: it discards the agent's *self-modifications* (instructions, tools, MCP servers, triggers) and keeps state and history.

---

## What a rewind reads

The engine rebuilds state from the process's own journal entries:

| Entry type | Effect when rebuilding |
|---|---|
| `checkpoint` | A full snapshot: replaces the context state, lifecycle state and conversation (the process writes one after every run) |
| `context_update` | Sets one context key |
| `state_transition` | Sets the lifecycle state |
| `user_prompt`, `assistant_message`, `conversation_turn` | Append a message to the conversation |
| Anything else | Ignored (observability only) |

Each mode takes its rewound part from the state rebuilt from the entries *before* the target and keeps the rest from the state rebuilt from *all* entries. Values the journal stored redacted (secrets, as `[REDACTED]`) come back redacted.

For the plan's counts, entries are grouped as **conversation** (`user_prompt`, `assistant_message`, `conversation_turn`, `trigger_event`, `invocation_result`) or **code/state** (`tool_call`, `tool_result`, `file_write`, `file_change`, `code_execution`, `context_update`, `state_transition`).

---

## AgentProcess.rewind() and rewind_plan()

```python
async def rewind_plan(target_entry_id: str, mode: str | RewindMode = "both") -> RewindPlan
async def rewind(
    target_entry_id: str,
    mode: str | RewindMode = "both",
    *,
    actor: str = "operator",
    wait_timeout: float = 60.0,
) -> RewindResult
```

What `rewind()` does:

1. Checks the target and the mode (nothing is changed when either is wrong).
2. Makes sure the live state is the journal's: if the process hasn't recovered from its journal yet, it does that first.
3. Suspends a `RUNNING` process (reason `"rewind"`) and waits up to `wait_timeout` seconds for a run in flight to finish. Events that arrive meanwhile wait in the queue. A suspended or stopped process stays as it is.
4. Applies the result: replaces the context state and/or the conversation buffer (or appends the summary note).
5. Appends a `rewind` entry (`target_entry_id`, `mode`, `actor`, `entries_affected`, `conversation_affected`, `code_affected`, `summary_note`) and a checkpoint of the rewound state, so a later [recovery](index.md#recovery-on-start) starts from it.
6. Resumes the process if it suspended it, emits the `process.rewound` event (severity `warning`) and dispatches the `CONFIG_CHANGE` [hook](../hooks.md) with `source="operator"`, `change="rewind"`.

`mode="cancel"` returns at once with only `result.plan` set, without suspending or writing anything.

| Raises | When |
|---|---|
| `RuntimeError` | The process has no journal |
| `ValueError` | The entry ID is not in the journal, or the mode is unknown |
| `TimeoutError` | A run was still in flight after `wait_timeout` (nothing was changed; the process is resumed) |

A hook must not `await process.rewind()` of the process that dispatched it; schedule it with `asyncio.create_task`.

### RewindPlan

| Field | Description |
|---|---|
| `process_id` | The process name |
| `target_entry_id`, `target_entry_type`, `target_timestamp` | The target entry |
| `mode` | The `RewindMode` |
| `entries_total` | Entries in the journal |
| `entries_affected` | Entries from the target on (the ones the rewind undoes) |
| `conversation_entries_affected`, `code_entries_affected` | How many of those are conversation or code/state entries |
| `summary` | One line, for example `rewind (both): 3 entries affected (1 conversation, 1 code/state).` |

`plan.to_dict()` gives the JSON form the API returns.

### RewindResult

| Field | Description |
|---|---|
| `process_id`, `target_entry_id`, `mode` | What was rewound |
| `context_state` | The context state after the rewind |
| `conversation` | The conversation after the rewind |
| `lifecycle_state` | The lifecycle state the journal had at that point (informational; the process's state is not changed) |
| `summary_note` | The note `summarize` added, else `None` |
| `plan` | The `RewindPlan` |
| `rewind_entry_id` | ID of the `rewind` journal entry (`rewind-<hex>`) |
| `applied_at` | When it was applied (UTC) |

---

## Over the Orchestration API

| Route | Description |
|---|---|
| `GET /api/v1/processes/{name}/rewind/plan?target_entry_id=...&mode=both` | The plan (`{"name": ..., "plan": {...}}`); changes nothing. `mode` defaults to `both` |
| `POST /api/v1/processes/{name}/rewind` | Body `{"target_entry_id": "...", "mode": "both", "actor": "alice"}`; `mode` defaults to `both`, `actor` to `"api"` (at most 200 characters) |

The `POST` answers with `name`, `mode`, `target_entry_id`, `rewind_entry_id`, `entries_affected`, `context_keys`, `conversation_messages`, `state` and `plan`. Entry IDs come from `GET /api/v1/processes/{name}/journal`.

| Status | Code | When |
|---|---|---|
| `400` | `JOURNAL_DISABLED` | The process has no journal |
| `404` | `PROCESS_NOT_FOUND` | No such process |
| `404` | `ENTRY_NOT_FOUND` | The entry ID is not in the journal |
| `409` | `REWIND_BUSY` | A run was still in flight when the rewind timed out |
| `422` | `MISSING_FIELD` / `INVALID_FIELD` | No `target_entry_id`, or a bad `mode` or `actor` |

With `OrchestrationClient`:

```python
from promptise.runtime.api_client import OrchestrationClient

async with OrchestrationClient("http://127.0.0.1:9100", token=token) as client:
    entries = await client.get_journal("notes", limit=20)
    target = entries[-3]["entry_id"]
    plan = await client.rewind_plan("notes", target, mode="both")   # the plan dict
    result = await client.rewind("notes", target, mode="both", actor="alice")
```

Errors raise `OrchestrationAPIError` with the codes above.

---

## From the CLI

```
promptise runtime rewind PROCESS [ENTRY_ID] [--mode MODE] [--plan] [--yes]
                         [--runtime NAME | --url URL [--token TOKEN]] [--lines N]
```

| Option | Default | Description |
|---|---|---|
| `ENTRY_ID` | -- | The entry to go back to. Omit it to list the newest entries with their IDs |
| `--mode`, `-m` | `both` | `both`, `conversation_only`, `code_only` or `summarize` |
| `--plan` | off | Show the plan and stop |
| `--yes`, `-y` | off | Apply without asking |
| `--runtime`, `-r` | -- | The runtime the process runs in (needed when several are running) |
| `--lines`, `-n` | `20` | Entries to list when no entry ID is given (1-500) |
| `--url`, `--token` | -- | Talk to an Orchestration API instead of a local runtime |

The command goes through the runtime's control API, so the process must be in a running runtime. It prints the plan, asks `Rewind <process>? [y/N]`, and records the rewind with actor `"cli"`. Answering no (or no answer) exits with code 1 and changes nothing.

### Worked example

A `notes` process with a webhook trigger and `journal: {level: checkpoint}` runs in the background. Two events arrived; the second run should not have happened.

```bash
$ promptise runtime logs notes
                                Journal: notes
 Time                     Entry ID                              Type               Data
 2026-10-19T20:25:00.948  4ce65790-942e-44a3-9397-d2cecf567c89  state_transition   {"from_state": "created", ...
 2026-10-19T20:25:00.950  18dff2cd-da54-4416-aa5c-a65b42f0979f  state_transition   {"from_state": "starting", ...
 2026-10-19T20:25:02.077  b354d9ca-f7b7-4783-964a-970f9ae156b3  invocation_result  {"invocation": 1, ...
 2026-10-19T20:25:02.077  38b220ec-6b42-4c81-a0ca-f27f4f1a4767  checkpoint         {"state": {"context_state": {}, ...
 2026-10-19T20:25:03.585  7c7bee77-f518-41de-b250-d1b586819d7a  invocation_result  {"invocation": 2, ...
 2026-10-19T20:25:03.586  cd3d6e60-4910-42bb-b236-4336367ba701  checkpoint         {"state": {"context_state": {}, ...

$ promptise runtime rewind notes 7c7bee77-f518-41de-b250-d1b586819d7a --plan
Rewind plan for notes: back to just before entry 7c7bee77-f518-41de-b250-d1b586819d7a (invocation_result, 2026-10-19T20:25:03.585572+00:00)
  Mode:      both (rewinds the conversation and the context state)
  Affected:  2 of 6 journal entries: the target and the 1 after it (1 conversation, 0 state/tool)
  Summary:   rewind (both): 2 entries affected (1 conversation, 0 code/state).
The journal itself is kept (a rewind entry is appended), so a rewind can be rewound.

$ promptise runtime rewind notes 7c7bee77-f518-41de-b250-d1b586819d7a --yes
...
Rewound notes to just before 7c7bee77-f518-41de-b250-d1b586819d7a (both): 2 entries undone, 2 conversation messages kept, state running
Journal entry of this rewind: rewind-0764e40877f84f1992def63cb066927d
```

The conversation is back to the two messages of the first run, the process is running again, and the journal now ends with the suspension, the `rewind` entry, the resume, a checkpoint of the rewound state and the `api_request` audit entry. "2 entries undone" is the plan's count: the target and the one entry after it. The suspension the rewind journals itself is not counted.

---

## RewindEngine

The engine only *computes* a rewind; `AgentProcess.rewind()` applies it. Use it directly on a journal without a live process, for example to inspect history offline:

```python
from promptise.runtime.journal import FileJournal, RewindEngine, RewindMode

engine = RewindEngine(FileJournal(".promptise/journal"))

plan = await engine.plan(process_id="notes", target_entry_id=target, mode=RewindMode.BOTH)
result = await engine.apply(
    process_id="notes",
    target_entry_id=target,
    mode=RewindMode.CODE_ONLY,
    actor="alice@example.com",
    record=False,      # True (the default) appends a `rewind` entry
)
print(result.context_state, len(result.conversation))
```

| Method | Description |
|---|---|
| `RewindEngine(journal)` | Wrap a `JournalProvider` (stateless otherwise; one engine can serve several processes) |
| `await plan(*, process_id, target_entry_id, mode=RewindMode.CANCEL)` | A `RewindPlan`; raises `ValueError` for an unknown entry |
| `await apply(*, process_id, target_entry_id, mode, actor="system", record=True)` | A `RewindResult`; with `record=True` a `rewind` entry is appended (never anything edited or deleted). `CANCEL` returns only the plan |

`process_id` is the name the entries are filed under (the process name).

---

## Common pitfalls

- **No journal.** Rewind needs `ProcessConfig.journal` with a level other than `"none"` (`RuntimeError`, `400 JOURNAL_DISABLED`).
- **Off by one entry.** The target is undone too. To keep what a checkpoint recorded, target the entry after it.
- **Expecting the budget back.** Spent runs, tool calls and cost stay counted.
- **Expecting the world to be rewound.** Files written, messages sent and API calls made by tools stay done. Rewind is for the agent's own state and history.
- **A long run in flight.** The rewind waits up to `wait_timeout` (60 seconds) for it, then gives up with `TimeoutError` / `409 REWIND_BUSY` and changes nothing. Retry when the run is done, or suspend the process first.
- **A memory journal.** `backend: memory` lives only as long as the Python process, and so does its rewind history.

---

## Related

- [Journal Overview](index.md) -- entry types, checkpoints and recovery on start
- [Replay Engine](replay.md) -- forward recovery from the last checkpoint
- [Orchestration API](../api.md) -- the journal and rewind routes
- [CLI Commands](../cli.md) -- `promptise runtime logs` and `promptise runtime rewind`
- [Agent Processes](../processes.md) -- suspend, resume and the process state

!!! tip "Learn the concept"

    - [Durable AI Agents: Crash Recovery and Safe Resume](https://www.promptise.com/guides/view/durable-ai-agent-crash-recovery) -- Journals, checkpoints and resuming safely after something went wrong.
    - [AI Agent Control API: Manage Running Agents Over HTTP](https://www.promptise.com/guides/view/ai-agent-control-api) -- Drive a running process, including its journal, over HTTP.
