# Agent Context

`AgentContext` is the unified context layer that every `AgentProcess` provides to its agent on each invocation. It merges four concerns into a single API: key-value state with an audit trail, semantic long-term memory, filtered environment variables, and mounted file paths.

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

## Concepts

The context object acts as a **Blackboard** pattern implementation: multiple sources (system initialization, trigger handlers, the agent itself) can read and write shared state. Every write is recorded as a `StateEntry` with a timestamp and source attribution, creating a full audit trail.

Four layers are unified under one interface:

| Layer | Purpose | Example |
|---|---|---|
| **State** | Persistent key-value store with audit trail | `ctx.get("status")`, `ctx.put("status", "ok")` |
| **Memory** | Semantic long-term memory via `MemoryProvider` | `await ctx.search_memory("error rate")` |
| **Environment** | Filtered snapshot of `os.environ` | `ctx.env` returns `{"AGENT_API_KEY": "..."}` |
| **Files** | Logical name to filesystem path mapping | `ctx.files` returns `{"data": "/mnt/data"}` |

---

## State Management

### Reading and writing

```python
# Read a value (returns default if key is missing)
status = ctx.get("pipeline_status")           # "healthy"
missing = ctx.get("nonexistent", "fallback")  # "fallback"

# Write a value
ctx.put("pipeline_status", "degraded", source="cron-trigger")

# Get a snapshot of all current state
snapshot = ctx.state_snapshot()
# {"pipeline_status": "degraded", "check_count": 0}

# List all keys
keys = ctx.state_keys()  # ["pipeline_status", "check_count"]

# Clear all state and history
ctx.clear_state()
```

### Writable key restrictions

When `writable_keys` is set, only those keys can be written to. This prevents the agent from accidentally overwriting system state:

```python
ctx = AgentContext(
    initial_state={"status": "ok", "secret": "abc"},
    writable_keys=["status"],
)

ctx.put("status", "error")    # Works
ctx.put("secret", "xyz")      # Raises KeyError
```

An empty `writable_keys` list (the default) means **all** keys are writable.

### Audit trail

Every write creates a `StateEntry` record. You can query the full history for any key:

```python
for entry in ctx.state_history("check_count"):
    print(f"  value={entry.value}, source={entry.source}, ts={entry.timestamp}")
```

---

## StateEntry

Each state write produces an audit record:

| Field | Type | Description |
|---|---|---|
| `key` | `str` | The key that was written |
| `value` | `Any` | The value that was stored |
| `timestamp` | `float` | Unix timestamp of the write |
| `source` | `str` | Origin: `"system"`, `"agent"`, `"trigger"`, etc. |

```python
from promptise.runtime.context import StateEntry

entry = StateEntry(key="status", value="healthy", source="system")
data = entry.to_dict()
restored = StateEntry.from_dict(data)
```

---

## Memory Integration

When a `MemoryProvider` is wired into the context, agents gain semantic long-term memory that persists across process restarts.

### Searching memory

```python
results = await ctx.search_memory("error rate", limit=3, min_score=0.5)
for r in results:
    print(f"  [{r.memory_id}] score={r.score:.2f}: {r.content}")
```

### Storing memory

```python
memory_id = await ctx.add_memory(
    "Pipeline had 5% error rate at 07:30",
    metadata={"severity": "warning"},
)
```

### Deleting memory

```python
deleted = await ctx.delete_memory(memory_id)  # True if found and deleted
```

If no memory provider is configured, `search_memory` returns an empty list, `add_memory` returns `None`, and `delete_memory` returns `False`.

---

## Environment Variables

The `env` property returns a filtered snapshot of `os.environ`, including only variables whose name starts with the configured prefix:

```python
ctx = AgentContext(env_prefix="AGENT_")
ctx.env  # {"AGENT_API_KEY": "sk-...", "AGENT_MODE": "production"}
```

The prefix is **not** stripped from the key names. This allows agents to access configuration values without exposing the full environment.

---

## File Mounts

File mounts provide a logical mapping from a descriptive name to a filesystem path:

```python
ctx = AgentContext(file_mounts={"data": "/mnt/data", "config": "/etc/agent"})
ctx.files  # {"data": "/mnt/data", "config": "/etc/agent"}
```

The context does not perform any file I/O itself -- it simply provides the mapping for agents and tools to use.

---

## Serialization

The context state is fully serializable for checkpointing and distribution:

```python
# Save state
snapshot = ctx.to_dict()

# Restore state (memory provider must be re-attached separately)
restored = AgentContext.from_dict(snapshot, memory_provider=my_provider)
```

!!! warning "Memory providers are not serialized"
    The memory provider instance is **not** included in the serialized dict. You must re-attach it when restoring from a checkpoint using the `memory_provider` parameter.

The serialized dict includes:

- Current state key-value pairs
- Full state history (all `StateEntry` records)
- Writable keys configuration
- Environment prefix
- File mount mappings

---

## API Summary

| Method / Property | Description |
|---|---|
| `get(key, default)` | Read a value from state |
| `put(key, value, source)` | Write a value to state (creates audit entry) |
| `state_snapshot()` | Shallow copy of current state dict |
| `state_history(key)` | List of `StateEntry` records for a key |
| `state_keys()` | List of all state keys |
| `clear_state()` | Remove all state and history |
| `memory` | The underlying `MemoryProvider` or `None` |
| `await search_memory(query, limit, min_score)` | Search semantic memory |
| `await add_memory(content, metadata)` | Store a new memory |
| `await delete_memory(memory_id)` | Delete a memory by ID |
| `env` | Filtered environment variables dict |
| `files` | File mount mapping dict |
| `to_dict()` | Serialize context for checkpointing |
| `AgentContext.from_dict(data, memory_provider)` | Restore from serialized dict |

---

## Tips and Gotchas

!!! tip "Use writable_keys for safety"
    In production, always restrict `writable_keys` to the specific keys the agent needs to modify. This prevents accidental overwrites of system-managed state.

!!! tip "Source attribution matters"
    Always pass a meaningful `source` parameter to `put()` -- it makes the audit trail useful for debugging. Common sources: `"system"`, `"agent"`, `"trigger"`, `"user"`.

!!! tip "State snapshot is injected into the LLM"
    The `state_snapshot()` dict is included in the agent's context on each invocation. Keep state values concise and JSON-serializable for best results.

!!! warning "Context state is in-memory"
    State is not automatically persisted. Use the journal system with `level="checkpoint"` to snapshot state after each invocation cycle. On crash recovery, the `ReplayEngine` reconstructs the context from journal entries.

---

## What's Next

- [Conversation](conversation.md) -- short-term conversation memory buffer
- [Configuration](configuration.md) -- `ContextConfig` fields and defaults
- [Journal Overview](journal/index.md) -- durable state persistence and crash recovery
- [Meta-Tools](meta-tools.md) -- how open-mode agents interact with context
