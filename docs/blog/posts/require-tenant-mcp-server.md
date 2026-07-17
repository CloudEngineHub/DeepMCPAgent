---
title: "Enforce Tenant Isolation with require_tenant=True"
description: "Opt-in tenant checks fail the first time someone ships a tool without one. This how-to drills into the build-time mechanics of a single flag…"
keywords: "require_tenant mcp server, enforce tenant isolation mcp, server-wide tenant invariant, requiretenant guard build-time, reject ungated tool no tenant"
date: 2026-07-16
slug: require-tenant-mcp-server
categories:
  - Multi-Tenancy
---

# Enforce Tenant Isolation with require_tenant=True

A **require_tenant mcp server** turns tenant isolation from a check you remember to add into an invariant the framework enforces at build time. Instead of adding a tenant guard to each tool by hand — and hoping the next tool, the next router, and the mounted third-party server all get one too — you set a single flag on the constructor, and every tool the server exposes is forced to authenticate and carry a tenant guard before you write a line of handler code. This post is about the exact mechanics of that flag: what `MCPServer(require_tenant=True)` does at build time, how to prove it with a runnable unit test, why it covers registration paths you didn't write yourself, and where it is honestly stronger than the opt-in checks other frameworks hand you.

## The opt-in trap: one forgotten guard is a cross-tenant hole

Per-handler tenant checks work perfectly until the day they don't. You add `guards=[RequireTenant()]` to your first five tools during review, everyone nods, and the pattern looks solid. Then one of these happens:

- A teammate adds a sixth tool in a hurry and forgets the guard. It authenticates fine, so nothing looks wrong — but it serves every tenant from one unscoped code path.
- You `server.mount()` a sub-server written by another team, or by a vendor. Its tools arrive already registered, and none of them know about your tenant convention.
- You import forty tools from an OpenAPI spec. They are generated, not hand-decorated, so there is nowhere obvious to hang a guard.

None of these throw. A tool with no tenant guard is not a syntax error; it is a silently over-broad tool. And because the failure is a *missing* check rather than a *wrong* one, it survives code review and it survives a test suite that — like almost every test suite — exercises a single tenant. It surfaces in production as the worst incident class a multi-customer platform can ship: one customer reading another's data. The same silent-collision dynamic on the storage side is the subject of [Same user_id, Two Tenants: Why That Isn't Isolation](same-user-id-across-two-tenants.md); this post is the server-boundary counterpart.

The fix is not "be more careful." Careful is a convention, and conventions fail the first time one call site forgets. The fix is to make an ungated tool impossible to have.

## What require_tenant=True does at build time

`MCPServer(require_tenant=True)` promotes tenancy to a **server-wide invariant**. Setting it does two things immediately: it implies `require_auth` (an unauthenticated caller has no tenant, so there is nothing to isolate on), and it registers a build-time pass that walks the entire tool registry and, for every tool, forces `auth=True` and appends a `RequireTenant` guard if one is not already present.

```python
from promptise.mcp.server import MCPServer

# One flag. Implies require_auth. Applies to every tool, however it was registered.
server = MCPServer(name="records-api", require_tenant=True)
```

The guard itself is small and fails closed. `RequireTenant` reads `ctx.client.tenant_id` — populated by `AuthMiddleware` from a configurable JWT claim (default `tenant_id`, or `org`, `org_id`, whatever your IdP emits) or from an API-key config dict — and denies the call when it is empty. No authentication means no tenant means denied; a valid token that simply lacks the tenant claim is denied too. The [multi-tenancy reference](../../mcp/server/multi-tenancy.md) lists everything the tenant then automatically isolates once it is present: rate-limit buckets become tenant-qualified, audit entries record the tenant, and `RequireTenant` / `HasTenant` govern tool access.

Where does the tenant value come from? On the auth provider, and the wiring is exactly the same whether you use JWTs or API keys — see [Authentication & Security](../../mcp/server/auth-security.md) for the full provider surface. For a signed JWT, `AuthMiddleware(provider, tenant_claim="tenant_id")` lifts the claim onto `ClientContext.tenant_id`. For pre-shared keys, the rich `APIKeyAuth` format carries the tenant inline:

```python
from promptise.mcp.server import APIKeyAuth

APIKeyAuth(keys={
    "sk-acme-1":   {"client_id": "acme-agent",   "roles": ["analyst"], "tenant_id": "acme"},
    "sk-globex-1": {"client_id": "globex-agent", "roles": ["analyst"], "tenant_id": "globex"},
})
```

A note on the keyword many people search for — "reject ungated tool no tenant." Promptise ships *two* build-time invariants, and they behave differently on purpose. For tenancy there is an obvious correct remediation (require a tenant), so `require_tenant=True` **retrofits** the guard onto every tool rather than refusing to build — the result is that there is simply no ungated tool in the running server. Its sibling, approval gates (`requires_approval=True`), *does* refuse to build an ungated tool and raises, because the framework cannot guess *who* approves and must not silently under-enforce. Same goal — no unenforced declaration ever ships — reached the way that is safe for each case. Both are covered end to end in the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md).

## Prove it without a network or an API key

The most reassuring thing about a build-time invariant is that you can pin it in a unit test. The snippet below runs with nothing but `pip install promptise` — no live model, no server process, no network. It registers a tool with **no** `auth=` and **no** `guards=`, then uses the in-process `TestClient` (which applies the same tenant invariant as the live build path) to show that the tool nonetheless denies a tenantless caller:

```python
import asyncio

from promptise.mcp.server import MCPServer, AuthMiddleware, APIKeyAuth, RequestContext
from promptise.mcp.server.testing import TestClient


async def main() -> None:
    # The invariant: one flag, applied server-wide.
    server = MCPServer(name="records-api", require_tenant=True)

    # Tenant is sourced from the API key's config (or a JWT claim in production).
    server.add_middleware(
        AuthMiddleware(
            APIKeyAuth(
                keys={
                    "sk-acme":      {"client_id": "acme-agent",   "tenant_id": "acme"},
                    "sk-globex":    {"client_id": "globex-agent", "tenant_id": "globex"},
                    "sk-no-tenant": {"client_id": "legacy-agent"},   # no tenant configured
                }
            )
        )
    )

    # Note: no auth=..., no guards=[...]. The invariant retrofits both.
    @server.tool()
    async def list_records(ctx: RequestContext = None) -> str:  # type: ignore[assignment]
        """Return records for the caller's tenant."""
        return f"records for tenant={ctx.client.tenant_id}"

    client = TestClient(server)

    # A tenant-bearing key is allowed and sees only its own tenant.
    ok = await client.call_tool("list_records", {}, headers={"x-api-key": "sk-acme"})
    print("acme      ->", ok[0].text)          # records for tenant=acme

    # Authenticated but no tenant claim -> RequireTenant denies (fail closed).
    denied = await client.call_tool("list_records", {}, headers={"x-api-key": "sk-no-tenant"})
    print("no-tenant ->", denied[0].text)      # ACCESS_DENIED: requires a tenant identity

    # Unauthenticated -> denied before the handler ever runs.
    anon = await client.call_tool("list_records", {})
    print("anon      ->", anon[0].text)        # AUTHENTICATION_ERROR: Missing API key


asyncio.run(main())
```

Run it and the tenantless key comes back with a structured `ACCESS_DENIED` naming the `RequireTenant` guard, while the anonymous call is stopped even earlier with `AUTHENTICATION_ERROR`. The point is not that the guard works — it is that the tool you wrote *declared no guard at all* and is still safe. That is the difference between a convention and an invariant: the safety does not depend on the handler author remembering anything.

Want a specific allow-list instead of "any tenant"? Swap in `HasTenant` for partner-only tools; it composes with the server-wide invariant rather than replacing it:

```python
from promptise.mcp.server import HasTenant

@server.tool(auth=True, guards=[HasTenant("acme", "globex")])
async def partner_report() -> str:
    """Only acme and globex may call this tool."""
    ...
```

## Decorators, routers, mounts, and OpenAPI imports all inherit it

Here is the question the opt-in model cannot answer cleanly: **when you mount a sub-server or import forty tools from an OpenAPI spec, are they all tenant-guarded — or did you just widen your attack surface with tools nobody remembered to guard?**

With per-handler checks the honest answer is "the ones I hand-annotated are; the imported ones are not, until I go back and wrap each." Generated and mounted tools are exactly the ones least likely to receive a manual guard, because there is no decorator call site to attach one to.

`require_tenant=True` sidesteps that entirely. The invariant is not applied per decorator — it is applied once, over the whole tool registry, after every registration path has populated it. So the same guarantee lands on tools you wrote, tools a router contributed, tools a mounted server brought in, and tools generated from an OpenAPI document:

```python
from promptise.mcp.server import MCPServer, MCPRouter, OpenAPIProvider, mount

server = MCPServer(name="platform", require_tenant=True)

# 1) Decorated tool — guarded.
@server.tool()
async def native_tool() -> str: ...

# 2) Router-contributed tools — guarded.
records = MCPRouter(prefix="records")
@records.tool()
async def list_records() -> list: ...
server.include_router(records)

# 3) A mounted sub-server's tools — guarded.
mount(server, billing_server, prefix="billing")

# 4) OpenAPI-imported tools — guarded.
OpenAPIProvider("./crm.openapi.json", prefix="crm_").register(server)
```

Every tool in that server — native, routed, mounted, imported — authenticates and carries a `RequireTenant` guard, because the invariant reads the finished registry, not the source you typed. That is the cleanly-solvable version of "did I guard everything," and it is answerable with a single flag rather than an audit.

## What other frameworks do today

To be fair, every serious framework gives you the ingredients to enforce tenancy. What differs is whether *forgetting* is a reachable state.

- **FastMCP** ships a full middleware system and bearer/JWT authentication — its `get_access_token()` exposes the verified claims to your handlers — plus tool mounting and import. You can absolutely read a tenant claim and reject a tenantless caller, in a middleware you write and apply. What it does not have is a server-wide flag that forces *every* registered tool to authenticate and carry a tenant guard; tenant enforcement is a convention you add per handler or per middleware, so a newly added or imported tool is unguarded until you wire it in. (Its rate limiting is likewise global or per-client, not tenant-qualified — a related delta covered in the multi-tenancy reference.)
- **The official MCP Python SDK** authenticates with a `TokenVerifier` and lets a tool declare `required_scopes`. You could encode a tenant as a scope, but scopes are declared per tool by hand, and there is no tenant primitive that spans mounted or OpenAPI-imported tools.
- **Agent-side frameworks** put the tenant in a key you compose yourself. **LangGraph** namespaces its long-term `BaseStore` with tuples you build (e.g. `(user_id, "memories")`) and has a `checkpoint_ns` meant for subgraphs, not tenancy. **CrewAI** and **AutoGen** scope memory by a single `user_id`. All real and useful; none carry a tenant dimension unless you add one — the storage-side mechanics are in [Same user_id, Two Tenants: Why That Isn't Isolation](same-user-id-across-two-tenants.md).

None of this means those frameworks *can't* isolate tenants — you can always add the check. The precise delta is where the check lives. In each of them, tenant enforcement is something you apply per handler (or per bespoke middleware), so the guarantee is exactly as strong as everyone's memory on the day a new tool lands. Promptise's contribution is to make it a build-time invariant: `require_tenant=True` retrofits authentication and a `RequireTenant` guard onto every tool from every registration path, so "ungated tool" is not a state the server can be in. The same structural-versus-conventional argument applied to the retrieval path is laid out in [Multi-Tenant RAG: Isolate Customer Data in a Shared Store](multi-tenant-rag.md).

## Frequently asked questions

### Does require_tenant=True refuse to build if a tool has no tenant guard?

No — and that distinction matters. For tenancy the correct remediation is unambiguous (require a tenant), so the invariant *retrofits* `auth=True` and a `RequireTenant` guard onto every tool at build time instead of raising. The net effect is the same as "reject ungated tools": there is no ungated tool in the running server. Its sibling invariant, `requires_approval=True` (approval gates), *does* refuse to build an ungated tool, because the framework cannot decide who approves and must fail loud rather than under-enforce.

### Where does the tenant value actually come from?

From the authenticated identity. `AuthMiddleware` extracts it from a configurable JWT claim (default `tenant_id`, but you can point it at `org`, `org_id`, etc.) into `ClientContext.tenant_id`. With `APIKeyAuth`, the tenant comes from the key's rich config dict (`{"client_id": ..., "tenant_id": "acme"}`). Only string claim values are accepted; anything else leaves `tenant_id` unset and the tenant guard fails closed. See [Authentication & Security](../../mcp/server/auth-security.md) for every provider.

### Does the invariant cover tools I didn't decorate myself?

Yes. The pass runs over the finished tool registry, so it applies identically to tools from decorators, `MCPRouter` includes, `server.mount()`ed sub-servers, and `OpenAPIProvider` imports. That is precisely the case per-handler guards leave exposed, because generated and mounted tools have no decorator call site to attach a guard to.

### Can I still allow-list specific tenants per tool?

Yes. `require_tenant=True` guarantees *some* tenant on every tool; layer `HasTenant("acme", "globex")` on individual tools to restrict them to named tenants. Guards compose — the server-wide `RequireTenant` and a per-tool `HasTenant` both run, and the first denial short-circuits with a descriptive error.

### Is this enough for full multi-tenant isolation?

It secures the server boundary — who may call which tool, under which tenant. Complete isolation also means per-tenant storage keys (memory, cache, conversation ownership) so two tenants sharing a `user_id` never collide. The agent side of that invariant, `CallerContext.isolation_key`, and the end-to-end token-to-storage flow are in the [multi-tenancy reference](../../mcp/server/multi-tenancy.md) and the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md).

## Next steps

Build `MCPServer(name="api", require_tenant=True)` and every tool is tenant-guarded at build time before you write a line of handler code — no per-handler discipline, no audit of imported tools, no state in which an ungated tool can exist. Start by adding the flag to an existing server and running the offline `TestClient` snippet above as a unit test, so the invariant is pinned in CI. Then read the [multi-tenancy reference](../../mcp/server/multi-tenancy.md) for everything the tenant automatically isolates, wire the tenant source with [Authentication & Security](../../mcp/server/auth-security.md), and follow the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md) to thread the same `tenant_id` from token to storage. `pip install promptise` and make a forgotten tenant guard impossible.
