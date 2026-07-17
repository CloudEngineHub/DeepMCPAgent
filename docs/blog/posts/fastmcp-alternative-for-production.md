---
title: "FastMCP vs Promptise: The Production MCP Stack Compared"
description: "FastMCP is a real production server SDK, not a toy — so the honest question isn't 'does it work' but 'what do you still assemble yourself.' This pillar…"
keywords: "fastmcp alternative for production, fastmcp vs promptise, production mcp server framework, integrated mcp middleware stack, mcp server sdk comparison"
date: 2026-07-16
slug: fastmcp-alternative-for-production
categories:
  - MCP
---

# FastMCP vs Promptise: The Production MCP Stack Compared

If you are shopping for a **fastmcp alternative for production**, start by dropping the framing that FastMCP is a toy — it is a genuinely capable MCP server SDK, and the honest question is not *whether it works* but *how much production plumbing you still assemble by hand*. FastMCP gets you typed tools, mounting, auth, and a real middleware pipeline. This pillar walks the **fastmcp vs promptise** decision feature by feature, gives FastMCP full credit for what it already does well, and then marks the four capabilities Promptise Foundry folds into one middleware stack that FastMCP leaves you to bolt together yourself: versioned tool coexistence, per-tool circuit breaking, tenant-qualified rate limits, and an MCP-native durable job queue.

The goal here is not to declare a winner in the abstract. It is to help you predict, before you commit, which parts of your **production mcp server framework** you will build once with a decorator and which parts you will design, test, and maintain yourself.

## What FastMCP already gets right

Let us give FastMCP its due, because for a large class of servers it is the right call.

FastMCP's ergonomics are excellent. `@mcp.tool`, `@mcp.resource`, and `@mcp.prompt` decorators generate JSON Schema straight from your Python type hints, so there is no hand-written schema boilerplate. It composes servers with prefix-namespaced mounting (`mount()` / `import_server`), so separate teams can ship separate servers behind one gateway. It ships real authentication — Bearer/JWT and OAuth providers — not a stub.

And critically for this comparison, FastMCP has a first-class **middleware pipeline** with a solid set of built-ins. Its current lineup includes `LoggingMiddleware` and `StructuredLoggingMiddleware`, `TimingMiddleware` and `DetailedTimingMiddleware`, `ResponseCachingMiddleware`, `ErrorHandlingMiddleware`, `RetryMiddleware`, `PingMiddleware`, `ResponseLimitingMiddleware`, and — the one people assume comparison articles will pretend does not exist — `RateLimitingMiddleware`, which uses the same token-bucket algorithm Promptise does, plus a `SlidingWindowRateLimitingMiddleware` variant.

So this is not a "raw SDK vs framework" story. Both tools are frameworks. Reach for FastMCP when you want a fast, well-documented path to a schema-typed MCP server with logging, timing, caching, retries, and basic rate limiting — and when the four capabilities below are not on your critical path. If that is you, FastMCP is a fine choice and this post will not try to talk you out of it.

## What other frameworks do today

Before the four deltas, the honest lay of the land — stated precisely, because vague "competitor X can't do Y" claims are how comparison posts lose trust.

**FastMCP (the real point of comparison).** Its token-bucket `RateLimitingMiddleware` is real, but it operates **globally by default**; per-client enforcement requires you to pass a `get_client_id` function, and there is no notion of *tenant* qualification in that key — you compose it yourself. Its middleware set includes retry and centralized error handling, but **no circuit breaker** — no built-in open/half-open/closed state machine per tool. Its documentation describes **no built-in tool versioning** that serves `search` (latest) alongside `search@1.0` (a pinned older contract) from one base name, and **no built-in durable job queue** or background-job system; tool calls are request/response. None of these are things FastMCP does badly — they are simply things it does not ship, so you build and maintain them.

**Agent frameworks (LangChain, LangGraph, CrewAI, AutoGen).** These ship **no production MCP server SDK at all**. They are MCP *consumers*: LangChain has `langchain-mcp-adapters` and CrewAI can connect to MCP servers as a client, so they call tools an MCP server exposes. They give you nothing for *authoring* a hardened, multi-tenant MCP server. If your job is to build the server, an agent framework is not in the running; the real **mcp server sdk comparison** is FastMCP vs Promptise.

That is the terrain. Now the four deltas.

## The four things you still assemble yourself

Here is the feature this article exists to showcase: Promptise treats **versioned tool coexistence, per-tool circuit breaking, tenant-qualified rate limits, and an MCP-native durable job queue as one composable middleware stack** — decorator-declared, installed on the same server, pre-compiled into one per-call chain. FastMCP can *reach* most of this by convention; the difference is that in Promptise the capability is structural.

### 1. Versioned tool coexistence

You ship `search` v1. Agents hard-code `search` in their prompts. Now you need to add a `filters` parameter — but changing the signature breaks every agent that pinned v1. In FastMCP you can absolutely emulate versioning by registering two separately named functions (`search_v1`, `search_v2`), and that works; what it has no first-class primitive for is serving `search` (an alias that always resolves to the latest) *alongside* `search@1.0` (the pinned older contract) from a single base name, with semantic-version comparison deciding what "latest" means.

Promptise makes that structural. `VersionedToolRegistry` is an overlay registry: register multiple `ToolDef`s under one base name with explicit version strings, and it resolves `search` to the newest while keeping every pinned `name@version` reachable, with semantic comparison deciding which one "latest" means.

```python
from promptise.mcp.server import ToolDef, VersionedToolRegistry

versions = VersionedToolRegistry()
versions.register("search", "1.0", ToolDef(
    name="search", description="Full-text search (v1: plain query).",
    handler=search_v1, input_schema={"type": "object"},
))
versions.register("search", "2.0", ToolDef(
    name="search", description="Full-text search (v2: adds sorting).",
    handler=search_v2, input_schema={"type": "object"},
))

versions.get("search")        # -> the v2 ToolDef (latest)
versions.get("search@1.0")    # -> the pinned v1 ToolDef
versions.list_versions("search")  # -> ["1.0", "2.0"]
```

Why this failure mode is worth a primitive — because the MCP schema *is* the contract, one renamed field silently breaks every connected agent. That is a whole story in itself: [Why a Small MCP Tool Change Broke Every Connected Agent](version-mcp-tools-without-breaking-clients.md).

### 2. Per-tool circuit breaking

Your server wraps a payments API. When that API has an outage, every tool call fails only after a 30-second timeout; agents keep retrying, blocked connections pile up, and the whole server slows to a crawl. Retry middleware makes this *worse* — it adds attempts to a dependency that is already down.

FastMCP ships `RetryMiddleware` and `ErrorHandlingMiddleware` but no circuit breaker, so the "fail fast while the dependency recovers" behavior is code you write. Promptise ships it first-class: `CircuitBreakerMiddleware` tracks consecutive failures *per tool*, trips to open, rejects immediately with a structured `CircuitOpenError`, then probes for recovery.

```python
server.add_middleware(CircuitBreakerMiddleware(
    failure_threshold=5,        # open after 5 consecutive failures
    recovery_timeout=30.0,      # probe for recovery after 30s
    excluded_tools={"health"},  # never break your health check
))
```

The full open/half-open/closed state machine, `CircuitOpenError` handling, and programmatic reset are in the [resilience patterns guide](../../mcp/server/resilience-patterns.md). This is also exactly the machinery that keeps one bad tool from taking down the rest — see [One Stalled MCP Tool Can Exhaust Your Connection Pool](mcp-tool-connection-pool-exhaustion.md).

### 3. Tenant-qualified rate limits

This is the "partial feature, exact delta" case, so precision matters. FastMCP *has* token-bucket rate limiting. What it does not do automatically is qualify the bucket key by tenant: its limiter is global by default, and per-client keying is a `get_client_id` function you supply. Two tenants whose agents happen to share a `client_id` string would share a bucket unless you engineer around it.

Promptise makes tenant qualification structural. Once you configure a `tenant_claim` (or map API keys to a `tenant_id`), bucket keys are tenant-qualified in **both** the middleware limiter and declared per-tool limits — one tenant's traffic can never exhaust another's quota, even for identical `client_id` values:

```python
server = MCPServer(name="api", require_tenant=True)  # server-wide invariant
server.add_middleware(AuthMiddleware(
    JWTAuth(secret="..."),
    tenant_claim="tenant_id",   # your IdP's org claim
))

@server.tool(rate_limit="100/min")   # a per-tenant bucket, automatically
async def generate_report(department: str) -> dict:
    ...
```

`require_tenant=True` forces every tool to authenticate and carry a `RequireTenant` guard, and the same `tenant_id` flows into audit entries. The declared `"100/min"` limit is enforced with no middleware wiring, and its bucket is per client *and* per tenant.

### 4. An MCP-native durable job queue

MCP tool calls are synchronous: the agent blocks until the response. That breaks down for report generation, data pipelines, or model training. FastMCP has no built-in durable job queue, so you reach for Celery or ARQ and hand-write submit/poll tools on top.

Promptise ships `MCPQueue`. Attach it to a server, decorate a handler with `@queue.job(...)`, and it **auto-registers five MCP tools** — `queue_submit`, `queue_status`, `queue_result`, `queue_cancel`, `queue_list` — with priority scheduling, retry-with-backoff, progress reporting, and cancellation built in. No extra wiring.

## One middleware stack, assembled once

Here is the whole point of an **integrated mcp middleware stack**: all four capabilities ship in one SDK and compose on the *same* server, and you can exercise them end-to-end with the in-process `TestClient` — no network, no API key. This block runs as-is after `pip install promptise`:

```python
import asyncio
import json

from promptise.mcp.server import (
    MCPServer,
    MCPQueue,
    RateLimitMiddleware,
    TokenBucketLimiter,
    CircuitBreakerMiddleware,
    VersionedToolRegistry,
    ToolDef,
    TestClient,
)

# --- One server, one composable production stack --------------------------
server = MCPServer(name="search-api", version="2.0.0")

# Token-bucket rate limiting. With a tenant claim configured, bucket keys are
# tenant-qualified automatically, so one tenant cannot drain another's quota.
server.add_middleware(
    RateLimitMiddleware(limiter=TokenBucketLimiter(rate_per_minute=120, burst=20))
)

# Per-tool circuit breaking: open after 5 consecutive failures, probe after 30s.
server.add_middleware(
    CircuitBreakerMiddleware(failure_threshold=5, recovery_timeout=30.0)
)


@server.tool()
async def search(query: str, sort_by: str = "relevance") -> list[dict]:
    """Full-text search over the corpus (served through the middleware chain)."""
    return [{"query": query, "sort_by": sort_by}]


# MCP-native durable job queue on the same server. Auto-registers
# queue_submit / queue_status / queue_result / queue_cancel / queue_list.
queue = MCPQueue(server, max_workers=2)


@queue.job(name="reindex", timeout=60)
async def reindex(corpus: str) -> dict:
    await asyncio.sleep(0.2)  # stand-in for real indexing work
    return {"corpus": corpus, "documents": 4200, "status": "ready"}


# Versioned tool coexistence: an overlay registry resolves one base name to
# many contracts. `search` -> latest; `search@1.0` -> the pinned old shape.
async def search_v1(query: str) -> list[dict]:
    return [{"query": query, "version": 1}]


async def search_v2(query: str, sort_by: str = "relevance") -> list[dict]:
    return [{"query": query, "sort_by": sort_by, "version": 2}]


versions = VersionedToolRegistry()
versions.register("search", "1.0", ToolDef(
    name="search", description="Full-text search (v1: plain query).",
    handler=search_v1, input_schema={"type": "object"},
))
versions.register("search", "2.0", ToolDef(
    name="search", description="Full-text search (v2: adds sorting).",
    handler=search_v2, input_schema={"type": "object"},
))


async def main() -> None:
    client = TestClient(server)

    # Live tool through the middleware chain (rate limit + circuit breaker).
    hit = json.loads((await client.call_tool("search", {"query": "mcp"}))[0].text)
    print("search    :", hit)

    # Versioned coexistence: latest alias plus every pinned version.
    print("versions  :", versions.list_versions("search"))
    print("latest    :", versions.get("search").description)
    print("pinned v1 :", versions.get("search@1.0").description)

    # Durable job: submit, let a worker run it, then fetch the result.
    await queue.start()
    try:
        submitted = await queue.submit("reindex", {"corpus": "docs"})
        for _ in range(50):
            status = await queue.status(submitted["job_id"])
            if status["status"] == "completed":
                break
            await asyncio.sleep(0.05)
        result = await queue.get_result(submitted["job_id"])
        print("job       :", result["result"])
    finally:
        await queue.stop()


if __name__ == "__main__":
    asyncio.run(main())
```

Running it prints the live search result through the middleware chain, the coexisting `search` / `search@1.0` versions the overlay registry resolves, and the completed background job — rate limiting, circuit breaking, versioning, and a durable queue, all from one SDK on one server. The [production features reference](../../mcp/server/production-features.md) shows how the chain is pre-compiled so this protection adds effectively zero per-call overhead, and the [advanced patterns guide](../../mcp/server/advanced-patterns.md) covers the versioning and composition primitives in depth.

## FastMCP vs Promptise, feature by feature

A fair **mcp server sdk comparison** — verified behavior only, no guesses:

| Capability | FastMCP | Promptise Foundry |
|---|---|---|
| Typed tool/resource/prompt decorators | Yes (`@mcp.tool`) | Yes (`@server.tool`) |
| JSON Schema from type hints | Yes | Yes |
| Prefix-namespaced mounting | Yes (`mount` / `import_server`) | Yes (`mount()`) |
| Auth (JWT / Bearer / API key) | Yes | Yes (`JWTAuth`, `AsymmetricJWTAuth`, `APIKeyAuth`) |
| Middleware pipeline | Yes | Yes (pre-compiled chain) |
| Token-bucket rate limiting | Yes (`RateLimitingMiddleware`) | Yes (`TokenBucketLimiter`) |
| Response caching | Yes (`ResponseCachingMiddleware`) | Yes (`CacheMiddleware`, `@cached`) |
| Retry / error handling | Yes (`RetryMiddleware`, `ErrorHandlingMiddleware`) | Yes (`ExceptionHandlerRegistry`, MRO-matched) |
| Versioned coexistence (`name` + `name@version`) | Emulate with two names | First-class (`VersionedToolRegistry`) |
| Per-tool circuit breaker | Build it yourself | First-class (`CircuitBreakerMiddleware`) |
| Tenant-qualified rate-limit buckets | Manual (`get_client_id`) | Automatic (`tenant_claim`) |
| MCP-native durable job queue | Build it yourself | First-class (`MCPQueue`) |

Read the top half of that table as agreement: for the common production surface, both frameworks give you a decorator and move on. The bottom half is where the **fastmcp vs promptise** decision actually lives.

## When FastMCP is the right call

Honesty converts better than hype, so here is when to pick FastMCP over Promptise:

- **Your servers are single-tenant.** If there is no tenant boundary to enforce, automatic tenant-qualified buckets buy you nothing, and FastMCP's global-or-per-client limiter is plenty.
- **Your tools are all fast and synchronous.** No 5-minute report jobs means no need for a durable queue; request/response is simpler and you should keep it.
- **Your tool contracts are stable.** If you are not evolving live schemas that pinned agents depend on, hand-named `_v2` tools are perfectly fine.
- **You want the largest MCP community and example gallery today.** FastMCP has broad adoption and a deep catalog of patterns to copy.

Promptise earns its place specifically when those conditions flip: multi-tenant SaaS with hard isolation, long-running work that must not block the agent, live schema evolution across many callers, and flaky downstreams that need a breaker — and when you would rather declare all of that on one server than assemble and maintain it yourself.

## Frequently asked questions

**Is FastMCP production-ready?** Yes. It has typed decorators, server composition, real auth, and a middleware pipeline that includes token-bucket rate limiting, caching, retries, and structured logging. Treat it as a real **production mcp server framework**, not a prototype tool. The delta is scope, not maturity.

**Can I migrate a FastMCP server to Promptise?** The mental model is the same — decorate typed functions, mount, add middleware — so the port is mechanical. You swap `@mcp.tool` for `@server.tool`, keep your handlers, and *delete* the custom code you wrote for versioning, circuit breaking, tenant rate limits, or a job queue, replacing each with a first-class primitive.

**Does Promptise's extra middleware slow down every call?** No. The middleware chain is pre-compiled at build time into one per-call path, so installed-but-unused middleware adds effectively zero overhead. The [production features reference](../../mcp/server/production-features.md) documents the ordering and compilation.

**Do I have to use all four capabilities?** No — they are independent. Install only `CircuitBreakerMiddleware` if that is all you need. The value of the **integrated mcp middleware stack** is that each is a one-line addition on the *same* server when you do need it, not a separate subsystem to wire in.

**What about agent frameworks like LangGraph or CrewAI — can't they serve MCP tools?** They consume MCP tools as clients; they do not ship an SDK for authoring production MCP servers. For the server side, the comparison that matters is FastMCP vs Promptise.

## Next steps

If the bottom half of that comparison table describes your roadmap, try the parts FastMCP leaves out. Install the framework and run the stack above end-to-end:

```bash
pip install promptise
```

Then go deeper on each pillar of the stack: the [advanced patterns guide](../../mcp/server/advanced-patterns.md) for versioned tools and server composition, the [resilience patterns guide](../../mcp/server/resilience-patterns.md) for circuit breakers and health probes, and the [production features reference](../../mcp/server/production-features.md) for how auth, tenant-qualified rate limits, audit, and the job queue fit into one hardened deployment. If you are still weighing the trade-offs, [Why a Small MCP Tool Change Broke Every Connected Agent](version-mcp-tools-without-breaking-clients.md) and [One Stalled MCP Tool Can Exhaust Your Connection Pool](mcp-tool-connection-pool-exhaustion.md) show two of these deltas as concrete failure modes — and why folding them into the stack, instead of assembling them yourself, is the whole point of a **fastmcp alternative for production**.
