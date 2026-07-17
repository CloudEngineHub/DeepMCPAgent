---
title: "Agent Reasoning Patterns: The Complete Guide"
description: "The honest hub the current listicles won't write: most guides imply more reasoning stages equal more accuracy, but on capable models multi-stage patterns…"
keywords: "agent reasoning patterns, llm reasoning patterns, agent reasoning graph, chain-of-thought agents, react vs plan and execute, prompt graph"
date: 2026-07-16
slug: agent-reasoning-patterns
categories:
  - Reasoning
---

# Agent Reasoning Patterns: The Complete Guide

Agent reasoning patterns are the different control flows an LLM follows to solve a task — a single tool-calling loop, a plan-then-act cycle, an adversarial debate, or a hand-built graph. Most guides quietly imply the opposite of what's true: they suggest that stacking more reasoning stages makes an agent smarter. On today's capable models it usually just adds latency and tokens. By the end of this guide you'll know what each of the built-in patterns actually does, when the extra stages earn their cost, and how to switch between them with a single argument.

## What an agent reasoning pattern really is

Under the hood, every pattern in Promptise Foundry is a `PromptGraph`: a set of nodes connected by edges, where each node is one shaped LLM call and the edges decide where control flows next. The default — plain **ReAct** — is a single node that loops: reason, call tools, reason again, answer. Everything more elaborate is just a different topology over that same primitive.

This matters because "reasoning pattern" is not magic. It's routing. When you pick `react`, `verify`, or `deliberate`, you're choosing:

- How many LLM turns run before an answer is allowed
- What each turn is told to do (plan, critique, synthesize)
- What context each turn sees
- Which turns get tools

More turns cost more money and more wall-clock time. Whether they buy you accuracy depends entirely on the model and the task.

## The honest part: more stages rarely means more accuracy

Here's the claim the listicles skip. A modern frontier model already does a great deal of reasoning internally. Wrapping it in a five-stage think-plan-act-observe-reflect pipeline often just makes it restate its own reasoning across several billed turns — same answer, higher bill. The multi-stage machinery earns its keep in two specific situations:

1. **Weaker or mainstream models** that don't reason strongly on their own. Here an explicit self-check or a plan step lifts accuracy.
2. **Structurally hard tasks** — deep multi-tool traversals, aggregation over many facts, adversarial or high-stakes decisions — where the *shape* of the work, not raw model IQ, is the bottleneck.

For the large majority of agents, the efficient default is **ReAct plus code-action when you need exact aggregation**. Start there. Add stages only when you can measure that they help. That's the recommendation baked into the [prebuilt patterns reference](../../core/engine-prebuilts.md), and it's the opposite of "always reach for the fanciest loop."

## The 10 built-in patterns, mapped to when each helps

Promptise ships ten ready-to-use reasoning patterns. Each is selectable by name via `build_agent(agent_pattern=...)`. Here's the honest map of what each is *for*:

| Pattern | Shape | Reach for it when… |
|---------|-------|--------------------|
| `react` | One tool loop (default) | Almost everything. The efficient baseline. |
| `verify` | Plan → solve → self-check, one turn | You want a cheap accuracy safety-net on weaker models, no multi-call overhead. |
| `managed` | Tool loop with a deduplicated facts ledger | Deep multi-tool tasks where a naive loop re-queries the same facts. |
| `code-action` | Model writes one sandboxed program over your tools | Aggregation and data-traversal — loops, sums, joins the model would fumble conversationally. |
| `peoatr` | Plan → Act → Think → Reflect | Multi-step tasks needing explicit subgoals and replanning. |
| `research` | Search → Verify → Synthesize | Research pipelines where findings must pass a quality gate before write-up. |
| `autonomous` | LLM picks the next node from a pool | Open-ended tasks where the path isn't known up front. |
| `deliberate` | Think → Plan → Act → Observe → Reflect | High-stakes, slower work where careful observation beats speed. |
| `debate` | Proposer vs critic, then a judge | Decisions that benefit from an adversarial challenge. |
| `pipeline` | Fixed sequential nodes, no loops | Deterministic multi-step transforms (extract → analyze → format). |

Two of these deserve a closer look because they are efficiency wins, not accuracy theater. **`managed`** bounds context with a compact "facts gathered" ledger and serves identical tool calls from cache, cutting redundant calls at equal accuracy. **`code-action`** lets the model write a single Python program that calls your tools as ordinary functions inside a hardened Docker sandbox — read-only rootfs, dropped capabilities, no network — so it gets code's exactness while every tool call keeps its guards. The full mechanics, requirements, and honest scope live in the [code-action guide](../../guides/code-action.md).

## React vs plan-and-execute: which loop should you default to?

This is the comparison most people actually want, so let's be concrete about `react vs plan and execute`-style patterns (`peoatr`, `deliberate`).

**ReAct** interleaves thinking and acting in one loop. It's fast, cheap, and adapts turn by turn — if a tool result surprises the model, it adjusts on the next thought. The tradeoff: on a long, branchy task it can wander, because it never commits to an overall plan. We break ReAct down step by step in [The ReAct Agent Pattern Explained (with Code)](react-agent-pattern.md).

**Plan-and-execute style patterns** (`peoatr`, `deliberate`) commit to subgoals first, then work them, then reflect and replan. That front-loaded structure keeps long tasks on-rails and makes progress auditable — but every stage is another billed turn, and on a task ReAct would have nailed in two turns, the plan overhead is pure cost. We walk through the mechanics of the planning loop in [Plan-and-Execute Agents: How Planning Loops Work](plan-and-execute-agent.md).

The honest default: **use ReAct until you observe it losing the thread on multi-step work, then graduate to `peoatr` or `deliberate`.** Don't start with a planning loop because it sounds more thorough.

## Switching patterns is one line

The whole point of treating reasoning as a `PromptGraph` is that you swap strategies without rewriting your agent. Here's a complete, runnable example that uses the single-pass `verify` pattern — plan, solve, and self-check in one turn:

```python
import asyncio
from promptise import build_agent


async def main():
    # One argument selects the entire reasoning strategy.
    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You are a careful analyst. Show your final answer clearly.",
        agent_pattern="verify",  # plan → solve → self-verify, at one-turn latency
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user",
                       "content": "What is 17.5% of 2,048, and is that more than 350?"}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

Change `agent_pattern="verify"` to `"react"`, `"managed"`, `"code-action"`, `"debate"`, or any of the ten names and the agent's control flow changes — no other edits. That's the reasoning engine doing its job: patterns are configuration, not code you maintain.

## When to build a custom prompt graph

The ten prebuilts cover common shapes, but sometimes your workflow has its own topology — a quality gate that loops back to research, a review node that can reject a draft, a branch that routes on a score. For that, build your own `prompt graph` from nodes and edges:

```python
from promptise import build_agent
from promptise.engine import PromptGraph, PromptNode
from promptise.engine.reasoning_nodes import ThinkNode, SynthesizeNode

analyst = PromptGraph(
    "analyst",
    nodes=[
        ThinkNode("think", is_entry=True),      # reason about the task first
        PromptNode("research", inject_tools=True),  # then act with tools
        SynthesizeNode("answer", is_terminal=True),  # then write the final answer
    ],
)

agent = await build_agent(model="openai:gpt-5-mini", agent_pattern=analyst)
```

Each node controls what the LLM sees, what tools it can call, what it must produce, and how data flows to the next node. Because a graph is data, you can inspect it, version it, and test it like any other component. The full node catalog, edge conditions, and routing logic are in the [custom reasoning guide](../../guides/custom-reasoning.md) — reach for it only when a prebuilt genuinely doesn't fit, not as a first move.

## When a simpler approach is the better fit

Reasoning patterns are for *agents* — LLM calls that use tools and take multiple turns. If your task is a single-shot transform (classify this ticket, rewrite this paragraph, extract these fields), you don't need a reasoning graph at all. A plain `build_agent(...)` call, or even a single prompt, is faster, cheaper, and easier to reason about. Likewise, if you're chaining fixed deterministic steps with no branching, the `pipeline` pattern — or ordinary Python — beats a dynamic loop. Match the machinery to the problem; the goal is the right answer at the lowest overhead, not the most impressive-looking graph.

## Frequently asked questions

### Do more reasoning stages make an agent more accurate?

Not by default. On capable models, extra stages mostly add latency and token cost while restating the same reasoning. Multi-stage patterns help most on weaker models (where an explicit self-check lifts accuracy) and on structurally hard tasks like deep tool traversals or adversarial decisions. Measure before you add stages.

### What is the difference between ReAct and plan-and-execute agents?

ReAct interleaves thinking and tool calls in one adaptive loop — fast and cheap, but it can wander on long tasks. Plan-and-execute patterns (`peoatr`, `deliberate`) commit to subgoals first, then act and reflect, which keeps long tasks on-track at the cost of extra billed turns. Default to ReAct and graduate to a planning loop only when you see ReAct losing the thread.

### How do I choose a reasoning pattern in Promptise?

Pass `agent_pattern="react"` (or any of the ten names) to `build_agent()`. For a topology the prebuilts don't cover, pass a custom `PromptGraph` instead. Switching is a one-argument change with no other code edits.

## Next steps

Pick a pattern in one line — `build_agent(model, servers, agent_pattern=...)` — and install with `pip install promptise`. Start from the [Quick Start](../../getting-started/quickstart.md) to get an agent running, then browse the [prebuilt patterns reference](../../core/engine-prebuilts.md) to match each of the ten shapes to your workload before you reach for anything custom.
