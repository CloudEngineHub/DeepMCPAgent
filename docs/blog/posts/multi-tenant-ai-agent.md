---
title: "Multi-Tenant AI Agents: Architecture for SaaS"
description: "Instead of hand-rolling tenant checks in every handler, show how a tenant_id invariant threaded from the JWT claim through rate limits, audit, and cache…"
keywords: "multi-tenant AI agent, multi-tenant AI SaaS, tenant isolation for agents, tenant_id JWT claim, multi-tenant LLM architecture, SaaS AI agent design"
date: 2026-07-16
slug: multi-tenant-ai-agent
categories:
  - Production
---

# Multi-Tenant AI Agents: Architecture for SaaS

Shipping a **multi-tenant AI agent** means one deployment serves many customers, and the single worst thing that can happen is that Acme's data surfaces in Globex's session. If you are building AI features into a SaaS product, tenant separation is not a nice-to-have — it is the invariant your whole security story rests on. This post walks through why per-handler tenant checks fail, what a real isolation boundary looks like, and how to make cross-tenant leakage structurally impossible in Promptise Foundry with one server flag and a `tenant_id` that flows from your JWT all the way to storage.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## What a multi-tenant AI agent has to guarantee

A multi-tenant AI SaaS backend usually has a lot of shared machinery: one process, one connection pool, one vector store, one rate limiter, one audit log. Tenancy is the promise that all of that shared machinery still keeps customers apart. Concretely, a multi-tenant AI agent has to guarantee that a tenant can only ever:

- Read and write its own memories and conversation history
- Get cache hits from its own prior requests, never another tenant's
- Spend against its own rate-limit quota, not a shared pool
- Appear in its own audit trail, cleanly attributable

The tricky part is that many of these surfaces are keyed by `user_id` — and two different customers can easily have a user called `alice`. If your isolation key is just `user_id`, Acme's Alice and Globex's Alice collide. The tenant has to be part of the key everywhere, or it is part of the key nowhere.

## The trap: hand-rolled tenant checks in every handler

The intuitive approach is to pass a tenant id around and check it at each call site: filter the vector query by tenant, prefix the cache key with the org, add a `WHERE tenant_id = ?` to the conversation lookup. This works right up until it doesn't.

The problem is that it is a **convention**, and conventions fail silently. The first time someone adds a new tool, a new cache scope, or a new memory query and forgets the prefix, you have a cross-tenant leak with no error, no failing test, and no log line. You find out from a customer. Every new handler is a fresh opportunity to forget, and the checks are scattered across dozens of files where no single reviewer sees them all.

A durable **multi-tenant LLM architecture** does the opposite: it derives the tenant into the isolation key in exactly one place per surface, so there is no code path that stores or reads tenant data without it. Forgetting is not possible because the individual handler never touches the key at all.

## Tenant isolation for agents: one invariant, threaded end to end

Promptise treats the tenant as a first-class part of identity rather than a value you stuff into `metadata`. On the agent side, `CallerContext` carries a `tenant_id` alongside `user_id`, and a single derivation — `CallerContext.isolation_key` — becomes the key for every per-user surface. With a tenant present it is `"{tenant_id}::{user_id}"`; without one it is the plain `user_id`. Because a raw `user_id` can never contain `::` (construction rejects it), the tenanted keyspace is provably disjoint from the untenanted one — an attacker cannot forge `acme::alice` by naming their user `acme::alice`.

That one key feeds the semantic cache scope, the memory provider's owner id, and conversation session ownership. Same user, different tenant, guaranteed separation:

```python
import asyncio
from promptise import build_agent, CallerContext
from promptise.conversations import SQLiteConversationStore


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You are a per-tenant support assistant.",
        conversation_store=SQLiteConversationStore("chat.db"),
    )

    # One user id, two tenants -> two disjoint isolation keys
    acme = CallerContext(user_id="alice", tenant_id="acme")
    globex = CallerContext(user_id="alice", tenant_id="globex")

    await agent.chat("Remember: our SLA is 4 hours.", session_id="acme-sla", caller=acme)
    reply = await agent.chat("What SLA did I set?", session_id="globex-sla", caller=globex)
    print(reply)  # globex shares nothing with acme's session

    await agent.shutdown()


asyncio.run(main())
```

Globex's Alice gets a fresh context. She never sees Acme's SLA, because her conversation, memory, and cache scopes are keyed on `globex::alice`, not `alice`. If she instead tried to open Acme's session id directly, session ownership keys on the isolation key too, so she is denied rather than served. The full `CallerContext` — tenant included — is inherited by any peer the agent delegates to, so isolation survives cross-agent hops.

## The one-line server flag: `require_tenant=True`

On the MCP server side, the same tenant identity is extracted from the token instead of constructed in code. `AuthMiddleware` reads a configurable JWT claim and attaches the result to `ctx.client.tenant_id`. Point it at whatever claim your identity provider issues — `tenant_id`, `org`, `org_id`:

```python
from promptise.mcp.server import (
    MCPServer,
    AuthMiddleware,
    JWTAuth,
    HasTenant,
)

# One flag makes tenancy a server-wide invariant (implies require_auth)
server = MCPServer(name="records", require_tenant=True)

server.add_middleware(
    AuthMiddleware(
        JWTAuth(secret="..."),
        tenant_claim="org",  # pull the tenant from your IdP's claim
    )
)


@server.tool(auth=True)  # already tenant-guarded by require_tenant
async def list_records() -> list:
    ...


@server.tool(auth=True, guards=[HasTenant("acme")])
async def acme_only_report() -> str:
    ...
```

That `require_tenant=True` on the constructor is the whole point. It makes tenancy a **server-wide invariant**: every tool — whether it comes from a decorator, a mounted sub-server, an `MCPRouter`, or an imported OpenAPI spec — is forced to authenticate and carries a `RequireTenant` guard. A token that lacks the tenant claim is denied on every call, with no per-handler code. Only string claim values are accepted; anything else leaves `tenant_id` unset and the guards fail closed. If you need finer control on a specific tool, the `RequireTenant()` and `HasTenant("acme", "globex")` guards compose exactly like the role and scope guards you already use. The full behavior is documented in the [Multi-Tenancy guide](../../mcp/server/multi-tenancy.md).

## What the tenant automatically isolates: rate limits, audit, and cache

Once the tenant is on the context, the rest of the production stack keys on it without any extra wiring. This is where the "invariant, not convention" design pays off — the shared infrastructure is shared, but tenant-qualified:

- **Rate limiting** — bucket keys in both `RateLimitMiddleware` and declared per-tool limits are tenant-qualified, so one tenant's traffic can never exhaust another's quota, even for identical `client_id` strings. If you are new to declaring limits, the [LLM tool rate limiting guide](llm-tool-rate-limiting.md) covers per-client and per-tool buckets.
- **Audit log** — `AuditMiddleware` records `tenant_id` in each entry's identity descriptors, so you get tenant-scoped forensics without joining against external data.
- **Semantic cache** — scope keys embed the tenant, making cross-tenant cache hits structurally impossible. For GDPR, `purge_user("alice", tenant_id="acme")` clears exactly that tenant's scope and nothing else.
- **Memory and conversations** — providers receive the composite isolation key as the owner id, so no provider changes are needed; the guarantee lives at the scoping layer.

You can see how these pieces fit alongside auth, guards, and health checks in the [production features overview](../../mcp/server/production-features.md), and the end-to-end pattern — from token to storage across both the agent and the server — is walked through in the [secure multi-tenant platform guide](../../guides/secure-multi-tenant-platform.md).

## When a multi-tenant LLM architecture is overkill

Tenancy is a real cost to reason about, and not every deployment needs it. Be honest with yourself about the shape of your product:

- **Single-tenant / self-hosted.** If each customer runs their own instance with their own database, the process boundary already is the tenant boundary. Adding `require_tenant=True` buys you little except an extra token claim to manage.
- **Internal tools with one trust domain.** A team-internal agent where every user is inside the same organization does not need cross-tenant isolation; per-user identity via `CallerContext(user_id=...)` is enough.
- **Prototypes.** While you are still discovering the product, skip it and add it before you onboard a second paying customer, not before your first demo.

Reach for first-class multi-tenancy when a single deployment serves multiple customers who must never see each other's data — that is exactly the case where a hand-rolled approach eventually leaks and a structural invariant does not.

## Frequently asked questions

### How do you isolate tenants in an AI agent?

Make the tenant part of every isolation key rather than a filter you apply per handler. In Promptise, `CallerContext.tenant_id` combines with `user_id` into a single `isolation_key` that scopes cache, memory, and conversations, while server-side `AuthMiddleware` reads the tenant from a JWT claim and `require_tenant=True` guards every tool. Because the key is derived in one place per surface, no individual handler can forget it.

### What is the tenant_id JWT claim used for?

It is how the server learns which customer a request belongs to. `AuthMiddleware(JWTAuth(...), tenant_claim="org")` extracts that claim from the verified token and attaches it to `ctx.client.tenant_id`, which then drives rate-limit buckets, audit entries, and the `RequireTenant` / `HasTenant` guards. Only string values are accepted; a missing or non-string claim leaves the tenant unset and fails closed.

### Can two tenants have a user with the same id?

Yes, and handling that safely is the point. Acme's `alice` and Globex's `alice` produce different isolation keys (`acme::alice` versus `globex::alice`), so their memory, cache, and conversations never collide. A caller from one tenant that tries to open another tenant's session is denied, not served.

## Next steps

Read the [Multi-Tenancy guide](../../mcp/server/multi-tenancy.md) and set `require_tenant=True` on your server so tenant isolation is enforced everywhere by default instead of remembered in every handler. From there, work through the [Quick Start](../../getting-started/quickstart.md) to get an agent running, and use the [Production AI Agent Checklist](production-ai-agent-checklist.md) to confirm the rest of your deployment is ready to ship.
