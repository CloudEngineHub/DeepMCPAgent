---
title: "Why trim_messages Drops Facts Your Agent Still Needs"
description: "The stock fix for a bloated tool loop is 'trim the messages' or 'summarize the history' -- but trimming evicts by token or message count and can throw away…"
keywords: "trim_messages vs facts ledger, langgraph trim_messages token cost, deduplicated tool ledger, context_scope ledger vs summarization, bound tool loop without extra llm call, last-value-wins facts ledger"
date: 2026-07-16
slug: trim-messages-vs-facts-ledger
categories:
  - Cost & Efficiency
---

# Why trim_messages Drops Facts Your Agent Still Needs

Put **trim_messages vs a facts ledger** side by side on a deep tool loop and the difference stops being academic: trimming evicts messages by token or message count, so it can throw away the exact record the model still needs three turns later — while a ledger keeps that fact and drops the *duplicates* instead. When a tool-calling agent balloons in the middle of a long task, the reflex is to reach for one of two stock helpers: trim the message history, or summarize it. Both bound the transcript. Both also have a failure mode that only shows up on the tasks that matter most — the deep ones. This post compares those helpers head-to-head with Promptise Foundry's `context_scope="ledger"`, a last-value-wins facts ledger that deduplicates repeated tool calls and cache-serves a repeat instead of re-running it, with no extra model call.

## The stock fix: trim the messages or summarize the history

A naive tool-calling loop feeds the model the **entire** conversation on every turn. By the twentieth call it is re-reading a growing wall of its own past work, paying for thousands of redundant tokens, and losing the thread. There are two well-worn ways to bound that:

- **Trim by count or tokens.** Keep the last *N* messages (or the last *N* tokens) and drop the rest. It is cheap and deterministic. But eviction is positional, not semantic — the helper has no idea whether message #4 was "the customer's account ID" or "an acknowledgement." On a chain that gathers a dozen distinct facts, the fact you fetched early and still need at aggregation time is exactly the one that scrolled out of the window. The loop then re-queries it, which grows the transcript again, which triggers another trim. You have built a treadmill.
- **Summarize the history.** Compress old turns into a running summary with a second LLM call. This preserves *meaning* better than positional trimming — but it costs you an extra model round-trip every time it fires, adds latency on the critical path, and is lossy in its own way: a summary of "checked 9 employees, tenure ranged 2–11 years" is not the same as the 9 exact numbers you need to compute an average.

Neither helper does the one thing a tool loop most wants: notice that it already called `get_tenure("alice") = 4` two turns ago and simply *not* pay for that line twice.

## What other frameworks do today

To be fair, most mature frameworks ship *something* here. The differences are in what exactly they bound, and at what cost.

- **LangGraph / LangChain** ships `trim_messages`, a first-class utility with `strategy="last"`/`"first"`, a `token_counter`, and `max_tokens`. It is genuinely useful — and it evicts by token or message count, which means on a deep loop it can drop the specific `tool(args)=result` still in play. LangGraph also supports summarization via a pre-model hook (for example a `SummarizationNode`), which preserves meaning but spends an **extra LLM call** each time it runs. What neither path does is deduplicate repeated `tool(args)=result` pairs — a tool called with the same arguments twice occupies the transcript twice until something evicts it.
- **LlamaIndex** bounds growth too: `ChatMemoryBuffer`/`Memory` exposes a `token_limit` that caps how much transcript is carried forward. So it is not true that LlamaIndex has no growth control — it does. The delta is that it **truncates** the oldest content to stay under the limit rather than collapsing duplicates; a repeated tool result still consumes budget on every turn until it ages out of the window.
- **CrewAI, AutoGen, and Pydantic AI** feed the full running transcript back to the model by default on a standard tool loop. Each gives you hooks to intervene, but none ships a deduplicating, last-value-wins tool ledger as a built-in `context_scope`-style primitive you flip on per node.

The honest summary: trimming and summarizing are real, shipped features — trimming can evict a live fact, summarization costs a call, and truncation-based bounding keeps duplicates around. Promptise's edge is not "we have context control and they don't." It is that Promptise makes a **deduplicated, last-value-wins tool ledger a first-class, structural node primitive** — the default behaviour of a built-in pattern — rather than something you assemble yourself from hooks.

## The facts ledger: last-value-wins, deduplicated, cache-served

In the Promptise reasoning engine, an agent is a graph of `PromptNode`s, and every node takes a `context_scope` argument that controls what it sees on each LLM call. Set it to `"ledger"` and, instead of an ever-growing transcript, the node sees a compact ledger built from the tool results so far:

- **One line per `tool(args) = result`, last value wins.** Duplicate `(tool, args)` calls collapse into a single line — the newest result. A fact you fetched at turn 3 is still there at turn 20, occupying one line, not scrolled off by a token counter.
- **Placed last, where the model reads it most reliably.** The ledger sits right before the model's turn — the most salient position — so the model consults its gathered facts instead of re-calling a tool. The most recent exchange is kept in-flow so continuity isn't lost.
- **Cache-served repeats.** A repeated `(tool, args)` call returns the cached result instead of re-executing the tool — no duplicate network round-trip, and crucially **no extra LLM call** to compress anything. The bounding is structural, not a second model pass.

That is the exact contrast with the two stock fixes. Trimming can evict the live fact; the ledger keeps it and evicts the *duplicate*. Summarization spends a model call to compress; the ledger spends nothing — it is a dictionary keyed by `(tool, args)`. The full mechanism, including how the most-recent turn is preserved, is documented in the [context lifecycle guide](../../guides/context-lifecycle.md).

## Switch your deepest tool loop to a ledger (runnable)

The shortest path is the built-in `managed` pattern: a single tool-using node run with `context_scope="ledger"`, tuned for traversing a database or graph where you gather many facts and then aggregate. This is a complete, runnable script — set `OPENAI_API_KEY` and point it at any MCP server:

```python
import asyncio
from promptise import build_agent
from promptise.config import StdioServerSpec


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"company": StdioServerSpec(command="python", args=["company_tools.py"])},
        agent_pattern="managed",  # one tool node, run with context_scope="ledger"
        instructions=(
            "Answer by calling tools. A deduplicated ledger of the facts you have "
            "already gathered is provided every turn — consult it and never re-fetch "
            "a fact you already have."
        ),
        max_agent_iterations=30,  # deep chains make many calls
    )

    result = await agent.ainvoke({"messages": [
        {"role": "user", "content":
         "Which team has the highest average tenure? Traverse the org and report."}
    ]})
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

When you need mixed modes across a custom topology — a bounded gather stage feeding a distilled write stage — drop `context_scope` onto individual nodes instead. Here `gather` runs a ledgered tool loop and `write` sees only the distilled output it inherits plus the task, so neither stage drowns in the other's raw messages:

```python
from promptise.engine import PromptGraph, PromptNode

graph = PromptGraph("research", mode="static")
graph.add_node(PromptNode("gather", inject_tools=True, context_scope="ledger"))
graph.add_node(PromptNode("write", context_scope="scoped",
                          inherit_context_from="gather"))
graph.sequential("gather", "write")
graph.set_entry("gather")

agent = await build_agent(model="openai:gpt-5-mini", servers=srv, agent_pattern=graph)
```

Same tools, same model — only the *view* of history changes. To reproduce the head-to-head, run your deepest tool chain under `managed` and compare its token growth against the same loop wrapped in `trim_messages`: the ledger flattens where trimming keeps re-fetching the fact it evicted.

## Where the ledger fits with your other token levers

The ledger manages the *transcript* within one run. It composes with the other cost levers rather than replacing them:

- **Semantic response cache.** The ledger's cache-serve is per-run tool memoization — it de-duplicates within the task. Across *separate* runs, Promptise's [semantic cache](../../core/cache.md) serves whole responses for similar queries and isolates them per user by default. If you run multi-tenant, mind the isolation boundary; we walk through exactly how a shared cache stays leak-proof in [Can a Paraphrase Leak Another Tenant's Cached Answer?](semantic-cache-cross-tenant-leak.md).
- **Code-action for extreme depth.** If a chain is so deep that even a ledger is churning, the more radical move is to collapse the whole tool loop into a single sandboxed program — one execution instead of thirty round-trips. That is the [code-action pattern](../../guides/code-action.md).
- **Whole-budget token cuts.** For the broader picture of trimming token cost across a real deployment — tool definitions, memory, and transcript together — see [How to Cut Token Cost for a Multi-Tenant AI Agent](cut-token-cost-multi-tenant-ai-agent.md).

One honest caveat: `context_scope="ledger"` is an **efficiency primitive**. On long chains it cuts redundant tool calls and bounds token growth at *equal accuracy* — a real cost and latency win. It does not, by itself, make a weak model aggregate facts more correctly. If the model can gather the facts but mis-reasons over them, that is a capability limit, not a context one.

## Frequently asked questions

### How is a facts ledger different from trim_messages?

`trim_messages` evicts messages by token or message count — positional eviction that can drop the exact `tool(args)=result` you still need on a deep loop, forcing a re-fetch. A facts ledger evicts *duplicates* instead: it keeps one line per unique `(tool, args)` with last value wins, so a fact fetched early survives to aggregation time while repeats collapse. Trimming bounds size; the ledger bounds redundancy without losing live facts.

### Does the ledger cost an extra LLM call like summarization?

No. Summarizing history compresses old turns with a second model round-trip on the critical path. The ledger is built mechanically from tool results — a dictionary keyed by `(tool, args)` — and repeated calls are cache-served, so there is no extra LLM call and no added latency. That is the core `context_scope` ledger vs summarization trade: same bounded transcript, one fewer model pass.

### Will switching to context_scope="ledger" change my existing agents?

Only where you opt in. A raw `PromptNode` defaults to `context_scope="full"`, so existing behaviour is unchanged until you set `"ledger"` (or adopt the `managed` pattern) on the node that runs your deep tool loop. Short tasks stay untouched; bounding only kicks in where the chain is actually long.

### Does deduplication ever serve a stale value?

The ledger is last-value-wins per `(tool, args)`: if a tool is called again with the *same* arguments, the newest result replaces the older line, so you always see the latest value for that exact call. Different arguments are different lines. Within-run cache-serving returns the most recent result for an identical call rather than re-executing it.

## Next steps

Switch your deepest tool chain to `context_scope="ledger"` — or just adopt the `managed` pattern — and compare token growth against a LangGraph loop using `trim_messages`. Start with the [context lifecycle guide](../../guides/context-lifecycle.md) for the full decision table and the ledger internals, then layer in the [semantic cache](../../core/cache.md) and, for the deepest chains, the [code-action pattern](../../guides/code-action.md).
