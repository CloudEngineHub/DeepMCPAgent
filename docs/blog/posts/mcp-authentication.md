---
title: "MCP Authentication: JWT, OAuth2 & API Keys"
description: "The MCP spec leaves auth to you, and most guides stop at a shared secret. This deep-dive shows layered, capability-based access done right — transport-level…"
keywords: "mcp authentication, mcp server auth, secure mcp server, mcp jwt auth, mcp api key, mcp oauth2"
date: 2026-07-16
slug: mcp-authentication
categories:
  - MCP
---

# MCP Authentication: JWT, OAuth2 & API Keys

MCP authentication is the part of the Model Context Protocol that the spec deliberately leaves to you — and most tutorials stop at a hard-coded shared secret. That is fine for a laptop demo and dangerous the moment an agent can call a tool that spends money, deletes records, or reads another tenant's data. This guide shows how to do layered, capability-based access properly in Promptise Foundry: transport-level providers (JWT, RS256/ES256, API keys) that verify *who is calling*, plus per-tool guards that decide *what they may do* — all in copy-paste code, not theory. By the end you will have a server where a public health check, a scoped read tool, and an admin-only write tool coexist safely.

New to the protocol itself? Start with [What Is MCP? Model Context Protocol Explained](what-is-mcp.md), then come back here to lock it down.

## Why MCP authentication is your job, not the spec's

The MCP specification standardizes how tools are discovered and invoked. It does **not** standardize how you prove identity or enforce permissions — those are transport and application concerns. That design keeps the protocol small, but it means a server you expose over HTTP is wide open until you add auth yourself.

Two mistakes are common:

- **Stopping at a shared secret.** A single bearer token that every client sends gives you a bouncer who checks that you have *a* ticket, not *which* ticket. You lose per-client identity, roles, scopes, and any audit trail worth the name.
- **Conflating authentication with authorization.** Verifying a token (authentication) and deciding whether that caller may void an invoice (authorization) are different jobs. Collapsing them into one `if` statement is how privilege bugs ship.

Promptise separates the two so each tool can declare exactly what it needs.

## The three layers: providers, middleware, and guards

A [secure MCP server](../../mcp/server/auth-security.md) in Promptise is built from three composable layers:

1. **Auth providers** — `JWTAuth`, `AsymmetricJWTAuth`, `JwksAuth`, and `APIKeyAuth` verify credentials and extract identity.
2. **`AuthMiddleware`** — runs a provider for every tool marked `auth=True`, then populates `ctx.client` with a typed `ClientContext`: client ID, roles, scopes, JWT claims (`iss`, `aud`, `sub`, `exp`), IP address, and user-agent.
3. **Guards** — `HasRole`, `HasAllRoles`, `HasScope`, `HasAllScopes`, `RequireClientId`, `RequireTenant`, and `HasTenant` enforce fine-grained permissions *after* authentication.

The split matters: authentication answers "who is this?" once, and guards answer "may they do this?" per tool. New access rules are added as guards, never as global flags.

## MCP JWT auth in under 20 lines

Here is a complete, runnable server that mixes public, scoped, and admin-only tools. It uses `JWTAuth` for HS256 tokens and Promptise's in-process `TestClient`, so it runs end-to-end with no network and no external identity provider:

```python
import asyncio
from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JWTAuth, HasScope, TestClient,
)

jwt_auth = JWTAuth(secret="change-me-in-prod")   # HS256 shared secret

server = MCPServer(name="billing-api")
server.add_middleware(AuthMiddleware(jwt_auth))

@server.tool()                                   # public — no auth
async def health() -> str:
    """Liveness probe."""
    return "ok"

@server.tool(auth=True, guards=[HasScope("invoices:read")])
async def get_invoice(invoice_id: str) -> dict:
    """Return an invoice (any client with the invoices:read scope)."""
    return {"id": invoice_id, "status": "paid"}

@server.tool(auth=True, roles=["admin"], guards=[HasScope("invoices:write")])
async def void_invoice(invoice_id: str) -> str:
    """Void an invoice (admins holding invoices:write only)."""
    return f"voided {invoice_id}"

async def main():
    # Mint a scoped token the way your IdP would issue one.
    token = jwt_auth.create_token(
        {"sub": "billing-agent", "roles": ["admin"],
         "scope": "invoices:read invoices:write"},
        expires_in=3600,
    )
    client = TestClient(server, meta={"authorization": f"Bearer {token}"})

    print(await client.call_tool("health", {}))
    print(await client.call_tool("get_invoice", {"invoice_id": "INV-42"}))
    print(await client.call_tool("void_invoice", {"invoice_id": "INV-42"}))

asyncio.run(main())
```

Three things are worth calling out:

- Only tools with `auth=True` require a token. `health` stays open for your load balancer's probe.
- `roles=["admin"]` is shorthand for a `HasRole("admin")` guard — declare it inline or as an explicit guard; they are equivalent.
- Scopes come from the JWT `scope` claim (space-separated, per RFC 8693). A token missing `invoices:write` sails through authentication but is denied by the guard on `void_invoice`, with an error that names exactly which scope was missing.

Swap `TestClient` for `server.run(transport="http", port=8080)` and the same rules apply to every real client over the wire. For the full server-building walkthrough — tools, resources, schemas from type hints — see [building MCP servers](../../mcp/server/building-servers.md).

## API keys and OAuth2: choosing your MCP server auth provider

`JWTAuth` is the right default when an OAuth2 identity provider already issues tokens for your agents. But it is not the only provider, and picking the right one is most of the work in getting MCP server auth right.

- **`JWTAuth` (HS256)** — a shared secret you control. Fast, simple, good for internal services where you also mint the tokens. Verified tokens are cached in an LRU so crypto stays off the hot path.
- **`AsymmetricJWTAuth` (RS256/ES256)** — the server holds only the *public* key, so it can verify tokens it could never forge. This is the standard fit for OAuth2 providers like Auth0, Keycloak, and Okta, which sign with a private key you never see. Point `JwksAuth` at the provider's JWKS endpoint and key rotation needs no redeploy.
- **`APIKeyAuth`** — pre-shared keys mapped to client IDs, with an optional rich format that attaches roles. Ideal for machine-to-machine callers, cron jobs, or partners who cannot run a full OAuth2 flow.

A key point about **MCP OAuth2**: Promptise does not reinvent the flow. Your identity provider issues the token; the matching provider (`AsymmetricJWTAuth` or `JwksAuth`) verifies its signature, issuer, and audience, then hands the claims to guards. That audience check is what stops an agent from replaying a token it was legitimately issued for a *different* resource.

One caveat worth remembering: scopes only populate from JWTs. When you use `APIKeyAuth` without a JWT, `ctx.client.scopes` is empty and scope guards always deny — reach for role-based guards (`HasRole`) instead.

## Per-tool guards: roles, scopes, and tenants

Guards are where capability-based access lives. Each is a small object you attach per tool, and each explains itself on denial:

| Guard | Grants access when the client… |
|---|---|
| `HasRole("admin", "ops")` | holds **any** listed role |
| `HasAllRoles("admin", "finance")` | holds **every** listed role |
| `HasScope("invoices:read")` | holds **any** listed OAuth2 scope |
| `HasAllScopes("read", "write")` | holds **every** listed scope |
| `RequireClientId("cron-service")` | is one of the named clients |
| `RequireTenant()` / `HasTenant("acme")` | carries a tenant identity |

For a multi-tenant platform, tenancy is often a server-wide invariant rather than a per-tool decision. Build the server with `require_tenant=True` and every tool — from decorators, mounted sub-servers, or OpenAPI import — is forced to authenticate and carries a `RequireTenant` guard automatically:

```python
server = MCPServer(name="api", require_tenant=True)  # implies require_auth
```

Now a token that lacks the tenant claim is rejected on every call, so one customer's agent can never touch another's tools. The tenant ID also flows into rate-limit buckets and audit entries without extra wiring. The [production MCP servers guide](../../guides/production-mcp-servers.md) shows how these guards sit alongside rate limiting, circuit breakers, and audit logging in a full deployment.

## When a shared secret (or another approach) is the better fit

Layered auth is the right default for anything internet-facing, but it is not free complexity you must always pay:

- **A single shared secret is genuinely fine** for a private stdio server that only your own process spawns, or a throwaway prototype behind a VPN with no per-client distinctions to make. Adding JWT ceremony there buys nothing.
- **If your identity needs are trivial** — a couple of internal services, no roles, no tenants — `APIKeyAuth` with a rich key map gives you named clients and roles without standing up an OAuth2 provider.
- **If you have already standardized on a full API gateway** (Kong, Envoy, an API Management layer) that terminates auth before traffic reaches your service, let it own token validation and keep your MCP tools focused on guards for authorization. Promptise's `AuthProvider` protocol lets you plug that gateway's introspection in as a custom provider rather than duplicating it.

Reach for the full JWT-plus-guards stack when tools have real blast radius, when callers differ in what they may do, or when you must prove *which* agent did *what* in an audit log. That is exactly the case Promptise is built for.

## Frequently asked questions

### What is the difference between MCP authentication and authorization?

Authentication verifies identity — Promptise's providers (`JWTAuth`, `APIKeyAuth`, and friends) confirm a caller is who they claim to be and populate `ctx.client`. Authorization decides what that verified caller may do, which is the job of per-tool guards like `HasRole` and `HasScope`. Keeping them separate lets one authenticated identity have different permissions on different tools.

### Do I need OAuth2 for a secure MCP server?

No. OAuth2 (via `AsymmetricJWTAuth` or `JwksAuth`) is the best fit when an external identity provider already issues tokens for your agents, because your server only needs the public key. For internal services you fully control, HS256 `JWTAuth` with a shared secret, or `APIKeyAuth` with roles, is perfectly secure and simpler to operate.

### Can some tools stay public while others require auth?

Yes. Only tools decorated with `auth=True` run through `AuthMiddleware`; everything else passes through untouched. That is how the example keeps `health` open for load-balancer probes while `void_invoice` demands both an admin role and a write scope.

## Next steps

Lock down your server today: add `JWTAuth` plus a `HasScope` guard to one real tool — it is under 20 lines, as shown above — then layer on roles and tenancy as your surface grows. Start from the [Quick Start](../../getting-started/quickstart.md) to get a server running, then work through the [Authentication & Security reference](../../mcp/server/auth-security.md) to wire in your own identity provider.
