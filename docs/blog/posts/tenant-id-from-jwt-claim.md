---
title: "Where Does tenant_id Come From? JWT Claim vs API Key"
description: "Every multi-tenant post assumes a tenant_id is already present, but where does it actually come from? This how-to fills that gap: the two supported sources …"
keywords: "tenant_id from jwt claim, source tenant_id in mcp server, configurable tenant claim jwt, bind tenant to api key, extract tenant from jwt claim, fail-closed missing tenant claim"
date: 2026-07-16
slug: tenant-id-from-jwt-claim
categories:
  - Multi-Tenancy
---

# Where Does tenant_id Come From? JWT Claim vs API Key

Every multi-tenant tutorial hands you a `tenant_id` and moves on, but the honest question is where that value comes from — and the two answers Promptise Foundry supports are a **tenant_id from jwt claim** and a tenant bound to each API key. Get that sourcing step right and the rest of your isolation story (tenant-qualified rate limits, per-tenant audit, cache scoping) follows for free. Get it wrong — a claim you forgot to read, a number where you expected a string, a request with no tenant at all — and a claim-less caller slips through as tenant-less, which is the one outcome a multi-customer platform can never afford.

This post is the missing chapter: exactly how to **source `tenant_id` in an MCP server**, the two places it can legitimately originate, how the value is coerced, and what happens when it is absent under `require_tenant=True`.

## The two legitimate sources of a tenant_id

Promptise resolves the tenant in exactly one place — `AuthMiddleware`, during authentication — and it reads from one of two sources depending on which auth provider you use:

| Auth provider | Where the tenant comes from |
|---------------|-----------------------------|
| `JWTAuth` / `AsymmetricJWTAuth` | A configurable JWT claim (default `tenant_id`, override to any claim name) |
| `APIKeyAuth` | The `tenant_id` field in that key's config dict |

Whichever source applies, the resolved value lands on the same field — `ClientContext.tenant_id` — and everything downstream (rate-limit buckets, audit entries, the `RequireTenant` / `HasTenant` guards) reads from there. Your tool handlers never parse a token, never look up a claim name, and never decide what "no tenant" means. That decision is made once, at the edge, before any handler runs.

That single-source design is the whole point. The alternative — reading the claim inside each tool — means the claim name, the type coercion, and the missing-claim case are yours to get right at *every* entry point, and the first handler that forgets is a silent cross-tenant leak.

## Source 1: a configurable JWT claim

Most deployments authenticate with signed JWTs, so this is the common path. Point `AuthMiddleware` at whatever claim your identity provider issues the tenant in — `tenant_id`, `org`, `org_id`, a namespaced `https://acme.example/tenant`, anything:

```python
from promptise.mcp.server import MCPServer, AuthMiddleware, JWTAuth, RequestContext

server = MCPServer(name="records", require_tenant=True)  # server-wide invariant

server.add_middleware(
    AuthMiddleware(
        JWTAuth(secret="dev-secret"),
        tenant_claim="org",   # name the claim once, here — not in every handler
    )
)


@server.tool(auth=True)
async def whoami(ctx: RequestContext) -> dict:
    # The tenant is already resolved by the time we arrive.
    return {"client": ctx.client.client_id, "tenant": ctx.client.tenant_id}
```

`tenant_claim="org"` is the configurable tenant claim in a JWT: the middleware calls `payload.get("org")` on the verified token and puts the result on `ctx.client.tenant_id`. To **extract the tenant from a JWT claim** safely, Promptise trusts only string values — the coercion is literally `value if isinstance(value, str) and value.strip() else None`. So a claim that arrives as a JSON number, an array of orgs, a nested object, `null`, or an empty string does *not* become a tenant; it leaves `tenant_id` unset, and the tenant guards then fail closed. That protects you from an IdP that encodes org ids as integers, or a token where the claim is structurally present but empty.

The claim name defaults to `tenant_id`, so if your IdP already issues that claim you can drop the `tenant_claim=` argument entirely.

## Source 2: a tenant bound to each API key

Not every caller carries a JWT. Service-to-service agents, CI jobs, and internal tools often authenticate with a static API key. For those, you **bind a tenant to each API key** in the key's config dict — there is no token to read a claim from, so the binding is declared at registration:

```python
from promptise.mcp.server import MCPServer, AuthMiddleware, APIKeyAuth

server = MCPServer(name="records", require_tenant=True)

server.add_middleware(
    AuthMiddleware(
        APIKeyAuth(keys={
            "sk-acme-1":   {"client_id": "acme-agent",   "roles": ["analyst"], "tenant_id": "acme"},
            "sk-globex-1": {"client_id": "globex-agent", "roles": ["analyst"], "tenant_id": "globex"},
        })
    )
)
```

Each rich-key config carries its own `tenant_id`, and it is coerced with the same string-or-nothing rule the JWT path uses. A key presenting `sk-acme-1` is now permanently `acme`'s; a key with no `tenant_id` in its config resolves to no tenant. Because both sources funnel into the identical `ClientContext.tenant_id` field, a server can mix JWT agents and API-key agents and the downstream rate limits, audit trail, and guards behave identically for both — they never learn which source the tenant came from.

## Fail closed: the missing-claim case

Here is the question the two sections above are really about, and the one most stacks leave to you: what happens when the tenant *cannot* be resolved — a JWT without the claim, a claim that is the wrong type, an API key with no binding?

Under `require_tenant=True`, the answer is a hard denial before the handler runs. `require_tenant=True` is a server-wide invariant: it implies `require_auth` and stamps a `RequireTenant` guard onto every tool — decorators, mounted sub-servers, `MCPRouter` groups, and imported OpenAPI specs alike. A request whose tenant can't be resolved is refused on every call, with no per-handler code.

This block is fully runnable as-is — no LLM key, no live server, no network. It mints two tokens with the built-in `create_token` helper and drives them through the real pipeline with `TestClient`: one carries the `org` claim, one does not.

```python
import asyncio
from promptise.mcp.server import MCPServer, AuthMiddleware, JWTAuth, TestClient, RequestContext

auth = JWTAuth(secret="dev-secret")
server = MCPServer(name="records", require_tenant=True)  # tenancy is a server-wide invariant
server.add_middleware(AuthMiddleware(auth, tenant_claim="org"))


@server.tool(auth=True)
async def whoami(ctx: RequestContext) -> dict:
    # By the time we arrive here, the tenant is already resolved and non-null.
    return {"client": ctx.client.client_id, "tenant": ctx.client.tenant_id}


async def main():
    client = TestClient(server)

    # Token A carries the tenant claim named "org" -> resolves to "acme"
    good = auth.create_token({"sub": "acme-agent", "org": "acme"})
    print("resolved:", await client.call_tool("whoami", headers={"authorization": f"Bearer {good}"}))

    # Token B has no "org" claim -> require_tenant denies it before the handler runs
    bad = auth.create_token({"sub": "claimless-agent"})
    print("denied:  ", await client.call_tool("whoami", headers={"authorization": f"Bearer {bad}"}))


asyncio.run(main())
```

The first call returns `{"client": "acme-agent", "tenant": "acme"}`. The second never reaches `whoami` — it comes back as a structured `ACCESS_DENIED` error whose message reads:

> This tool requires a tenant identity, but the client presented no tenant claim (check the token's tenant claim or the API-key config's tenant_id)

That is **fail-closed on a missing tenant claim** made observable. The claim-less token is authenticated (its signature is valid) but tenant-less, and the invariant refuses it rather than defaulting the request to tenant `None`. Swap the JWT provider for `APIKeyAuth` and the behavior is identical: a key with no `tenant_id` in its config is denied the same way.

## What other frameworks do today

To be fair about the landscape: reading a claim out of a verified token is not hard, and every serious stack lets you do it. The delta is not *can you extract a tenant* — it's whether extraction is a structural invariant or a thing you re-implement at each entry point.

- **FastMCP** verifies bearer tokens and exposes the authenticated identity through `get_access_token()`, whose `AccessToken` carries the token's `scopes` and `claims`. That is a real, usable primitive — but the tenant is yours to pull out of `.claims` *inside each tool*, you choose the claim name at every call site, you decide how to coerce a non-string value, and you decide what a missing claim means. There is no single `tenant_claim=` binding and no server-wide "deny if tenant unresolved" switch, so the missing-claim case defaults to whatever your handler code happens to do.
- **The MCP Python SDK** is the same shape one layer down: `get_access_token()` gives you the decoded auth context; tenant semantics are application code.
- **Agent-orchestration frameworks** (LangChain/LangGraph, CrewAI, AutoGen, LlamaIndex) are clients rather than MCP servers, so they hand your application whatever identity you thread through and leave tenant scoping to you entirely.

None of these are wrong — they hand you the decoded identity and trust you to use it. What Promptise makes *first-class* is the step in between: the tenant is resolved once, from one configured source, coerced to a string-or-nothing, and — under `require_tenant=True` — a request that can't produce one is denied before any handler runs, rather than defaulting to none. The capability is common; making it an invariant is the edge. The full mechanism is documented in the [Multi-Tenancy guide](../../mcp/server/multi-tenancy.md), and the auth providers it builds on are covered in [Authentication & Security](../../mcp/server/auth-security.md).

Once a resolved `tenant_id` is guaranteed present, downstream isolation stops being your problem. The same key qualifies rate-limit buckets, stamps audit entries, and scopes a shared vector store — see [Multi-Tenant RAG: Isolate Customer Data in a Shared Store](multi-tenant-rag.md) for the retrieval side. And because the tenant is part of the key rather than a filter, two customers who happen to share a `user_id` still never collide — the subject of [Same user_id, Two Tenants: Why That Isn't Isolation](same-user-id-across-two-tenants.md). The end-to-end path, from token to storage across both the agent and the server, is walked through in the [secure multi-tenant platform guide](../../guides/secure-multi-tenant-platform.md).

## Frequently asked questions

### How do I source tenant_id in an MCP server?

From one of two providers, resolved once by `AuthMiddleware`. With `JWTAuth`, set `tenant_claim="<name>"` and the middleware reads that claim from the verified token onto `ctx.client.tenant_id`. With `APIKeyAuth`, put a `tenant_id` in each key's config dict. Either way the value lands on the same `ClientContext.tenant_id` field, and rate limits, audit, and the tenant guards read from there — your handlers never parse a token.

### Which JWT claim does Promptise read the tenant from?

Whichever one you name. `tenant_claim` defaults to `"tenant_id"`, but you can point it at `org`, `org_id`, or any namespaced claim your identity provider issues. Only string claim values are trusted: a number, array, object, `null`, or empty string leaves `tenant_id` unset, so a malformed claim fails closed instead of producing a garbage tenant.

### What happens if the tenant claim is missing?

With `require_tenant=True`, the request is denied before the handler runs. The token can be perfectly valid — signed, unexpired — but if it carries no resolvable tenant, the server-wide `RequireTenant` invariant refuses it with an `ACCESS_DENIED` error rather than treating it as tenant `None`. The same holds for an API key whose config has no `tenant_id`.

### Can I mix JWT agents and API-key agents on one server?

Yes. Both sources resolve into the identical `ClientContext.tenant_id` field, so a single server can authenticate JWT-bearing agents and API-key services side by side. Downstream isolation is unaware of the source — it only sees the resolved tenant.

## Next steps

Decide your source, then make it an invariant. If your agents carry JWTs, set `tenant_claim` on `JWTAuth` to whatever claim your IdP issues; if they use static keys, add a `tenant_id` to each key's config in `APIKeyAuth`. Then turn on `require_tenant=True` so every request arrives with a resolved `tenant_id` or is denied — no per-handler tenant code, no tenant-less requests. Start with the [Multi-Tenancy guide](../../mcp/server/multi-tenancy.md), confirm your provider setup against [Authentication & Security](../../mcp/server/auth-security.md), and wire the full token-to-storage flow using the [secure multi-tenant platform guide](../../guides/secure-multi-tenant-platform.md).
