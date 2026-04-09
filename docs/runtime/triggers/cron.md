# Cron Trigger

The `CronTrigger` fires at scheduled intervals defined by a standard cron expression. It is the most common trigger type for periodic monitoring, data pipeline checks, and scheduled reporting.

```python
from promptise.runtime.triggers.cron import CronTrigger

trigger = CronTrigger("*/5 * * * *")
await trigger.start()

event = await trigger.wait_for_next()  # Blocks up to 5 minutes
print(event.payload)
# {"scheduled_time": "2026-03-04T10:05:00+00:00", "cron_expression": "*/5 * * * *"}

await trigger.stop()
```

---

## Concepts

The `CronTrigger` calculates the next fire time from the current moment, sleeps until that time, and then produces a `TriggerEvent`. This cycle repeats for as long as the trigger is active.

Two cron expression backends are supported:

- **`croniter`** (recommended) -- full cron expression support including complex schedules, day-of-week, ranges, and lists. Install with `pip install croniter`.
- **Built-in fallback** -- handles simple expressions like `*/N * * * *` (every N minutes), `* * * * *` (every minute), and specific-minute expressions like `30 * * * *`. No extra dependency required.

---

## Configuration

### Via TriggerConfig

```python
from promptise.runtime import ProcessConfig, TriggerConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Check data pipelines every 5 minutes.",
    triggers=[
        TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
    ],
)
```

### Direct instantiation

```python
from promptise.runtime.triggers.cron import CronTrigger

# Every 5 minutes
trigger = CronTrigger("*/5 * * * *")

# Every hour at minute 0
trigger = CronTrigger("0 * * * *")

# Every day at 9:00 AM
trigger = CronTrigger("0 9 * * *")

# Custom trigger ID
trigger = CronTrigger("*/10 * * * *", trigger_id="pipeline-check")
```

---

## Cron Expression Reference

Standard 5-field cron format:

```
┌───────────── minute (0-59)
│ ┌───────────── hour (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month (1-12)
│ │ │ │ ┌───────────── day of week (0-7, 0 and 7 are Sunday)
│ │ │ │ │
* * * * *
```

Common patterns:

| Expression | Schedule |
|---|---|
| `*/5 * * * *` | Every 5 minutes |
| `*/15 * * * *` | Every 15 minutes |
| `0 * * * *` | Every hour |
| `0 */2 * * *` | Every 2 hours |
| `0 9 * * *` | Daily at 9:00 AM |
| `0 9 * * 1` | Every Monday at 9:00 AM |
| `0 0 1 * *` | First day of every month |
| `* * * * *` | Every minute |

!!! tip "Install croniter for full support"
    The built-in fallback only supports `*/N * * * *`, `* * * * *`, and single-minute expressions. For anything more complex, install `croniter`: `pip install croniter`.

---

## How It Works

### Wait mechanism

`CronTrigger` uses `asyncio.wait_for` with an `asyncio.Event` to implement cancellable sleeping:

1. Calculate the next fire time from the cron expression.
2. Compute the delay in seconds from now.
3. Sleep for the delay using `wait_for(event.wait(), timeout=delay)`.
4. If the event is set (by `stop()`), raise `CancelledError`.
5. If the timeout expires naturally, produce a `TriggerEvent`.

This design allows `stop()` to immediately unblock a waiting trigger rather than sleeping for the full delay.

### Event payload

```python
{
    "scheduled_time": "2026-03-04T10:05:00+00:00",
    "cron_expression": "*/5 * * * *"
}
```

---

## Lifecycle

```python
trigger = CronTrigger("*/5 * * * *")

# Start the trigger
await trigger.start()

# Wait for events in a loop
while True:
    try:
        event = await trigger.wait_for_next()
        print(f"Fired at {event.payload['scheduled_time']}")
    except asyncio.CancelledError:
        break

# Stop the trigger
await trigger.stop()
```

---

## API Summary

| Method / Property | Description |
|---|---|
| `CronTrigger(cron_expression, trigger_id)` | Create a cron trigger |
| `trigger_id` | Unique identifier (auto-generated: `cron-XXXXXXXX`) |
| `await start()` | Mark the trigger as active |
| `await stop()` | Stop and unblock any waiters |
| `await wait_for_next()` | Block until the next scheduled time |

---

## Tips and Gotchas

!!! tip "Use croniter for production"
    The built-in fallback is convenient for quick starts, but `croniter` handles edge cases (DST transitions, month boundaries, etc.) correctly. Always use it in production.

!!! tip "Sub-minute scheduling"
    If you need sub-minute intervals, consider using a webhook trigger with an external scheduler.

!!! warning "Clock drift"
    The trigger computes the next fire time relative to `datetime.now(UTC)`. On systems with significant clock drift, scheduled times may shift. Use NTP synchronization in production.

!!! warning "Cron expression validation"
    Invalid expressions raise `TriggerError` at the first `wait_for_next()` call, not at construction time. Validate manifests with `promptise runtime validate` to catch errors early.

---

## What's Next

- [Triggers Overview](index.md) -- all trigger types and the base protocol
- [Event and Webhook Triggers](event-webhook.md) -- event-driven activation
- [File Watch Trigger](file-watch.md) -- filesystem change detection
