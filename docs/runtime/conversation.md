# Conversation Buffer

`ConversationBuffer` provides a rolling window of message exchanges for short-term memory within an `AgentProcess`. It retains the last N messages across trigger invocations, giving the agent continuity within a single process lifecycle.

```python
from promptise.runtime.conversation import ConversationBuffer

buffer = ConversationBuffer(max_messages=50)
buffer.append({"role": "user", "content": "Hello!"})
buffer.append({"role": "assistant", "content": "Hi there!"})

# Next invocation sees the previous exchange
history = buffer.get_messages()
# [{"role": "user", "content": "Hello!"}, {"role": "assistant", "content": "Hi there!"}]
```

---

## Concepts

Agent processes are often invoked repeatedly by triggers (cron jobs, webhooks, events). Each invocation needs context from prior exchanges to maintain coherent behavior. The `ConversationBuffer` provides this by maintaining a FIFO message buffer:

- **Short-term only** -- messages are lost on process restart. For persistence across restarts, use a `MemoryProvider`.
- **Rolling window** -- oldest messages are automatically evicted when `max_messages` is exceeded.
- **Dual interface** -- synchronous methods for single-threaded use and tests; async methods with internal locking for concurrent access.
- **Serializable** -- can be checkpointed to the journal for crash recovery.

---

## Basic Usage

### Adding messages

```python
buffer = ConversationBuffer(max_messages=100)

# Add a single message
buffer.append({"role": "user", "content": "Check the pipeline status."})

# Add multiple messages at once
buffer.extend([
    {"role": "assistant", "content": "Pipeline is healthy. No anomalies."},
    {"role": "user", "content": "What about the error rate?"},
])
```

### Reading messages

```python
# Get all messages as a list copy (oldest first)
messages = buffer.get_messages()

# Check message count
print(len(buffer))  # 3
```

### Replacing the buffer

Used during hot-reload to preserve conversation across agent rebuilds:

```python
buffer.set_messages(messages)
```

### Clearing

```python
buffer.clear()
assert len(buffer) == 0
```

---

## Async-Safe Operations

When multiple coroutines access the buffer concurrently (for example, a process handling concurrent triggers), use the async methods which acquire an internal `asyncio.Lock`:

```python
# Thread-safe read
snapshot = await buffer.async_snapshot()

# Thread-safe replace
await buffer.async_replace([{"role": "user", "content": "Fresh start"}])

# Thread-safe append
await buffer.async_append({"role": "assistant", "content": "Ready."})
```

---

## Automatic Eviction

When the buffer exceeds `max_messages`, the oldest messages are evicted automatically:

```python
buffer = ConversationBuffer(max_messages=3)

buffer.append({"role": "user", "content": "Message 1"})
buffer.append({"role": "user", "content": "Message 2"})
buffer.append({"role": "user", "content": "Message 3"})
buffer.append({"role": "user", "content": "Message 4"})  # Evicts Message 1

messages = buffer.get_messages()
assert len(messages) == 3
assert messages[0]["content"] == "Message 2"
```

Set `max_messages=0` in `ContextConfig` to disable conversation buffering entirely.

---

## Serialization

The buffer can be serialized for journal checkpointing and restored after a crash:

```python
# Save
data = buffer.to_dict()
# {"max_messages": 100, "messages": [...]}

# Restore
restored = ConversationBuffer.from_dict(data)
```

---

## API Summary

| Method | Sync/Async | Description |
|---|---|---|
| `append(message)` | Sync | Add a single message dict |
| `extend(messages)` | Sync | Add multiple message dicts |
| `get_messages()` | Sync | Return all messages as a list copy |
| `set_messages(messages)` | Sync | Replace the entire buffer |
| `clear()` | Sync | Remove all messages |
| `async_snapshot()` | Async | Thread-safe copy of all messages |
| `async_replace(messages)` | Async | Thread-safe replacement of the entire buffer |
| `async_append(message)` | Async | Thread-safe append of a single message |
| `to_dict()` | Sync | Serialize to dict for checkpointing |
| `ConversationBuffer.from_dict(data)` | Classmethod | Reconstruct from serialized dict |
| `__len__()` | Sync | Current message count |

---

## Tips and Gotchas

!!! tip "Size the buffer for your use case"
    A cron agent that runs every 5 minutes may only need `max_messages=20`. A webhook handler processing rapid events might need `max_messages=200`. The default is `100`.

!!! tip "Use async methods in production"
    When `concurrency > 1` in `ProcessConfig`, multiple invocations can access the buffer simultaneously. Always use `async_snapshot`, `async_append`, and `async_replace` in that scenario.

!!! warning "Messages are lost on restart"
    The conversation buffer is ephemeral. On process restart, the buffer starts empty unless you restore from a journal checkpoint. For persistent memory across restarts, use a `MemoryProvider`.

!!! warning "Eviction is silent"
    When the buffer overflows, the oldest messages are dropped without warning. If you need to preserve important context, store it in the agent's long-term memory.

---

## What's Next

- [Context](context.md) -- the full `AgentContext` that wraps the conversation buffer
- [Configuration](configuration.md) -- `conversation_max_messages` in `ContextConfig`
- [Journal Overview](journal/index.md) -- checkpointing the buffer for crash recovery
