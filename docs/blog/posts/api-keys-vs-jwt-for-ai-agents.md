---
title: "API Keys vs JWT for AI Agent Tools: Which to Use"
description: "An honest decision article: for a simple single-tenant internal tool a static API key is perfectly fine, and Promptise ships APIKeyAuth as a first-class…"
keywords: "API keys vs JWT for AI agents, API key vs bearer token, when to use JWT auth, static API key risks, APIKeyAuth, agent tool authentication comparison"
date: 2026-07-16
slug: api-keys-vs-jwt-for-ai-agents
categories:
  - Identity
---

# API Keys vs JWT for AI Agent Tools: Which to Use

The debate over API keys vs JWT for AI agents usually gets strawmanned: a vendor comparison declares static keys "insecure" and pushes you toward tokens you don't need yet. That framing is wrong. For a single internal tool with one caller, a pre-shared API key is a perfectly good, production-grade choice — and Promptise Foundry ships `APIKeyAuth` as a first-class provider, not a training-wheels fallback. By the end of this article you'll be able to match your actual threat model to the right provider, and know the exact moment JWT and verifiable identity start earning their extra complexity.

## Two first-class providers, one decision

Both auth styles are supported on equal footing by the same `AuthMiddleware` and the same per-tool guards. Nothing about a Promptise MCP server is "designed for JWT" and grudgingly tolerant of keys. You pick the provider; the rest of the stack — role guards, tenant guards, audit logging — behaves identically.

- **`APIKeyAuth`** — pre-shared keys mapped to a client identity, sent in an `x-api-key` header. Simple to issue, simple to rotate by editing a dict.
- **`JWTAuth`** / **`AsymmetricJWTAuth`** — signed bearer tokens. Claims (roles, scopes, issuer, expiry) travel inside the token and are verified cryptographically on every call.

The real question behind "API key vs bearer token" is not *which is more secure in the abstract* — it's *who issues credentials, how many callers exist, and what you have to prove after the fact*.

## When a static API key is the right call

Reach for `APIKeyAuth` when the situation is bounded and you own both ends of the wire:

- A single agent (or a handful) calling one internal MCP server.
- One tenant. No per-user data isolation requirement.
- No external identity provider in the picture.
- Rotation measured in "we'll swap the key when someone leaves," not "keys must auto-expire in 15 minutes."

Here's a complete, runnable server. The rich key format maps each key to a client ID *and* roles, so role guards work with zero JWT machinery:

```python
from promptise.mcp.server import MCPServer, AuthMiddleware, APIKeyAuth

server = MCPServer(name="billing-tools")

# Each key resolves to a client identity plus roles.
server.add_middleware(AuthMiddleware(APIKeyAuth(
    keys={
        "sk-ops-abc": {"client_id": "ops-agent", "roles": ["admin"]},
        "sk-view-xyz": {"client_id": "viewer-agent", "roles": ["read"]},
    },
    header="x-api-key",  # default
)))

@server.tool(auth=True, roles=["admin"])
async def issue_refund(order_id: str, amount: float) -> dict:
    """Issue a refund (admin only)."""
    return {"order_id": order_id, "refunded": amount}

@server.tool(auth=True)
async def list_orders() -> list[str]:
    """List recent orders (any authenticated caller)."""
    return ["ord-1001", "ord-1002"]

if __name__ == "__main__":
    server.run(transport="http", host="127.0.0.1", port=8080)
```

`viewer-agent` can call `list_orders` but is denied `issue_refund` — the `roles=["admin"]` guard rejects it, and the error explains *which* role was missing versus what the client holds. That's real role-based access control with a static key. No token service, no clock skew, no key-rotation cron. For the workload it fits, that simplicity *is* the security win: fewer moving parts, fewer ways to misconfigure.

## Static API key risks: where keys start to hurt

Being fair to keys does not mean pretending the sharp edges aren't there. The static API key risks that actually bite show up as your deployment grows:

- **They don't expire on their own.** A leaked key is valid until a human notices and edits the config. JWTs carry an `exp` claim and die on schedule.
- **They're bearer secrets in plain form.** Anything that can read the key can impersonate the client. There's no signature binding it to a caller or an audience.
- **Attribution blurs across a fleet.** If ten agents share `sk-ops-abc`, "which agent issued that refund?" has no trustworthy answer after the fact. A process asserting its own name in a log isn't proof.
- **Rotation is coordinated, not automatic.** Changing a key means redistributing it to every caller at once.

None of these are disqualifying for an internal tool. All of them become liabilities the moment you have a fleet, multiple tenants, or an auditor asking questions.

## When to use JWT auth (and verifiable identity)

JWT earns its keep once the credential itself needs to carry trustable, time-boxed claims. Switch to `JWTAuth` (HS256, shared secret) or `AsymmetricJWTAuth` (RS256/ES256, issued by an external IdP) when any of these are true:

- **A fleet of agents**, where you need to know exactly which one acted.
- **Multiple tenants**, where each caller must be scoped to its own data.
- **Rotation and expiry requirements** — short-lived tokens that expire without human intervention.
- **Audit obligations** — you have to prove, later, who did what.

The swap is a one-liner. The same server, the same guards, the same tools — you change only the provider:

```python
from promptise.mcp.server import AuthMiddleware, JWTAuth

# Same server, same @tool guards — only the provider changes.
server.add_middleware(AuthMiddleware(JWTAuth(secret="my-shared-secret")))
```

With `AsymmetricJWTAuth` you don't hold the signing secret at all: the server verifies tokens against an identity provider's public key, so keys can rotate on the IdP without touching your server. This is the point where authentication and *identity* converge. A verifiable, IdP-issued credential lets an agent present a stable, non-human identity — the model behind Promptise's [Agent Identity](../../identity/overview.md) system — so both attribution and authorization work across a whole fleet. If you're standing up multi-tenant tools, the full request path from your app's `CallerContext` through the agent to server-side role checks is walked end to end in the [Multi-User Identity guide](../../guides/multi-user-identity.md). For the complete provider reference — `JwksAuth`, token caching, `on_authenticate` hooks — see [Authentication & Security](../../mcp/server/auth-security.md). If you've decided JWT is where you're headed, the companion walkthrough [JWT Authentication for MCP Servers: Step by Step](jwt-authentication-for-mcp-servers.md) takes you from zero to a verified token.

## Agent tool authentication comparison at a glance

A side-by-side to make the decision concrete:

| Dimension | `APIKeyAuth` | `JWTAuth` / `AsymmetricJWTAuth` |
|---|---|---|
| Credential | Pre-shared key in `x-api-key` | Signed bearer token in `Authorization` |
| Expiry | Manual (edit config) | Automatic via `exp` claim |
| Roles / scopes | From key config dict | From verified token claims |
| Rotation | Coordinated redistribution | IdP-driven, no server change (asymmetric) |
| Best-fit scale | 1 caller, 1 tenant, internal | Fleet, multi-tenant, external IdP |
| Attribution | Weak if keys are shared | Strong, cryptographically bound |
| Setup cost | Minimal | Token issuance + verification |

### When a static key is the better fit

To keep this honest in the other direction: do not reach for JWT just to look enterprise-ready. If you have one agent talking to one internal server, `APIKeyAuth` is lower-risk *because it's simpler*. A token pipeline you don't need is more code to get wrong — a misconfigured audience, a leaked signing secret, a clock-skew bug. Start with the key. The whole point of shipping both as first-class providers is that graduating later is a one-line change, not a rewrite.

## Frequently asked questions

### Are static API keys insecure for AI agents?

No — not inherently. A pre-shared key transmitted over TLS is a legitimate production credential for a bounded, single-tenant internal tool. The real static API key risks are operational: keys don't self-expire, and attribution blurs when many callers share one. Those matter at fleet scale, not for a single trusted caller.

### Can I get role-based access control without JWT?

Yes. `APIKeyAuth`'s rich key format maps each key to a `client_id` and a `roles` list, which populates the client context that guards like `HasRole` read. So `@server.tool(auth=True, roles=["admin"])` works with a plain API key — no token required.

### How hard is it to migrate from API keys to JWT later?

It's a one-line swap of the provider passed to `AuthMiddleware`. Your tools, role guards, and tenant guards don't change, because they read a uniform client context regardless of which provider authenticated the request. That's why starting with `APIKeyAuth` doesn't paint you into a corner.

## Next steps

Match your threat model to a provider: start with `APIKeyAuth` for the internal tool you're shipping this week, and graduate to verifiable identity when the audit ask arrives. Stand up your first authenticated server from the [Quick Start](../../getting-started/quickstart.md), then read [Authentication & Security](../../mcp/server/auth-security.md) for the full provider and guard reference. For the bigger picture on attributing and governing a fleet of agents, start with [AI Agent Identity & Authentication: The Complete Guide](ai-agent-identity.md).
