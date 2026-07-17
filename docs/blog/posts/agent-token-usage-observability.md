---
title: "Where Are Your Agent's Tokens Actually Going?"
description: "You can't cut what you can't see. Before you reach for a cache, a ledger, or tool selection, you need to know where the tokens are actually going -- which…"
keywords: "agent token usage observability, track llm token usage per turn, which tool calls burn tokens, attribute agent token cost, llm token usage tracking, agent observability token count"
date: 2026-07-16
slug: agent-token-usage-observability
categories:
  - Cost & Efficiency
---

# Where Are Your Agent's Tokens Actually Going?

**Agent token usage observability** is the difference between guessing at your token bill and knowing, turn by turn, exactly where it goes. Most teams reach for a fix — a semantic cache, a facts ledger, tool selection — before they have a single number telling them which part of the agent is actually expensive. That's backwards. You can't cut what you can't see, and the tokens are almost never spread evenly: a couple of fat tool schemas and one or two long reasoning turns usually dominate, while your cache quietly misses more often than you think. This post shows how to attribute token usage across every LLM turn and every tool call with Promptise Foundry's built-in observability — no external tracing service, no code beyond one flag.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## You can't cut what you can't see

An agent's token bill is a sum of many small line items, and they don't contribute equally. Three drivers dominate almost every stack:

- **Fat tool schemas.** Connect an agent to a few MCP servers and you re-send 20–50 tool definitions — name, description, and full JSON Schema — on *every* single turn. That's often the largest constant in the whole request, and it scales with your integration surface, not your conversation.
- **Per-turn accumulation.** A tool-using agent doesn't make one LLM call per request. It makes a turn to decide the tool, another to read the result, maybe a third to answer. Each turn re-sends the growing message history plus those schemas. Long tool loops multiply the cost.
- **Cache misses you assumed were hits.** If you added a semantic cache, you *believe* it's saving money. But a cache that hits 8% of the time is a rounding error you're paying to maintain. Without a hit-rate number, you're optimizing on faith.

The trap is that none of this is visible in your application code. You never wrote the schema payload — the framework assembled it. You never counted the turns — the agent loop did. So the natural instinct is to guess, apply a fix, and hope the invoice moves next month. Instrument first, and you stop guessing. You see that turn 3 spent 6,200 prompt tokens re-reading a bloated schema, that the `search` tool is called on 90% of requests, and that your cache hit rate is 11%. *Now* you know which lever to pull.

## Turn on observability with one flag

Here's the part the brief is really about. Promptise captures per-turn token usage, every tool call, latency, retries, and cache hit/miss automatically — you flip it on with `observe=True`. The agent below connects to an MCP server, answers a request, and then prints an attributed breakdown and writes an interactive HTML report you can open in a browser.

```python
import asyncio
from promptise import build_agent
from promptise.config import HTTPServerSpec


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
        instructions="You are a support agent. Use tools to resolve tickets.",
        observe=True,  # STANDARD level: every LLM turn's tokens + every tool call
    )

    await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Refund order #4021 and confirm the balance."}]}
    )

    stats = agent.get_stats()
    print("total tokens:      ", stats["total_tokens"])
    print("prompt/completion: ", stats["total_prompt_tokens"], "/", stats["total_completion_tokens"])
    print("LLM turns:         ", stats["llm_call_count"])
    print("tool calls:        ", stats["tool_call_count"])
    print("retries / errors:  ", stats["retry_count"], "/", stats["error_count"])
    print("cache hit/miss:    ",
          stats["events_by_type"].get("cache.hit", 0), "/",
          stats["events_by_type"].get("cache.miss", 0))
    print("tokens by agent:   ", stats["tokens_by_agent"])

    # Write a self-contained interactive HTML report.
    path = agent.generate_report("./reports/token-audit.html")
    print("report written to: ", path)


if __name__ == "__main__":
    asyncio.run(main())
```

The only prerequisite is an API key in your environment (`OPENAI_API_KEY`) and an MCP server on `localhost:8000`. Everything else — token accounting, per-turn latency percentiles, cache-event counts — is captured in an in-process ring buffer via a LangChain callback handler. Nothing leaves your machine. Full configuration lives in the [Observability guide](../../core/observability.md).

## Read the breakdown: which turns and tool schemas burn tokens

`get_stats()` returns the numbers you need to answer "where did the tokens go?" without opening a dashboard:

| Field | What it tells you |
|---|---|
| `total_prompt_tokens` / `total_completion_tokens` | How lopsided you are toward *input* (schemas + history) vs. *output* (the model actually writing) |
| `llm_call_count` | How many turns each request really costs — the multiplier on your per-turn prompt |
| `tool_call_count` | How tool-heavy the run is, and therefore how many extra reasoning turns you're paying for |
| `events_by_type["cache.hit"]` / `["cache.miss"]` | Your real cache hit rate — the honest signal on whether the cache is earning its keep |
| `tokens_by_agent` | In a multi-agent or delegated setup, which agent is spending the budget |
| `latency_p50_ms` / `latency_p95_ms` / `latency_p99_ms` | The tail latency behind slow requests, alongside the token story |

A high `total_prompt_tokens` with a low `total_completion_tokens` is the classic fingerprint of the fat-schema problem: the model is *reading* far more than it *writes*. That's your cue to reach for per-query tool selection — the [Tool Optimization guide](../../core/tool-optimization.md) shows how semantic top-K selection sends only the tools a query needs and trims 40–70% of that input. A poor `cache.hit` / `cache.miss` ratio tells you the [semantic cache](../../core/cache.md) needs a wider similarity threshold or better scoping before it's worth the maintenance. The point is that you decide *after* seeing the data, not before.

The auto-generated HTML report renders the same story as a timeline: each LLM turn with its prompt/completion split, each tool call with its arguments and latency, and every cache event inline — so you can literally scroll to the turn that spiked and see the schema it was re-sending. This is the disciplined starting line for the deeper work in [How to Cut Token Cost for a Multi-Tenant AI Agent](cut-token-cost-multi-tenant-ai-agent.md), where the same numbers drive per-tenant attribution.

## What other frameworks do today

Every serious framework can *count* tokens. The honest differentiator isn't counting — it's whether you get an attributed, per-turn-and-per-tool breakdown, rendered locally, without standing up a separate service. Here's where the major stacks actually stand:

- **LangChain / LangGraph** expose token usage in-process through callback handlers and `usage_metadata` on messages (for example `UsageMetadataCallbackHandler` and `get_openai_callback`). Those give you *totals*. The rich per-step trace UI — the turn-by-turn, tool-by-tool timeline you'd browse to find the expensive step — is **LangSmith**, a hosted SaaS you send traces to (with a self-hostable enterprise tier). So the token counts are local; the attributed visualization is a separate, mostly hosted product.
- **CrewAI** aggregates usage via `crew.usage_metrics` (prompt, completion, and total tokens rolled up per crew) and offers event listeners you wire into third-party observability platforms such as AgentOps or Langfuse. The roll-up is built in; per-turn and per-tool attribution and the report are what you point an external backend at.
- **AutoGen** provides usage summaries from its model clients and ships OpenTelemetry instrumentation you export to a collector and backend of your choice. First-class token totals: yes. A built-in local report: no — you bring the OTel pipeline.
- **Pydantic AI** tracks usage in-process via `result.usage()` and instruments natively over OpenTelemetry, with **Logfire** (its OTel-based hosted product) or any OTel backend rendering the trace. Again: usage locally, the browsable trace via an external service.

None of these frameworks "can't see tokens" — that would be false. The precise delta is that with each of them, per-turn *and* per-tool attribution with a browsable view means either settling for aggregate totals or wiring an external tracing service (LangSmith, Logfire, AgentOps, or a raw OTel backend). Promptise makes the attributed view **structural**: one flag on `build_agent()` captures every LLM turn's token usage alongside every tool call, latency, retry, and cache hit/miss in the same in-process timeline, at four graded verbosity levels, and hands you a self-contained HTML report with zero external dependencies. And to be clear about scope: Promptise reports token counts and cache hit rate, not provider dollar estimates — we don't control external LLM pricing, so we don't invent it.

## Graded levels and eight transporters

Observability that's all-or-nothing gets turned off in production. Promptise grades capture across four levels so you pay only for the detail you want, and routes events to any of eight transporters — including the same in-process HTML report, plus Prometheus and OpenTelemetry when you *do* want to feed a central backend.

```python
from promptise import build_agent
from promptise.config import HTTPServerSpec
from promptise.observability_config import (
    ObservabilityConfig,
    ObserveLevel,
    TransporterType,
)

config = ObservabilityConfig(
    level=ObserveLevel.FULL,  # OFF / BASIC / STANDARD / FULL
    session_name="token-audit",
    transporters=[
        TransporterType.HTML,        # interactive report on disk
        TransporterType.PROMETHEUS,  # /metrics for Grafana
        TransporterType.OTLP,        # spans to any OpenTelemetry backend
    ],
    output_dir="./observability",
)

agent = await build_agent(
    model="openai:gpt-5-mini",
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    observe=config,
)
```

`ObserveLevel.BASIC` records tool calls, agent I/O, and errors; `STANDARD` adds every LLM turn with token usage and latency; `FULL` adds prompt/response content and streaming tokens. The eight transporter types — HTML, JSON, structured log, console, Prometheus, OTLP, webhook, and callback — mean the *same* captured timeline can drive a local investigation today and a fleet-wide Grafana board tomorrow, without re-instrumenting. You start with `observe=True` on your laptop and graduate to the config object when you're ready, never switching tools.

## Frequently asked questions

**Does observability require an external service like LangSmith or Logfire?**
No. Everything is captured in an in-process ring buffer and rendered to a self-contained HTML report on disk. External backends (Prometheus, OpenTelemetry, webhooks) are *optional* transporters, not requirements. This is the core difference from stacks where the attributed trace view lives in a hosted product.

**Where do the token numbers come from?**
The `PromptiseCallbackHandler` reads token counts from each LLM result's `usage_metadata` — prompt, completion, and total tokens per turn. They're the model provider's own reported counts, not an estimate.

**Does turning it on slow the agent down?**
`observe=True` uses `ObserveLevel.STANDARD`, which records structured events to memory with negligible overhead. Use `BASIC` if you want tool-and-error tracking only, or `OFF` to disable it entirely. The transporters that do I/O (HTML, JSON, structured log) flush at the end, not on the hot path.

**Can I attribute tokens per user or tenant?**
Yes. Every timeline entry carries the authenticated caller's `user_id` and `session_id` from the `CallerContext`, so `collector.query(...)` and `collector.for_user(...)` slice the buffer per tenant — and `purge_user()` supports GDPR erasure. See [Can a Paraphrase Leak Another Tenant's Cached Answer?](semantic-cache-cross-tenant-leak.md) for how the same isolation applies to the cache.

**Does it report cost in dollars?**
No — by design. Promptise reports token counts, cache hit/miss, latency, and retries. It does not estimate provider prices, because those are set by external vendors we don't control.

## Next steps

Set `observe=True` on `build_agent()`, run one representative request, open the auto-generated HTML report, and find the turns and tool schemas driving your token bill *before* you optimize. Once you can see the breakdown, the fixes are obvious: trim fat schemas with [tool optimization](../../core/tool-optimization.md), verify your [semantic cache](../../core/cache.md) is actually hitting, and read the full [Observability guide](../../core/observability.md) for the four levels and eight transporters. Then measure the same numbers again — this time with proof that the line you moved was the one that mattered.
