---
title: "When Agent Tool Loops Fail: Fixing Context Bloat"
description: "Names the exact production failure everyone hits on deep tool tasks: the transcript grows unbounded, the model loses the thread and re-fetches facts it…"
keywords: "agent stuck in tool loop, llm agent repeated tool calls, agent context window overflow, context_scope, managed reasoning pattern, deduplicated tool ledger"
date: 2026-07-16
slug: agent-stuck-in-tool-loop
categories:
  - Reasoning
---

# When Agent Tool Loops Fail: Fixing Context Bloat

If your agent is stuck in a tool loop — calling the same lookup over and over, its answer never arriving, its token bill climbing every turn — you are hitting the single most common production failure on deep tool tasks. It is not a prompt-wording problem and it is not a model problem. It is context bloat: a naive tool-calling loop feeds the model its entire transcript on every turn, and once that transcript grows large enough, the model loses the thread and re-fetches facts it already has. By the end of this post you will know exactly why the loop degrades, how the `context_scope` lever bounds it, and how to flatten token growth with a one-argument switch.

## Why an agent gets stuck in a tool loop

A hand-rolled reason–act–observe loop appends every tool request and every tool result to the running conversation, then replays the whole thing to the model on the next turn:

```
turn 1:  [system, user]
turn 3:  [system, user, ai→tool, tool_result, ai→tool, tool_result]
turn 12: [system, user, + 22 more messages]   ← the model re-reads ALL of this
```

For a task that needs, say, thirteen distinct facts, a naive loop can make *dozens* of calls. The relevant result gets buried deep in the transcript, the model can no longer see it clearly, and it does the obvious thing: it calls the tool again. That is where **llm agent repeated tool calls** come from — not stubbornness, but a signal that got lost in the middle of a wall of prior output.

Two things go wrong at once:

- **Tokens grow super-linearly.** Every turn re-bills the entire history, so cost and latency climb even though the actual work per step is small.
- **Accuracy can *drop*.** This is the counterintuitive part. More context is not more signal — past a point it is more noise, and the model's recall of any single buried fact gets worse.

Left unchecked, this ends one of two ways: you blow past the model's window (a classic **agent context window overflow**), or you hit your iteration cap with no answer. Both look like "the agent is stuck." Both have the same root cause.

## What context_scope actually does

Promptise Foundry treats context as a resource you manage, not a side effect you inherit. The lever is `context_scope`, an argument on every reasoning node in the [reasoning engine](../../core/engine.md). It controls exactly what a node sees on each LLM call, and it is fully opt-in — the default preserves existing behavior with zero regression.

| Mode | What the node sees | Use it for |
|------|--------------------|------------|
| `"full"` | The whole accumulated transcript | Short tasks, or when every prior message matters |
| `"scoped"` | System prompt + the original task + only its own in-progress tool loop | Multi-stage graphs — drops other stages' verbose output |
| `"ledger"` | System prompt + task + the most recent exchange + a compact deduplicated facts ledger | Long single-node tool loops that gather many facts, then aggregate |
| `"auto"` | Full transcript on simple tasks; auto-switches to the ledger once a loop grows | The default — bounded context without picking a pattern |

The important detail: the default `react` agent already runs `context_scope="auto"`. Simple tasks keep the full transcript, unchanged. But the moment a tool loop grows deep, the node switches itself to a bounded, deduplicated ledger. You get the protection without choosing anything. The full mechanism is documented in the [context lifecycle guide](../../guides/context-lifecycle.md).

## The managed reasoning pattern and its deduplicated tool ledger

When you *know* a task is a deep traversal — walking a database, hopping across a graph, gathering many records then aggregating — you can commit to the ledger explicitly with the **managed reasoning pattern**. Under the hood it is a single tool-using node run with `context_scope="ledger"`, and it changes what the model reads on every turn.

Instead of an ever-growing transcript, the node builds a compact ledger from the tool results so far:

- **One line per `tool(args) = result`, last value wins.** Identical `(tool, args)` pairs collapse automatically, so a **deduplicated tool ledger** never lists the same fact twice.
- **The ledger sits last, right before the model's turn** — the most salient position — so the model consults it instead of re-calling a tool.
- **The most recent exchange stays in-flow**, so the model keeps continuity and doesn't lose its place.
- **Repeated calls are cache-served.** If the model does re-request an identical `(tool, args)`, it gets the cached result instead of re-executing the tool.

That combination is what flattens the curve: the transcript stops growing, duplicate calls stop firing, and the model spends its attention on the facts rather than on its own history.

## Runnable: switch a runaway loop to the ledger

Here is the whole fix. Take an agent that was thrashing on a deep lookup and set `agent_pattern="managed"`:

```python
import asyncio
from promptise import build_agent
from promptise.config import StdioServerSpec


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"company": StdioServerSpec(command="python", args=["tools.py"])},
        agent_pattern="managed",
        instructions=(
            "Answer by calling tools. A ledger of facts you already gathered is "
            "provided each turn — consult it and never re-fetch a fact you have."
        ),
        max_agent_iterations=30,   # deep chains legitimately make many calls
    )

    result = await agent.ainvoke({"messages": [
        {"role": "user",
         "content": "Who manages the manager of the manager of employee E-4472?"}
    ]})
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

That is the entire change from a stock agent — one keyword. The instructions telling the model to consult its ledger are a helpful nudge, not a requirement; the ledger is assembled and injected by the engine either way.

If you are building a custom topology rather than using a prebuilt, `context_scope` drops onto any node directly, and you can mix modes per stage:

```python
from promptise.engine import PromptGraph, PromptNode

graph = PromptGraph("research", mode="static")
graph.add_node(PromptNode("gather", inject_tools=True, context_scope="ledger"))
graph.add_node(PromptNode("write", context_scope="scoped",
                          inherit_context_from="gather"))
graph.sequential("gather", "write")
graph.set_entry("gather")
```

Here `gather` runs a bounded tool loop, then `write` sees only the distilled output it inherits plus the task — neither stage drowns in the other's raw messages. Both `managed` and this pattern are catalogued in the [prebuilt patterns reference](../../core/engine-prebuilts.md).

## When a growing transcript is actually fine

Be honest about which problem you have, because the ledger solves exactly one of them.

`managed` is an **efficiency primitive**. On a long chain it cuts redundant tool calls and bounds token growth *at equal accuracy* — a real cost and latency win. What it does **not** do is make the model's final answer more correct. If your agent gathers the right facts but then mis-aggregates them, that is a model-capability limit, not a context one, and the ledger will not fix it. Reach instead for `code-action`, where the model writes one program to do the aggregation exactly, or a stronger model.

There are also cases where plain `context_scope="full"` is the right call and the ledger would only get in the way:

- **Short Q&A where every message matters.** If the conversation is five turns of nuanced back-and-forth, you want all of it in view. Collapsing it into a facts ledger throws away exactly the context that makes the answer good.
- **Tasks where ordering and phrasing carry meaning.** The ledger keeps values, not the prose around them. For reasoning that depends on *how* a prior step was expressed, keep the transcript intact.

The good news is you rarely have to make this call by hand: `"auto"` keeps the full transcript on the simple cases and only reaches for the ledger when a loop actually grows. For a broader map of when each control flow earns its cost, see [Agent Reasoning Patterns: The Complete Guide](agent-reasoning-patterns.md).

## Frequently asked questions

### Why does my LLM agent keep making repeated tool calls?

Because a naive loop replays the whole transcript every turn, and once it is long enough the model can no longer clearly see a result it already fetched — so it fetches it again. It is a context-visibility failure, not a reasoning failure. A deduplicated facts ledger fixes it by putting each gathered fact in one salient place and serving identical calls from cache.

### Do I have to change patterns to avoid context window overflow?

No. The default `react` agent already runs `context_scope="auto"`, which keeps the full transcript on simple tasks and automatically switches to a bounded ledger once a tool loop grows deep. You only need to set `agent_pattern="managed"` when you want to commit to the ledger up front for a task you already know is a deep traversal.

### Does the managed pattern make my agent more accurate?

Not by itself. It bounds context and eliminates redundant tool calls at equal accuracy — a cost and latency win, not a correctness one. If the model gathers the right facts but aggregates them wrong, that is a model limitation; consider `code-action` for exact aggregation or a more capable model.

## Next steps

Switch a runaway loop to `agent_pattern="managed"` (or simply keep the default `context_scope="auto"`) and watch token growth flatten on your next deep task. Start from the [Quick Start](../../getting-started/quickstart.md) to stand up an agent, then read the [context lifecycle guide](../../guides/context-lifecycle.md) for the full decision table on picking `full`, `scoped`, `ledger`, or `auto`.
