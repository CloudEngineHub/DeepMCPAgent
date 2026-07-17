---
title: "The ReAct Agent Pattern Explained (with Code)"
description: "Most ReAct tutorials hand-roll a fragile reason-act-observe while-loop that quietly degrades as the transcript grows and the model re-queries the same facts.…"
keywords: "react agent pattern, reason and act llm, what is a react agent, react agent example python, reasoning and acting agent"
date: 2026-07-16
slug: react-agent-pattern
categories:
  - Reasoning
---

# The ReAct Agent Pattern Explained (with Code)

The ReAct agent pattern is the loop underneath almost every tool-using LLM agent you have ever built: the model reasons about what to do, acts by calling a tool, observes the result, and repeats until it can answer. It is simple enough to hand-roll in twenty lines — which is exactly why so many production agents ship with a fragile version of it that quietly degrades on longer tasks. By the end of this post you will understand the pattern from first principles, be able to name the specific failure mode that bites hand-rolled loops, and stand up a production ReAct agent in a single function call where context stays bounded automatically.

## What is a ReAct agent?

A ReAct agent is a **reasoning and acting agent**: it interleaves chain-of-thought reasoning with concrete actions (tool calls) instead of trying to answer everything in one shot. The name comes from *Reason + Act*. Rather than the model guessing an answer, it thinks out loud about what information it needs, calls a tool to get that information, reads the result, and uses it to decide the next step.

If you have ever asked "what is a react agent and how is it different from a plain LLM call?", the answer is the loop. A plain call is one round trip. A ReAct agent runs a **reason and act LLM** cycle that can take many round trips, grounding each step in real tool output rather than the model's priors.

- **Reason** — the model decides what it needs and which tool gets it.
- **Act** — it emits a tool call with concrete arguments.
- **Observe** — the tool result is fed back into the conversation.
- **Repeat** — until the model has enough to produce a final, tool-free answer.

## The reason-act-observe loop, step by step

Here is the shape of the loop that a **reason and act LLM** runs. Each arrow is a message appended to the running transcript:

```
reason ──▶ (tool call) ──▶ observe result ──▶ reason ──▶ ... ──▶ final answer
```

Concretely, a single task like "which orders for customer 42 shipped late?" might unfold as: reason ("I need this customer's orders") → act (`get_orders(customer=42)`) → observe (a list of order IDs) → reason ("now I need ship dates") → act (`get_shipment(order_id=...)`) → observe → ... → final answer. The model is never told the plan up front; it discovers the plan one observation at a time. That adaptivity is the pattern's strength, and it is why ReAct is the default reasoning shape in Promptise's [reasoning patterns](../../core/agents/reasoning-patterns.md) and in most agent frameworks.

## Where hand-rolled ReAct loops break

The naive implementation feeds the model the **entire** transcript on every turn. That is fine for three tool calls. It is a problem for thirty. Watch what the context looks like as the loop runs:

```
turn 1:  [system, user]
turn 2:  [system, user, ai→tool, tool_result]
turn 3:  [system, user, ai→tool, tool_result, ai→tool, tool_result]
...
turn 12: [system, user, + 22 more messages]   ← the model re-reads ALL of this
```

Three things go wrong at once, and they compound:

- **Tokens grow super-linearly.** Every turn re-sends every prior tool call and result. A task needing a dozen distinct facts can cost thousands of redundant tokens per turn.
- **The model re-queries facts it already has.** When the relevant result is buried far back in the transcript, the model loses the thread and calls the same tool again — sometimes dozens of times for a handful of unique facts.
- **Accuracy can drop.** The signal gets lost in the middle of a growing wall of the model's own past output.

This is the failure mode most ReAct tutorials never mention: the loop works in the demo and degrades in production precisely as the task gets harder. Promptise's [context lifecycle guide](../../guides/context-lifecycle.md) documents the mechanism in detail — transcripts grow, models drown.

## A production ReAct agent example in Python

You do not need to write the loop, the transcript bookkeeping, or the tool-calling glue. `build_agent()` returns a ReAct agent by default — a single reasoning node wired to your tools, with the reason-act-observe loop handled by the engine. Here is a complete **react agent example in Python** against an MCP tool server:

```python
import asyncio
from promptise import build_agent
from promptise.config import StdioServerSpec


async def main():
    # The default agent_pattern is "react" — a ReAct graph with
    # context_scope="auto". No manual while-loop to maintain.
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"orders": StdioServerSpec(command="python", args=["tools.py"])},
        instructions="You are a support analyst. Use the tools to answer precisely.",
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user",
                       "content": "Which orders for customer 42 shipped late?"}]}
    )
    print(result["messages"][-1].content)

    await agent.shutdown()


asyncio.run(main())
```

The agent discovers every tool the MCP server exposes, converts each schema to a typed tool, and runs the reason-act-observe loop until it produces a final answer. Passing `agent_pattern="react"` explicitly gives you the exact same graph — it is just the default made visible. For the full list of factory options, see the [prebuilt patterns reference](../../core/engine-prebuilts.md).

## How `context_scope="auto"` bounds the loop automatically

This is where the Promptise default differs from a hand-rolled loop. The ReAct prebuilt runs its reasoning node with **`context_scope="auto"`**, which fixes the failure mode above without you choosing anything:

- **Short tasks are unchanged.** While the tool loop is small, the node sees the full transcript — identical behavior to a plain ReAct loop.
- **Deep loops switch to a facts ledger.** Once enough tool results have accumulated, the node automatically stops re-sending the raw transcript and instead sees the task, the most recent exchange, and a compact **deduplicated "facts gathered" ledger** — one line per `tool(args) = result`, last value wins, duplicates collapsed.
- **Repeat calls are cache-served.** An identical `(tool, args)` call returns the cached result instead of re-executing, so the model cannot burn turns re-querying what it already looked up.

The net effect is that context stays bounded and token-efficient on deep tasks while simple tasks pay nothing extra. If you want to force the bounded behavior for a known-deep task, the `managed` pattern pins the same ledger on every turn:

```python
# Same ReAct shape, but the facts-ledger context is always on —
# for long tool chains that gather many facts then aggregate.
agent = await build_agent(
    model="openai:gpt-5-mini",
    servers={"orders": StdioServerSpec(command="python", args=["tools.py"])},
    agent_pattern="managed",
)
```

Honestly scoped: the ledger is an **efficiency** primitive. On long chains it cuts redundant tool calls and bounds context at equal accuracy — it does not by itself make the model's final answer more correct. It removes a failure mode; it does not add reasoning power.

## When a different pattern is a better fit

ReAct is the right default for tool-calling agents, Q&A, and most general tasks — but it is not universal. Be honest about where it is the wrong shape:

- **Computing over data** (sums, averages, multi-hop joins across many records) is better served by the `code-action` pattern, where the model writes one sandboxed program over your tools instead of chaining dozens of calls. The engine ships this as a prebuilt too.
- **Tasks with a clear up-front plan** benefit from an explicit planning stage. A ReAct loop discovers its plan one step at a time; when the plan is knowable in advance, a plan-then-execute shape can be more predictable, as covered in [Plan-and-Execute Agents: How Planning Loops Work](plan-and-execute-agent.md).
- **A throwaway script** with three tool calls and no growth genuinely does not need any of this. A ten-line hand-rolled loop, or a bare LangGraph/LangChain agent, is perfectly fine — the transcript never gets long enough to drown. Reach for the managed default when tasks are long-lived, deep, or user-facing, not for a one-off.

If you are weighing several shapes against each other, the [Agent Reasoning Patterns: The Complete Guide](agent-reasoning-patterns.md) walks through all ten built-in patterns and when each earns its latency.

## Frequently asked questions

### What is the difference between a ReAct agent and chain-of-thought?

Chain-of-thought is reasoning only — the model thinks step by step but never leaves its own head. A ReAct agent interleaves that reasoning with **actions**: it calls tools and observes real results between reasoning steps, so its conclusions are grounded in live data rather than the model's training-time priors.

### Do I have to write the reason-act-observe loop myself?

No. With Promptise, `build_agent()` returns a ReAct agent by default and the engine runs the loop, manages the transcript, and calls your tools. You supply a model, your MCP servers, and instructions — there is no while-loop to write or maintain.

### Does the ReAct pattern get slow on long tasks?

A naive one does, because it re-sends a growing transcript every turn. The Promptise default avoids this: `context_scope="auto"` keeps short tasks unchanged and automatically switches deep tool loops to a bounded, deduplicated facts ledger, so context and token cost stay in check as the work gets deeper.

## Next steps

Spin up a real ReAct agent in five minutes with `build_agent()` — no manual loop to maintain and no transcript bookkeeping to babysit. Start from the [Quick Start](../../getting-started/quickstart.md), then read the [reasoning patterns overview](../../core/agents/reasoning-patterns.md) to see how ReAct fits alongside the other nine built-in patterns.
