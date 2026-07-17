---
title: "JWT Authentication for MCP Servers: Step by Step"
description: "The MCP spec docs stop at transport; developers still ask how to actually gate a tool by role. This shows the three-layer model (JWTAuth provider ->…"
keywords: "JWT authentication for MCP servers, secure MCP server, MCP server auth, AuthMiddleware, role-based access MCP tools, authenticate MCP tool calls"
date: 2026-07-16
slug: jwt-authentication-for-mcp-servers
categories:
  - Identity
---

# JWT Authentication for MCP Servers: Step by Step

JWT authentication for MCP servers is the piece the protocol spec leaves out: it tells you how to move bytes over stdio, HTTP, or SSE, but not how to prove *who* is calling a tool or stop an unauthorized agent from invoking your `delete_all` endpoint. If you have shipped a Model Context Protocol server and now need to lock it down, you are in the right place. By the end of this post you will understand the three-layer model Promptise Foundry uses — a JWT provider, `AuthMiddleware`, and per-tool guards — and you will have a copy-paste server where only the tools you mark are protected.

## Why transport auth alone is not enough

Most MCP tutorials stop at "put a token in the `Authorization` header." That gets a token to the server, but it answers only one question: is the caller who they say they are? It does not answer the harder question: *is this caller allowed to run this specific tool?*

A production MCP server almost always has mixed sensitivity:

- A `health` check any client should reach, authenticated or not.
- A `list_users` tool any authenticated agent can call.
- A `delete_user` tool only admins should touch.

Baking that policy into each handler with hand-rolled `if token.role != "admin"` checks is how servers drift into inconsistency and quiet security holes. A **secure MCP server** needs authentication and authorization as declarative, testable layers — not scattered conditionals. That is exactly what Promptise separates out.

## The three-layer model for MCP server auth

Promptise splits MCP server auth into three composable layers, each with one job:

1. **Auth provider** — verifies a credential and extracts identity. `JWTAuth` (HS256 shared secret), `AsymmetricJWTAuth` (RS256/ES256), `JwksAuth` (rotating IdP keys), and `APIKeyAuth` all implement the same provider protocol.
2. **`AuthMiddleware`** — runs the provider for any tool marked `auth=True`. On success it populates `ctx.client` with a typed `ClientContext`: `client_id`, `roles`, `scopes`, JWT claims (`iss`, `aud`, `sub`, `exp`), IP address, and user-agent.
3. **Guards** — per-tool permission checks that run *after* authentication: `RequireAuth`, `HasRole`, `HasAllRoles`, `HasScope`, `HasAllScopes`, `RequireClientId`.

The clean separation matters. The provider does not know about roles; the middleware does not know about tools; guards do not know how the credential was verified. You can swap `JWTAuth` for `JwksAuth` later without touching a single guard. The full reference for every provider, middleware option, and guard lives in the [Authentication & Security guide](../../mcp/server/auth-security.md).

## A copy-paste secure MCP server

Here is a complete, runnable server that shows all three layers. It defines a public tool, an authenticated-only tool, a role-gated tool, and a scope-gated tool, then exercises each one in-process with `TestClient` — no network, no external IdP required.

```python
import asyncio
from promptise.mcp.server import (
    MCPServer, JWTAuth, AuthMiddleware,
    HasRole, HasScope, RequireAuth, TestClient,
)

# Layer 1: the provider verifies HS256 tokens against a shared secret.
jwt_auth = JWTAuth(secret="change-me-in-prod")

server = MCPServer(name="secure-api")

# Layer 2: AuthMiddleware runs the provider for every auth=True tool
# and populates ctx.client with identity, roles, and scopes.
server.add_middleware(AuthMiddleware(jwt_auth))


@server.tool()  # no auth=True -> public
async def health() -> str:
    """Liveness probe any client can reach."""
    return "ok"


@server.tool(auth=True, guards=[RequireAuth()])
async def list_users() -> list[str]:
    """Any authenticated client may list users."""
    return ["alice", "bob"]


# Layer 3: guards gate the tool by role or scope after authentication.
@server.tool(auth=True, guards=[HasRole("admin")])
async def delete_user(user_id: str) -> str:
    """Admins only."""
    return f"deleted {user_id}"


@server.tool(auth=True, guards=[HasScope("reports:read")])
async def export_report() -> str:
    """Requires the reports:read OAuth2 scope."""
    return "report.csv"


async def main():
    # Mint tokens the way your IdP would. roles and scope are standard claims.
    admin = jwt_auth.create_token(
        {"sub": "agent-admin", "roles": ["admin"], "scope": "reports:read"},
        expires_in=3600,
    )
    viewer = jwt_auth.create_token(
        {"sub": "agent-viewer", "roles": ["viewer"]},
        expires_in=3600,
    )

    # Public tool: no token needed.
    print(await TestClient(server).call_tool("health", {}))

    # Authenticated tool with an admin token.
    admin_client = TestClient(server, meta={"authorization": f"Bearer {admin}"})
    print(await admin_client.call_tool("list_users", {}))
    print(await admin_client.call_tool("delete_user", {"user_id": "bob"}))
    print(await admin_client.call_tool("export_report", {}))

    # Viewer token: allowed to list, denied on the admin + scope tools.
    viewer_client = TestClient(server, meta={"authorization": f"Bearer {viewer}"})
    print(await viewer_client.call_tool("list_users", {}))
    print(await viewer_client.call_tool("delete_user", {"user_id": "bob"}))

    # No token at all: even list_users is refused.
    print(await TestClient(server).call_tool("list_users", {}))


asyncio.run(main())
```

Run it and you will see the admin succeed everywhere, the viewer get an `ACCESS_DENIED` on `delete_user` and `export_report`, and the anonymous client get refused on anything marked `auth=True`. Nothing changes in `health` — tools without `auth=True` pass straight through the middleware.

Two details worth calling out. First, the `roles=["admin"]` shorthand on `@server.tool()` is exactly equivalent to `guards=[HasRole("admin")]`; use whichever reads better. Second, guard denials are self-explaining — a `HasRole` failure returns `"Requires any of roles [admin], but client has [viewer]"`, so you never have to reverse-engineer why a call was blocked.

## Role-based access for MCP tools, in practice

The example above uses `HasRole` (any of the listed roles) and `HasScope` (any of the listed OAuth2 scopes). Real deployments usually need a bit more nuance, and the guard set covers it without custom code:

- **`HasAllRoles("admin", "finance")`** — the caller must hold *every* listed role. Good for tools that sit at the intersection of two teams, like `approve_budget`.
- **`HasAllScopes("read", "write")`** — every scope required, mirroring fine-grained OAuth2 grants.
- **`RequireClientId("cron-service")`** — pin a tool to specific machine identities, so only your scheduler can trigger a migration.

Because roles and scopes come straight off the verified token, your identity provider stays the single source of truth. One caveat to internalize: scopes are read from the JWT `scope` claim, so scope guards only work with JWT-based providers. If you authenticate with `APIKeyAuth`, `ctx.client.scopes` is empty and scope guards will always deny — reach for `HasRole` instead, since rich API keys can carry roles. This trade-off, and the broader question of mapping human and agent identities to tool permissions, is covered in the [multi-user identity guide](../../guides/multi-user-identity.md).

## From shared secrets to real identity providers

`JWTAuth` with a shared HS256 secret is perfect for getting started, internal services, and tests. In production you rarely mint your own tokens — an identity provider does. Promptise gives you two drop-in upgrades that keep every guard in this post unchanged:

- **`AsymmetricJWTAuth`** verifies RS256/ES256 tokens with a public key, so the signing key never lives on your MCP server.
- **`JwksAuth`** fetches and caches a provider's rotating public keys from a JWKS endpoint (Microsoft Entra, Okta, Auth0, Keycloak) and verifies the `aud` claim, which is what stops an agent replaying a token minted for a different resource.

`JwksAuth` is the server-side counterpart to Promptise's **agent identity** system: an agent presents an IdP-issued credential, and the server verifies which agent is calling. If you are wiring agents into this, start with the [Agent Identity overview](../../identity/overview.md) to see how the two halves fit, and read our companion post [AI Agent Identity & Authentication: The Complete Guide](ai-agent-identity.md) for the full picture across both sides of the connection.

### When a simpler approach is the better fit

Be honest with yourself about scope. If your MCP server runs on a stdio transport as a local subprocess for a single trusted client, transport-level isolation may already be sufficient, and adding JWT verification is ceremony without benefit. Likewise, if you only need to authenticate machine-to-machine callers and never model human roles, a rich `APIKeyAuth` map is simpler to operate than a JWT pipeline. Reach for full JWT authentication when your server is network-exposed, serves multiple callers, or needs role- and scope-level authorization — which is most HTTP-transport deployments.

## Frequently asked questions

### How do I authenticate MCP tool calls without changing every handler?

You do not touch handlers at all. Add `AuthMiddleware(JWTAuth(secret=...))` once, then mark the tools that need protection with `auth=True`. The middleware verifies the token and populates `ctx.client`; guards on the decorator enforce roles or scopes. Handlers stay focused on business logic.

### What happens when a client sends an invalid or expired JWT?

`AuthMiddleware` calls the provider, which rejects the token, and the client receives an authentication error instead of the tool result. Expired tokens fail on the `exp` claim automatically. Verified tokens are cached in an LRU so repeated valid calls do not pay the crypto cost twice.

### Can I mix JWT and API key authentication on one MCP server?

Each `AuthMiddleware` wraps one provider, but you can add multiple middleware or implement the `AuthProvider` protocol to try several credential types. In most designs, though, you standardize on one provider per server — JWT for role- and scope-based access, or API keys for simple machine identity — and keep the guard layer identical either way.

## Next steps

Copy the secure-server template above and protect your first admin-only tool in under ten minutes: mark it `auth=True`, add a `HasRole` guard, and verify it in-process with `TestClient`. From there, follow the [Quick Start](../../getting-started/quickstart.md) to stand the server up over HTTP with `promptise serve`, then dig into the full [Authentication & Security guide](../../mcp/server/auth-security.md) to move from a shared secret to your real identity provider.
