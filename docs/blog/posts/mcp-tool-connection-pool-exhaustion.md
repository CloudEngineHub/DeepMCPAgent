---
title: "One Stalled MCP Tool Can Exhaust Your Connection Pool"
description: "A single slow downstream API doesn't just fail one call — its MCP tool keeps handler coroutines and pooled DB/HTTP connections open through every timeout…"
keywords: "mcp tool connection pool exhaustion, stalled mcp tool, per-tool concurrency limit mcp, connection pool starvation agent tools, isolate slow mcp tool"
date: 2026-07-16
slug: mcp-tool-connection-pool-exhaustion
categories:
  - MCP
---

# One Stalled MCP Tool Can Exhaust Your Connection Pool

MCP tool connection pool exhaustion is what happens when one slow downstream API doesn't just fail its own call — its tool keeps handler coroutines and pooled DB or HTTP connections open through every timeout, the shared pool saturates, and unrelated healthy tools start queueing behind it. The failure is sneaky because the sick tool often *works*, just slowly. By the end of this post you'll be able to reproduce that cascade in-process and cap its blast radius with two composable middlewares, so a single stalled MCP tool can never consume the server's shared capacity.

<!-- more -->

## How one stalled tool starves a shared connection pool

Picture an MCP server exposing a dozen tools to your agents. Ten of them are fast and local. One — `fetch_shipping_quote` — calls a third-party API that has just started to hang, returning after 8 seconds instead of 80 milliseconds. Nobody has changed a line of your code.

Here's the cascade, step by step:

- An agent calls the stalled tool. The handler coroutine sits inside `await`, holding whatever it acquired: a database session, a pooled HTTP connection, a slot in your outbound connection pool.
- Because an LLM agent is a retry engine, it (and every other agent) keeps calling that tool. Each call parks another coroutine and grabs another connection from the same pool.
- Your pool has a fixed size — say 20 connections. Once 20 stalled `fetch_shipping_quote` coroutines are parked, there are **zero** connections left.
- Now a healthy tool — `get_order`, which reads a local table in 2ms — asks the pool for a connection and *blocks*, waiting behind the stalled tool. That is connection pool starvation, and from the outside your entire server looks dead.

This is the crux of connection pool starvation for agent tools: pools are a *shared* resource, and the framework's default is to let any single tool consume all of it. A per-request timeout alone doesn't save you either — a 30-second timeout on a tool called ten times a second still keeps 300 connections busy. What you actually need is to **bound how much of the pool one tool is allowed to hold**, and to **stop calling a tool that is clearly unhealthy** so the pool drains and recovers.

## Reproduce the connection-pool exhaustion cascade — and cap it

Promptise Foundry gives you both bounds as drop-in, server-side middleware. `PerToolConcurrencyLimiter` reads a per-tool `max_concurrent` cap from each tool's declaration and sheds anything over the cap instead of queueing it. `CircuitBreakerMiddleware` tracks consecutive failures per tool and, once a dependency is clearly down, rejects calls in microseconds until a recovery window elapses. Together they put a hard ceiling on how many coroutines — and therefore how many pooled connections — one stalled MCP tool can ever hold.

The script below is fully runnable. It uses `TestClient`, which drives the complete middleware pipeline in-process, so you can watch the cascade get contained without standing up a server or a real database:

```python
import asyncio
from promptise.mcp.server import (
    MCPServer,
    CircuitBreakerMiddleware,
    PerToolConcurrencyLimiter,
    TimeoutMiddleware,
    CircuitOpenError,
    ToolError,
    TestClient,
)

server = MCPServer(name="orders")

# Outer: cap in-flight calls PER TOOL, so one stalled tool can never hold
# more than its share of handler coroutines (and the pooled connections
# each coroutine keeps open).
server.add_middleware(PerToolConcurrencyLimiter())
# Middle: trip a per-tool breaker after repeated failures; reject in
# microseconds while it is open, and probe recovery later.
breaker = CircuitBreakerMiddleware(failure_threshold=2, recovery_timeout=30.0)
server.add_middleware(breaker)
# Inner: bound every call so a stalled dependency fails fast (a real
# TIMEOUT the breaker above counts) instead of pinning a connection open.
server.add_middleware(TimeoutMiddleware(default_timeout=2.0))


# A degraded third-party dependency. max_concurrent caps how many of THIS
# tool's handler coroutines can be alive at once — everything above the cap
# is shed instead of queued onto the shared pool.
@server.tool(max_concurrent=2, timeout=1.0)
async def fetch_shipping_quote(order_id: str) -> dict:
    """Call a third-party shipping API (currently stalled)."""
    await asyncio.sleep(5)  # simulates the stall; the 1s timeout fires first
    return {"order_id": order_id, "quote_cents": 799}


# A healthy, unrelated tool sharing the same server and pool.
@server.tool()
async def get_order(order_id: str) -> dict:
    """Read an order from the local database."""
    return {"order_id": order_id, "status": "confirmed"}


# Turn an open circuit into a structured, retryable error the agent can act on.
@server.exception_handler(CircuitOpenError)
async def on_open(ctx, exc):
    return ToolError(
        message=f"shipping temporarily unavailable; retry in {exc.retry_after:.0f}s",
        code="SERVICE_UNAVAILABLE",
        retryable=True,
    )


async def main():
    client = TestClient(server)

    # Wave 1: flood the stalled tool with 8 concurrent calls, and hit the
    # healthy tool at the same time. Only 2 stalled calls run at once; the
    # other 6 are shed immediately instead of piling up on the pool.
    flood = [
        client.call_tool("fetch_shipping_quote", {"order_id": f"o{i}"})
        for i in range(8)
    ]
    healthy = client.call_tool("get_order", {"order_id": "o-vip"})
    results = await asyncio.gather(*flood, healthy)

    shed = sum("RATE_LIMIT" in r[0].text for r in results[:8])
    print(f"stalled tool: {shed}/8 calls shed instantly, 2 ran and timed out")
    print("healthy tool:", results[8][0].text)  # instant — never starved
    print("circuit     :", breaker.get_state("fetch_shipping_quote").value)

    # Wave 2: the breaker is open now, so the next call is rejected in
    # microseconds and the handler hands the agent a retryable message.
    r = await client.call_tool("fetch_shipping_quote", {"order_id": "o99"})
    print("next call   :", r[0].text[:90])


asyncio.run(main())
```

Running it prints:

```
stalled tool: 6/8 calls shed instantly, 2 ran and timed out
healthy tool: {"order_id": "o-vip", "status": "confirmed"}
circuit     : open
next call   : {"error": {"code": "SERVICE_UNAVAILABLE", "message": "shipping temporarily una...
```

Three things happened. The `PerToolConcurrencyLimiter` let only two `fetch_shipping_quote` coroutines exist at once and shed the other six with a retryable rate-limit error — so the stalled tool held at most two pooled connections, not eight. The healthy `get_order` returned instantly because the stalled tool was never allowed to monopolize capacity. And after two calls timed out, the breaker opened, converting every further call into an immediate `SERVICE_UNAVAILABLE` the agent can back off on — no coroutine, no connection, no wait.

## The two caps that isolate a slow MCP tool

The point isn't either middleware in isolation; it's that they enforce *different* limits and reinforce each other. Order matters, and Promptise applies middleware outermost-first in the order you add it:

| Middleware | The limit it enforces | Why it protects the pool |
|-----------|-----------------------|--------------------------|
| `PerToolConcurrencyLimiter` | A per-tool concurrency limit in MCP, read from each tool's `max_concurrent` | Caps how many of one tool's coroutines — and their connections — can be alive at once. Excess is shed, not queued. |
| `CircuitBreakerMiddleware` | Consecutive-failure threshold per tool | Once a dependency is clearly down, stops calling it entirely so parked connections drain and the pool recovers. |
| `TimeoutMiddleware` | Max duration per call | Turns an indefinite stall into a fast, countable failure — the signal the breaker trips on. |

Declaring `max_concurrent=2` on the tool is the load-bearing detail. It makes the concurrency cap a **property of the tool**, colocated with the handler, not a bespoke semaphore you thread through your business logic. `PerToolConcurrencyLimiter()` takes no arguments — it simply honors whatever each tool declares, so a new tool with a fragile upstream gets isolated the moment someone writes `max_concurrent=` on it. The breaker's `failure_threshold` and `recovery_timeout` govern how patient you are before you cut a tool off, and `excluded_tools` keeps critical probes like a health check out of the breaker entirely. The full menu — half-open recovery probes, webhook alerting on trips, and health checks — is documented in the [resilience patterns guide](../../mcp/server/resilience-patterns.md), and the [production features reference](../../mcp/server/production-features.md) explains how the middleware chain is pre-compiled so this protection adds effectively zero per-call overhead on the healthy path.

## What other frameworks do today

Being precise here matters more than scoring points. Most agent frameworks — LangChain, LangGraph, CrewAI, AutoGen — model a tool as a plain callable. They give you excellent *orchestration*, but tool-failure isolation is left to your handler code: if you want a slow tool bounded, you write the `try`/`except`, the `asyncio.Semaphore`, and the failure-counting state machine yourself, per tool, and keep them in sync. There is no framework-level object that says "this tool may hold at most N connections and should be cut off after M failures." That's not a bug in those frameworks; it's a layer they don't claim to own.

FastMCP is closer, because it ships a real middleware system with built-in middleware for logging, timing, rate limiting, and error handling. But as of its documented middleware toolkit, it does not include a circuit breaker, and it does not include a per-tool concurrency cap keyed off the tool definition. So the specific combination that stops connection pool exhaustion — a per-tool in-flight limit paired with a per-tool breaker that trips on timeouts — is still something you assemble and maintain yourself. If you're weighing the two stacks feature by feature, [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md) lays out where each one draws the line.

Promptise's edge is not that isolation is *possible* — it's possible anywhere with enough handler code. The edge is that isolation is **structural**: a declared `max_concurrent` on the tool plus two stock middlewares, with no per-handler plumbing. The cap lives next to the tool, travels with it, and shows up in the same declaration your team already reads. That "isolate the slow MCP tool by declaring it" property is the whole design goal.

## Tuning the caps without overcorrecting

Two honest cautions, because a cap set wrong causes its own problems.

First, size `max_concurrent` against your pool, not against your traffic. If your database pool holds 20 connections and you expose four tools that each touch it, giving every tool `max_concurrent=20` recreates the original problem — any one of them can still drain the pool. Budget the pool across tools so their caps *sum* to something the pool can serve, leaving headroom for the healthy ones. The concurrency limiter sheds excess with a retryable error, so a slightly tight cap costs a fast retry, while a slightly loose one costs an outage.

Second, don't put a breaker on failures that aren't outages. A shipping API that's genuinely down is a breaker's job; a tool that legitimately returns "no quote available" for some orders is not — counting those toward the threshold would trip the circuit on healthy inputs. Reserve the breaker for infrastructure failures and timeouts, and keep predictable business rejections out of the failure count. When a tool's contract itself changes, that's a versioning problem, not a resilience one — [Why a Small MCP Tool Change Broke Every Connected Agent](version-mcp-tools-without-breaking-clients.md) covers that failure mode separately.

## Frequently asked questions

### What causes MCP tool connection pool exhaustion?

A single slow downstream dependency. When a tool's handler stalls on an upstream API or database, its coroutine keeps holding whatever pooled connection it acquired. Because agents retry, many stalled coroutines accumulate, and once they've claimed every connection in a shared pool, unrelated healthy tools block waiting for a connection that never frees up. The server looks dead even though only one tool is actually sick.

### How is a per-tool concurrency limit different from a global one?

A global limiter (`ConcurrencyLimiter`) caps total in-flight calls across the whole server — useful, but a single tool can still consume the entire global budget. `PerToolConcurrencyLimiter` enforces a separate cap per tool, read from each tool's declared `max_concurrent`, so a flood of calls to one stalled tool is shed at *that tool's* ceiling and leaves the rest of the budget for everything else.

### Why pair a circuit breaker with the concurrency cap?

They solve adjacent halves of the problem. The concurrency cap limits how much of the pool one tool can hold *right now*; the breaker stops calling a tool that has been failing so those held connections drain and the dependency gets room to recover. The cap contains the blast radius; the breaker ends the incident. Neither alone fully prevents connection pool starvation.

### Do these middlewares slow down healthy tools?

No. On the healthy path the concurrency limiter checks one semaphore and the breaker increments one in-memory counter, and Promptise pre-compiles the middleware chain so there's effectively no per-call overhead. The cost is paid only when a tool is at capacity or its circuit is open — exactly when you want calls rejected fast.

## Next steps

Take the runnable script above, drop it into your project, and change `max_concurrent` and `failure_threshold` to match your real pool size and your dependency's typical recovery time — then watch a stalled tool get contained instead of taking the server with it. Read the [resilience patterns guide](../../mcp/server/resilience-patterns.md) to add half-open recovery probes and webhook alerts, and the [production features reference](../../mcp/server/production-features.md) to see how per-tool isolation sits alongside auth, rate limiting, and audit logging in a hardened deployment. One stalled MCP tool should cost you one degraded feature — never the whole server.
