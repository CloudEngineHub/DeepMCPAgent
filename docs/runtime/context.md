# Agent Context

`AgentContext` is the context every `AgentProcess` gives its agent: key-value **state** with an audit trail, optional long-term **memory**, a filtered view of the **environment**, and named **file mounts**. The state is how an agent keeps facts between runs (progress markers, counters, flags an operator sets): it is shown to the agent at the start of every run, written by the system, triggers, the [control API](api.md) and, if you allow it, the agent itself, and with a journal it survives a crash or a restart.

```python
from promptise.runtime import AgentProcess, ProcessConfig
from promptise.runtime.config import ContextConfig, JournalConfig

process = AgentProcess("pipeline-watch", ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Check the pipeline. Record what you found with update_state.",
    context=ContextConfig(
        initial_state={"pipeline_status": "unknown", "check_count": 0},
        writable_keys=["pipeline_status", "check_count"],
        state_tool=True,                      # the agent gets update_state(key, value)
    ),
    journal=JournalConfig(path=".promptise/journal"),  # state survives restarts
))
await process.start()

process.context.put("pipeline_status", "degraded", source="operator")
print(process.context.state_snapshot())  # {'pipeline_status': 'degraded', 'check_count': 0}
```

---

## Concepts

Four layers share one object:

| Layer | Purpose | Example |
|---|---|---|
| **State** | Key-value store with an audit trail, shown to the agent every run | `ctx.get("status")`, `ctx.put("status", "ok")` |
| **Memory** | Semantic long-term memory through a `MemoryProvider` | `await ctx.search_memory("error rate")` |
| **Environment** | Filtered snapshot of `os.environ` | `ctx.env` returns `{"AGENT_MODE": "production"}` |
| **Files** | Logical name to filesystem path mapping | `ctx.files` returns `{"data": "/mnt/data"}` |

The state works like a blackboard: several writers (initial config, triggers, operators over the API, the agent through its state tool) read and write the same keys, and every write is recorded as a `StateEntry` with a timestamp and a source.

You can also use `AgentContext` on its own:

```python
from promptise.runtime.context import AgentContext

ctx = AgentContext(
    initial_state={"pipeline_status": "healthy", "check_count": 0},
    writable_keys=["pipeline_status", "check_count"],
    env_prefix="AGENT_",
    file_mounts={"data": "/mnt/data", "config": "/etc/agent"},
)

ctx.put("check_count", 1)
assert ctx.get("check_count") == 1
assert len(ctx.state_history("check_count")) == 2  # initial + put
```

---

## State

### Reading and writing

```python
status = ctx.get("pipeline_status")           # "healthy"
missing = ctx.get("nonexistent", "fallback")  # "fallback"

ctx.put("pipeline_status", "degraded", source="cron-trigger")

# Several keys, all or nothing
written = ctx.update({"pipeline_status": "ok", "check_count": 2}, source="operator")
# ["pipeline_status", "check_count"]

snapshot = ctx.state_snapshot()   # shallow copy of the current state
keys = ctx.state_keys()           # ["pipeline_status", "check_count"]
ctx.clear_state()                 # remove all state and history
```

`put()` records the write with `source` (default `"agent"`). `update()` checks every key before writing any: if one is not writable, it raises `KeyError` naming all of them and writes nothing.

### Writable keys

When `writable_keys` is set, `put()` and `update()` only accept those keys, whoever writes (agent, API or trigger code):

```python
ctx = AgentContext(
    initial_state={"status": "ok", "owner": "ops"},
    writable_keys=["status"],
)

ctx.put("status", "error")    # works
ctx.put("owner", "me")        # raises KeyError: Key 'owner' is not writable. Allowed: ['status']
ctx.can_write("owner")        # False
ctx.denied_keys(["status", "owner"])  # ["owner"]
```

`None` or an empty list (the default) means **every** key is writable. `initial_state` and recovered state are loaded without the check. `ctx.writable_keys` returns the sorted list, or `None` when all keys are writable.

### Audit trail

Every write appends a `StateEntry` to the key's history:

```python
for entry in ctx.state_history("check_count"):
    print(entry.value, entry.source, entry.timestamp)
```

| Field | Type | Description |
|---|---|---|
| `key` | `str` | The key that was written |
| `value` | `Any` | A copy of the value stored (later in-place changes don't rewrite it) |
| `timestamp` | `float` | Unix time of the write |
| `source` | `str` | Who wrote it: `"system"` (initial state), `"agent"` (the state tool, and `put()`'s default), `"api"` (`PATCH /context`), `"recovery"` (restored from the journal), or any label you pass |

`StateEntry.to_dict()` / `StateEntry.from_dict()` serialize an entry; `to_dict()` is also the data of the `context_update` journal entry.

---

## How the agent sees the state

Before every run, when the state is not empty, the process adds it to the prompt as a system message, first in the list (before the budget, mission and inbox blocks and the conversation history):

```
[Context State] {'pipeline_status': 'degraded', 'check_count': 2}
```

The whole state is shown on every run, so keep values short and JSON-friendly.

---

## Letting the agent write state

The agent has no way to write state unless you give it one. `ContextConfig(state_tool=True)` adds an `update_state(key, value)` tool to the agent, in any execution mode:

```python
ContextConfig(
    initial_state={"tables_done": 0},
    writable_keys=["tables_done", "last_table"],
    state_tool=True,
)
```

- The tool writes through `put()` with source `"agent"`, so it obeys `writable_keys`; with writable keys set, the key is offered to the model as a fixed list of choices.
- A value must be JSON (string, number, boolean, list or object) and at most 10,000 characters as JSON. Without `writable_keys`, the state may hold at most 100 keys; a write that adds a key beyond that is refused.
- Refusals come back to the model as text starting with `Error:` (for example the allowed keys), never as exceptions.
- Writes are journaled like any other.

`state_tool` is off by default. Turn it on together with `writable_keys`: without them the agent can write any key, including ones your code relies on.

To give your own agent the same tool, use `create_state_tools(context, source="agent", max_value_chars=10_000, max_keys=100)` from `promptise.runtime.context`; it returns `[update_state]`.

---

## Changing state over the API

```bash
curl http://localhost:9100/api/v1/processes/pipeline-watch/context -H "Authorization: Bearer $TOKEN"
# {"state": {"pipeline_status": "degraded", "check_count": 2},
#  "writable_keys": ["check_count", "pipeline_status"], "all_keys_writable": false}

curl -X PATCH http://localhost:9100/api/v1/processes/pipeline-watch/context \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"state": {"pipeline_status": "ok"}}'
# {"name": "pipeline-watch", "updated_keys": ["pipeline_status"]}
```

`PATCH /context` is atomic: if any key is not writable, the answer is `403 WRITE_DENIED` naming the denied keys and the allowed ones, and **nothing** is written. The writes are recorded with source `"api"` and journaled. `GET /context` returns `writable_keys: []` with `all_keys_writable: true` when every key is writable. The Python client has `get_context(name)` and `update_context(name, state)`.

---

## Persistence and recovery

Without a journal, the state lives in memory. It survives `stop()` / `start()` and `restart_process()` of the same process object, but a new process object (a new Python process, a redeploy) starts from `initial_state`.

With a [journal](journal/index.md) (`ProcessConfig.journal`, off by default):

1. **Every write is journaled** as a `context_update` entry with the `StateEntry` data (`key`, `value`, `timestamp`, `source`), including writes from the state tool and the API.
2. **Checkpoints** with the whole state are written after every run, on `stop()` and after a rewind.
3. **Recovery on start.** With `JournalConfig.recover_on_start=True` (the default), the first `start()` of a process object restores the state of the last checkpoint for its name plus the `context_update` entries written after it (or those entries alone when there is no checkpoint yet). Recovered keys replace the same keys of `initial_state`; other initial keys stay. Recovered values get a history entry with source `"recovery"`.

```python
process = AgentProcess("pipeline-watch", config)  # same name and journal path as before
await process.start()
print(process.recovered)
# {'lifecycle_state': 'stopped', 'entries_replayed': 1, 'context_keys': ['check_count', 'pipeline_status'],
#  'mission_restored': False, 'budget_restored': False, 'conversation_messages': 6, 'invocation_count': 3}
```

`process.recovered` is `None` when nothing was restored; the restore is also journaled as a `recovered` entry. Set `recover_on_start=False` to always start fresh. To go back to an earlier state, use [rewind](journal/rewind.md).

### Secret values in state

With [secret scoping](governance/secrets.md) on, the journal redacts secret values: a `context_update` entry or checkpoint never holds one in plain text, it holds `[REDACTED]`. A recovered value therefore comes back as `[REDACTED]`, not as the secret. The live state is not redacted: the agent's prompt and `GET /context` show the value as stored. Keep secrets out of the state; read them with the secret scope where they are needed.

---

## Configuration

`ProcessConfig.context` is a `ContextConfig`:

| Field | Default | Description |
|---|---|---|
| `initial_state` | `{}` | State the process starts with |
| `writable_keys` | `[]` | Keys `put()` / `update()` may write; empty means all |
| `state_tool` | `False` | Give the agent the `update_state(key, value)` tool |
| `env_prefix` | `"AGENT_"` | Only environment variables with this prefix appear in `ctx.env` |
| `file_mounts` | `{}` | Logical name to filesystem path |
| `memory_provider` | `None` | `"in_memory"`, `"chroma"` or `"mem0"` for long-term memory |
| `memory_max` | `5` | Memories injected per run |
| `memory_min_score` | `0.0` | Minimum relevance score for injected memories |
| `memory_timeout` | `5.0` | Seconds to wait for a memory search or auto-store write |
| `memory_auto_store` | `False` | Store each exchange in long-term memory |
| `memory_collection` | `"agent_memory"` | ChromaDB collection |
| `memory_persist_directory` | `None` | ChromaDB persist directory |
| `memory_user_id` | `"default"` | Mem0 user scope |
| `conversation_max_messages` | `100` | Conversation buffer size (`0` = unlimited); see [Conversation](conversation.md) |
| `conversation_history` | `True` | `False` turns short-term conversation memory off |
| `conversation_clear_on_stop` | `False` | Clear the conversation on `stop()` |

Unknown keys are ignored with a `FutureWarning`.

In a `.agent` manifest, `world:` sets `initial_state` and the `context:` block takes any `ContextConfig` field:

```yaml
name: pipeline-watch
model: openai:gpt-5-mini
instructions: Check the pipeline and record its status.
world:
  pipeline_status: unknown
  check_count: 0
context:
  writable_keys: [pipeline_status, check_count]
  state_tool: true
journal:
  level: checkpoint
```

Setting initial state both in `world:` and in `context: initial_state` is an error, as is an unknown `context:` key.

---

## Memory integration

When `memory_provider` is set, the agent gets semantic long-term memory: relevant memories are injected into each run, and `memory_auto_store` stores each exchange.

```python
results = await ctx.search_memory("error rate", limit=3, min_score=0.5)
for r in results:
    print(f"[{r.memory_id}] score={r.score:.2f}: {r.content}")

memory_id = await ctx.add_memory("Pipeline had 5% error rate at 07:30", metadata={"severity": "warning"})
deleted = await ctx.delete_memory(memory_id)  # True if found and deleted
```

Without a provider, `search_memory` returns `[]`, `add_memory` returns `None` and `delete_memory` returns `False`.

The provider stays open across `stop()` / `start()` and restarts of the process object. `close()` (and `AgentRuntime.remove_process()`) closes it; a later `start()` opens a new one, and an `in_memory` provider then starts empty. Use `chroma` with `memory_persist_directory`, or `mem0`, for memories that outlive the Python process.

---

## Environment variables

`ctx.env` is a filtered snapshot of `os.environ` with only the variables whose name starts with `env_prefix`. The prefix is **not** stripped:

```python
ctx = AgentContext(env_prefix="AGENT_")
ctx.env  # {"AGENT_MODE": "production"}
```

---

## File mounts

```python
ctx = AgentContext(file_mounts={"data": "/mnt/data", "config": "/etc/agent"})
ctx.files  # {"data": "/mnt/data", "config": "/etc/agent"}
```

The context does no file I/O itself; it provides the mapping for agents and tools.

---

## Serialization

```python
snapshot = ctx.to_dict()
restored = AgentContext.from_dict(snapshot, memory_provider=my_provider)
```

The dict holds the state, the full history, the writable keys, the environment prefix and the file mounts. The memory provider is **not** serialized; pass it again with `memory_provider`. A process doesn't use these for recovery: it uses its journal (see [Persistence and recovery](#persistence-and-recovery)).

---

## API summary

| Method / property | Description |
|---|---|
| `get(key, default=None)` | Read a value |
| `put(key, value, *, source="agent")` | Write one key (audited, journaled); `KeyError` if not writable |
| `update(values, *, source="agent")` | Write several keys, all or nothing; returns the keys written |
| `can_write(key)` / `denied_keys(keys)` | Check keys against `writable_keys` |
| `writable_keys` | Sorted writable keys, or `None` when all are writable |
| `restore_state(state, *, source="recovery")` | Load recovered state: bypasses `writable_keys`, fires no update hooks |
| `add_update_hook(hook)` | Call `hook(entry)` on every write (sync, or returning an awaitable); returns a remover |
| `await flush_update_hooks()` | Wait for pending async hooks |
| `state_snapshot()` | Shallow copy of the state |
| `state_history(key)` | `StateEntry` list for a key |
| `state_keys()` | All keys |
| `clear_state()` | Remove all state and history |
| `memory` | The `MemoryProvider`, or `None` |
| `await search_memory(query, *, limit=5, min_score=0.0)` | Search long-term memory |
| `await add_memory(content, *, metadata=None)` | Store a memory |
| `await delete_memory(memory_id)` | Delete a memory |
| `env` | Filtered environment variables |
| `files` | File mount mapping |
| `to_dict()` / `AgentContext.from_dict(data, *, memory_provider=None)` | Serialize and restore |

---

## Common pitfalls

- **The agent can't write state by default.** Without `state_tool=True` (or your own tool), only your code, triggers and the API write it.
- **`state_tool` without `writable_keys` lets the agent write any key.** Name the keys it may change.
- **In-place changes bypass the audit trail.** `ctx.get("items").append(x)` changes the stored list, but nothing is recorded or journaled. Write a new value with `put()`.
- **Writes from another thread aren't journaled.** `put()` called outside the process's event loop updates the state but skips the journal hook (with a warning).
- **Secrets in state come back redacted.** With secret scoping, a recovered value that contained a secret is `[REDACTED]`, while the live state and `GET /context` show the plain value.
- **The state is in every prompt.** Large values cost tokens on every run; store summaries.
- **A new process object without a journal starts from `initial_state`.** Configure a journal to keep state across redeploys and crashes.

---

## What's next

- [Conversation](conversation.md) -- short-term conversation memory
- [Orchestration API](api.md) -- `GET` / `PATCH /context` and the other routes
- [Journal](journal/index.md) and [Rewind Engine](journal/rewind.md) -- persistence, recovery and going back
- [Secret Scoping](governance/secrets.md) -- keeping secret values out of journals and prompts
- [Configuration](configuration.md) -- every `ProcessConfig` section
- [Meta-Tools](meta-tools.md) -- the tools an open-mode agent gets

!!! tip "Learn the concept"

    - [AI Agent State Between Runs: Context, Keys and Audit](https://www.promptise.com/guides/view/ai-agent-state-between-runs) -- Keep state between runs, restrict who writes it, and recover it.
    - [Durable AI Agents: Crash Recovery and Safe Resume](https://www.promptise.com/guides/view/durable-ai-agent-crash-recovery) -- How journals and checkpoints bring state back after a crash.
