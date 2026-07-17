---
title: "Behavioral Anomaly Detection for AI Agents (No LLM)"
description: "Turn and iteration caps only answer 'too many?' — they miss search-read-search-read pattern loops, degenerate empty outputs, and a rising error rate. Maps…"
keywords: "behavioral anomaly detection for ai agents, detect ai agent loop, repeating tool pattern detection, agent empty response detection, anomaly detection llm agent, agent loop vs stuck detection"
date: 2026-07-16
slug: behavioral-anomaly-detection-for-ai-agents
categories:
  - Governance
---

# Behavioral Anomaly Detection for AI Agents (No LLM)

Behavioral anomaly detection for AI agents is the difference between knowing an agent took *too many* steps and knowing it is stuck in a `search → read → search → read` loop, emitting empty responses, or failing half its tool calls. A turn cap or iteration limit answers exactly one question — "how many steps?" — and nothing about *what kind* of trouble the agent is in. This post maps four lightweight detectors to the four failures they catch, and shows why they cost zero extra tokens: detection is pure pattern matching over the agent's own call-and-response history, gated by cooldowns so it never turns into an alert storm.

## Why turn and iteration caps miss the real failures

Every agent framework ships a step counter. It exists to stop a runaway before it exhausts a budget or a context window, and it does that job. But a counter is blind to the *shape* of the work. Three of the most common production failures all sit comfortably under a generous iteration cap:

- **A pattern loop.** The agent calls `search`, then `read`, then `search` again with the same query, then `read` again — making real, distinct-looking tool calls that never converge. At call twelve of a thirty-call budget, a counter sees nothing wrong. The agent is looping; the cap is silent. (For the context-bloat mechanism that *causes* this, see [Catch an AI Agent Stuck Repeating the Same Tool Call](ai-agent-stuck-repeating-tool-call.md).)
- **Degenerate empty output.** The model starts returning `""`, `"..."`, or a single token, invocation after invocation. It is technically "responding," so no error fires and no counter trips — but it has stopped doing useful work.
- **A rising error rate.** Half the tool calls are now throwing. The agent keeps going because each individual failure is recoverable, but the *rate* is the signal, and a step counter cannot see a rate.

A cap tells you when to pull the plug. It does not tell you the agent is drowning. That gap is what behavioral health monitoring fills, and it does it without a single additional model call.

## Four detectors, four failures they catch

Promptise Foundry's runtime ships a `HealthMonitor` with four detectors. Each one owns a distinct failure mode, and every one is pure Python over recent history — no LLM, no embeddings, no network. The full API is documented in the [behavioral health monitoring guide](../../runtime/governance/health.md); here is what each detector is actually watching.

| Detector | `AnomalyType` | What it watches | The failure it catches |
|----------|---------------|-----------------|------------------------|
| Stuck | `STUCK` | The last N tool calls being *identical* — same tool, same arguments | The agent frozen on one call, retrying the exact same thing |
| Loop | `LOOP` | A repeating *subsequence* of any length in the last `loop_window` calls | A `search → read` cycle that never converges but looks busy |
| Empty response | `EMPTY_RESPONSE` | N consecutive responses under `empty_max_chars` | Degenerate, near-empty output — the model gave up |
| Error rate | `HIGH_ERROR_RATE` | The failure fraction in a sliding window | A tool or upstream API that started failing systematically |

The **stuck vs loop** distinction is the one people conflate, so it is worth being precise about the delta. Stuck detection tracks `(tool_name, hash(args))` and fires only when the last `stuck_threshold` calls are *byte-identical*. That catches a hard freeze. Loop detection is more general: it scans the recent window for any repeating pattern — length two, three, or more — so `search → read → search → read` trips it even though no two *consecutive* calls are the same. Agent-loop-vs-stuck-detection is not a naming quibble; the two detectors catch structurally different failures, and you want both.

Agent-empty-response detection is deliberately trivial by design: it counts consecutive responses whose stripped length is at or below `empty_max_chars` (default 10). Three `"..."` replies in a row is a stronger signal of a wedged agent than any single output could be. And repeating-tool-pattern detection plus error-rate detection round out the set so that "the agent is misbehaving" has a concrete, testable definition instead of a vibe.

Two properties make this safe to leave on in production:

- **Zero LLM cost.** Every detector is a comparison over a bounded `deque`. Anomaly detection for an LLM agent that itself required an LLM call would double your spend and add latency to the exact moment things are already going wrong. This does neither.
- **Cooldowns.** Each anomaly type has a `cooldown` (default 300s). Once a `LOOP` fires, the monitor stays quiet on loops until the cooldown elapses, so a genuinely stuck agent produces one alert, not a thousand.

## What other frameworks do today

To be fair to the ecosystem: every mainstream framework gives you a way to stop a runaway, and those primitives are real and useful. The honest gap is that they are *counters and caps*, not behavioral-health detectors — so the loop, empty-response, and error-rate cases above fall through.

- **LangChain / LangGraph.** `AgentExecutor` exposes `max_iterations` and `max_execution_time`; LangGraph enforces a `recursion_limit` (default 25) that raises `GraphRecursionError` when a graph runs too long. Both are step counters. LangGraph is a low-level graph runtime, so you *can* hand-write a conditional edge that inspects state and detects a repeated pattern — but you author, host, and tune that logic yourself; there is no built-in loop/empty/error-rate subsystem.
- **CrewAI.** Agents take `max_iter` and `max_rpm` (a requests-per-minute rate limit), plus `max_execution_time`. Those bound how hard an agent works. They do not inspect the *content* of the call sequence for a repeating cycle or degenerate output.
- **AutoGen.** This is the closest partial. AutoGen gives you `max_consecutive_auto_reply` and composable termination conditions (`MaxMessageTermination`, `TextMentionTermination`) plus an `is_termination_msg` callback. That callback is a genuine hook — you could write one that flags a repeated message and terminates. The delta is exactly that: *you write the detector*. There is no shipped, tuned loop/empty/error-rate monitor; there is a place to bolt one on.
- **Pydantic AI.** `UsageLimits` caps request count and token totals per run. That is budget enforcement, adjacent to but distinct from behavioral anomaly detection.
- **LlamaIndex.** ReAct-style agents take a `max_iterations` that raises once exceeded. A counter again.

None of these ship behavioral-health anomaly detection as a first-class subsystem. Where a framework hands you a hook (AutoGen's termination callback) or a low-level graph you could extend (LangGraph), the capability is *possible* but not *provided* — you become the author, maintainer, and on-call owner of that monitor. Promptise's edge is structural: the four detectors, their thresholds, cooldowns, and escalation are one config object you turn on, not a component you build. The [runtime overview](../../runtime/index.md) positions health as a peer of budget and mission governance rather than a bolt-on.

## Runnable: watch the detectors fire with zero LLM calls

Because detection is pure pattern matching, you can exercise the whole thing offline — no API key, no model, no network. This drives a `HealthMonitor` with a synthetic `search → read` cycle to trip loop detection, then feeds it three trivial responses to trip empty-response detection:

```python
import asyncio

from promptise.runtime import HealthConfig, HealthMonitor


async def main() -> None:
    monitor = HealthMonitor(
        HealthConfig(
            enabled=True,
            stuck_threshold=3,     # 3 identical calls in a row = stuck
            loop_window=12,        # scan the last 12 calls for a pattern
            loop_min_repeats=2,    # a 2x-repeated subsequence = loop
            empty_threshold=3,     # 3 short replies in a row = empty
            empty_max_chars=10,    # "short" means <= 10 chars
        ),
        process_id="research-agent",
    )

    # The model keeps re-running the same two-step cycle without converging.
    cycle = [
        ("search", {"q": "q3 revenue"}),
        ("read", {"doc": "10-Q"}),
    ] * 3

    for tool, args in cycle:
        anomaly = await monitor.record_tool_call(tool, args)
        if anomaly:
            print(f"[{anomaly.anomaly_type.value}] {anomaly.description}")
            break

    # Then it degenerates into near-empty output — no LLM needed to notice.
    for _ in range(3):
        empty = await monitor.record_response("...")
        if empty:
            print(f"[{empty.anomaly_type.value}] {empty.description}")
            break

    print("total anomalies:", len(monitor.anomalies))


asyncio.run(main())
```

Running it prints exactly the two anomalies, and nothing was sent to a model:

```text
[loop] Agent in loop: sequence ['search', 'read'] repeating 2 times
[empty_response] Agent producing empty responses: 3 consecutive responses under 10 characters
total anomalies: 2
```

Note that `STUCK` did *not* fire here — no two consecutive calls were identical — but `LOOP` did, because the two-step subsequence repeated. That is the stuck-vs-loop distinction working in your favor: the failure a step counter would have missed is exactly the one that got caught. In a live agent you never call `record_tool_call` yourself; the runtime feeds the monitor as the agent works. This standalone form just lets you unit-test your thresholds against real call sequences before you ship them.

## Turn it on in production

In a running agent you do not touch `HealthMonitor` directly — you attach a `HealthConfig` to a `ProcessConfig`, and the runtime drives detection on every tool call and response. This is the wiring the [CTA cheat-sheet](../../runtime/governance/health.md) documents in full:

```python
from promptise.runtime import ProcessConfig, HealthConfig, EscalationTarget

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Monitor data pipelines and summarize anomalies.",
    health=HealthConfig(
        enabled=True,
        stuck_threshold=3,
        loop_window=20,
        loop_min_repeats=2,
        empty_threshold=3,
        error_rate_threshold=0.5,   # 50%+ failures in the window
        on_anomaly="escalate",      # "log", "pause", or "escalate"
        cooldown=300,               # one alert per type per 5 min
        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/services/…",
        ),
    ),
)
```

The `on_anomaly` action decides what happens the instant a detector trips:

| Action | Behavior |
|--------|----------|
| `"log"` | Record the anomaly and keep running — good for tuning thresholds in staging |
| `"pause"` | Suspend the process so a human can inspect it |
| `"escalate"` | Fire the escalation notification, then suspend |

Health monitoring is one of three governance layers, and they compose. A [per-run and daily budget](../../runtime/governance/budget.md) answers "has this agent done *too much*?"; behavioral health answers "is it doing it *wrong*?" Run both: the budget is your hard ceiling, health is your early-warning system that trips long before the ceiling. If you want the ceiling to be a genuine kill switch rather than a soft pause, pair health with the hard-stop patterns in [How to Stop a Runaway AI Agent (Runtime Kill Switches)](stop-a-runaway-ai-agent.md).

The same block works declaratively in an `.agent` manifest:

```yaml
name: pipeline-monitor
health:
  enabled: true
  stuck_threshold: 3
  loop_window: 20
  error_rate_threshold: 0.5
  on_anomaly: escalate
  cooldown: 300
  escalation:
    webhook_url: "https://hooks.slack.com/services/…"
```

## Frequently asked questions

### Does behavioral anomaly detection add LLM cost or latency?

No. All four detectors are pure pattern matching over a bounded history of tool calls and response lengths — comparisons over a `deque`, no model calls, no embeddings, no I/O. That is deliberate: anomaly detection for an LLM agent that itself needed an LLM call would double your token spend and add latency at the worst possible moment. Detection runs in microseconds and costs nothing.

### What is the difference between the loop and stuck detectors?

Stuck detection fires when the last `stuck_threshold` tool calls are byte-identical — same tool, same arguments — which catches a hard freeze. Loop detection is broader: it scans the recent window for any repeating subsequence, so a `search → read → search → read` cycle trips it even though no two *consecutive* calls match. Agent-loop-vs-stuck-detection matters because the two catch structurally different failures; keep both enabled.

### Won't a wedged agent flood me with alerts?

No — every anomaly type has its own `cooldown` (default 300 seconds). Once a `LOOP` fires, the monitor suppresses further loop anomalies until the cooldown elapses, so a stuck agent produces one alert per type, not a stream. You tune the window with the `cooldown` field.

### Can I test my thresholds without deploying an agent?

Yes. Construct a `HealthMonitor(HealthConfig(...), process_id=...)` and feed it recorded call sequences with `await monitor.record_tool_call(...)` and `await monitor.record_response(...)`, exactly as in the runnable example above. It is fully offline, so you can unit-test detection against real production traces before shipping.

## Next steps

Turn on `HealthConfig` and let the runtime flag loops, empty responses, and error spikes before they drain your context window. Copy the offline demo above to tune `stuck_threshold`, `loop_window`, and `empty_max_chars` against your own call traces, then attach the tuned `HealthConfig` to your `ProcessConfig` with `on_anomaly="escalate"`. Read the [behavioral health monitoring guide](../../runtime/governance/health.md) for the complete detector reference, pair it with a [governance budget](../../runtime/governance/budget.md) for a hard ceiling, and see the [runtime overview](../../runtime/index.md) for how health, budget, and mission governance fit together.
