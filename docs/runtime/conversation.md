---
seo_title: "Conversation Buffer for Agent Processes"
description: "ConversationBuffer, an agent process's short-term memory: a rolling window of recent messages replayed each run, kept across restarts with secrets redacted."
---

# Conversation Buffer

`ConversationBuffer` is an agent process's short-term memory: a rolling window of the last messages exchanged, replayed to the agent on every run so it remembers what it saw and said on earlier runs. Each `AgentProcess` owns one. It keeps its history across `stop()` / `start()` and restarts, can be checkpointed and recovered with a journal, and never stores a secret value in plain text.

```python
from promptise.runtime import ProcessConfig
from promptise.runtime.config import ContextConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Triage incoming tickets.",
    context=ContextConfig(
        conversation_max_messages=50,      # keep the last 50 messages
        conversation_clear_on_stop=False,  # default: history survives stop()/start()
    ),
)
```

On its own:

```python
from promptise.runtime.conversation import ConversationBuffer

buffer = ConversationBuffer(max_messages=50)
buffer.append({"role": "user", "content": "Hello!"})
buffer.append({"role": "assistant", "content": "Hi there!"})

history = buffer.get_messages()
# [{"role": "user", "content": "Hello!"}, {"role": "assistant", "content": "Hi there!"}]
```

---

## How a process uses it

On every run the process:

1. Puts the buffer's messages into the prompt after the system blocks (context state, budget, mission, inbox) and before the new trigger message.
2. After the run, appends two messages: the trigger message as the `user` message and the agent's final reply as the `assistant` message. Tool calls, tool results and the injected system blocks are not stored.
3. Drops the oldest messages when the buffer is over `conversation_max_messages`.

| `ContextConfig` field | Default | Description |
|---|---|---|
| `conversation_max_messages` | `100` | Messages kept. `0` means **unlimited**, not off |
| `conversation_history` | `True` | `False` turns the buffer off: every run sees only its own trigger message |
| `conversation_clear_on_stop` | `False` | Clear the history on `stop()` |

When an append makes the buffer drop old messages, the process fires the `pre_compact` hook (with the messages about to be dropped) and the `post_compact` hook. See [Runtime Hooks](hooks.md).

---

## Lifetime

| Event | History |
|---|---|
| `stop()` then `start()` on the same process object | Kept (unless `conversation_clear_on_stop=True`) |
| `restart_process()`, `POST /restart` (a stop, then a start) | Kept (unless `conversation_clear_on_stop=True`) |
| An automatic restart after a failure (restart policy) | Kept |
| An instructions change (`update_instructions`, `PATCH /instructions`) | Kept: the agent is rebuilt with the same history |
| `close()`, `async with process:` exit, `AgentRuntime.remove_process()`, `DELETE /processes/{name}` | Cleared |
| A new process object with the same name and a journal | Restored from the last checkpoint (`JournalConfig.recover_on_start`, default `True`) |
| A new process object without a journal | Starts empty |

With a journal, the conversation is part of every checkpoint (written after each run and on stop), and the first `start()` of a new process object restores it when `conversation_history` is on. `process.recovered["conversation_messages"]` says how many messages came back. A [rewind](journal/rewind.md) can roll the conversation back to an earlier point.

Facts that must outlive all of this belong in long-term memory or in the [context state](context.md).

---

## Secret values are redacted

With [secret scoping](governance/secrets.md) enabled, both messages are stored with every secret value replaced by `[REDACTED]`: current values, and values replaced by a rotation or expired. An agent that echoes a secret in its reply doesn't carry it into later prompts, and the conversation in journal checkpoints holds no plain-text secret.

```python
# The agent replied "the token is tok-SECRET-42"; the buffer holds:
{"role": "assistant", "content": "the token is [REDACTED]"}
```

---

## Basic usage

### Adding messages

```python
buffer = ConversationBuffer(max_messages=100)

buffer.append({"role": "user", "content": "Check the pipeline status."})
buffer.extend([
    {"role": "assistant", "content": "Pipeline is healthy. No anomalies."},
    {"role": "user", "content": "What about the error rate?"},
])
```

### Reading messages

```python
messages = buffer.get_messages()  # list copy, oldest first
print(len(buffer))                # 3
```

### Replacing and clearing

```python
buffer.set_messages(messages)  # replace the whole buffer (oldest dropped if over the limit)
buffer.clear()
assert len(buffer) == 0
```

### Turning it off

```python
buffer = ConversationBuffer(max_messages=100, enabled=False)
buffer.append({"role": "user", "content": "Hello"})
assert len(buffer) == 0  # nothing is retained
```

---

## Async-safe operations

When several coroutines use the buffer (a process with `concurrency > 1`), use the async methods, which hold an internal `asyncio.Lock`:

```python
snapshot = await buffer.async_snapshot()
await buffer.async_replace([{"role": "user", "content": "Fresh start"}])
await buffer.async_append({"role": "assistant", "content": "Ready."})
```

The synchronous methods take no lock; use them outside concurrent code (tests, serialization). The buffer is not thread-safe.

---

## Automatic eviction

When the buffer exceeds `max_messages`, the oldest messages are dropped:

```python
buffer = ConversationBuffer(max_messages=3)

for i in range(1, 5):
    buffer.append({"role": "user", "content": f"Message {i}"})  # the 4th evicts Message 1

messages = buffer.get_messages()
assert len(messages) == 3
assert messages[0]["content"] == "Message 2"
```

---

## Serialization

```python
data = buffer.to_dict()
# {"max_messages": 100, "enabled": True, "messages": [...]}

restored = ConversationBuffer.from_dict(data)
```

---

## API summary

| Method | Sync/Async | Description |
|---|---|---|
| `ConversationBuffer(max_messages=100, enabled=True)` | -- | `max_messages=0` is unlimited; `enabled=False` retains nothing |
| `append(message)` | Sync | Add one message dict |
| `extend(messages)` | Sync | Add several message dicts |
| `get_messages()` | Sync | All messages as a list copy, oldest first |
| `set_messages(messages)` | Sync | Replace the whole buffer |
| `clear()` | Sync | Remove all messages |
| `async_snapshot()` | Async | Locked copy of all messages |
| `async_replace(messages)` | Async | Locked replacement of the whole buffer |
| `async_append(message)` | Async | Locked append of one message |
| `to_dict()` | Sync | Serialize (`max_messages`, `enabled`, `messages`) |
| `ConversationBuffer.from_dict(data)` | Classmethod | Rebuild from `to_dict()` output |
| `__len__()` | Sync | Current message count |

---

## Common pitfalls

- **`conversation_max_messages=0` keeps everything.** It means unlimited, so the prompt grows with every run. Use `conversation_history=False` to turn the buffer off.
- **`close()` clears the history; `stop()` doesn't.** Removing a process from a runtime closes it. To keep the conversation across a redeploy, configure a journal.
- **Each run adds two messages.** `max_messages=20` holds the last 10 runs, not 20.
- **Eviction is silent unless you hook it.** Old messages are dropped without a log line; use the `pre_compact` hook to summarize or archive them, or store important facts in the context state or long-term memory.
- **Tool results aren't in the history.** Only the trigger message and the final reply are kept; a value the agent read from a tool is gone on the next run unless the reply or the state holds it.

---

## What's next

- [Agent Context](context.md): state and long-term memory around the conversation
- [Runtime Hooks](hooks.md): `pre_compact` / `post_compact`
- [Journal](journal/index.md) and [Rewind Engine](journal/rewind.md): checkpointing, recovery and rolling back the conversation
- [Secret Scoping](governance/secrets.md): the values that are redacted

!!! tip "Learn the concept"

    - [AI Agent State Between Runs: Context, Keys and Audit](https://www.promptise.com/guides/view/ai-agent-state-between-runs): How state, conversation and memory differ, and what survives a restart.
    - [Durable AI Agents: Crash Recovery and Safe Resume](https://www.promptise.com/guides/view/durable-ai-agent-crash-recovery): Checkpoints that bring the conversation back after a crash.
