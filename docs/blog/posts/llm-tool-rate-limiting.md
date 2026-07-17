---
title: "LLM Tool Rate Limiting: Per-Client & Per-Tool Guide"
description: "Generic API rate-limiting guides ignore that agents fan out many tool calls per turn; this walks through token-bucket limits scoped per-client AND per-tool…"
keywords: "LLM tool rate limiting, rate limit MCP tools, token bucket rate limiter, per-tool rate limits, API rate limiting for agents, Retry-After for tool calls"
date: 2026-07-16
slug: llm-tool-rate-limiting
categories:
  - Production
---

# LLM Tool Rate Limiting: Per-Client and Per-Tool Guide

LLM tool rate limiting is the part of production agent infrastructure that most tutorials skip, and it is exactly the part that pages your team at 2 a.m. An agent is not a well-behaved REST client that makes one request per user action. Give it a task and it will fan out five, ten, or fifty tool calls in a single turn, retrying and reformulating as it reasons. A flat "requests per minute" cap treats all of that as one undifferentiated stream, which is why it protects nothing. By the end of this guide you will know how to declare a rate limit directly on the tool that needs it, how the token bucket behaves under bursty agent traffic, and when a plain API gateway is the better tool for the job.

## Why a global limiter is the wrong abstraction for agents

Classic API rate-limiting guides assume a predictable client: one browser, one mobile app, one request at a time. Agents break that assumption in two ways.

- **They fan out.** A research agent might call `search`, `fetch_page`, and `summarize` a dozen times before it produces an answer. A global "120 calls/min" limit is either too tight (it throttles legitimate reasoning) or too loose (it lets one runaway loop starve everyone else).
- **Their tools are not equal.** A `lookup_customer` call hits a warm cache and returns in 2 ms. A `generate_report` call spins up a heavy query that pins a database connection for 30 seconds. Counting both against the same bucket means your cheapest tool and your most expensive tool share one budget — so a burst of cheap calls can lock out the expensive one, and vice versa.

The fix is to scope the limit to the two dimensions that actually matter: **who is calling** (per-client) and **what they are calling** (per-tool). That is the difference between "this agent is being greedy" and "this specific expensive operation needs breathing room."

## Declarative per-tool rate limits with `@server.tool`

In Promptise Foundry you attach a rate limit to the tool, not to a global config object. You declare it once and the server wires the enforcement in automatically at build time — no middleware to remember to install, no drift between the declaration and the behavior.

```python
import asyncio
import json

from promptise.mcp.server import MCPServer, TestClient

server = MCPServer("billing-tools")


# An expensive tool declares its own contract: 5 calls per minute, per client.
@server.tool(rate_limit="5/min")
async def generate_report(department: str) -> dict:
    """Build a heavy analytics report for one department."""
    return {"department": department, "rows": 10_000}


# A cheap lookup can run hot without starving the expensive one.
@server.tool(rate_limit="1000/min")
async def lookup_customer(customer_id: str) -> str:
    """Fetch a single customer record."""
    return f"customer {customer_id}: premium"


async def main() -> None:
    client = TestClient(server)
    for i in range(1, 8):
        result = await client.call_tool("generate_report", {"department": "sales"})
        text = result[0].text
        if "RATE_LIMIT_EXCEEDED" in text:
            hint = json.loads(text)["error"]["details"]["retry_after_seconds"]
            print(f"call {i}: 429 rate limited — retry after {hint:.0f}s")
        else:
            print(f"call {i}: ok")


asyncio.run(main())
```

Run it and the first five calls to `generate_report` succeed, then calls six and seven are refused with a machine-readable hint telling the caller how long to wait. `lookup_customer` is completely unaffected — it has its own bucket. The `rate_limit` string accepts `s`/`sec`/`second`, `m`/`min`/`minute`, and `h`/`hr`/`hour` units, so `"10/sec"` and `"500/hour"` are equally valid. A typo like `"100/century"` fails loudly at registration instead of silently never limiting. The [Caching & Performance](../../mcp/server/caching-performance.md) reference documents the full parsing rules and the declared-vs-global precedence.

Notice there is no network in that example — `TestClient` runs the entire pipeline (validation, middleware, guards, handler) in-process, which makes rate-limit behavior something you can assert in a unit test rather than discover in production.

## How the token bucket rate limiter keeps bursts sane

Under the hood, each declared limit gets its own **token bucket**. The mental model is simple:

- The bucket holds up to `burst` tokens and refills at a steady `rate_per_minute`.
- Every tool call removes one token. If a token is available, the call proceeds.
- If the bucket is empty, the call is refused and told exactly how many seconds until the next token drips in.

This is the right shape for agents specifically because it separates *sustained* rate from *burst* capacity. An agent that legitimately needs to make eight quick calls to gather context should not be throttled the way a client stuck in an infinite retry loop should be. A token bucket lets the first burst through, then enforces the steady rate once the bucket drains.

You can also install a server-wide limiter as a safety net and configure its burst explicitly:

```python
from promptise.mcp.server import RateLimitMiddleware, TokenBucketLimiter

# 120 sustained calls/min per client, tolerating short bursts of 20,
# counted separately per tool.
server.add_middleware(
    RateLimitMiddleware(
        limiter=TokenBucketLimiter(rate_per_minute=120, burst=20),
        per_tool=True,
    )
)
```

The declared per-tool limits and this server-wide policy **coexist**: the middleware enforces your blanket abuse-prevention rule, while each `@server.tool(rate_limit=...)` enforces that specific tool's contract. Neither overrides the other — a call has to satisfy both.

## Per-client keys so one agent can't starve the rest

A limit is only useful if it isolates callers from each other. Promptise keys each bucket by the authenticated client identity. When your [authentication middleware](../../mcp/server/production-features.md) populates a client ID from a JWT or API key, every client gets its own independent bucket for every tool. Agent A burning through its `generate_report` budget has zero effect on Agent B.

Multi-tenancy is handled at the same layer. If your tokens carry a tenant claim, buckets are tenant-qualified, so one tenant's traffic can never exhaust another tenant's quota — the keys are joined injectively, meaning a colon inside a tenant or client ID can't accidentally collide two distinct tenants onto one bucket. If you are building a SaaS product on top of agents, that isolation is a hard requirement; the [Multi-Tenant AI Agents: Architecture for SaaS](multi-tenant-ai-agent.md) post covers how tenant identity flows through the rest of the stack.

For unauthenticated tools, calls fall back to a single shared bucket — safe by default, but a strong signal that anything worth rate-limiting is probably worth authenticating too.

## Retry-After for tool calls: what the agent actually sees

When a limit trips, the caller does not get a bare failure. It gets a structured `RateLimitError` with `retryable=True` and a `retry_after` hint (surfaced as `retry_after_seconds` in the error details, and as a `Retry-After` signal over HTTP transport). That matters because the caller is often an LLM. A generic "500 error" invites the model to thrash — retry immediately, reformulate, retry again. A structured, retryable error with an explicit wait time gives it something to reason about: back off, do other work, come back in N seconds.

This ties into a broader pattern. Rate limiting is one member of a family of backpressure and failure-isolation tools — timeouts, concurrency limits, and circuit breakers all shape how load reaches your handlers. The [Resilience Patterns](../../mcp/server/resilience-patterns.md) page shows how to combine a circuit breaker (which trips a tool *off* after repeated downstream failures) with rate limiting (which paces a tool that is working fine but being called too hard). They solve different problems and are strongest together.

## When a global limiter or an API gateway is the better fit

Per-tool, per-client limiting is the right default for agent servers, but it is not always the right answer.

- **You are protecting the process, not individual tools.** If your real constraint is total CPU or a shared connection pool, a single server-wide `RateLimitMiddleware` (or a concurrency limiter) is simpler and more honest than a dozen per-tool declarations.
- **All your traffic already flows through an API gateway.** If you run Kong, Envoy, or an AWS API Gateway in front of everything, coarse per-IP or per-key limiting there is cheap and centralized. Use the in-server per-tool limits for the fine-grained rules a gateway can't express — like "5 report generations per minute per tenant" — and let the gateway handle the blunt edge-level throttling.
- **You need distributed, cross-replica limits.** Promptise's token bucket is in-process and per-replica. Across ten replicas, a "5/min" tool is effectively "up to 50/min" cluster-wide. If you need a globally exact budget, put a Redis- or gateway-backed limiter in front and treat the in-server limit as a per-replica backstop.

The honest summary: use declared per-tool limits for rules specific to your tools and tenants, and reach for a global limiter or gateway when the constraint is genuinely global. They are complementary, not competing.

## Frequently asked questions

### What's the difference between a per-tool and a global rate limit?

A global rate limit counts every call against one shared budget, regardless of which tool ran. A per-tool limit gives each tool its own budget, so a flood of cheap calls can't lock out an expensive tool and vice versa. In Promptise, `@server.tool(rate_limit="5/min")` is per-tool and per-client; `RateLimitMiddleware` is the server-wide net. They coexist, and a call must satisfy both.

### How do I return a Retry-After value for rate-limited tool calls?

You don't build it by hand. When a declared or middleware limit trips, Promptise raises a `RateLimitError` with `retryable=True` and a computed `retry_after` hint, exposed as `retry_after_seconds` in the error details and as a `Retry-After` signal over HTTP transport. The token bucket calculates the wait from how far the bucket is below one token, so the hint is accurate rather than a fixed guess.

### Does rate limiting work across multiple server replicas?

The built-in `TokenBucketLimiter` is in-process, so limits apply per replica. For a globally exact budget across a fleet, put a distributed limiter (Redis-backed or at your API gateway) in front and use the in-server limit as a per-replica backstop. For most abuse-prevention cases, per-replica limiting is sufficient and far simpler.

## Next steps

Read the [Resilience Patterns](../../mcp/server/resilience-patterns.md) page and add a `rate_limit` to your single busiest tool — it is a one-line change that pays for itself the first time an agent gets stuck in a retry loop. If you're hardening a server for real traffic, pair this with the [Production AI Agent Checklist: Ship Agents Safely](production-ai-agent-checklist.md), and start from the [Quick Start](../../getting-started/quickstart.md) if you're new to building MCP tools with Promptise Foundry.
