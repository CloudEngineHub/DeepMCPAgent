---
title: "Per-Tenant Data Isolation for AI Agents, Enforced"
description: "Isolation is only real if it holds across every layer an agent touches; this shows Promptise enforcing the tenant_id invariant through rate-limit buckets…"
keywords: "per-tenant data isolation, prevent cross-tenant leakage, tenant-scoped cache, per-tenant rate limits, tenant isolation invariant, secure multi-tenant agents"
date: 2026-07-16
slug: per-tenant-data-isolation
categories:
  - Production
---

# Per-Tenant Data Isolation for AI Agents, Enforced

Per-tenant data isolation is the promise that Acme's data never surfaces in Globex's session — and it only counts if it holds at *every* layer your agent touches, not just the one you remembered to check. Most frameworks leave you to hand-roll a tenant check in each handler, which fails silently the first time one call site forgets. This post shows how Promptise Foundry makes tenant separation a structural invariant: a `tenant_id` that flows from the JWT claim into rate-limit buckets, audit entries, tool guards, memory, and — critically — the semantic cache scope. By the end you'll be able to turn on server-wide isolation with one flag and reason about exactly where the boundary lives.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## Why one check per handler is not isolation

A prototype multi-tenant agent usually shares a lot of machinery: one process, one connection pool, one vector store, one rate limiter, one audit log. Isolation is the guarantee that all of that shared machinery still keeps customers apart. The failure mode is subtle because most isolation surfaces are keyed by `user_id`, and two customers can trivially both have a user named `alice`. If your key is just `user_id`, Acme's Alice and Globex's Alice collide — in the cache, in memory, in conversation history.

Hand-rolled checks concentrate the risk in the worst place: human discipline. Every new tool, every new cache lookup, every new audit call is one more site that has to remember the tenant. Miss one and you get the single worst incident class for a multi-customer platform — cross-tenant leakage — with no error, just wrong data served confidently. The fix is to bake the tenant into the key derivation itself, in one place per surface, so there is no code path that stores or reads tenant data without it. That is the design behind Promptise's [multi-tenancy model](../../mcp/server/multi-tenancy.md).

## The tenant isolation invariant, in one line

Promptise threads a single derivation through every per-user surface. On the agent side, `CallerContext` carries a `tenant_id`, and its `isolation_key` property is the join `"{tenant_id}::{user_id}"` (or the plain `user_id` when no tenant is present). That composite key — not the raw `user_id` — is what the cache, memory, and conversation store actually use.

```python
from promptise import CallerContext

acme_alice   = CallerContext(user_id="alice", tenant_id="acme")
globex_alice = CallerContext(user_id="alice", tenant_id="globex")

assert acme_alice.isolation_key   == "acme::alice"
assert globex_alice.isolation_key == "globex::alice"
# Same user_id, different tenants — provably disjoint keyspaces.
```

The `::` separator is reserved: `CallerContext` refuses to construct if a `tenant_id` contains a colon or a `user_id` contains `::`. That makes the join injective — an untenanted `user_id="acme::alice"` cannot forge tenant `acme`'s user `alice`; it simply fails to build. Single colons in SSO ids like `google:12345` or `auth0|abc` stay fine, because real tenant ids are colon-free identifiers. One derivation, enforced at construction, is what lets the rest of the stack stay simple.

## Enforce it server-side to prevent cross-tenant leakage

Your tools live behind an MCP server, so that boundary is where most enforcement belongs — it's the one place every tool call must pass through. `AuthMiddleware` extracts the tenant from a configurable JWT claim and attaches it to `ctx.client.tenant_id`. Build the server with `require_tenant=True` and every tool — from decorators, routers, mounts, or OpenAPI import — is forced to authenticate and carries a `RequireTenant` guard automatically. A token missing the tenant claim is denied on every call, so you can't accidentally ship an unguarded tool.

Here is a minimal billing server that makes tenancy a server-wide invariant, adds a tenant-qualified rate limit, and records the tenant in a tamper-evident audit log:

```python
from promptise.mcp.server import (
    MCPServer, JWTAuth, AuthMiddleware,
    RateLimitMiddleware, AuditMiddleware, RequireTenant,
)

# require_tenant implies require_auth: no token, no tenant -> denied everywhere
server = MCPServer(name="billing", require_tenant=True)

server.add_middleware(
    AuthMiddleware(
        JWTAuth(secret="change-me"),
        tenant_claim="tenant_id",   # or "org", "org_id" — your IdP's claim
    )
)
server.add_middleware(RateLimitMiddleware())   # bucket keys are tenant-qualified
server.add_middleware(AuditMiddleware())       # each entry records tenant_id

@server.tool(auth=True, guards=[RequireTenant()], rate_limit="100/min")
async def list_invoices(status: str = "open") -> list[dict]:
    """List invoices for the calling tenant only."""
    # Your data layer scopes to the tenant it was handed — never a global read.
    return [{"id": "inv-1", "status": status}]
```

With a tenant present, three server surfaces become tenant-aware without any per-handler code:

- **Per-tenant rate limits** — bucket keys in both `RateLimitMiddleware` and declared per-tool limits are tenant-qualified, so one tenant's traffic can never exhaust another's quota, even with an identical `client_id`.
- **Audit entries** — `AuditMiddleware` writes `tenant_id` into each entry's identity descriptors, giving you tenant-scoped forensics without joining external data. The chain is HMAC-linked, so edits are detectable.
- **Tool access** — the `RequireTenant` guard (or the server-wide invariant) fails closed when the tenant claim is missing or non-string.

You can run this whole pipeline in-process with `TestClient(server)` before it ever hits a network, which is the fastest way to prove your isolation assumptions in a test.

## The tenant-scoped cache: the layer people forget

A semantic cache is the surface most likely to leak, because its whole job is to return a *different* caller's stored answer for a similar-enough query. If the cache scope isn't tenant-aware, a fuzzy match on "what's our balance?" could serve Globex the answer computed for Acme. That is why Promptise embeds the tenant into the cache's scope keys: cross-tenant cache hits are structurally impossible, not merely unlikely.

On the agent side you opt into the cache with one parameter, and the `CallerContext` you pass to each call carries the isolation. Nothing else changes:

```python
import asyncio
from promptise import build_agent, CallerContext
from promptise.cache import SemanticCache

async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You are a billing assistant. Answer only about the caller's own account.",
        cache=SemanticCache(),   # scope keys embed the tenant automatically
    )

    acme   = CallerContext(user_id="alice", tenant_id="acme")
    globex = CallerContext(user_id="alice", tenant_id="globex")
    q = {"messages": [{"role": "user", "content": "What's our current balance?"}]}

    # Same user_id, same question, different tenants — never a shared cache hit.
    await agent.ainvoke(q, caller=acme)
    await agent.ainvoke(q, caller=globex)

    await agent.shutdown()

asyncio.run(main())
```

The cache still delivers its published **30–50% cost reduction** on repeated, semantically similar queries — it just delivers it *within* each tenant's scope. GDPR cleanup respects the same boundary: `purge_user("alice", tenant_id="acme")` removes exactly that tenant's scope and leaves Globex's Alice untouched. Memory providers receive the isolation key as their `user_id`, and conversation session ownership keys on it too, so a same-`user_id` caller from another tenant gets a `SessionAccessDenied` rather than someone else's history. The mechanics of the cache scope are covered in the [caching & performance guide](../../mcp/server/caching-performance.md).

## Where the boundary lives: a quick reference

| Surface | What carries the tenant |
|---------|-------------------------|
| Rate limiting | Tenant-qualified bucket keys (middleware + declared limits) |
| Audit log | `tenant_id` in every entry, HMAC-chained |
| Tool access | `RequireTenant` / `HasTenant` guards, or `require_tenant=True` |
| Semantic cache | Scope keys embed the tenant; `purge_user(..., tenant_id=...)` |
| Memory | Isolation key passed as the provider's `user_id` |
| Conversations | Ownership keys on the isolation key |

For the full end-to-end build — role-based access and server-side approval gates layered on top of isolation — work through the [secure multi-tenant platform guide](../../guides/secure-multi-tenant-platform.md). For a broader view of where isolation sits among the other production concerns, the [Multi-Tenant AI Agents architecture post](multi-tenant-ai-agent.md) covers the same invariant from the SaaS-design angle.

## When a different approach is the better fit

Promptise's model shines when one deployment serves many customers from shared infrastructure and you need the tenant baked into the keyspace. It is not always the right tool:

- **One tenant per deployment.** If each customer gets a fully isolated stack — separate process, database, and vector store per tenant — you already have physical isolation, and a `tenant_id` invariant inside a single-tenant process is redundant ceremony.
- **Hard data-residency or compliance separation.** When regulation requires a tenant's data to live in a distinct database, region, or encryption boundary, that is an infrastructure decision. Promptise's logical isolation complements it but does not replace a separate datastore.
- **You're not on MCP.** The server-side enforcement assumes your tools sit behind a Promptise MCP server. If your tools are plain in-process functions with no MCP boundary, you get the agent-side isolation but not the middleware guarantees.

The honest framing: logical per-tenant isolation removes an entire class of application bugs, but it is one layer. Pair it with the right infrastructure boundary for your regulatory posture rather than treating either as complete on its own.

## Frequently asked questions

### How does Promptise prevent cross-tenant cache leakage?

The semantic cache derives its scope keys from the caller's `isolation_key` (`tenant::user`), so a query from tenant `acme` can only ever match entries stored under `acme`. Two callers with the same `user_id` in different tenants occupy disjoint keyspaces, which makes a cross-tenant hit structurally impossible rather than merely improbable.

### What happens if a request arrives without a tenant on a `require_tenant` server?

It is denied. Building the server with `require_tenant=True` forces every tool to authenticate and attaches a `RequireTenant` guard, and the guard fails closed when the tenant claim is missing or non-string. There is no tool path that runs without a validated tenant.

### Do I have to change my memory or conversation code to get isolation?

No. You pass a `CallerContext` with a `tenant_id`, and the agent hands the composite isolation key to providers as their `user_id`. Memory, conversations, and the cache all key on it automatically — the isolation happens at the scoping layer, so your provider code stays unchanged.

## Next steps

Run `pip install promptise`, set `require_tenant=True` on your MCP server, and verify isolation end to end with the [secure multi-tenant platform guide](../../guides/secure-multi-tenant-platform.md). New to the framework? Start with the [Quick Start](../../getting-started/quickstart.md), then harden the rest of your deployment with the [production AI agent checklist](production-ai-agent-checklist.md).
