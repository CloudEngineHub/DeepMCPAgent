---
title: "Plan-and-Execute Agents: How Planning Loops Work"
description: "Clears up the biggest misconception about planning agents: a separate plan step is not free accuracy. This walks the Plan-to-Act-to-Think-to-Reflect (PEOATR)…"
keywords: "plan and execute agent, plan and execute llm, planning agent pattern, peoatr reasoning, plan act reflect agent"
date: 2026-07-16
slug: plan-and-execute-agent
categories:
  - Reasoning
---

# Plan-and-Execute Agents: How Planning Loops Work

A plan-and-execute agent writes an explicit plan before it touches a single tool, then works through that plan step by step instead of improvising one call at a time. It sounds strictly better than a plain tool-calling loop—and that intuition is the biggest trap in agent design. By the end of this post you'll understand exactly what the Plan → Act → Think → Reflect loop does, how to run one in Promptise Foundry with a single parameter, and—just as important—how to tell when the extra planning step earns its cost versus when it just burns tokens.

<!-- more -->

## What a plan-and-execute agent actually does

The default agent loop is ReAct: reason, call a tool, read the result, reason again, repeat until you have an answer. It's greedy. It decides the *next* action based on what it just saw, with no commitment to a larger structure. That's fast and it's usually enough—see [The ReAct Agent Pattern Explained (with Code)](react-agent-pattern.md) for the mechanics.

A plan-and-execute LLM inverts the order of operations. Before acting, it decomposes the task into **subgoals**: an ordered list of smaller objectives that, completed in sequence, satisfy the request. Then it executes against that plan, checking progress as it goes. The value proposition is coherence over long tasks—when a job has six moving parts, a plan keeps the agent from wandering off after step two and forgetting steps four through six.

The important nuance, and the one most write-ups skip: **a separate plan step is not free accuracy.** A plan is just more generated text. On a short, single-hop task, the model would have gotten the same answer in one ReAct turn—the plan is pure overhead. Planning pays off only when the structure it imposes prevents a real failure mode, and that mostly happens on long, multi-subgoal work.

## Inside the PEOATR planning agent pattern

Promptise ships this as a prebuilt reasoning graph called **PEOATR**: Plan → Act → Think → Reflect. Each stage is a specialized node with a distinct job, and the routing between them is what makes it a *loop* rather than a straight line:

- **Plan** (`PlanNode`) — Creates subgoals and self-evaluates the plan's quality. If the plan is weak, it rejects it and re-plans instead of committing to a bad decomposition.
- **Act** (`PromptNode`) — Executes tools to make progress on the active subgoal. This is the only stage with tool access.
- **Think** (`ThinkNode`) — Analyzes the tool results and checks whether the current subgoal is actually complete.
- **Reflect** (`ReflectNode`) — Evaluates overall progress and routes: continue to the next subgoal, loop back to replan, or produce the final answer.

That routing is the substance of the pattern. After the agent acts and thinks, reflection can send it *backward*—to replan when the situation changed, or to keep acting when the subgoal isn't done. The self-evaluated subgoals in the Plan stage are the second half: the graph doesn't blindly trust its first plan, which is where a lot of naive "planner" implementations quietly go wrong. The full node catalog and the `PromptGraph.peoatr(...)` signature (including per-stage instruction overrides) are documented in [engine prebuilts](../../core/engine-prebuilts.md).

## Run a plan-and-execute agent in Promptise

You don't wire any of this by hand. `build_agent()` accepts an `agent_pattern` string, and `"peoatr"` swaps the default ReAct graph for the full Plan → Act → Think → Reflect loop. Everything else about the agent—memory, guardrails, cache, observability—stays identical; the reasoning graph only replaces the inner loop.

```python
import asyncio
from promptise import build_agent


async def main():
    # Plan → Act → Think → Reflect on a multi-subgoal task.
    agent = await build_agent(
        model="openai:gpt-5-mini",
        agent_pattern="peoatr",
        instructions="You are a planning analyst. Decompose the task into "
        "subgoals before acting, and verify each subgoal before moving on.",
    )

    task = (
        "Draft a production launch checklist for a new payments API. Cover "
        "authentication, rate limiting, observability, and rollback — with "
        "one concrete, verifiable step for each area."
    )
    result = await agent.ainvoke({"messages": [{"role": "user", "content": task}]})
    print(result["messages"][-1].content)

    await agent.shutdown()


asyncio.run(main())
```

That runs end to end with nothing but an `OPENAI_API_KEY` set. To make the **Act** stage do real work, point the agent at MCP tools—the planner will call them from inside the loop:

```python
from promptise.config import StdioServerSpec

agent = await build_agent(
    model="openai:gpt-5-mini",
    servers={"tools": StdioServerSpec(command="python", args=["tools.py"])},
    agent_pattern="peoatr",
    instructions="You are a planning analyst.",
)
```

To feel the difference the pattern makes, change one string: `agent_pattern="react"`. Run both on the same task and watch where each spends its turns. On the checklist above—four distinct areas, each needing its own concrete step—PEOATR's structure tends to keep coverage complete. On a one-line question, you'll see it generate a plan for something ReAct answers in a single turn. That contrast is the whole lesson.

## When a planning loop earns its cost

Reach for `peoatr` when the task has these properties:

- **Multiple independent subgoals.** Several distinct things must all get done, not one thing done well. Checklists, migration plans, and multi-section reports fit.
- **Long horizons where drift is the real risk.** The failure you're preventing is the agent losing the thread halfway through, not the agent getting one lookup wrong.
- **Steps whose results reshape the plan.** Reflection's replan edge only matters if new information can invalidate the original decomposition. If nothing ever changes the plan, you're paying for a loop you never use.

Skip it when the task is short, single-hop, or conversational. On those, the plan is generated text you pay for and then discard, and plain ReAct reaches the same answer with fewer tokens and lower latency. This is the honest tradeoff the [Agent Reasoning Patterns: The Complete Guide](agent-reasoning-patterns.md) makes across every pattern: multi-stage graphs buy structure, not raw accuracy. On a capable model, structure you don't need is just latency.

If PEOATR is close but not quite the shape you want—say you need a plan-act-reflect agent with a verification gate between subgoals, or a different stage order—you don't fork the framework. You compose your own graph from the same nodes. The [custom reasoning guide](../../guides/custom-reasoning.md) shows how to assemble `PlanNode`, `ThinkNode`, and `ReflectNode` into a `PromptGraph` and pass it straight to `agent_pattern`.

## When plain ReAct is the better fit

Be honest with yourself about the default. For the large majority of agents, `build_agent(model, servers)` with no pattern at all is the right call. ReAct is faster, cheaper, and—on a strong model—frequently just as accurate, because a capable model already reasons about next steps inside each turn. If you're computing over structured data (sums, joins, multi-hop aggregation), the [reasoning-patterns overview](../../core/agents/reasoning-patterns.md) will point you at `code-action` instead, which writes one sandboxed program rather than chaining tool calls—usually a better fit than a planning loop for that class of work.

The rule of thumb: don't adopt PEOATR expecting more correct answers on tasks ReAct already handles. Adopt it when a task's length and structure make *coherence* the bottleneck. That's the narrow band where a planning agent pattern clearly wins, and outside it, the plan is overhead.

## Frequently asked questions

### Does a plan-and-execute agent give more accurate answers than ReAct?

Not by default. The plan step adds structure, not correctness. On short or single-hop tasks a capable model reaches the same answer with plain ReAct while spending fewer tokens. PEOATR's advantage shows up on long, multi-subgoal tasks where the real risk is the agent drifting off course, not getting one step wrong.

### What does PEOATR stand for in Promptise?

PEOATR is the Plan → Act → Think → Reflect reasoning graph. Plan decomposes the task into self-evaluated subgoals, Act runs the tools, Think checks whether a subgoal is complete, and Reflect decides whether to continue, replan, or answer. You enable it with `agent_pattern="peoatr"` on `build_agent()`.

### Can I customize the planning and reflection steps?

Yes. `PromptGraph.peoatr(...)` accepts per-stage instruction overrides (`planning_instructions`, `acting_instructions`, `thinking_instructions`, `reflecting_instructions`), and if you need a different shape entirely you can build a custom `PromptGraph` from the same nodes and pass it as `agent_pattern`. See the [custom reasoning guide](../../guides/custom-reasoning.md).

## Next steps

Run a plan-and-execute agent with `agent_pattern="peoatr"` and compare it directly to the ReAct default on one of your real multi-subgoal tasks—that side-by-side is the fastest way to learn where planning earns its keep. Start from the [Quick Start](../../getting-started/quickstart.md) to get an agent running, then browse the full pattern catalog in [engine prebuilts](../../core/engine-prebuilts.md) to see how PEOATR fits alongside the other nine built-in reasoning graphs.
