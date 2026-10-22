---
seo_title: "Autonomy Budget: Limit Agent Actions and Cost"
description: "Limit what an autonomous agent does on its own: tool calls, LLM turns, cost units and irreversible actions per run and per day, enforced before each call."
---

# Autonomy Budget

An autonomy budget puts explicit limits on what an agent may do on its own: tool calls, LLM turns, abstract cost units and irreversible actions, per run and per day. The runtime refuses a tool call that would cross a limit before the tool runs, and then pauses, stops or escalates the process. The agent can see what is left of its budget, so it can plan around it.

```python
from promptise.runtime import ProcessConfig, BudgetConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Process customer support tickets.",
    budget=BudgetConfig(enabled=True, max_tool_calls_per_run=20, max_runs_per_day=200),
)
```

## When you need it

Without a budget, an autonomous agent can loop indefinitely, call expensive APIs without limit, or take irreversible actions at 3am with no guardrails. The autonomy budget defines the envelope within which the agent operates freely. Anything outside that envelope stops the agent until a human looks at it.

## Configuration

```python
from promptise.runtime import ProcessConfig, BudgetConfig, EscalationTarget, ToolCostAnnotation

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Process customer support tickets.",
    budget=BudgetConfig(
        enabled=True,
        max_tool_calls_per_run=20,
        max_llm_turns_per_run=10,
        max_cost_per_run=25.0,
        max_tool_calls_per_day=500,
        max_cost_per_day=100.0,
        tool_costs={
            "stripe_charge": ToolCostAnnotation(cost_weight=5.0, irreversible=True),
            "send_email": ToolCostAnnotation(cost_weight=2.0, irreversible=True),
            "search": ToolCostAnnotation(cost_weight=0.5),
        },
        on_exceeded="escalate",   # "pause", "stop", or "escalate"
        daily_reset_hour_utc=6,   # the budget day runs 06:00-06:00 UTC
        inject_remaining=True,    # Show budget in agent context
        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/...",
        ),
    ),
)
```

Or in a `.agent` manifest:

```yaml
name: support-agent
model: openai:gpt-5-mini
budget:
  enabled: true
  max_tool_calls_per_run: 20
  max_cost_per_day: 100.0
  on_exceeded: escalate
  inject_remaining: true
  tool_costs:
    stripe_charge:
      cost_weight: 5.0
      irreversible: true
    search:
      cost_weight: 0.5
  escalation:
    webhook_url: "https://hooks.slack.com/..."
```

An unknown key under `budget:` is rejected in a manifest; in Python, `BudgetConfig` ignores it with a `FutureWarning`.

## How it works

### Per-run and daily tracking

The budget system tracks two scopes:

- **Per-run**: Resets at the start of every invocation. Limits how much the agent can do in a single trigger-driven execution.
- **Per-day**: Limits total daily activity. The budget day runs from `daily_reset_hour_utc` (default `0`, midnight UTC) to the same hour the next day.

The daily counters are reset when a run starts and the last reset happened before the most recent reset hour. With `daily_reset_hour_utc=6`, a run at 05:59 UTC still counts toward the previous day, and the first run at or after 06:00 UTC resets the counters and emits `budget.daily_reset`. Nothing resets while no run starts, so `process.status()["budget"]` can show the previous day's usage until the next run.

### Counters survive restarts

Within one process object, the counters live in memory: `stop()` / `start()` and `restart_process()` keep them.

With a [journal](../journal/index.md), they also survive a new process object (a restarted runtime, a redeploy under the same name). Every checkpoint stores the budget counters: after each run, after a failed run, after a run refused by `max_runs_per_day`, and on `stop()`. On the first `start()`, with `journal.recover_on_start` on (the default), the daily counters and the time of the last daily reset are restored, so a restart can't reset the daily limits. The per-run counters start at zero. `process.recovered["budget_restored"]` says whether it happened.

```python
from promptise.runtime import BudgetConfig, JournalConfig, ProcessConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    journal=JournalConfig(level="checkpoint", path=".promptise/journal"),
    budget=BudgetConfig(enabled=True, max_tool_calls_per_day=500),
)
```

If the restored counters are from before the most recent reset hour, the next run resets them as usual. A journal that has no checkpoint for the process name, or a checkpoint without budget counters, leaves them at zero.

---

## Understanding cost_weight

!!! danger "Critical: cost_weight is NOT real money"
    Budget cost is measured in **abstract weight units that you define**, not dollars. A `cost_weight=5.0` means "5 budget units" — it does NOT mean $5. The framework does not connect to any LLM provider's pricing API, does not know your token rates, and does not track actual monetary spend.

    **You** are responsible for defining what the numbers mean and keeping them aligned with reality.

### What the budget tracks and what it does NOT

| Tracked | Not tracked |
|---------|-------------|
| Tool calls (counted and weighted by `cost_weight`) | LLM API costs (token pricing from OpenAI, Anthropic, etc.) |
| LLM turns (counted, but with zero cost) | Actual dollar spend on any provider |
| Irreversible actions (counted) | Infrastructure costs (compute, network, storage) |
| Abstract cost units (sum of `cost_weight` values) | Real-time exchange rates or dynamic pricing |

**This means:** An agent with `max_cost_per_day=100.0` and zero tool cost annotations can make unlimited LLM API calls consuming thousands of dollars in tokens — the budget system will not stop it, because LLM turns have no cost weight.

The budget system controls **what the agent does** (which tools it calls, how many times), not **how much it costs the provider**.

### Default behavior for unannotated tools

Any tool without a `ToolCostAnnotation` entry defaults to `cost_weight=1.0`. This means:

```python
# These two are identical:
tool_costs={"search": ToolCostAnnotation(cost_weight=1.0)}
# vs. not listing "search" at all (it defaults to 1.0)
```

If you want a tool to be "free" (not consume budget), set `cost_weight` to a very small value:

```python
tool_costs={"health_check": ToolCostAnnotation(cost_weight=0.01)}
```

---

## Designing your cost scale

### Approach 1: Relative risk weighting (recommended)

Assign weights based on how expensive or risky the tool is **relative to a baseline read operation** (weight 1.0). This is the simplest and most robust approach — you don't need to know actual dollar costs.

```python
tool_costs={
    # Baseline: internal reads — cheap, low risk
    "search_docs": ToolCostAnnotation(cost_weight=0.5),
    "get_user": ToolCostAnnotation(cost_weight=1.0),       # 1.0 = baseline

    # Write operations — moderate risk
    "update_record": ToolCostAnnotation(cost_weight=2.0),
    "send_email": ToolCostAnnotation(cost_weight=3.0, irreversible=True),

    # External API calls — expensive, high risk
    "stripe_charge": ToolCostAnnotation(cost_weight=10.0, irreversible=True),
    "deploy_production": ToolCostAnnotation(cost_weight=20.0, irreversible=True),
}
```

**How to set limits with this approach:**

Think in terms of operations, not dollars:

- `max_cost_per_run=50.0` → the agent can do 50 searches, or 5 Stripe charges, or 25 email sends, or any mix
- `max_cost_per_day=500.0` → daily limit of ~500 baseline operations

This approach works because you're limiting **agent behavior**, not monetary spend. You don't need to update weights when provider prices change.

### Approach 2: Dollar-based approximation

If you want cost units to roughly map to real money, assign weights based on the actual cost you incur per tool call:

```python
tool_costs={
    # ~$0.01 per call (internal DB query, negligible infrastructure cost)
    "search": ToolCostAnnotation(cost_weight=0.01),

    # ~$0.10 per call (third-party geocoding API with per-request pricing)
    "geocode_address": ToolCostAnnotation(cost_weight=0.10),

    # ~$2.50 per call (Stripe processing fee)
    "stripe_charge": ToolCostAnnotation(cost_weight=2.50, irreversible=True),

    # ~$0.50 per call (email delivery service fee)
    "send_email": ToolCostAnnotation(cost_weight=0.50, irreversible=True),
}
# max_cost_per_day=50.0 → approximately $50/day in tool-related costs
```

!!! warning "Dollar-based weights require manual maintenance"
    The framework does NOT validate these weights against real pricing. If Stripe raises their fee to $3.00, you must manually update `cost_weight=3.00`. If your email provider changes pricing tiers, the weights become inaccurate silently.

    **Dollar-based weights also do NOT include LLM API costs.** An agent making 100 GPT-5 calls at $0.015/1K tokens could spend $15 in LLM costs alone — none of this is tracked by the budget.

### Approach 3: Combined with external cost tracking

For production systems that need real monetary limits, combine the budget system with external cost tracking:

```python
from promptise.runtime import ProcessConfig, BudgetConfig, ToolCostAnnotation

config = ProcessConfig(
    model="openai:gpt-5-mini",
    budget=BudgetConfig(
        enabled=True,

        # Use relative weights for tool budget
        max_cost_per_day=500.0,
        tool_costs={
            "stripe_charge": ToolCostAnnotation(cost_weight=10.0, irreversible=True),
            "search": ToolCostAnnotation(cost_weight=0.5),
        },

        # Use quantity limits for LLM cost control
        max_llm_turns_per_run=15,     # Cap LLM turns per invocation
        max_tool_calls_per_day=1000,  # Cap total daily tool calls

        on_exceeded="pause",
    ),
)
```

For actual dollar-level cost control, track LLM spending through:

- **Your LLM provider's dashboard** (OpenAI usage page, Anthropic console, etc.)
- **OpenAI/Anthropic API usage headers** (`x-ratelimit-remaining-tokens`, usage metadata)
- **Promptise observability** — token counts are tracked per invocation via `get_stats()` and can be exported to Prometheus/Grafana for alerting on real spend

The budget system handles **behavioral limits** (what the agent is allowed to do). External systems handle **monetary limits** (how much the infrastructure costs).

---

## Context injection

When `inject_remaining=True`, the agent sees remaining budget in a system message before every invocation. It lists every limit, `None` for one that is not set (with `max_tool_calls_per_run=20`, `max_irreversible_per_run=2` and `max_runs_per_day=50`, at the start of a run):

```
[Budget Remaining] {'tool_calls_run': 20, 'llm_turns_run': None, 'cost_run': None, 'irreversible_run': 2, 'tool_calls_day': None, 'runs_day': 49, 'cost_day': None}
```

The values are what is left of each limit (`BudgetState.remaining()`); the run that is starting already counts toward `runs_day`. The same dict is `process.status()["budget"]`.

This lets the agent prioritize — if it's running low on budget, it can choose cheaper tools or skip non-essential operations. In open mode, the agent can also call `check_budget` to inspect remaining limits.

---

## Per-call enforcement

Tool-call, cost and irreversible-action limits are checked **before each tool call runs**. A call that would push a counter past its limit (or any call once `max_llm_turns_per_run` is exceeded) is refused: the tool body never executes, the call is not counted, and the agent receives a `BudgetExceededError` as the tool's result, telling it to stop calling tools and answer. Parallel tool calls in one turn are checked one by one, so a limit of 2 lets exactly 2 of 3 parallel calls through.

```python
from promptise.runtime import BudgetExceededError  # error the agent sees as the tool result
```

The message the agent gets looks like this:

```text
Budget limit max_tool_calls_per_day reached (limit 5); tool call 'send' was not executed. Stop calling tools and give your final answer.
```

LLM turns are counted but not blocked: the turn that exceeds `max_llm_turns_per_run` still runs, and every tool call after it is refused.

A refused call never runs, so the [journal](../journal/index.md) has no `tool_call` entry for it. It gets a `tool_denied` entry instead (with the call's `args` at journal level `"full"`), and the `permission_denied` [hook](../hooks.md) fires with `kind="budget"` and the call's `arguments`. The violation the process acts on is journaled as `budget_violation`.

After the invocation finishes, the first violation of the run triggers the action configured in `on_exceeded` (below). `max_runs_per_day` is checked when an invocation starts; a run over that limit does not start, and `on_exceeded` is applied at once. Its event goes to the [dead letters](../processes.md#dead-letters) with reason `"budget: max_runs_per_day"` (the `task_completed` hook reports outcome `"refused"`), so `redeliver_dead_letters()` can run it after the daily reset.

!!! note "Changed in 1.3.0"
    Earlier versions counted tool calls and applied `on_exceeded` only after the invocation had finished, so an agent could exceed `max_irreversible_per_run` (or any per-run limit) within a single run.

---

## Enforcement actions

When a limit is exceeded (after the invocation in which it happened, or when a run is refused by `max_runs_per_day`):

| Action | Behavior |
|--------|----------|
| `"pause"` (default) | Suspend the process (`suspend_reason == "budget"`). Resume it with `resume()` or the API. |
| `"stop"` | Stop the process. Start it again by hand. |
| `"escalate"` | Send the escalation notification when `escalation` is set (webhook and/or event bus), then suspend. |

A process paused by the budget does not resume by itself, not even after the daily reset hour: the reset only happens when a run starts. Its trigger events keep queueing and run in order after `resume()`.

Escalation sends a POST to the configured webhook URL (and emits the same payload as `escalation.event_type` on the runtime's event bus) with the violation:

```json
{
  "type": "budget_violation",
  "process_id": "0c8e5d0a-6b8f-4c56-9a52-0f4f3c1e9b7d",
  "process_name": "support-agent",
  "violation": {
    "limit_name": "max_cost_per_day",
    "limit_value": 100.0,
    "current_value": 102.5,
    "tool_name": "send_email"
  }
}
```

`limit_name` is the `BudgetConfig` field that was exceeded; `current_value` is the value the refused call would have produced; `tool_name` is the tool whose call exceeded it, or `null` when no tool call did (a run over `max_runs_per_day`, or a model turn over `max_llm_turns_per_run`).

The webhook URL must resolve to a public address (SSRF protection). The host is resolved before every notification, the notification is dropped with a warning if any address is private, loopback, link-local (cloud metadata) or otherwise internal, and the POST goes to the address that was checked, so a DNS record that changes in between (DNS rebinding) cannot send it to an internal service. Redirects are not followed. The same applies to health and mission escalations.

---

## Events

| Event | Severity | When | Payload |
|---|---|---|---|
| `budget.warning` | warning | After a run, for each limit at `warning_threshold` (default 80%) or more of its value (and not over it) at the run's last budget check | `process_name`, `limit_type`, `current`, `limit`, `percentage` |
| `budget.exceeded` | critical | After a run with a violation (once per run, for the first one), and when a run is refused by `max_runs_per_day` | `process_name`, `limit_type`, `current`, `limit` |
| `budget.daily_reset` | info | When a run start resets the daily counters | `process_name` |

`limit_type` is the `BudgetConfig` field name, for example `max_tool_calls_per_day`. The threshold is `warning_threshold` (default `0.8`). A run refused by `max_runs_per_day` emits `budget.exceeded` with `limit_type: "max_runs_per_day"` and then applies `on_exceeded`; when `on_exceeded` suspends the process (`"pause"` or `"escalate"`), `process.suspended` follows with `reason: "budget"`. Events reach the runtime's [event notifier](../../core/events.md) sinks.

---

## Changing limits while it runs

Every `BudgetConfig` field can change on a running process through `PATCH /api/v1/processes/{name}/budget` (or `client.update_budget(name, **fields)`):

```bash
curl -X PATCH http://127.0.0.1:9100/api/v1/processes/support-agent/budget \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"max_tool_calls_per_day": 1000}'
# {"name": "support-agent", "budget_updated": true, "effective_from": "next_budget_check", "live": true}
```

The patch is validated as a whole first (`422 UNKNOWN_FIELD` for a misspelt field, `422 INVALID_CONFIG` for a bad value; nothing changes then). The counters are kept, and each value applies at the next check that reads it:

| Field | Takes effect |
|---|---|
| Tool-call, cost and irreversible limits, `tool_costs` | The next tool call, also inside a run already in progress |
| `max_llm_turns_per_run` | The next LLM turn |
| `max_runs_per_day`, `daily_reset_hour_utc`, `inject_remaining` | The next run start |
| `on_exceeded`, `escalation` | The next violation |
| `warning_threshold` | The warnings computed after the next run |
| `enabled: true` | Builds the budget with zero counters; applies from the next run |
| `enabled: false` | Drops the budget and its counters |

Lowering a limit below what was already used blocks the next tool call. Raising a limit does not resume a process that the budget paused; call `resume()`.

In Python, change `process.config.budget` and call `process.apply_governance_config("budget")`.

---

## Irreversible action tracking

Tools marked `irreversible=True` are tracked separately. Use `max_irreversible_per_run` to limit how many destructive actions the agent can take per invocation:

```python
budget=BudgetConfig(
    max_irreversible_per_run=2,  # Max 2 irreversible actions per invocation
    tool_costs={
        "delete_record": ToolCostAnnotation(cost_weight=3.0, irreversible=True),
        "send_email": ToolCostAnnotation(cost_weight=1.0, irreversible=True),
        "search": ToolCostAnnotation(cost_weight=0.5),  # Not irreversible
    },
)
```

The agent can call `search` as many times as the cost budget allows, but it can only call `delete_record` and/or `send_email` a combined total of 2 times per invocation.

A call an [approval gate](../../core/approval.md) denies did nothing irreversible, so it gives its irreversible action back: with `max_irreversible_per_run=2`, two denied `delete_record` calls still leave room for two approved ones. It still counts as a tool call and against the cost budget.

---

## Common pitfalls

- **`cost_weight` is not money.** LLM token spend is not tracked at all; see [Understanding cost_weight](#understanding-cost_weight).
- **No journal, no persistence across runtimes.** Without a `JournalConfig`, a new process object starts with zero daily counters.
- **A daily limit pauses until someone resumes.** With `on_exceeded="pause"`, a process that hits `max_runs_per_day` stays suspended past the reset hour. Resume it on a schedule, or use `"stop"` and restart it, if that is what you want.
- **`escalate` without a target.** The process is suspended, but nobody is notified. Set `escalation`.
- **Disabling to "reset".** `PATCH {"enabled": false}` drops the counters; enabling again starts from zero. That defeats the daily limit.
- **Tool names in `tool_costs`.** Keys must match the tool names the agent sees (MCP tool names); an unmatched key has no effect and the tool costs `1.0`.

---

## API reference

### `BudgetConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `False` | Enable budget tracking |
| `max_tool_calls_per_run` | `int \| None` (`>= 1`) | `None` | Per-invocation tool call limit |
| `max_llm_turns_per_run` | `int \| None` (`>= 1`) | `None` | Per-invocation LLM turn limit |
| `max_cost_per_run` | `float \| None` (`> 0`) | `None` | Per-invocation cost limit (abstract units) |
| `max_irreversible_per_run` | `int \| None` (`>= 0`) | `None` | Per-invocation irreversible action limit |
| `max_tool_calls_per_day` | `int \| None` (`>= 1`) | `None` | Daily tool call limit |
| `max_runs_per_day` | `int \| None` (`>= 1`) | `None` | Daily invocation limit |
| `max_cost_per_day` | `float \| None` (`> 0`) | `None` | Daily cost limit (abstract units) |
| `tool_costs` | `dict[str, ToolCostAnnotation]` | `{}` | Per-tool cost annotations |
| `on_exceeded` | `"pause" \| "stop" \| "escalate"` | `"pause"` | Action on violation |
| `escalation` | `EscalationTarget \| None` | `None` | Where `"escalate"` sends violation notifications |
| `daily_reset_hour_utc` | `int` (0-23) | `0` | UTC hour at which the budget day starts |
| `inject_remaining` | `bool` | `True` | Show remaining budget in agent context |
| `warning_threshold` | `float` (0-1, above 0) | `0.8` | Fraction of a limit at which `budget.warning` is emitted |

### `ToolCostAnnotation`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `cost_weight` | `float` | `1.0` | Abstract cost units consumed per call. Must be > 0. |
| `irreversible` | `bool` | `False` | Whether this tool performs an irreversible action (delete, send, charge). |

### `BudgetState`

`BudgetState(config)`; the process creates it for you.

| Method | Description |
|--------|-------------|
| `.record_tool_call(tool_name, *, enforce=False)` | Record a tool call and add its `cost_weight` to run and daily totals. Returns a `BudgetViolation` if any limit is exceeded, or `None`. With `enforce=True` the call is checked first: if it would exceed a limit it is **not** counted and the violation has `blocked=True` (the caller must not run the tool). |
| `.record_tool_call_sync(tool_name, *, enforce=False)` | Thread-safe synchronous form, used by the runtime's budget guard (synchronous tools run on worker threads). |
| `.record_llm_turn()` | Record an LLM turn (counts toward `max_llm_turns_per_run` only, no cost). Returns a `BudgetViolation` if the limit is exceeded. |
| `.record_run_start()` | Count a run toward `max_runs_per_day`; returns a violation when it is over the limit. |
| `.reset_run()` | Reset per-run counters. Called automatically at the start of each invocation. |
| `.check_daily_reset()` | Reset the daily counters if a reset hour has passed since the last reset; returns `True` when it did. |
| `.remaining()` | Dict of what is left per limit (`tool_calls_run`, `llm_turns_run`, `cost_run`, `irreversible_run`, `tool_calls_day`, `runs_day`, `cost_day`); `None` = no limit, negative = exceeded. |
| `.snapshot()` | The counters as a JSON-safe dict (what checkpoints store). |
| `.restore(data)` | Load a snapshot: the daily counters and `last_daily_reset`; per-run counters start at zero. Raises `ValueError` for a malformed snapshot, changing nothing. |
| `.pending_warnings` | `BudgetWarning`s for limits at `warning_threshold` or more after the latest check. |

### `BudgetViolation`

| Field | Description |
|-------|-------------|
| `limit_name` | The `BudgetConfig` field that was exceeded |
| `limit_value` | The configured maximum |
| `current_value` | The counter value that exceeded it (for a refused call: the value the call would have produced) |
| `tool_name` | The tool that triggered it, or `None` |
| `blocked` | `True` when the tool call was refused before it ran |
| `arguments` | The refused call's arguments (a refused call only), or `None` |

`BudgetWarning` has `limit_name`, `limit_value`, `current_value` and `percentage` (0.0-1.0).

### `BudgetEnforcer`

| Method | Description |
|--------|-------------|
| `.handle_violation(violation, process)` | Execute the configured enforcement action (pause, stop, or escalate). |

## See also

- [Behavioral Health](health.md) -- detect stuck, looping or failing agents
- [Mission](mission.md) -- invocation and time limits for goal-driven processes
- [Journal](../journal/index.md) -- the checkpoints that carry budget counters across restarts
- [Orchestration API](../api.md) -- patch budgets and resume processes over HTTP
- [Events & Notifications](../../core/events.md) -- deliver `budget.*` events

!!! tip "Learn the concept"

    - [AI Agent Cost Control: Budgets That Stop Runaway Spend](https://www.promptise.com/guides/view/ai-agent-cost-control-budget) -- Budgets that refuse tool calls before runaway spend.
    - [Topic hub: Cost control and rate limits](https://www.promptise.com/guides/topics/cost-control) -- Every guide on cost control and rate limits.
