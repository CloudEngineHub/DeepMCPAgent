---
title: "Stop One Tenant Draining a Shared MCP Server's Quota"
description: "Per-client rate limiting isn't tenant isolation. On a shared MCP server, two tenants that happen to share a user_id land in the same bucket, and a single…"
keywords: "per-tenant mcp rate limiting, tenant-isolated rate limit, multi-tenant mcp throttling, tenant-qualified token bucket, per-tenant quota mcp server"
date: 2026-07-16
slug: per-tenant-mcp-rate-limiting
categories:
  - MCP
---

# Stop One Tenant Draining a Shared MCP Server's Quota

Per-tenant MCP rate limiting is the difference between a quota that protects each customer and a quota that merely protects your process — and on a shared MCP server, per-client throttling quietly gives you the second while you think you bought the first. The trap is subtle: a token-bucket limiter keyed by "client" looks tenant-aware right up until two of your tenants authenticate a user whose id happens to be the same string. The moment `acme`'s user `u-42` and `globex`'s user `u-42` collide onto one bucket, one customer's burst starts spending another customer's quota, and neither of them did anything wrong. Worse, a single tenant that spins up many client ids can collectively soak up the shared throughput everyone else depends on, because nothing in a per-client scheme says "this tenant, as a whole, gets this much."

This post is about closing that gap structurally. We will look at why per-client is not per-tenant, what other frameworks give you today, and how Promptise Foundry's `RateLimitMiddleware` tenant-qualifies every bucket with an injective key — with per-tool granularity on top — so one tenant's traffic can never touch another's.

## Per-client throttling isn't tenant isolation

Start from what a token bucket actually keys on. A limiter maintains one bucket per key; each request consumes a token, tokens refill at a fixed rate, and when a bucket is empty the request is rejected. Everything hinges on how you derive that key. If the key is the client id, then "one bucket per client" is exactly as isolated as your client ids are unique — and in a multi-tenant system, they are not.

Two failure modes fall straight out of that:

- **The shared-id collision.** Tenants bring their own identity providers, their own user numbering, their own SSO. `acme` and `globex` can each legitimately have a `user_id` of `u-42`, `admin`, or `1`. Key the bucket on that string alone and the two tenants share a bucket. `acme` draining its burst throttles `globex`'s identical id — a cross-tenant denial of service that no one can see in the code, because the code looks correct.
- **The many-clients starvation.** Even with globally unique client ids, per-client limiting has no notion of a tenant *ceiling*. A tenant that opens fifty client connections gets fifty full buckets and can push fifty times a single client's throughput through a server whose total capacity is shared. The limiter is doing its job per client; it just has no dimension that says "these fifty clients are all one customer."

Both are the same root cause: the tenant is not part of the key. Fixing it by hand — prefixing the client id with an org string at every call site — is the kind of convention that works until the first place someone forgets, and then it fails silently, which is the worst way for an isolation control to fail. The [Multi-Tenancy guide](../../mcp/server/multi-tenancy.md) makes the general version of this argument: isolation has to be a structural invariant baked into key derivation in one place, not a naming discipline sprinkled across handlers.

## What other frameworks do today

It is worth being precise here, because the gap is narrow and the competitors are not careless.

**FastMCP** ships real, built-in rate limiting — a `RateLimitingMiddleware` backed by a token bucket, plus a `SlidingWindowRateLimitingMiddleware`. By default the bucket is global: every client shares one limiter. To partition it, you pass a `get_client_id` callable that returns a key per request, and now you have per-client buckets. That is a genuine, useful feature. What it does not carry is a first-class *tenant* or *per-tool* dimension. The key is a single flat string you produce, so to get tenant isolation you would fold the tenant into what `get_client_id` returns — and to get per-tool separation you would fold the tool name in too. You can approximate both. The delta is that you now own the composition and its collision-safety: concatenating `f"{tenant}:{client}"` re-introduces exactly the ambiguity you were trying to remove (a tenant `"a"` with client `"b:c"` collides with tenant `"a:b"` client `"c"`), and there is no built-in notion of "this key is a `(tenant, client, tool)` triple." It is reachable by convention, not structural.

**In-process agent frameworks** — LangChain, CrewAI, AutoGen — sit on the other side of the wire entirely. Their tools are ordinary Python callables invoked inside the agent process; there is no MCP server layer to rate-limit, so per-tenant throttling of a shared tool service is not a question they answer at the tool boundary at all. You would enforce it wherever the tool's backend lives. The moment those agents consume tools over MCP, the rate-limiting question moves to the MCP server — which is where this whole discussion belongs. For a fuller side-by-side of where each stack draws the production line, see [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md).

Promptise's contribution is to make the tenant dimension part of the bucket key derivation itself, in one place, with a join that cannot collide.

## Tenant-qualify every bucket with an injective key

`RateLimitMiddleware` derives its bucket key from the request context. When the authenticated client carries a `tenant_id` — populated by `AuthMiddleware` from a JWT claim or an API-key config — the tenant becomes part of the key automatically. No per-tool wiring, no custom extractor: two requests with the same `client_id` but different tenants land in different buckets, period.

The following example is fully self-contained and runs as-is against the public `promptise.mcp.server` API. It drives the middleware directly with hand-built request contexts so the isolation is visible in a handful of calls:

```python
import asyncio

from promptise.mcp.server import (
    ClientContext,
    RateLimitError,
    RateLimitMiddleware,
    RequestContext,
    TokenBucketLimiter,
)

# burst=2 makes the isolation visible in a handful of calls.
limiter = TokenBucketLimiter(rate_per_minute=60, burst=2)
rate_limit = RateLimitMiddleware(limiter, per_tool=True)


async def handler(ctx: RequestContext) -> str:
    return "ok"


def request(tool: str, user_id: str, tenant: str | None) -> RequestContext:
    # Two tenants can legitimately hand you the SAME user_id — the bucket
    # must still belong to exactly one of them.
    return RequestContext(
        server_name="reports-api",
        tool_name=tool,
        client_id=user_id,
        client=ClientContext(client_id=user_id, tenant_id=tenant),
    )


async def call(tool: str, user_id: str, tenant: str | None) -> bool:
    try:
        await rate_limit(request(tool, user_id, tenant), handler)
        return True
    except RateLimitError:
        return False


async def main() -> None:
    # acme and globex both authenticate a user whose id happens to be "u-42".
    acme_export = [await call("export", "u-42", "acme") for _ in range(3)]
    # globex, identical user_id, identical tool — its own untouched bucket.
    globex_export = await call("export", "u-42", "globex")
    # acme again, same user, but a DIFFERENT tool — its own bucket too.
    acme_query = await call("query", "u-42", "acme")

    print("acme export calls  :", acme_export)    # [True, True, False]
    print("globex export call :", globex_export)  # True  (tenant-isolated)
    print("acme query call    :", acme_query)     # True  (per-tool)


asyncio.run(main())
```

Run it and you get exactly the isolation you want:

```text
acme export calls  : [True, True, False]
globex export call : True
acme query call    : True
```

Read the three lines as three guarantees. `acme` drains its two-token burst on `export` and the third call is throttled — the limiter works. `globex`, presenting the *same* `user_id` `u-42` against the *same* tool, still has its full burst, because its bucket was never `acme`'s. And `acme`'s `query` call sails through even though `acme` just exhausted `export`, because per-tool granularity gives each tool its own bucket. One tenant cannot spend another's quota, and one hot tool cannot starve the rest.

## Why the injective key is the whole game

The reason this holds under adversarial input is the join, not just the presence of a tenant field. Promptise composes the key by length-prefixing every part before joining — each segment becomes `"<len>:<part>"` — so a `:` or `|` *inside* a tenant id or client id can never merge with the separator and collide two distinct triples onto one bucket. Naive concatenation (`f"{tenant}:{client}"`) is where hand-rolled tenant keys go wrong: the boundary between fields is ambiguous, and ambiguity is a collision waiting for the right pair of ids. The length prefix makes the encoding uniquely decodable, which makes the mapping from `(tenant, client, tool)` to bucket injective — the mathematical property that "different inputs never share an output," which is precisely what tenant isolation *means* at the bucket layer.

There is a second, quieter guarantee. An untenanted request produces a one-part key; a tenanted request produces a two-or-three-part key that always contains the `tenant=` segment. Because the length-prefixed encoding is decodable across different arities, the untenanted keyspace is provably disjoint from the tenanted one — a client with no tenant can never, by any choice of id, forge a key that lands in some tenant's bucket. This is the same injective-key discipline the [Multi-Tenancy guide](../../mcp/server/multi-tenancy.md) applies across the whole stack (cache scopes, memory owners, conversation ownership, audit entries): the tenant is folded into key derivation once, correctly, in a way that survives hostile ids.

## Per-tool granularity and declared limits

The `per_tool=True` flag you saw above adds the tool name as another key segment, so `export` and `query` throttle independently even for the same tenant and client. That is what stops a single expensive tool — a bulk export, a report generator — from consuming the budget your cheap, high-frequency tools need. Each `(tenant, client, tool)` combination gets its own token bucket, all from the same injective join.

You can also declare a limit on the tool itself and let the server enforce it. A `rate_limit` on the decorator is compiled into the middleware chain at build time — no manual wiring, and the buckets are tenant-qualified the same way:

```python
from promptise.mcp.server import MCPServer

server = MCPServer(name="reports-api")

@server.tool(auth=True, rate_limit="100/min")
async def export(dataset: str) -> dict:
    """Bulk export — capped at 100 calls/min per tenant-client."""
    return {"dataset": dataset, "status": "queued"}
```

When a bucket empties, the enforced `RateLimitError` carries a `retry_after_seconds` value and a concrete "wait N seconds" suggestion in its structured details, so a well-behaved agent backs off instead of hammering. Declared per-tool limits and an installed `RateLimitMiddleware` coexist cleanly: the middleware enforces a server-wide policy, the declaration enforces each tool's own contract, and both key on the tenant. Rate limiting sits alongside the rest of the [production features](../../mcp/server/production-features.md) stack — caching, metrics, audit — and pairs naturally with the backpressure controls in the [Resilience Patterns](../../mcp/server/resilience-patterns.md) guide: a circuit breaker contains a failing *dependency*, tenant-qualified rate limiting contains a noisy *neighbor*. Both isolate one moving part so it cannot take down the whole. It is the same instinct that makes per-tool [tool versioning](version-mcp-tools-without-breaking-clients.md) a first-class primitive rather than a per-project convention: give each dimension that matters its own structural handle.

## Frequently asked questions

### Isn't per-client rate limiting already per-tenant if my client ids are unique?

Only if you can guarantee global uniqueness *and* you never want a per-tenant ceiling — and in a real multi-tenant system you can guarantee neither. Tenants bring their own identity providers, so `user_id` collisions across tenants (`u-42`, `admin`, `1`) are normal, not pathological. And per-client limiting has no concept of "this tenant, in aggregate," so one tenant opening many clients can still out-consume the shared capacity. Tenant-qualifying the bucket key fixes both at once: identical ids in different tenants never collide, and the tenant is a real dimension you can reason about.

### How does Promptise get the tenant onto the request in the first place?

`AuthMiddleware` extracts it. For JWT auth it reads a configurable claim (default `tenant_id`, or `org` / `org_id` / whatever your IdP uses) and only accepts a string value — anything else leaves the tenant unset and tenant guards fail closed. For API-key auth the tenant comes from the key's config dict. Either way it lands on `ctx.client.tenant_id`, which is exactly the field `RateLimitMiddleware` folds into the bucket key. The [Multi-Tenancy guide](../../mcp/server/multi-tenancy.md) shows the full token-to-storage flow.

### Can I do this with FastMCP's built-in rate limiting?

You can approximate it. FastMCP's `RateLimitingMiddleware` is global by default and becomes per-client when you supply a `get_client_id` callable. To get tenant isolation you would return a tenant-inclusive key from that callable, and to get per-tool separation you would fold the tool name in as well — but you own the key composition and its collision-safety, and a single flat string gives you one dimension rather than a `(tenant, client, tool)` triple. Promptise's difference is that the tenant is part of the key derivation structurally, joined injectively so hostile ids cannot collide, with `per_tool` as a first-class flag. See the [FastMCP vs Promptise](fastmcp-alternative-for-production.md) comparison for where this sits in the broader stack.

### What happens to a request that exceeds its tenant's limit?

It is rejected with a `RateLimitError` before the handler runs. The error carries `retry_after_seconds` and a "wait N seconds before retrying" suggestion in its structured details, so agents can honor a backoff rather than retry-storming. The tenant that hit its ceiling is the only one affected — every other tenant's bucket is untouched, which is the entire point.

## Next steps

If you run a shared MCP server, audit your limiter for the collision today: if its bucket key is the client id (or a hand-concatenated tenant prefix), two tenants with a shared `user_id` are one incident away from spending each other's quota. Switch to tenant-qualified buckets by giving your clients a tenant claim and letting `RateLimitMiddleware` fold it in — turn on `per_tool=True` where one heavy tool shouldn't starve the rest, and declare `rate_limit` contracts on the tools that need their own ceiling. Start from the [Multi-Tenancy guide](../../mcp/server/multi-tenancy.md) to wire tenant identity end to end, fit rate limiting into the wider hardening story with the [Production Features](../../mcp/server/production-features.md) overview and the [Resilience Patterns](../../mcp/server/resilience-patterns.md) guide, and if you are choosing a stack, read [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md) to see where first-class tenant isolation changes the math.
