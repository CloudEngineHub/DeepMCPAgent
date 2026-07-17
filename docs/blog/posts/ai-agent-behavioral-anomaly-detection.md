---
title: "Catch a Runaway AI Agent: Behavioral Anomaly Detection"
description: "A wedged agent isn't always expensive per call — it can quietly repeat the same tool with the same arguments, bounce between two tools, or emit empty replies…"
keywords: "ai agent behavioral anomaly detection, detect runaway ai agent, agent stuck calling same tool, agent empty response detection, agent error rate monitoring, stop agent infinite loop"
date: 2026-07-16
slug: ai-agent-behavioral-anomaly-detection
categories:
  - Runtime
---

# Catch a Runaway AI Agent: Behavioral Anomaly Detection

**AI agent behavioral anomaly detection** is what tells you *which* way your agent has gone wrong — a poller frozen on `get_status(id=42)`, a `search → read` cycle that never converges, an agent answering with empty strings, or one whose tool calls are half-failing — by watching the *pattern* of its calls instead of merely counting them. A runaway agent is not always expensive per call. The quiet failures cost you a whole night of the same cheap request, or a slow drip of trivial replies, while every infrastructure dashboard stays green because CPU and memory look fine. This post shows how the Promptise Foundry runtime catches those failures with four pure-pattern-matching detectors — no extra LLM calls — how to tell a behavioral stall apart from a context-bloat loop so you apply the *right* fix, and how the runtime pauses or escalates a live process the moment a detector trips.

<!-- more -->

## Two agents look "stuck." They fail for opposite reasons.

Two very different failures both get reported as "the agent is stuck in a loop," and they need opposite fixes. Confusing them is the reason people spend a day tuning the wrong knob.

The first is a **context-bloat loop**. On a deep tool task the transcript grows unbounded; once it is large enough the model loses the thread and re-fetches facts it already has, its answer never arriving while the token bill climbs. This is not a behavioral problem at all — it is a context-window and attention problem, and the fix is to bound what the model sees per turn with the reasoning engine's `context_scope` lever. We cover that failure end to end in [When Agent Tool Loops Fail: Fixing Context Bloat](agent-stuck-in-tool-loop.md). The tell is in the *arguments*: a bloating agent usually keeps making distinct-looking calls (a new page, a new id, a query it already ran) as its recall degrades. The problem is the transcript, not the pattern.

The second is a **behavioral stall**. Here the agent can be three turns in, with a tiny transcript that fits comfortably in the window, and still be pinned on the same call forever. No amount of context management catches that, because nothing is wrong with the context — the wrongness is in the *behavior over time*. Catching it needs a monitor that watches the sequence of actions, not a prompt that trims the transcript. That is what behavioral anomaly detection is: pure pattern matching over tool-call and response history, with no LLM in the loop. The rest of this post is about that second failure and the runtime subsystem built for it.

## Four detectors define what "misbehaving" means

Promptise Foundry's runtime ships a `HealthMonitor` with four detectors. Each one owns a distinct failure mode, and each is plain Python over a bounded window of recent history — no model call, no embeddings, no network. The full API lives in the [behavioral health monitoring reference](../../runtime/governance/health.md); here is what each detector actually watches.

| Detector | `AnomalyType` | What it watches | The failure it catches |
|----------|---------------|-----------------|------------------------|
| Stuck | `STUCK` | The last N tool calls being *identical* — same tool, same arguments | An agent stuck calling the same tool with the same input, retrying the exact same thing |
| Loop | `LOOP` | A repeating *subsequence* of any length within the last `loop_window` calls | A `search → read` cycle that never converges but looks busy |
| Empty response | `EMPTY_RESPONSE` | N consecutive responses at or under `empty_max_chars` | Degenerate, near-empty output — the model quietly gave up |
| Error rate | `HIGH_ERROR_RATE` | The failure fraction across a sliding window | A tool or upstream API that started failing systematically |

The **stuck versus loop** distinction is the one people conflate. Stuck detection fingerprints `(tool_name, hash(arguments))` and fires only when the last `stuck_threshold` calls are byte-identical — a hard freeze. Loop detection is broader: it scans the recent window for any repeating pattern of length two or more, so `search → read → search → read` trips it even though no two *consecutive* calls match. They catch structurally different failures; keep both on. Agent empty response detection is deliberately trivial — it counts consecutive replies whose stripped length is at or below `empty_max_chars` (default 10) — and error rate monitoring rounds out the set so that "the agent is misbehaving" has a concrete, testable definition instead of a vibe.

Two properties make this safe to leave on in production. **Detection is free**: every check is a comparison over a `deque`, so it adds no token spend and no measurable latency at the exact moment things are already going wrong. And every anomaly type has its own `cooldown` (default 300 seconds), so once a `LOOP` fires the monitor stays quiet on loops until the cooldown elapses — a genuinely stuck agent produces one alert per type, not a thousand.

## Diagnose it in code: which loop do you actually have?

Because detection is pure pattern matching, you can exercise the whole thing offline — no API key, no model, no network. The script below drives three `HealthMonitor` instances through three call sequences: a context-growth loop whose arguments *advance* every turn, a true behavioral stall on one identical call, and a failing dependency. Watch which ones trip, and — just as importantly — which one stays silent.

```python
import asyncio

from promptise.runtime import HealthConfig, HealthMonitor


async def main() -> None:
    cfg = HealthConfig(
        enabled=True,
        stuck_threshold=3,        # 3 identical calls in a row = stuck
        loop_window=12,           # scan the last 12 calls for a repeating cycle
        error_window=4,           # sliding window for the error-rate detector
        error_rate_threshold=0.5, # 50%+ failures in the window = anomaly
    )

    # 1) A *context-growth* loop: the agent keeps reading, but every call
    #    advances to a new page. Nothing is wrong with the *pattern* of calls,
    #    so the behavioral monitor stays silent. The fix here is context_scope.
    growth = HealthMonitor(cfg, process_id="reader")
    hit = None
    for page in range(6):
        hit = await growth.record_tool_call("read_page", {"cursor": page})
        if hit:
            break
    print("context-growth loop ->",
          hit.anomaly_type.value if hit else "no behavioral anomaly")

    # 2) A *behavioral* stall: the same call, byte-for-byte, forever.
    #    The STUCK detector trips on the third identical call.
    stall = HealthMonitor(cfg, process_id="poller")
    for _ in range(5):
        hit = await stall.record_tool_call("get_status", {"id": 42})
        if hit:
            print(f"behavioral stall     -> [{hit.anomaly_type.value}] {hit.description}")
            break

    # 3) A failing dependency: most tool calls throw. HIGH_ERROR_RATE trips
    #    once the sliding window fills.
    flaky = HealthMonitor(cfg, process_id="caller")
    err = None
    for failed in (True, False, True, True):  # True = the tool call raised
        if failed:
            err = await flaky.record_error()
        else:
            await flaky.record_success()
    label = f"[{err.anomaly_type.value}] {err.description}" if err else "healthy"
    print("failing dependency   ->", label)


asyncio.run(main())
```

The runtime logs each registered anomaly at `WARNING` level; the standard-output summary is:

```text
context-growth loop -> no behavioral anomaly
behavioral stall     -> [stuck] Agent stuck: tool 'get_status' called 3 times consecutively with identical arguments
failing dependency   -> [high_error_rate] High error rate: 75% errors in last 4 invocations (threshold: 50%)
```

That first line is the diagnostic payoff. The context-growth loop makes six real calls and the monitor stays silent, because the arguments advance every turn — which is exactly the signal that this is *not* a behavioral stall. If your agent is looping but the health monitor says "no anomaly," stop tuning thresholds and go bound the transcript with `context_scope`. The stall and the failing dependency, by contrast, are caught deterministically: the third identical call trips `STUCK`, and the error-rate window trips `HIGH_ERROR_RATE` at 75%. In a live agent you never call `record_tool_call` yourself — the runtime feeds the monitor as the agent works — but this standalone form lets you unit-test your thresholds against real call traces before you ship them.

## What other frameworks do today

To be fair to the ecosystem: every mainstream framework gives you a way to stop a runaway, and those primitives are real and useful. The honest gap is that they are *counters, caps, or hooks* — not a behavioral-health subsystem that discriminates *which* failure occurred and responds to each differently.

- **LangGraph** enforces a `recursion_limit` (default 25) that raises `GraphRecursionError` when a graph runs too long. It is a step counter. Because LangGraph is a low-level graph runtime, you *can* hand-write a conditional edge that inspects state and flags a repeated pattern — but you author, host, and tune that logic yourself; there is no built-in stuck/loop/empty/error-rate subsystem.
- **CrewAI** gives an `Agent` a `max_iter` (reasoning iterations per task), a `max_rpm` requests-per-minute self-throttle, and `max_execution_time`. Those bound how *hard* and how *fast* an agent works. They do not inspect the content of the call sequence for a repeating cycle, degenerate output, or a rising failure rate.
- **AutoGen** is the closest partial: alongside `MaxMessageTermination` and `max_consecutive_auto_reply`, it exposes an `is_termination_msg` callback. That callback is a genuine hook — you could write one that spots a repeated message and terminates. The exact delta is that *you write the detector*; there is no shipped, tuned loop/empty/error-rate monitor, just a place to bolt one on.
- **Pydantic AI** offers `UsageLimits` (`request_limit`, `total_tokens_limit`) that raise `UsageLimitExceeded`. That is budget enforcement — adjacent to, but distinct from, behavioral anomaly detection.
- **LlamaIndex** ReAct agents take a `max_iterations` that raises once exceeded. A counter again.

Every one of these bounds a runaway *eventually*, which is genuinely valuable. But a count of 25 says nothing about *what* those calls were — forty `get_status(id=42)` polls and forty distinct, productive calls look identical to an integer — so it cannot tell "stuck on one call" from "looping between two" from "answering empty" from "erroring repeatedly," and it cannot respond to each differently. Promptise's edge is not that the others "can't count." It is that Promptise makes the four detectors, their thresholds, cooldowns, and the pause/escalate response one **first-class, structural property of a running process** you turn on with a config object — not a component you author, tune, and remember to wire into every agent.

## Graduated response over a live process: log, pause, escalate

In a running agent you do not touch `HealthMonitor` directly. You attach a `HealthConfig` to a `ProcessConfig`, and the runtime drives detection on every tool call and response of a supervised [agent process](../../runtime/processes.md). The `on_anomaly` action decides what happens the instant a detector trips:

```python
from promptise.runtime import ProcessConfig, HealthConfig, EscalationTarget

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Poll each submitted job until it reaches a terminal state.",
    health=HealthConfig(
        enabled=True,
        stuck_threshold=3,
        loop_window=20,
        empty_threshold=3,
        error_rate_threshold=0.5,   # 50%+ failures in the window
        on_anomaly="escalate",      # "log", "pause", or "escalate"
        cooldown=300,               # one alert per anomaly type per 5 min
        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/services/XXX",
            event_type="agent.health.anomaly",
        ),
    ),
)
```

The three enforcement actions are the whole graduated-response story:

| Action | Behavior |
|--------|----------|
| `"log"` | Record the anomaly and keep running — ideal for tuning thresholds in staging |
| `"pause"` | Suspend the process so a human can inspect it |
| `"escalate"` | Fire the escalation notification (webhook plus an EventBus event), then suspend |

Because the action lands on the *process*, not on your prompt, it survives across trigger-driven invocations — the deeper reason a runtime beats an in-call counter. `"escalate"` is the clean hand-off to a human, the pattern in [Escalate to a Human When an AI Agent Keeps Failing](escalate-ai-agent-to-human.md); if you need a hard kill rather than a suspend, pair health with the runtime kill switches in [How to Stop a Runaway AI Agent](stop-a-runaway-ai-agent.md).

Every anomaly the monitor registers is also written to the process [journal](../../runtime/journal/index.md) as part of the append-only event stream — which is what separates this from an ephemeral log line. The record is durable: if the process crashes and the runtime rebuilds it, the anomaly history comes back with it, part of the [durable execution](durable-execution-for-ai-agents.md) guarantee the runtime makes for every supervised process. That append-only stream is a different unit from a graph checkpoint snapshot; the distinction, and why it matters for a self-triggering process, is drawn out in [LangGraph Checkpointing vs Journal-Replay Explained](langgraph-checkpointing-vs-journaling.md).

The same block works declaratively in an `.agent` manifest:

```yaml
name: job-poller
health:
  enabled: true
  stuck_threshold: 3
  loop_window: 20
  error_rate_threshold: 0.5
  on_anomaly: escalate
  cooldown: 300
  escalation:
    webhook_url: "https://hooks.slack.com/services/XXX"
```

Health is deliberately not the whole safety envelope. It answers "is the agent behaving?"; a [per-run and daily budget](../../runtime/governance/budget.md) answers "is the agent doing too much?" Run both and a stuck poller trips the health wire in three calls while a runaway spender trips the budget — two different runaways, two independent trip wires, all sitting inside one supervised process.

## Frequently asked questions

### How do I know if I have a behavioral stall or a context-bloat loop?

Look at the arguments and the transcript. A behavioral stall repeats the *same* call (or a fixed cycle) with a small, stable transcript — the health monitor's `STUCK` or `LOOP` detector fires. A context-bloat loop keeps making distinct-looking calls (a new page, a new id) as the model's recall degrades under a growing transcript — the health monitor stays *silent*, which is your signal to bound the context with `context_scope` instead. The runnable script above shows both cases side by side.

### Does behavioral anomaly detection add LLM cost or latency?

No. All four detectors are pure pattern matching over a bounded history of tool calls and response lengths — comparisons over a `deque`, no model calls, no embeddings, no I/O. Anomaly detection that itself needed an LLM call would double your token spend and add latency at the worst possible moment. This runs in microseconds and costs nothing, which is why the demo works with no API key set.

### Won't a wedged agent flood me with alerts?

No — every anomaly type has its own `cooldown` (default 300 seconds). Once a `LOOP` fires, the monitor suppresses further loop anomalies until the cooldown elapses, so a stuck agent produces one alert per type, not a stream. Tune the window with the `cooldown` field.

### Can `on_anomaly` stop the process outright?

The three configurable actions are `"log"`, `"pause"`, and `"escalate"`; both `"pause"` and `"escalate"` suspend the process, so escalate is effectively a notify-then-halt. If you need an unconditional hard stop rather than a suspend, drive it from the runtime kill switches described in [How to Stop a Runaway AI Agent](stop-a-runaway-ai-agent.md), which sit above per-anomaly enforcement.

## Next steps

Attach a health policy and let a looping agent get paused and escalated automatically — before it spends the night making the same call. Copy the offline demo above to tune `stuck_threshold`, `loop_window`, `empty_threshold`, and `error_rate_threshold` against your own call traces, then attach the tuned `HealthConfig` to a `ProcessConfig` with `on_anomaly="escalate"` and run it as a supervised [agent process](../../runtime/processes.md). Read the [behavioral health monitoring reference](../../runtime/governance/health.md) for the complete detector API, pair it with a [governance budget](../../runtime/governance/budget.md) for a hard ceiling, and confirm your process survives a restart with the [durable execution](durable-execution-for-ai-agents.md) guarantees the runtime makes for every process.
