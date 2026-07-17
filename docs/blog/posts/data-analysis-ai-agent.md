---
title: "Data Analysis AI Agent: Natural Language to SQL"
description: "Generic ReAct agents hallucinate numbers when they cross-reference tables. This deep-dive uses a plan-execute-verify reasoning pattern that validates…"
keywords: "data analysis ai agent, natural language to sql agent, nl2sql agent, analytics ai agent, llm sql agent"
date: 2026-07-16
slug: data-analysis-ai-agent
categories:
  - Use Cases
---

# Data Analysis AI Agent: Natural Language to SQL

A data analysis AI agent turns a plain-English question — "which region grew fastest last quarter, and by how much?" — into SQL, runs it, cross-references the results, and hands back an exact number. The catch is that a generic ReAct loop is dangerous here: when it has to join two tables and do arithmetic across the results, it will confidently invent a figure that looks plausible and is simply wrong. By the end of this post you'll know why that happens, and how to wire a plan-execute-verify reasoning pattern plus a bounded, ledger-scoped tool loop so your agent checks its math before it speaks.

## Why generic ReAct agents hallucinate numbers

Plain ReAct is one node in a loop: reason, call a tool, reason again, answer. For a single lookup — "what's the status of order 4471?" — that's perfect. It falls apart on analytics for two structural reasons.

- **No verification stage.** ReAct produces the final number in the same breath as its reasoning. If it fetched `revenue = 1.2M` from one query and `revenue = 980K` from another, nothing forces it to reconcile the two before answering. It picks one, or averages them in its head, and moves on.
- **Unbounded context.** Deep analytical questions mean many queries. Each tool call and its full result get appended to the transcript. Twenty queries in, the model is staring at a wall of prior SQL and rows, loses track of what it already knows, and re-runs the same `SELECT` it ran ten turns ago — or worse, reads a stale row from earlier and reports it as current.

The fix isn't a bigger model. It's a control flow that *separates gathering facts from committing to an answer*, and that keeps the working context small enough for the model to reason over cleanly. A model that has to hold twenty query results in its head will drop one; a model that reasons over a short, deduplicated summary of those results will not.

## The plan-execute-verify pattern for a natural language to SQL agent

Promptise Foundry ships reasoning patterns as `PromptGraph` topologies you select with a single argument. For a natural language to SQL agent, the `peoatr` pattern (Plan → Execute → Observe → Reflect) is the right shape:

1. **Plan** — the agent breaks the question into 2–4 subgoals ("get Q3 revenue by region", "get Q2 revenue by region", "compute growth") and self-rates the plan before proceeding.
2. **Act** — it executes one SQL tool call per turn against your database.
3. **Think** — it analyzes each result: is this subgoal done? What's still missing?
4. **Reflect** — it rates its own confidence and progress, and routes: replan, keep going, or answer.

Crucially, the answer only gets produced after the reflect stage clears it. That reflect gate is where a cross-table calculation gets a second look instead of shipping unchecked.

```python
import asyncio
from promptise import build_agent
from promptise.config import HTTPServerSpec


async def main() -> None:
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            # An MCP server that exposes read-only SQL tools over your warehouse.
            "warehouse": HTTPServerSpec(
                url="https://mcp.internal/warehouse",
                bearer_token="...",
            ),
        },
        agent_pattern="peoatr",  # plan -> act -> think -> reflect
        instructions=(
            "You are a careful data analyst. Break analytical questions into "
            "subgoals, query one thing at a time, and VERIFY every cross-table "
            "calculation against the raw rows before you report a number."
        ),
    )

    result = await agent.ainvoke({
        "messages": [{
            "role": "user",
            "content": "Which region grew revenue fastest from Q2 to Q3, and by what percent?",
        }],
    })
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

Swap `agent_pattern="peoatr"` for `"deliberate"` if you want an explicit Think → Plan → Act → Observe → Reflect chain with a dedicated up-front thinking stage — useful when the *interpretation* of the question is ambiguous, not just the arithmetic.

## Keep deep query loops bounded with context_scope=ledger

Here's the piece that stops long analytical sessions from exploding the prompt. When a single node makes many tool calls, you don't want it to re-read every prior query and result on every turn. Promptise's context lifecycle management solves this with `context_scope="ledger"`: instead of the growing transcript, the tool-using node sees the task plus a compact, **deduplicated "facts gathered" ledger** built from the results so far. The model stops re-fetching what it already knows, and token growth stays bounded on deep chains.

You can drop it into a custom graph so the *tool loop* runs ledger-scoped while a final node still verifies against the gathered facts:

```python
from promptise import build_agent
from promptise.config import HTTPServerSpec
from promptise.engine import PromptGraph, PromptNode
from promptise.engine.reasoning_nodes import ThinkNode, SynthesizeNode

graph = PromptGraph(
    "data-analyst",
    nodes=[
        ThinkNode("plan", is_entry=True),                    # decompose the question
        PromptNode(                                          # bounded SQL tool loop
            "query",
            inject_tools=True,
            context_scope="ledger",                          # dedup facts, cap tokens
        ),
        SynthesizeNode("verify", is_terminal=True),          # reconcile + report
    ],
)

agent = await build_agent(
    model="openai:gpt-5-mini",
    servers={"warehouse": HTTPServerSpec(url="https://mcp.internal/warehouse", bearer_token="...")},
    agent_pattern=graph,
    instructions="Verify every number against the gathered facts before reporting it.",
)
```

If you'd rather not hand-build a graph, `agent_pattern="managed"` gives you the same ledger-scoped tool node out of the box — a single deep-loop worker that never re-queries a fact it already has. Either way, `context_scope="ledger"` is an efficiency primitive: it cuts redundant tool calls and bounds token growth at equal accuracy — it is not itself an accuracy claim. The accuracy comes from the plan-execute-verify structure around it.

The fully runnable version of this — with a sample SQLite database, the MCP SQL server, and questions to try — lives in the [Data Analysis Agent lab](../../guides/lab-data-analysis.md). Start there against the bundled data, then point the same graph at your own warehouse. For a wider tour of what these building blocks compose into, see [what you can build](../../resources/showcase.md).

## When a plain text-to-SQL tool is enough

Be honest with yourself about the task before reaching for a multi-stage graph. A reasoning pattern with plan and verify stages costs more LLM turns, more latency, and more money than a single call. It earns that cost only when the question involves **multiple queries, cross-table joins, or arithmetic the model would otherwise fake**.

A plain text-to-SQL tool — one LLM call that emits one query you run yourself — is the better fit when:

- Every question maps to a single `SELECT` with no follow-up.
- Your BI layer or semantic model already handles joins and aggregation, and the LLM just needs to fill in a filter.
- You need sub-second responses and can't spend turns on reflection.
- The queries are templated, and you mostly need natural-language *slot-filling*, not open-ended analysis.

For those cases, expose one query tool from an MCP server and let a default ReAct agent call it. The `peoatr`/`managed` machinery is for the deep, error-prone questions — the ones where a wrong number quietly costs someone a decision. If you're orchestrating several such analysts together — one per data domain, coordinated by a planner — the patterns in [How to Build Multi-Agent Systems in Python](multi-agent-systems-python.md) show how to compose them.

## Frequently asked questions

### How is this different from just prompting GPT to write SQL?

Prompting a model to emit SQL gives you one query and no guardrails on the result. A data analysis AI agent runs the query against real tables, reads the actual rows, cross-references across queries, and — with the `peoatr` pattern — passes cross-table calculations through a reflect/verify stage before answering. That verification step is what catches the invented numbers a one-shot prompt ships silently.

### Does context_scope=ledger reduce accuracy?

No. Ledger scope changes *what the tool-using node sees each turn* — a deduplicated facts ledger instead of the full transcript — not what it can conclude. It bounds token growth and cuts redundant re-queries at equal accuracy. Accuracy on cross-table math comes from the plan-execute-verify structure, not from the context scope.

### Can I point this at my real data warehouse?

Yes. The agent talks to your database through an MCP server that exposes read-only SQL tools, configured with a `HTTPServerSpec` (or `StdioServerSpec` for a local process) and a bearer token. Give that server credentials scoped to read-only, run the lab against the sample database first, then swap the connection string for your warehouse.

## Next steps

Run the data analysis lab against the sample database, then swap in your warehouse connection — the [Data Analysis Agent lab](../../guides/lab-data-analysis.md) walks through every step. New to the framework? Start with the [Quick Start](../../getting-started/quickstart.md), then come back and pick your reasoning pattern.
