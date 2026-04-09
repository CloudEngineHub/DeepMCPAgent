# Process Lifecycle

The `ProcessLifecycle` class implements a thread-safe, auditable state machine that governs every `AgentProcess`. It defines the valid states a process can be in, the legal transitions between them, and records every transition with timestamps and reasons.

```python
from promptise.runtime.lifecycle import ProcessLifecycle, ProcessState

lc = ProcessLifecycle()
assert lc.state == ProcessState.CREATED

await lc.transition(ProcessState.STARTING, reason="user requested start")
await lc.transition(ProcessState.RUNNING)

assert len(lc.history) == 2
```

---

## Concepts

Agent processes follow a strict state machine. Invalid transitions are rejected at the API level, preventing processes from entering inconsistent states. Every transition is recorded as a `ProcessTransition` with a timestamp, reason, and optional metadata -- creating a complete audit trail of what happened and why.

---

## State Diagram

```
CREATED ----> STARTING ----> RUNNING ----> STOPPING ----> STOPPED
                 |              |  ^           ^
                 |              |  |           |
                 v              v  |           |
               FAILED      SUSPENDED       FAILED ----> STARTING (restart)
                              |
                              v
                          AWAITING ----> RUNNING (trigger fires)
```

---

## Process States

| State | Description |
|---|---|
| `CREATED` | Initial state after registration. Not yet started. |
| `STARTING` | Start sequence in progress (building agent, connecting MCP servers). |
| `RUNNING` | Active and processing triggers. |
| `SUSPENDED` | Paused due to idle timeout. Can resume. |
| `AWAITING` | Waiting for next trigger event. A sub-state of running. |
| `STOPPING` | Graceful shutdown in progress. |
| `STOPPED` | Fully stopped. Can be restarted. |
| `FAILED` | Encountered a fatal error. Can be restarted. |

---

## Valid Transitions

| From | Allowed Targets |
|---|---|
| `CREATED` | `STARTING`, `STOPPED` |
| `STARTING` | `RUNNING`, `FAILED`, `STOPPING` |
| `RUNNING` | `SUSPENDED`, `STOPPING`, `FAILED`, `AWAITING` |
| `SUSPENDED` | `RUNNING`, `STOPPING`, `FAILED` |
| `AWAITING` | `RUNNING`, `STOPPING`, `FAILED` |
| `STOPPING` | `STOPPED`, `FAILED` |
| `STOPPED` | `STARTING` |
| `FAILED` | `STARTING` |

Any transition not in this table raises a `StateError`.

---

## Using ProcessLifecycle

### Creating and transitioning

```python
from promptise.runtime.lifecycle import ProcessLifecycle, ProcessState

lc = ProcessLifecycle()  # starts in CREATED

# Transition with a reason
await lc.transition(ProcessState.STARTING, reason="CLI start command")
await lc.transition(ProcessState.RUNNING, reason="agent built successfully")

# Check current state
assert lc.state == ProcessState.RUNNING
```

### Checking validity before transitioning

```python
if lc.can_transition(ProcessState.SUSPENDED):
    await lc.transition(ProcessState.SUSPENDED, reason="idle timeout")
```

### Transition with metadata

```python
await lc.transition(
    ProcessState.FAILED,
    reason="Max consecutive failures reached",
    metadata={"failures": 3},
)
```

### Invalid transitions raise StateError

```python
from promptise.runtime.lifecycle import StateError

lc = ProcessLifecycle()  # CREATED

try:
    await lc.transition(ProcessState.RUNNING)  # Invalid: must go through STARTING
except StateError as e:
    print(e)
    # Cannot transition from 'created' to 'running'.
    # Allowed targets: ['starting', 'stopped']
```

---

## ProcessTransition

Every state transition is recorded as a `ProcessTransition` dataclass:

| Field | Type | Description |
|---|---|---|
| `from_state` | `ProcessState` | Previous state |
| `to_state` | `ProcessState` | New state |
| `timestamp` | `datetime` | When the transition occurred (UTC) |
| `reason` | `str` | Human-readable explanation |
| `metadata` | `dict[str, Any]` | Additional context (error details, etc.) |

```python
transition = await lc.transition(ProcessState.STOPPING, reason="user request")

print(transition.from_state)   # ProcessState.RUNNING
print(transition.to_state)     # ProcessState.STOPPING
print(transition.reason)       # "user request"
print(transition.timestamp)    # datetime (UTC)
```

### Serialization

```python
data = transition.to_dict()
restored = ProcessTransition.from_dict(data)
```

---

## Audit History

The full transition history is available as a list:

```python
for t in lc.history:
    print(f"{t.from_state.value} -> {t.to_state.value}: {t.reason}")
```

### Snapshotting and restoring

```python
# Serialize current state + full history
snapshot = lc.snapshot()

# Restore from a snapshot (e.g., after crash recovery)
restored_lc = ProcessLifecycle.from_snapshot(snapshot)
assert restored_lc.state == lc.state
assert len(restored_lc.history) == len(lc.history)
```

---

## Thread Safety

`ProcessLifecycle` uses an internal `asyncio.Lock` to ensure that concurrent transition attempts are serialized. This prevents race conditions when multiple triggers or external signals attempt to change the process state simultaneously.

```python
# Safe for concurrent use from multiple coroutines
await asyncio.gather(
    lc.transition(ProcessState.SUSPENDED, reason="idle"),
    lc.transition(ProcessState.STOPPING, reason="shutdown"),
)
# Only one transition will succeed; the other raises StateError
```

---

## API Summary

| Method / Property | Description |
|---|---|
| `ProcessLifecycle(initial)` | Create with initial state (default: `CREATED`) |
| `state` | Current `ProcessState` |
| `history` | List of all `ProcessTransition` records |
| `can_transition(target)` | Check if transitioning to target is valid |
| `await transition(target, reason, metadata)` | Perform a state transition |
| `snapshot()` | Serialize state and history to dict |
| `ProcessLifecycle.from_snapshot(data)` | Restore from serialized dict |

---

## Tips and Gotchas

!!! tip "Always provide a reason"
    Transition reasons make debugging straightforward. Include context about who or what triggered the transition: `"cron trigger fired"`, `"max failures reached"`, `"user stop command"`.

!!! tip "Use metadata for error details"
    When transitioning to `FAILED`, include the error details in `metadata`. This information flows into the journal for later analysis.

!!! warning "STOPPED and FAILED are restartable"
    Both states allow transitioning back to `STARTING`. This is intentional for the restart policy feature. If you need a process to stay dead, remove it from the runtime entirely.

!!! warning "AWAITING is not SUSPENDED"
    `AWAITING` means the process is healthy but waiting for its next trigger. `SUSPENDED` means the process was explicitly paused (idle timeout). They have different allowed transitions.

---

## What's Next

- [Runtime Manager](runtime-manager.md) -- how `AgentRuntime` coordinates process lifecycles
- [Journal Overview](journal/index.md) -- durable recording of lifecycle events
