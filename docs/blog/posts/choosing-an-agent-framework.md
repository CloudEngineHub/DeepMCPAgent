---
title: "How to Choose an AI Agent Framework: 2026 Checklist"
description: "A vendor-neutral scored rubric (tool wiring cost, multi-user access control, governance, lock-in, air-gap support) instead of a feature-brag. It shows where…"
keywords: "choosing an agent framework, how to pick an agent framework, agent framework selection criteria, agent framework requirements checklist, evaluate AI agent frameworks"
date: 2026-07-16
slug: choosing-an-agent-framework
categories:
  - Comparisons
---

# How to Choose an AI Agent Framework: 2026 Checklist

Choosing an agent framework is a decision you live with for years, so it deserves more rigor than a feature comparison table and a GitHub star count. Most teams pick based on the demo that ran fastest, then discover six months later that the real cost was somewhere the demo never touched — tool maintenance, access control, or vendor lock-in. This post gives you a vendor-neutral scored rubric so you can evaluate AI agent frameworks against the criteria that actually bite in production, and shows concretely where an MCP-native design removes an entire category of work most frameworks make you own forever.

By the end you'll have a repeatable scorecard you can run against any candidate — including Promptise Foundry — and a clear sense of which trade-offs matter for your team.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## A vendor-neutral rubric for evaluating AI agent frameworks

Score each framework 0–3 on five dimensions. Zero means "you build it yourself," three means "it ships and works out of the box." Add the scores; anything under 10 means you're inheriting a lot of undifferentiated engineering.

| Dimension | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| **Tool wiring cost** | Every tool hand-coded per framework format | Adapters exist but manual | Some auto-discovery | Tools discovered from a standard protocol, not wired |
| **Multi-user access control** | Global keys, no per-request identity | Roles bolted on | Per-request identity | Capability-based auth + per-request identity + tenancy |
| **Governance** | None | Logging only | Rate limits + audit | Budgets, health checks, approval gates, audit chains |
| **Lock-in** | Proprietary tool format + hosted-only | Proprietary format, self-host | Open format, one vendor | Open protocol, tools portable across agents |
| **Air-gap support** | Cloud-only | Cloud-only models | Local models, cloud deps | Fully offline: local models, local embeddings, no phone-home |

The point of scoring is not to crown a winner. It's to make the hidden costs visible *before* you commit, so "how to pick an agent framework" becomes an evidence question instead of a vibes question.

## Agent framework selection criteria that actually matter

Here's what each dimension is really testing, and why it belongs on your agent framework requirements checklist.

- **Tool wiring cost.** Every tool your agent calls is code someone maintains. If the framework makes you translate each API into its private tool format, that translation layer is now yours to keep in sync forever. This is the single most underestimated line item.
- **Multi-user access control.** A prototype has one user: you. Production has thousands, each allowed to see different data. If identity is an afterthought, you'll retrofit it under deadline pressure — the worst time to design a security boundary.
- **Governance.** Autonomous agents take actions. Without spend budgets, loop detection, and human approval for irreversible operations, a single bad reasoning step can page your on-call at 3 a.m.
- **Lock-in.** Ask one question: if you switch frameworks next year, do your tools come with you? If tools are written in a proprietary format, the answer is no.
- **Air-gap support.** Regulated and defense workloads can't call a hosted embedding API. If tool selection, guardrails, or memory quietly depend on a cloud endpoint, the framework is off the table for those teams.

## The biggest hidden cost: hand-wired tools

The dimension that separates frameworks most sharply is tool wiring, so it's worth showing concretely. In a hand-wired framework, adding a CRM integration means writing a tool wrapper, a schema, and glue code — and rewriting all of it when the API changes. Multiply by every service your agent touches.

Promptise Foundry takes the MCP-first route: **tools are discovered, not wired.** You point the agent at a [Model Context Protocol](../../getting-started/what-is-mcp.md) server, and the agent calls `tools/list` on it at startup, reads the returned schemas, and converts them into typed tools automatically. You never hand-write a tool wrapper for a service that already speaks MCP.

```python
import asyncio
from promptise import build_agent
from promptise.config import HTTPServerSpec


async def main():
    # Point the agent at MCP servers. It calls tools/list on each,
    # discovers every tool, and converts the schemas to typed tools.
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            "crm":     HTTPServerSpec(url="https://mcp.internal/crm/mcp", bearer_token="..."),
            "billing": HTTPServerSpec(url="https://mcp.internal/billing/mcp", bearer_token="..."),
        },
        instructions="You are a customer support agent.",
        optimize_tools="semantic",  # local embeddings pick only relevant tools per query
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What's the balance on account 4471?"}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

No tool definitions. No schema translation. When the CRM server adds a tool, the agent picks it up on the next start — no code change on your side. That `optimize_tools="semantic"` line does one more thing worth calling out: it uses local embeddings to send only the tools relevant to each query, which the framework measures at **40–70% fewer tokens** on tool-heavy servers. Because the models run locally, it works air-gapped too.

The strategic payoff is portability. A tool you expose over MCP works with Promptise, Claude Desktop, Cursor, and any other MCP-compatible client — the same server, unchanged. That directly moves your **lock-in** score toward 3, because your integration work survives a framework switch. For a deeper walkthrough of discovery and configuration, see the [building agents guide](../../guides/building-agents.md).

## Scoring an example: a production support agent

Say you're building a customer support agent that reads from a CRM and a billing system, serves external users, and can issue refunds. Run it through the rubric:

- **Tool wiring cost (3):** CRM and billing already expose MCP servers, so both are discovered, not wired.
- **Multi-user access control (3):** each request carries a `CallerContext` with `user_id`, `roles`, and `tenant_id`, and that identity propagates to cache, memory, and audit logs.
- **Governance (3):** the refund tool sits behind a server-side approval gate, so a human approves before money moves — enforced for *any* MCP client, not just this agent.
- **Lock-in (3):** the CRM and billing tools are plain MCP servers, reusable by other agents unchanged.
- **Air-gap support (2 or 3):** local embeddings and local guardrail models mean the only external dependency is the LLM itself — swap in a local model and you're at 3.

That's a 14–15. The same build on a hand-wired, cloud-coupled framework often lands near 6, and the gap is almost entirely *work you would have to do and maintain yourself*. This is the difference the [Why Promptise Foundry](../../getting-started/why-promptise.md) page describes as production primitives being included, not optional.

## When another framework is the better fit

No rubric is complete without the honest counter-case. Promptise Foundry is MCP-native and opinionated, and that's not always what you want:

- **Your tools don't speak MCP and never will.** If your entire ecosystem is a pile of ad-hoc Python functions and you have no appetite to expose them over a protocol, a framework built around plain in-process functions has less ceremony on day one.
- **You want the largest possible ecosystem of prebuilt integrations right now.** LangChain's breadth of community integrations is real and hard to match; if you need an obscure connector today, it may already exist there. Our [LangChain alternatives for production Python agents](langchain-alternative.md) post covers that trade-off in detail and is fair about where LangChain wins.
- **You need a visual, low-code builder.** Promptise is a code-first Python framework. If your team wants drag-and-drop flows over Python, that's a different product category.
- **You're doing a throwaway prototype.** For a weekend spike that will never see a second user, governance, tenancy, and audit chains are overhead you don't need yet.

The rubric handles this gracefully: weight the dimensions for *your* context. A solo hacker can zero out multi-user access control; a bank cannot. For a broader head-to-head across several frameworks, the [Best AI Agent Framework in 2026: An Honest Guide](best-ai-agent-framework-2026.md) applies the same scoring philosophy across the field.

## Frequently asked questions

### What are the most important agent framework selection criteria?

Tool wiring cost, multi-user access control, governance, lock-in, and air-gap support. These are the dimensions where the true cost of a framework shows up months after the initial demo. Score each 0–3 and weight them for your context — a regulated enterprise weights access control and governance heavily, while a solo builder may ignore them.

### How is choosing an agent framework different from choosing an LLM?

The model is swappable; the framework is structural. Most frameworks let you change the underlying LLM with one line, but the way tools are wired, how identity flows, and how governance is enforced are baked into the framework's architecture. That's why choosing an agent framework deserves more scrutiny than choosing a model.

### Does MCP-first tool discovery lock me into one vendor?

No — it's the opposite. The Model Context Protocol is an open standard, so a tool you expose over MCP is callable by any MCP-compatible agent, not just one framework. That portability is exactly what improves your lock-in score, because your integration work survives a framework migration.

## Next steps

Score your candidates with the rubric above, then see how discovered-not-wired tools work in practice in the [building agents guide](../../guides/building-agents.md). When you're ready to run your own scorecard hands-on, start with the [Quick Start](../../getting-started/quickstart.md) — `pip install promptise` and you can have a tool-discovering agent running in a few minutes.
