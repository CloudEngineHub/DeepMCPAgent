---
title: "Reflection, Self-Critique & Self-Consistency Agents"
description: "Separates the introspection techniques that pay for themselves from the ones that just cost tokens. A cheap single-pass self-verification (verify)…"
keywords: "self-critique agent, reflection agent llm, self-consistency llm, reflexion pattern, llm self-verification, chain-of-thought agents"
date: 2026-07-16
slug: self-critique-agent
categories:
  - Reasoning
---

# Reflection, Self-Critique & Self-Consistency Agents

A self-critique agent is an LLM that checks its own work before it answers — it drafts a response, inspects that draft for mistakes, and fixes them in the same step. The appeal is obvious: catch the careless error before the user ever sees it. The catch is that reflection is not free, and most teams bolt it onto models that don't need it. By the end of this article you'll know which introspection techniques pay for themselves, which mostly burn tokens, and how to add a cheap self-check to a small model in a single line.

<!-- more -->

## What a self-critique agent actually does

"Reflection" gets used for three different things, and conflating them is where the wasted spend starts. It helps to name them precisely:

- **Single-pass self-verification** — the model plans, solves, and re-checks its own answer *within one generation*. It's still one LLM call; the self-check is structure inside that call.
- **Multi-turn reflection (the reflexion pattern)** — the model acts, observes the result, writes an explicit critique of what went wrong, then tries again. Several billed turns per task.
- **Self-consistency** — the model answers the same question *N* independent times (each an independent chain-of-thought) and you take the majority vote. The most expensive of the three: N full passes.

All three are variations on the same idea — make the model examine its own reasoning instead of trusting the first token stream. Where they differ wildly is cost. A single-pass check adds a few hundred tokens; a five-turn reflexion loop or a five-sample self-consistency vote multiplies your bill and latency. The honest question is never "should my agent reflect?" It's "does *this* reflection technique earn its cost on *this* model?"

## The introspection techniques that pay for themselves

Here's the part the listicles skip: a modern frontier model already does a great deal of reasoning internally. Wrapping it in a heavy reflection pipeline often just makes it restate its own reasoning across several turns — same answer, higher bill. Reflection earns its keep in two specific places:

1. **Weaker or mainstream models** that don't reason strongly on their own. An explicit self-check recovers errors a single pass would miss.
2. **Structurally hard tasks** — arithmetic, multi-hop logic, constraint satisfaction — where a quick second look catches a careless slip regardless of model size.

That's why Promptise Foundry ships **`verify`** as its default self-critique tool rather than a multi-turn loop. `verify` is a single reasoning pass that forces a `PLAN → SOLVE → VERIFY → answer` structure, where the VERIFY step independently re-checks the answer a different way and corrects it if it's wrong — the accuracy benefit of an explicit self-verification step at **one-turn latency**, no extra LLM calls. The full mechanics and honest scope live in the [prebuilt patterns reference](../../core/engine-prebuilts.md). This is the technique to reach for first, because it's the one whose cost is small enough to justify almost anywhere.

## Add llm self-verification to a small model

You don't build a self-critique agent by hand-writing a critique prompt. You pick the pattern by name. In Promptise, every agent runs on a reasoning graph, and swapping the graph is a single argument to `build_agent()` — memory, guardrails, caching, and observability all keep working unchanged. Set `agent_pattern="verify"` and the model gets forced llm self-verification on every turn:

```python
import asyncio
from promptise import build_agent


async def main():
    # A cheap self-check bolted onto a small, affordable model —
    # one extra reasoning step inside a single LLM call.
    agent = await build_agent(
        model="openai:gpt-5-mini",
        agent_pattern="verify",
        instructions="You are a careful reasoning assistant.",
    )

    result = await agent.ainvoke({
        "messages": [
            {
                "role": "user",
                "content": (
                    "A shirt costs $40 after a 20% discount. "
                    "What was the original price?"
                ),
            }
        ]
    })
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

That's the whole change. The model now plans its approach, solves, and re-derives the answer a second way before committing — the kind of arithmetic slip a single greedy pass gets wrong is exactly what the VERIFY step catches. Because it's still one generation, you pay for it in tokens, not in round-trips. For how `verify` slots into the ten built-in reasoning patterns, see the [reasoning patterns guide](../../core/agents/reasoning-patterns.md).

The right way to adopt it is empirical: run your own eval set once with the plain default and once with `agent_pattern="verify"` on the same small model, and compare. On weaker and mainstream models the forced self-check reliably lifts accuracy; on a frontier model that already reasons strongly, it mostly adds a cheap safety net rather than more correctness. Measure before you commit it everywhere.

## When full deliberation earns its cost — and when it doesn't

Sometimes one pass genuinely isn't enough: high-stakes analysis, tasks where the model needs to observe a tool result and revise its plan, work that maps to the classic reflexion pattern of act → observe → reflect → retry. For those, Promptise ships **`deliberate`**, a five-stage graph — Think → Plan → Act → Observe → Reflect — where the reflect stage decides whether to continue, replan, or answer:

```python
agent = await build_agent(
    model="openai:gpt-5-mini",
    servers=my_servers,
    agent_pattern="deliberate",   # Think → Plan → Act → Observe → Reflect
)
```

Be honest with yourself about when this is worth it. A full Think-Plan-Act-Observe-Reflect deliberation rarely beats one good pass on a strong model — you're paying for five turns to get an answer the model would have produced in one. Where `deliberate` shines is genuinely branchy, tool-heavy work on tasks whose *shape* is the bottleneck, not the model's raw ability. If you're only chasing accuracy on a capable model, the extra stages are latency you can't measure a return on.

Self-consistency sits at the far end of the cost curve. Promptise doesn't ship a one-line prebuilt for it, because for most teams N full samples is a poor trade against a single `verify` pass that captures much of the same error-correction at a fraction of the spend. If you have a task where a majority vote across independent chains genuinely helps — and you can measure it — you can express that yourself as a custom reasoning graph; the [reasoning engine overview](../../core/engine.md) documents `PromptGraph`, parallel nodes, and how to wire your own topology. Reach for it only after `verify` has plateaued.

## When a plain pass is the better fit

Reflection is a tool, not a virtue, so here's the fair counter-case. **If you're running a strong frontier model on straightforward tasks, skip the self-critique entirely.** A well-prompted single pass is faster, cheaper, and — on that class of model — usually just as accurate. Adding `verify` there buys you a modest safety check; adding `deliberate` there usually buys you a bigger bill and nothing else. The same goes for latency-sensitive paths: a chatbot that must answer in under a second can't afford a five-stage loop no matter how careful it is.

The efficient default for the large majority of agents is the plain ReAct loop, escalating to `verify` when you can measure that a small model needs the safety net. We break the baseline loop down step by step in [The ReAct Agent Pattern Explained (with Code)](react-agent-pattern.md), and map every pattern to when it actually helps in [Agent Reasoning Patterns: The Complete Guide](agent-reasoning-patterns.md). Start cheap, add stages only when your own numbers justify them.

## Frequently asked questions

### What is the difference between a reflection agent and a self-consistency agent?

A reflection agent (or reflexion pattern) has one model critique and revise its *own* single line of reasoning — draft, inspect, fix. A self-consistency LLM setup instead samples several independent answers to the same question and takes the majority vote, so it never critiques a specific draft; it relies on agreement across runs. Reflection is usually cheaper because it revises one chain; self-consistency multiplies your cost by the number of samples.

### Does self-critique make every model more accurate?

No. The measurable gains from a self-critique agent show up on weaker and mainstream models, where a forced self-check recovers careless errors. On a strong frontier model that already reasons well internally, single-pass `verify` is roughly comparable to a good direct prompt — a cheap safety net, not a meaningful accuracy jump. Always benchmark on your own model and task before rolling it out.

### Is single-pass verification slower than a normal answer?

Barely. Because `verify` does its planning, solving, and checking inside one generation, it runs at one-turn latency — you pay a few hundred extra tokens, not an extra round-trip. That's the whole reason it's the default self-critique tool over multi-turn reflection loops, which cost several full LLM calls per task.

## Next steps

Add a cheap self-check to a small model with `agent_pattern="verify"` and measure the lift against your own eval set — that single comparison tells you more than any generic benchmark. Start from the [Quick Start](../../getting-started/quickstart.md) to get an agent running, then read the [prebuilt patterns reference](../../core/engine-prebuilts.md) to see how `verify` and `deliberate` fit alongside the other built-in reasoning patterns.
