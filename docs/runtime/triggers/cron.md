---
seo_title: "Cron Trigger: Schedule AI Agent Runs"
description: "Run an agent on a cron schedule for monitoring, pipeline checks and reports: cron expressions, time zones and DST, how ticks become runs, the event payload."
---

# Cron Trigger

The `CronTrigger` runs an agent on a schedule defined by a cron expression. Use it for periodic monitoring, pipeline checks, daily digests and scheduled reports. Each tick becomes one agent run.

```python
from promptise.runtime import ProcessConfig, TriggerConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Check the data pipelines and report failures.",
    triggers=[
        # 09:00 Zurich time, Monday to Friday
        TriggerConfig(type="cron", cron_expression="0 9 * * 1-5", cron_timezone="Europe/Zurich"),
    ],
)
```

On its own, outside a process:

```python
from promptise.runtime.triggers.cron import CronTrigger

trigger = CronTrigger("*/5 * * * *")
await trigger.start()

event = await trigger.wait_for_next()  # Blocks up to 5 minutes
print(event.payload)
# {"scheduled_time": "2026-03-04T10:05:00+00:00", "cron_expression": "*/5 * * * *", "timezone": "UTC"}

await trigger.stop()
```

---

## Concepts

The `CronTrigger` calculates the next fire time from the current moment, sleeps until that time, and then produces a `TriggerEvent`. This cycle repeats for as long as the trigger is active.

Expressions are evaluated with **`croniter`**, which ships with `pip install promptise`: ranges, lists, steps, day-of-week names and an optional seconds field all work. Macros such as `@daily` are not accepted; write the five fields. If `croniter` has been removed from the environment, a built-in fallback handles only `*/N * * * *`, `* * * * *` and single-minute expressions like `30 * * * *`.

**Schedules run in UTC** unless you set a time zone (see [Time zones](#time-zones)). `0 9 * * 1-5` therefore means 09:00 UTC on weekdays by default, which is 10:00 or 11:00 in Zurich depending on daylight saving time.

The expression (and time zone) is validated when the `TriggerConfig` or `CronTrigger` is created. A bad expression raises a `ValidationError` / `TriggerError` straight away, so a process with a broken schedule never starts.

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
        # 09:00 Zurich time on weekdays, DST-aware
        TriggerConfig(
            type="cron",
            cron_expression="0 9 * * 1-5",
            cron_timezone="Europe/Zurich",
        ),
    ],
)
```

| Field | Default | Description |
|---|---|---|
| `cron_expression` | required | 5-field cron expression, or 6 fields with a trailing seconds field |
| `cron_timezone` | `None` (UTC) | IANA time zone name the expression is read in |
| `filter_expression` | `None` | Skip events before the agent runs (see [Filtering](index.md#filtering-events-before-the-agent-runs)) |

Setting an option of another trigger type (for example `watch_path`) on a `cron` trigger is a validation error.

Inside a process, the trigger's ID is a [stable ID](index.md#trigger-ids) such as `cron-6d904fd75c`, derived from its config. It stays the same across restarts and is the `trigger_id` of every event.

### Direct instantiation

```python
from promptise.runtime.triggers.cron import CronTrigger

# Every 5 minutes
trigger = CronTrigger("*/5 * * * *")

# Every hour at minute 0
trigger = CronTrigger("0 * * * *")

# Every day at 9:00 AM (UTC)
trigger = CronTrigger("0 9 * * *")

# Every day at 9:00 AM New York time
trigger = CronTrigger("0 9 * * *", timezone="America/New_York")

# Every 10 seconds (sixth field = seconds)
trigger = CronTrigger("* * * * * */10")

# Custom trigger ID
trigger = CronTrigger("*/10 * * * *", trigger_id="pipeline-check")
```

---

## Cron Expression Reference

Standard 5-field cron format, with an optional sixth field for seconds:

```
┌───────────── minute (0-59)
│ ┌───────────── hour (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month (1-12)
│ │ │ │ ┌───────────── day of week (0-7, 0 and 7 are Sunday)
│ │ │ │ │ ┌───────────── second (0-59, optional, croniter's convention: last)
│ │ │ │ │ │
* * * * * *
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
| `0 9 * * 1-5` | Weekdays at 9:00 AM |
| `0 0 1 * *` | First day of every month |
| `* * * * *` | Every minute |
| `* * * * * */10` | Every 10 seconds |
| `* * * * * 30` | Every minute at second 30 |

When both day of month and day of week are restricted (`0 9 1 * 1`), the job runs when **either** matches: on the 1st of the month and on every Monday.

---

## Time zones

By default the expression is evaluated in **UTC**. Set `cron_timezone` (or `timezone=` on `CronTrigger`) to an IANA name to schedule in local time:

```python
TriggerConfig(type="cron", cron_expression="0 9 * * 1-5", cron_timezone="Europe/Zurich")
```

Daylight-saving changes are handled by `croniter`: the trigger fires at 09:00 local time all year, and `scheduled_time` in the payload carries the local offset (`2026-03-02T09:00:00+01:00`). On Windows, install the `tzdata` package so time-zone names resolve.

### Jobs in the hour that DST changes

A job scheduled inside the hour that the clock change repeats or skips behaves differently on those two nights. With `cron_expression="30 2 * * *"` and `cron_timezone="Europe/Zurich"`:

| Night | What happens | `scheduled_time` values |
|---|---|---|
| Fall back (2026-10-25: 03:00 becomes 02:00) | 02:30 occurs twice, so the job **runs twice** | `2026-10-25T02:30:00+02:00`, then `2026-10-25T02:30:00+01:00` |
| Spring forward (2026-03-29: 02:00 becomes 03:00) | 02:30 does not exist, so the job **runs at 03:00** local time | `2026-03-29T03:00:00+02:00` |

The same applies to every job in the affected hour (`0 2 * * *` also runs at 03:00 on the spring-forward night, and `*/30 2 * * *` runs four times on the fall-back night), and to other zones on their own dates (01:30 runs twice in `America/New_York` on 2026-11-01). To avoid it, schedule outside 01:00-03:00 local time, use UTC, or make the job idempotent so a second run does no harm.

---

## How It Works

### Wait mechanism

`CronTrigger` uses `asyncio.wait_for` with an `asyncio.Event` to implement cancellable sleeping:

1. Calculate the next fire time from the cron expression, starting from now.
2. Compute the delay in seconds from now.
3. Sleep for the delay using `wait_for(event.wait(), timeout=delay)`.
4. If the event is set (by `stop()`), raise `CancelledError`.
5. If the timeout expires naturally, produce a `TriggerEvent`.

This design allows `stop()` to immediately unblock a waiting trigger rather than sleeping for the full delay.

### Ticks, runs and the queue

In a process, the trigger's listener runs independently of the workers. A tick that comes due while the agent is still running is queued and runs after the current run (with the default `concurrency=1`); ticks are not merged or skipped. If a run takes longer than the interval, the queue grows by one event per tick.

The next fire time is always computed from the current time. Ticks that fell into a period when the process was stopped, or the machine was asleep, are **not** caught up: after a restart the trigger simply waits for the next one.

A suspended process keeps queueing ticks and runs them in order after `resume()`. A process suspended by `idle_timeout` resumes by itself when the next tick arrives (see [suspension](index.md#what-happens-when-the-process-is-suspended)).

### Event payload

```python
{
    "scheduled_time": "2026-03-04T10:05:00+00:00",
    "cron_expression": "*/5 * * * *",
    "timezone": "UTC"
}
```

`scheduled_time` is the tick the event was produced for, not the time the agent started running.

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

To change a running process's schedule, add the new trigger and remove the old one with [`process.add_trigger()` / `process.remove_trigger()`](index.md#adding-and-removing-triggers-at-runtime) or the API. Both take effect at once.

---

## API Summary

| Method / Property | Description |
|---|---|
| `CronTrigger(cron_expression, *, trigger_id, timezone)` | Create a cron trigger (raises `TriggerError` on a bad expression or time zone) |
| `trigger_id` | Identifier (standalone default: `cron-XXXXXXXX`; in a process: the stable config ID) |
| `await start()` | Mark the trigger as active |
| `await stop()` | Stop and unblock any waiters |
| `await wait_for_next()` | Block until the next scheduled time |

---

## Common pitfalls

- **UTC unless told otherwise.** `0 9 * * *` is 09:00 **UTC**, not server-local time. Set `cron_timezone` when the schedule follows office hours.
- **Jobs between 01:00 and 03:00 local time.** On DST nights they run twice or shift (see [above](#jobs-in-the-hour-that-dst-changes)).
- **Runs longer than the interval.** Ticks queue up behind the running one. Lengthen the interval, raise `concurrency` (runs then share context and conversation), or keep runs short.
- **Expecting catch-up.** Ticks missed while the process was down are lost. If a job must cover a period, have the agent work from "everything since the last successful run" (kept in [context state](../context.md)), not from the tick time.
- **Sub-minute schedules.** `* * * * * */10` fires every 10 seconds, and each firing is a full agent run. Watch the cost; an [autonomy budget](../governance/budget.md) with `max_runs_per_day` puts a ceiling on it.
- **Clock drift.** The trigger computes the next fire time from the system clock. Use NTP synchronization in production.
- **Several replicas.** Every process with the trigger fires it. Run a scheduled process in one place only.

!!! info "Validated up front"
    Invalid expressions and unknown time zones are rejected when the config is created, so `promptise runtime validate` and process start-up both catch them; a running process never ends up retrying a broken schedule.

---

## What's Next

- [Triggers Overview](index.md) -- all trigger types, stable IDs and live trigger changes
- [Event and Webhook Triggers](event-webhook.md) -- event-driven activation
- [File Watch Trigger](file-watch.md) -- file-system change detection
- [Autonomy Budget](../governance/budget.md) -- cap runs and tool calls per day

!!! tip "Learn the concept"

    - [Event-Driven AI Agents: Webhook, Cron and File Triggers](https://www.promptise.com/guides/view/event-driven-ai-agents-webhook-cron-triggers) -- Scheduled agents alongside webhook and file triggers.
    - [AI Agent Cron Jobs: Run an Agent on a Schedule](https://www.promptise.com/guides/view/ai-agent-cron-schedule) -- Time zones and DST, overlapping runs, missed ticks and testing.
