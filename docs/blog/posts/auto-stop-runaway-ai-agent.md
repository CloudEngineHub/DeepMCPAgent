---
title: "Auto-Stop a Runaway AI Agent: Behavioral Health Checks"
description: "A recursion counter tells you an agent hit a step ceiling; it can't tell a stuck agent from a busy one. This is honest about what step and iteration caps can…"
keywords: "auto-stop runaway ai agent, behavioral anomaly detection agents, detect agent repeating tool calls, ai agent empty response loop, recursion limit vs behavioral health, pause runaway agent automatically"
date: 2026-07-16
slug: auto-stop-runaway-ai-agent
categories:
  - Comparisons
---

# Auto-Stop a Runaway AI Agent: Behavioral Health Checks

To **auto-stop a runaway AI agent**, you need something that can tell a *stuck* agent apart from a merely *busy* one — and a recursion counter cannot, because all it knows is how many steps have gone by. Set `recursion_limit=25` and an agent can take twenty-five productive steps toward a finished task, or it can call `get_status(id=42)` twenty-five times in a row and learn nothing. To the counter those runs are identical: same integer, same ceiling, same hard error at the end. The count is the wrong signal. What you actually want is a monitor that watches the *shape* of the agent's behavior over time — the same call repeated, a tool pattern cycling, responses collapsing to nothing, errors spiking — and halts the process the moment that shape turns pathological, long before the step budget runs out. This post shows how Promptise Foundry's [behavioral health governance](../../runtime/governance/health.md) does exactly that: four anomaly detectors, per-anomaly cooldowns, and a graduated log → pause → escalate response that stops the runaway automatically.

<!-- more -->

The thesis in one line: a step counter measures *how far* an agent has gone; behavioral health measures *whether it is still making progress*.

## A recursion counter can't tell a stuck agent from a busy one

The recursion-limit-vs-behavioral-health distinction is easiest to see with two runs that a counter scores identically.

- **Run A** — a research agent takes 24 distinct steps: search, read, cross-reference, summarize, cite. Every call advances the task. On step 25 it would finish, but the `recursion_limit` fires first and raises `GraphRecursionError`. You just killed a *healthy* agent one step from done.
- **Run B** — the same agent gets wedged on step 3 and calls `get_status(id=42)` for the next 22 turns. Every call is wasted. The `recursion_limit` fires at step 25 with the identical error — 22 turns and a full model-call bill *after* the agent stopped making progress.

A single counter cannot separate these because it has no notion of *what* each step was. It sees a monotonically rising integer and trips at a fixed number. That is genuinely useful as a last-resort ceiling — an agent cannot loop forever under a `recursion_limit` — but it is a blunt instrument pointed at the wrong target. It catches "too many steps," which is a proxy, and it catches it *late*, only once the budget is spent. It never catches the real failure — "this agent stopped progressing" — because that failure has a behavioral signature the counter is structurally blind to. This is the gap that makes purpose-built behavioral monitoring worth having, and it is a core part of [why Promptise Foundry exists](../../getting-started/why-promptise.md): the framework treats "is the agent still behaving?" as a first-class runtime question, not something you bolt on with callbacks.

## Four behavioral signatures a step counter is blind to

Promptise's `HealthMonitor` runs four detectors, each keyed to a distinct failure signature. All four are pure pattern matching over the tool-call and response history — **zero LLM calls**, so they are free to run on the hot path — and each emits a typed `Anomaly` the runtime can act on.

| Detector | `AnomalyType` | What it fingerprints | Default trigger |
|----------|---------------|----------------------|-----------------|
| **Stuck** | `STUCK` | The same tool called with the *same arguments* N times consecutively — `(tool_name, hash(args))` repeating | `stuck_threshold=3` |
| **Loop** | `LOOP` | A repeating *subsequence* of tools, e.g. `search → read → search → read → …` | `loop_min_repeats=2` over a `loop_window=20` |
| **Empty response** | `EMPTY_RESPONSE` | Consecutive responses under a character floor — the agent producing nothing | `empty_threshold=3`, `empty_max_chars=10` |
| **High error rate** | `HIGH_ERROR_RATE` | Failed calls exceeding a rate in a sliding window — something is systematically broken | `error_rate_threshold=0.5` over `error_window=10` |

Notice what these have in common: not one of them is a *count of steps*. `STUCK` fingerprints the arguments, so a genuine polling loop that advances (`id=42`, then `id=43`) never trips, while `get_status(id=42)` forever trips on the third call. `LOOP` inspects the *order* of calls, catching a `search → read → analyze` cycle that repeats with no new information. `EMPTY_RESPONSE` watches the *output* side — the classic **AI agent empty response loop** where the model returns a blank turn after blank turn. `HIGH_ERROR_RATE` watches *outcomes* across a window, catching a downstream dependency that has gone dark. Each is a different lens on the same underlying question — *is the agent still making progress?* — and a step counter answers none of them. If you want the narrow, single-detector deep dives, we cover the identical-call case in [Catch an AI Agent Stuck Repeating the Same Tool Call](ai-agent-stuck-repeating-tool-call.md) and the context-window flavor of "stuck" in [When Agent Tool Loops Fail: Fixing Context Bloat](agent-stuck-in-tool-loop.md); this post is about the *whole* health envelope and the auto-stop it drives.

## What other frameworks do today

To be fair and precise: every serious framework ships a run limiter here, each is real and useful within its scope, and none of them "can't count." Look closely at *what* each one measures.

- **LangGraph** — a `recursion_limit` (default 25) on graph execution; exceeding it raises `GraphRecursionError`. It is a count of super-steps.
- **LangChain** — `AgentExecutor` takes `max_iterations` (default 15) and `max_execution_time`; on breach it stops the run or forces a final answer via `early_stopping_method`. A step/time cap.
- **CrewAI** — an `Agent`'s `max_iter` caps reasoning iterations per task, and `max_rpm` throttles the *outbound request rate*. Both are quantity limits.
- **AutoGen** — `max_turns` plus termination conditions such as `MaxMessageTermination` end a conversation after a number of messages. A message count.
- **Pydantic AI** — `UsageLimits` (`request_limit`, `total_tokens_limit`, and related counters) raise `UsageLimitExceeded` mid-run. Genuinely finer-grained — it can stop on tokens — but every request and token is worth the same.

Every one of these bounds a runaway *eventually*, and that matters. Here is the exact delta. All of them count volume — steps, turns, messages, requests, tokens — and raise a hard error (or force a stop) at a fixed ceiling. **None of them classify the four behavioral signatures above:** none key on identical-argument repetition, none recognize a repeating tool *pattern*, none watch for an empty-response streak, and none track a sliding-window error rate as a distinct trip wire. And because the trigger is a single ceiling, none offers a *graduated, per-anomaly-typed* response — there is no built-in "log this kind, pause on that kind, escalate on the other," and no cooldown so one flapping anomaly doesn't page you a hundred times.

The honest boundary: you *can* build any of this yourself. Every one of these frameworks exposes callbacks, hooks, or middleware where you could inspect each step, hash the arguments, and raise. The capability is reachable everywhere. Promptise's edge is not that the others are incapable — it is that Promptise makes typed behavioral-anomaly detection with a graduated response a **structural, first-class property of the running process**, so you declare a policy once instead of hand-rolling four detectors, tuning their thresholds, remembering the cooldowns, and wiring them into every agent you ship. If you like this kind of precise, no-overclaiming comparison, the same honest treatment of a different feature is in [Does LangChain Support Multi-Tenancy? The Honest Answer](does-langchain-support-multi-tenancy.md), and the broader "what a framework gives you vs what's left to you" audit is in the [Enterprise-Ready Agent Framework Checklist](enterprise-ready-agent-framework-checklist.md).

## Runnable: watch the health monitor auto-flag a failing worker

Here is a complete, runnable script — no API key, no network — that exercises the `HIGH_ERROR_RATE` detector directly. It models a worker whose downstream API goes dark after two clean calls; the next four fail, and the sliding-window error rate crosses the threshold. Every symbol is a real, exported runtime API, and because detection makes no LLM calls, it runs with nothing configured.

```python
import asyncio
from promptise.runtime import HealthConfig, HealthMonitor


async def main() -> None:
    # Pure pattern matching — zero LLM calls, no API key required.
    monitor = HealthMonitor(
        HealthConfig(
            enabled=True,
            error_window=6,             # judge the last 6 invocations...
            error_rate_threshold=0.5,   # ...and trip if half or more failed
        ),
        process_id="ingest-worker",
    )

    # A downstream API goes dark after two clean calls: the next four fail.
    outcomes = [False, False, True, True, True, True]  # True = the call failed
    for n, failed in enumerate(outcomes, start=1):
        if failed:
            anomaly = await monitor.record_error()
        else:
            await monitor.record_success()
            anomaly = None
        tag = "fail" if failed else "ok"
        verdict = (
            f"{anomaly.anomaly_type.value.upper()}: {anomaly.description}"
            if anomaly is not None
            else "healthy"
        )
        print(f"invocation {n} ({tag})  ->  {verdict}")

    status = monitor.health_status()
    print(f"\nhealthy? {status['is_healthy']}  |  anomalies recorded: {status['anomaly_count']}")


asyncio.run(main())
```

Running it prints:

```text
invocation 1 (ok)  ->  healthy
invocation 2 (ok)  ->  healthy
invocation 3 (fail)  ->  healthy
invocation 4 (fail)  ->  healthy
invocation 5 (fail)  ->  healthy
invocation 6 (fail)  ->  HIGH_ERROR_RATE: High error rate: 67% errors in last 6 invocations (threshold: 50%)

healthy? False  |  anomalies recorded: 1
```

The window fills at invocation 6, at which point four of the last six calls have failed — 67%, over the 50% threshold — and `record_error` returns an `Anomaly` whose `description` names the exact rate and window. No step counter would have noticed: the worker took only six steps, nowhere near any `recursion_limit`. The failure was in the *outcomes*, not the *count*, and `health_status()` flips `is_healthy` to `False` so a supervisor can act. Swap in `record_tool_call("get_status", {"id": 42})` three times and you get a `STUCK` anomaly instead; feed a repeating tool sequence and you get `LOOP`; feed short responses to `record_response` and you get `EMPTY_RESPONSE`. Same monitor, four signatures.

## Auto-stop the runaway: the log → pause → escalate ladder

Detection is half the value; the other half is *doing something* automatically. In production you don't drive `HealthMonitor` by hand — you declare a `HealthConfig` on a `ProcessConfig` and let the [Agent Runtime](../../runtime/index.md) run it around every invocation of a supervised `AgentProcess`. The `on_anomaly` action is the auto-stop:

```python
from promptise.runtime import ProcessConfig, HealthConfig, EscalationTarget

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Ingest each dropped file and reconcile it against the ledger.",
    health=HealthConfig(
        enabled=True,
        stuck_threshold=3,          # same tool + args 3x in a row = stuck
        loop_window=20,             # examine the last 20 calls for patterns
        loop_min_repeats=2,         # a pattern seen twice = loop
        empty_threshold=3,          # 3 blank responses in a row = anomaly
        error_rate_threshold=0.5,   # half the window failing = anomaly
        on_anomaly="escalate",      # "log" | "pause" | "escalate"
        cooldown=300,               # 5 min between repeats of the same type
        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/services/XXX",
            event_type="agent.health.anomaly",
        ),
    ),
)
```

The three `on_anomaly` behaviors are the whole enforcement ladder, and they are graduated by how hard you want to slam the brakes:

| `on_anomaly` | Behavior | Use it when |
|--------------|----------|-------------|
| `"log"` | Record the anomaly, keep running | You want to *observe* which detectors fire before enforcing |
| `"pause"` | Suspend the process (`RUNNING → SUSPENDED`) | You want to **pause the runaway agent automatically** and inspect it |
| `"escalate"` | Fire the webhook + an EventBus event, *then* suspend | You want a human paged and the process halted in one move |

Two design points make this safe rather than noisy. First, **cooldowns**: `cooldown=300` means a given anomaly *type* won't re-fire for five minutes, so a flapping worker escalates once, not on every call — and each type has its own cooldown, so a `STUCK` alert doesn't mute a genuine `HIGH_ERROR_RATE` one. Second, the action lands on the **process**, not on your prompt, so it survives across trigger-driven invocations — the deeper reason a runtime beats an in-call counter, unpacked in [How to Stop a Runaway AI Agent (Runtime Kill Switches)](stop-a-runaway-ai-agent.md). The `"escalate"` path is the clean hand-off to a human, the pattern in [Escalate to a Human When an AI Agent Keeps Failing](escalate-ai-agent-to-human.md).

One honesty note, because it matters: health's `on_anomaly` accepts `"log"`, `"pause"`, and `"escalate"` — both `"pause"` and `"escalate"` *suspend* the process, which is the auto-stop you want for a behavioral anomaly (you almost always want to inspect a wedged agent, not terminate it). For a *hard terminal kill* on a hard limit — a maximum tool-call or irreversible-action ceiling — that is the [Autonomy Budget's](../../runtime/governance/budget.md) job via its `on_exceeded="stop"`. The two are complementary and you run both: **budget** stops an agent that does *too much* (a hard cap on calls, weighted cost, and irreversible actions), while **health** stops one that does the same cheap *nothing* forever. A stuck poller trips the health wire in three calls; a runaway spender trips the budget — two different runaways, two independent trip wires.

## Frequently asked questions

### How is behavioral health different from a recursion limit or max_iterations?

A `recursion_limit` / `max_iterations` counts steps and trips at a fixed ceiling, so it cannot tell a productive 25-step run from an agent wedged on one call for 25 turns — both look identical and both trip *late*, only once the budget is spent. Behavioral health inspects *what* the calls are: identical arguments (`STUCK`), a repeating tool pattern (`LOOP`), blank outputs (`EMPTY_RESPONSE`), or an error-rate spike (`HIGH_ERROR_RATE`). It fires on the *third* identical call, not the twenty-fifth, and it names which signature tripped rather than merely reporting "hit the ceiling." Run a step limit too, as a last-resort backstop — the two are not mutually exclusive.

### Can I auto-stop the agent, or only log the anomaly?

Both. `on_anomaly="log"` records and keeps going; `on_anomaly="pause"` suspends the process the moment an anomaly fires; `on_anomaly="escalate"` fires your webhook and an EventBus event and *then* suspends. Pause and escalate are the auto-stop — they halt the runaway without you writing a supervising loop. Note that health's ladder does not include a hard terminal `"stop"`; for that, pair it with the [Autonomy Budget](../../runtime/governance/budget.md) (`on_exceeded="stop"`) or a runtime kill switch.

### Will the detectors fire on a legitimate polling or retry loop?

Only if the behavior is genuinely pathological. `STUCK` hashes the arguments, so a loop that advances (`id=42`, then `id=43`, or a moving cursor) never trips — only *identical* consecutive calls do. `LOOP` needs a repeating subsequence, not just repeated tools. `HIGH_ERROR_RATE` needs the failure fraction to cross your threshold across a full window. If a detector is too eager for your workload, raise its threshold or set `on_anomaly="log"` to observe before you enforce.

### Does anomaly detection cost extra LLM calls or add latency?

No. All four detectors are pure pattern matching over the in-memory tool-call and response history — hashing arguments, scanning a deque, computing a rate. There is no model invocation, which is why the runnable example above works with no API key set and why detection is safe to run on the hot path of every call.

### What does an anomaly carry, so I can alert on it?

Each `Anomaly` has an `anomaly_type` (one of `STUCK`, `LOOP`, `EMPTY_RESPONSE`, `HIGH_ERROR_RATE`), a human-readable `description`, a `timestamp`, and a `details` dict with the evidence (the offending tool name, the repeating pattern, the measured error rate). The monitor also exposes `.anomalies`, `.latest_anomaly`, and `health_status()` for a process-level `is_healthy` summary you can surface on a dashboard or a readiness probe.

## Next steps

Add a `HealthConfig(enabled=True, on_anomaly="escalate")` to your supervised process and the runtime will auto-stop a stuck, looping, empty-responding, or error-spiking agent on the *third* bad signal instead of the twenty-fifth — with a webhook fired and the process suspended, no supervising loop of your own. Start from the runnable script above to feel how a detector trips, then read the [Behavioral Health reference](../../runtime/governance/health.md) for every threshold and the [Autonomy Budget reference](../../runtime/governance/budget.md) for the hard-limit half of the safety envelope. New to the framework? `pip install promptise`, then stand up a supervised agent from the [Agent Runtime overview](../../runtime/index.md) and design the exact behavior-and-volume envelope your agent must never leave.
