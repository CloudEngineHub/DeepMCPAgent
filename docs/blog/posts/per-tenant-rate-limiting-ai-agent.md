---
title: "Noisy Neighbors: Per-Tenant Rate Limits for AI Agents"
description: "In a shared agent deployment one tenant hammering a tool can starve everyone else. This post shows the noisy-neighbor failure mode and how Promptise…"
keywords: "per-tenant rate limiting ai agent, noisy neighbor multi-tenant rate limit, tenant-scoped rate limit, per-tenant quota llm, stop one tenant exhausting quota"
date: 2026-07-16
slug: per-tenant-rate-limiting-ai-agent
categories:
  - Multi-Tenancy
---

# Noisy Neighbors: Per-Tenant Rate Limits for AI Agents

Per-tenant rate limiting AI agent platforms need is the difference between a shared deployment that degrades gracefully and one where a single customer's runaway loop pages your on-call at 2 a.m. because every other tenant's requests are suddenly getting refused. In a multi-customer platform, every tenant's agents hit the same MCP tool servers. Most of the time that is fine. Then one tenant ships a buggy prompt, an agent gets stuck reformulating the same `generate_report` call forty times a minute, and your token bucket drains — for everyone. This is the noisy-neighbor problem, and the uncomfortable truth is that a per-client or per-tool limiter alone does not solve it.

<!-- more -->

This post walks through exactly where the leak is, and how Promptise Foundry closes it by making the tenant part of the rate-limit key itself — a structural invariant, not a convention you have to remember to apply at every call site.

## The noisy-neighbor failure mode

Rate limiting exists to answer one question: *when should this call be refused?* A classic answer is "when the caller has made too many requests recently." For a browser or a mobile app that maps cleanly to a user. For agents it does not, because an agent is not a well-behaved one-request-per-action client. Give it a task and it fans out five, ten, fifty tool calls per turn, retrying and reformulating as it reasons.

Now put several customers on the same server. Tenant `acme` and tenant `globex` both run agents that call your `generate_report` tool. You did the responsible thing and added a limit. The question that decides whether you have actually isolated your tenants is subtle: **what is the limit keyed on?** If the answer is anything other than "the tenant," you have a shared quota with extra steps, and the first tenant to misbehave gets to decide how much service the others receive.

## Why per-client and per-tool limits still leak

The obvious fixes are real improvements, and Promptise ships both. You can limit *per tool* so a flood of cheap `lookup_customer` calls can't lock out the expensive `generate_report`. You can limit *per client* so one authenticated agent can't drain another's budget. The [per-client and per-tool rate limiting guide](llm-tool-rate-limiting.md) covers those two dimensions end to end.

But neither dimension is the tenant. Consider the case that breaks a naive setup: two tenants whose agents authenticate with the **same** `client_id` string. That is not exotic. If you provision one service identity per agent *role* rather than per *customer* — a single `report-agent` credential deployed into every tenant's workspace — then `acme`'s `report-agent` and `globex`'s `report-agent` present identical client IDs. A per-client bucket keyed on `client_id` alone sees one caller. `acme` burns the budget; `globex` gets the 429s. The isolation you thought you had evaporates on the one dimension that matters most for a SaaS product.

This is the same class of bug as sharing a cache scope or a memory namespace across tenants because they happen to collide on a `user_id`. We wrote about that failure directly in [Same user_id, Two Tenants: Why That Isn't Isolation](same-user-id-across-two-tenants.md) — identical inner identifiers must not imply a shared resource. Rate-limit buckets are just another surface where that rule has to hold.

## Tenant-qualified buckets, demonstrated

Here is the mechanism, and it is worth running because the result is the whole argument. Two API keys deliberately share one `client_id` (`shared-agent`) and differ only in `tenant_id`. The tool declares a small limit so the buckets are easy to exhaust. Watch what happens to `globex` after `acme` is throttled.

```python
import asyncio

from promptise.mcp.server import MCPServer, TestClient, AuthMiddleware, APIKeyAuth

server = MCPServer("reports")

# Two tenants, ONE shared client_id. The only thing that differs is the tenant.
server.add_middleware(
    AuthMiddleware(
        APIKeyAuth(
            keys={
                "sk-acme":   {"client_id": "shared-agent", "roles": ["analyst"], "tenant_id": "acme"},
                "sk-globex": {"client_id": "shared-agent", "roles": ["analyst"], "tenant_id": "globex"},
            }
        )
    )
)


@server.tool(rate_limit="3/min", auth=True)
async def generate_report(department: str) -> dict:
    """A heavy tool: 3 calls per minute is plenty."""
    return {"department": department, "rows": 10_000}


async def burst(client: TestClient, api_key: str, label: str) -> None:
    for i in range(1, 5):
        result = await client.call_tool(
            "generate_report", {"department": "sales"}, headers={"x-api-key": api_key}
        )
        text = result[0].text
        status = "429 rate limited" if "RATE_LIMIT_EXCEEDED" in text else "ok"
        print(f"{label} call {i}: {status}")


async def main() -> None:
    client = TestClient(server)
    await burst(client, "sk-acme", "acme  ")   # drains acme's bucket
    await burst(client, "sk-globex", "globex")  # SAME client_id — untouched?


asyncio.run(main())
```

Running it prints:

```
acme   call 1: ok
acme   call 2: ok
acme   call 3: ok
acme   call 4: 429 rate limited
globex call 1: ok
globex call 2: ok
globex call 3: ok
globex call 4: 429 rate limited
```

`acme` exhausts its three tokens and is refused on the fourth call. Then `globex` — presenting the *identical* `client_id` `shared-agent` — starts with a completely full bucket. One tenant's runaway loop cannot touch another tenant's quota, even when the client identity is byte-for-byte the same. That isolation is not something you wired up; it is what the declared `rate_limit="3/min"` does automatically once a tenant is present. And because `TestClient` runs the full pipeline (auth, middleware, the declared limit, the handler) in-process with no network, this is a property you can assert in a unit test rather than discover in production.

## How the tenant-qualified key is derived

The behavior above comes from one design decision: the tenant is part of the bucket key, and it is joined *injectively*. Both the declared per-tool limit and the server-wide `RateLimitMiddleware` build their key from `ctx.client.tenant_id` when it is present, so there is no code path that rate-limits tenant data without the tenant in the key.

The join is the interesting part. A tenant or client id can legitimately contain a colon — think a URN tenant `org:acme` or a provider-prefixed client id `okta:bob`, which come straight from JWT claims and are not colon-validated. A naive `f"{tenant}:{client}"` join would let `("org", "acme:report")` and `("org:acme", "report")` collide onto one bucket. Promptise length-prefixes each part (`"<len>:<part>"`, joined by `|`), which is uniquely decodable, so two distinct `(tenant, client, tool)` triples can never map to the same bucket. The untenanted keyspace (one part) is provably disjoint from the tenanted one (two parts), so a client with no tenant can never forge a tenanted key. This is the same injective-key discipline described across the stack in the [Multi-Tenancy reference](../../mcp/server/multi-tenancy.md).

Where does the tenant come from in a real deployment? Not from a hard-coded API-key map — that was just to keep the demo self-contained. In production it rides on the JWT. `AuthMiddleware` extracts it from a configurable claim and both limiters pick it up:

```python
from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JWTAuth, RateLimitMiddleware, TokenBucketLimiter,
)

server = MCPServer("reports")

# The tenant is read from the JWT's `org_id` claim (whatever your IdP uses).
server.add_middleware(
    AuthMiddleware(JWTAuth(secret="...", audience="api://reports"), tenant_claim="org_id")
)

# A server-wide safety net: 120 sustained calls/min, bursts of 20, per tool.
# Its buckets are tenant-qualified too — same invariant as the declared limits.
server.add_middleware(
    RateLimitMiddleware(limiter=TokenBucketLimiter(rate_per_minute=120, burst=20), per_tool=True)
)
```

The two layers coexist: a call has to satisfy both the blanket abuse-prevention net *and* the specific tool's declared contract, and both are isolated per tenant. If you want tenancy to be a hard server-wide invariant — every tool must authenticate and carry a tenant or the call is denied — build the server with `require_tenant=True`. The full walkthrough, wired together with tenant-scoped tools, approval gates, and tenant-stamped audit, lives in the [Build a Secure Multi-Tenant Agent Platform](../../guides/secure-multi-tenant-platform.md) guide. The same tenant identity should key your data stores as well; [Multi-Tenant RAG: Isolate Customer Data in a Shared Store](multi-tenant-rag.md) shows the retrieval side of the same principle.

## What other frameworks do today

It is worth being precise about the state of the art, because the gap here is specific, not a blanket "nobody else can do this."

- **CrewAI** ships `max_rpm`, a requests-per-minute cap you can set at the agent and crew level. It is a real limiter — but it is a single global *outbound self-throttle*: the crew paces its own LLM/tool calls against one shared number, with no per-tenant, per-client, or per-tool dimension. In a shared server it slows the whole process uniformly; it does not stop one tenant from consuming another's share.
- **LangChain** provides an `InMemoryRateLimiter` for its chat models. It, too, is a single global bucket that throttles a model's outbound request rate. There is no notion of a calling tenant, so it cannot isolate one from another.
- **LangGraph** and **AutoGen** ship no built-in request rate limiter at all. Rate limiting is expected to live elsewhere — typically an API gateway keyed by API key or IP.

That gateway pattern is exactly where the noisy-neighbor leak reappears: keyed on an API key that a role-based service identity shares across tenants, one client_id still draws from one quota. Promptise's contribution is not "we invented rate limiting." It is that the **tenant is a first-class dimension of the bucket key**, enforced in the same place the tool is defined, so per-tenant fairness is an invariant of the server rather than a rule you hope every gateway config remembers to express. A gateway is still useful for coarse, cluster-wide, per-IP throttling; use it alongside the in-server limits, not as a substitute for the tenant dimension it can't cleanly see.

## Frequently asked questions

### Does per-tenant limiting still work if two tenants share a client_id?

Yes — that is the case it is built for. Buckets are keyed on the tenant first, then the client id, then (optionally) the tool. Two tenants presenting the identical `client_id` string get two independent buckets, as the runnable example above demonstrates: `acme` hitting its limit leaves `globex`'s bucket full. The keys are joined injectively, so a colon inside a tenant or client id can't accidentally collide two distinct tenants onto one bucket.

### Do declared per-tool limits and RateLimitMiddleware conflict?

No, they compose. A `@server.tool(rate_limit="3/min")` declaration enforces that one tool's contract; a server-wide `RateLimitMiddleware` enforces a blanket abuse-prevention rule. A call must satisfy both, and both derive tenant-qualified keys, so neither weakens the other. Reach for the declared limit for tool-specific rules and the middleware for a process-wide safety net.

### Does this hold across multiple server replicas?

The built-in `TokenBucketLimiter` is in-process, so limits apply per replica: across ten replicas a `3/min` tool is effectively up to `30/min` cluster-wide, but still isolated per tenant on each replica. For a globally exact budget, put a distributed (Redis-backed or gateway) limiter in front and treat the in-server, tenant-qualified limit as a per-replica backstop. For most noisy-neighbor cases, per-replica isolation is enough.

### Where does the tenant_id actually come from?

From the caller's credentials. With `JWTAuth`, `AuthMiddleware` reads it from a configurable claim (`tenant_claim="org_id"`, `"tenant_id"`, whatever your IdP emits). With `APIKeyAuth`, it comes from the key's config dict. Only string claim values are accepted; anything else leaves the tenant unset and tenant guards fail closed. See the [Multi-Tenancy reference](../../mcp/server/multi-tenancy.md) for every surface the tenant isolates.

## Next steps

Add a `rate_limit` to your busiest shared tool, make sure your tokens carry a tenant claim, and point `AuthMiddleware` at it — the buckets become tenant-qualified with no further code. Then read the [Build a Secure Multi-Tenant Agent Platform](../../guides/secure-multi-tenant-platform.md) guide to wire the same tenant identity through auth, tool guards, approval gates, and audit, and skim [Multi-Tenant AI Agents: Architecture for SaaS](multi-tenant-ai-agent.md) for how tenant identity flows through the rest of the stack. One tenant's bad day should never become everyone's.
