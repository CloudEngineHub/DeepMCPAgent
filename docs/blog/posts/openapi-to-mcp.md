---
title: "Turn a REST or OpenAPI API into an MCP Server"
description: "You don't have to rewrite your API to make it agent-callable. This shows generating MCP tools directly from an existing OpenAPI spec, then layering Promptise…"
keywords: "openapi to mcp, rest api to mcp, mcp for existing api, openapi mcp server, convert rest api to mcp"
date: 2026-07-16
slug: openapi-to-mcp
categories:
  - MCP
---

# Turn a REST or OpenAPI API into an MCP Server

Going from OpenAPI to MCP is the fastest way to make an API you already own callable by AI agents — and you can do it without touching a single route handler. If your service already ships a Swagger or OpenAPI document, Promptise Foundry can read that spec and generate one MCP tool per operation, complete with JSON Schema derived from your existing parameter definitions. By the end of this article you'll have an OpenAPI mcp server running that proxies real HTTP calls to your API, and you'll know how to layer authentication, rate limits, and endpoint filtering on top so agents only touch what you allow.

<!-- more -->

## Why you shouldn't rewrite your API for agents

The naive path to agent access is to hand-write an MCP tool wrapper for every endpoint: copy the path, restate the parameters, re-encode the request, and keep all of it in sync with the API forever. For a service with fifty endpoints, that's fifty wrappers and fifty chances for drift between what the schema promises and what the endpoint actually accepts.

The insight behind converting a REST API to MCP automatically is that your OpenAPI spec is already a complete, machine-readable description of every operation — method, path, parameters, request body, and types. That's exactly the information an MCP client needs to build valid tool calls. If you're new to the protocol itself, the [What Is MCP? Model Context Protocol Explained](what-is-mcp.md) primer covers the client/server model in plain terms. The short version: MCP is how agents discover and call your tools, and an OpenAPI document is a near-perfect source for that tool list.

## From an OpenAPI spec to MCP tools in one call

Promptise Foundry ships `OpenAPIProvider`, a class that parses an OpenAPI 3.x or Swagger 2.x document and registers one MCP tool per operation. Each generated tool makes a real HTTP request to the target API when an agent calls it — the provider handles path parameters, query strings, request bodies, and the auth header for you.

Install the framework into a virtual environment:

```bash
pip install promptise
```

Then point the provider at any spec. Here it targets the public Swagger Petstore, but the same three lines work against your own `openapi.json`:

```python
# api_bridge.py
import os

from promptise.mcp.server import MCPServer, OpenAPIProvider

server = MCPServer(name="petstore-bridge", version="1.0.0")

# Point OpenAPIProvider at a URL, a local file, or a pre-parsed dict.
# Every operation in the spec becomes a typed MCP tool.
provider = OpenAPIProvider(
    "https://petstore3.swagger.io/api/v3/openapi.json",
    prefix="petstore_",
    include={"getPetById", "findPetsByStatus"},
    auth_header=("Authorization", f"Bearer {os.environ['PETSTORE_TOKEN']}"),
    tags=["petstore"],
)

count = provider.register(server)
print(f"Registered {count} tools from the OpenAPI spec")

if __name__ == "__main__":
    server.run(transport="http", host="127.0.0.1", port=8080)
```

Run it and every included operation is now an MCP tool. `getPetById` becomes `petstore_getPetById`, its `petId` path parameter becomes a required, typed field in the tool's JSON Schema, and calling the tool issues a `GET` to the live API with the `Authorization` header attached. An agent connected to this server discovers the tools and starts calling them — no per-endpoint wrappers anywhere.

A few things worth knowing about how the provider behaves:

- **Spec sources are flexible.** Pass a URL (fetched over HTTPS), a local `.json`/`.yaml` file path, or a dict you've already parsed.
- **Tool names are cleaned up.** Operation IDs are normalized (dashes and slashes stripped) and prefixed with whatever you set in `prefix`, which keeps names collision-free when you mount several bridges into one server.
- **Annotations are inferred from the HTTP method.** `GET` operations are marked read-only, `DELETE` operations destructive — hints agents and gateways can use for safety.
- **SSRF is blocked by default.** The provider refuses spec URLs and base URLs that resolve to private or loopback addresses, so a malicious spec can't point your bridge at internal metadata endpoints.

The full option set — `base_url` overrides, tags, and dict-based specs — is documented in the [advanced MCP server patterns](../../mcp/server/advanced-patterns.md) guide.

## Layering auth and rate limits on your MCP for an existing API

Generating tools is only half the job. Exposing an internal API to autonomous agents without access control is how you get a bad afternoon. Because `OpenAPIProvider` registers ordinary tools on a normal `MCPServer`, every server-wide protection Promptise offers applies to the generated tools automatically — you don't configure anything per endpoint.

Add authentication and a rate limit before you register the spec:

```python
import os

from promptise.mcp.server import (
    MCPServer,
    OpenAPIProvider,
    AuthMiddleware,
    JWTAuth,
    RateLimitMiddleware,
)

server = MCPServer(name="petstore-bridge", version="1.0.0")

# Every request must carry a valid JWT; the tenant is read from the "org" claim.
server.add_middleware(AuthMiddleware(JWTAuth(secret=os.environ["JWT_SECRET"]), tenant_claim="org"))

# Cap each client to 100 calls/min, counted per tool so one hot endpoint
# can't drain the budget for the rest.
server.add_middleware(RateLimitMiddleware(rate_per_minute=100, per_tool=True))

provider = OpenAPIProvider(
    "https://petstore3.swagger.io/api/v3/openapi.json",
    prefix="petstore_",
    include={"getPetById", "findPetsByStatus"},
)
provider.register(server)

server.run(transport="http", host="127.0.0.1", port=8080)
```

Now the whole bridge sits behind JWT authentication and a token-bucket rate limiter. Because Promptise is multi-tenant aware, the rate-limit buckets are qualified by the `org` claim, so one tenant's traffic can't exhaust another tenant's quota. For per-tool role checks and approval gates on the higher-risk operations, and for hardening the deployment end to end, follow the [production MCP servers](../../guides/production-mcp-servers.md) guide — it walks through auth providers, guards, audit logging, and health checks for exactly this kind of gateway.

## Filtering which endpoints agents can see

You almost never want to expose an entire API surface to an agent. `OpenAPIProvider` gives you two allow/deny controls that operate on operation IDs:

```python
# Allow-list: only these operations become tools.
provider = OpenAPIProvider(spec, include={"getUser", "listUsers"})

# Deny-list: everything except these.
provider = OpenAPIProvider(spec, exclude={"deleteUser", "resetDatabase"})
```

Start with `include` and add operations deliberately — an allow-list is far safer than trying to enumerate every dangerous endpoint in an `exclude` set. Pair the filter with the read-only/destructive annotations the provider infers, and you have a defensible boundary: agents see a curated subset of your API, each call is authenticated, and destructive operations are clearly flagged.

## Test the generated tools without a network

Before you serve anything, verify the bridge in-process. Promptise's `TestClient` runs the full server pipeline — validation, middleware, and the handler — with no sockets involved. For a live external API you'd typically stub the spec with a dict, but the call pattern is identical to any other MCP tool:

```python
from promptise.mcp.server import TestClient

result = await TestClient(server).call_tool(
    "petstore_getPetById", {"petId": 1}
)
print(result)
```

This is the same testing approach used for hand-written tools, so an OpenAPI-generated bridge fits the exact workflow described in [How to Build an MCP Server in Python](mcp-server-python.md) and in the [building servers](../../mcp/server/building-servers.md) reference. You write no schema by hand, yet you get the same validation and in-process test coverage.

## When a hand-written MCP server is the better fit

`OpenAPIProvider` is the right tool when you have a stable REST surface you can't afford to rewrite and want agent access quickly. It is not always the best choice:

- **Coarse or chatty endpoints.** If an agent needs three sequential calls to accomplish one task, a purpose-built tool that composes those calls into a single operation gives the model a cleaner interface than three raw endpoints.
- **Rich domain logic in the tool layer.** When you want elicitation, streaming progress, background jobs, or dependency injection, hand-write the tool with `@server.tool()` — the generated proxy is intentionally a thin HTTP passthrough.
- **No spec, or an inaccurate one.** The provider is only as good as your OpenAPI document. If the spec drifts from the real API, generated tools will drift too. Fix the spec first, or wrap the endpoints by hand.

A common pattern is to do both: generate the bulk of the surface from the spec, then hand-write a handful of high-value composite tools alongside it in the same server.

## Frequently asked questions

### Does OpenAPIProvider support Swagger 2.0 as well as OpenAPI 3.x?

Yes. The provider parses both Swagger 2.x and OpenAPI 3.x documents, including the older `host` plus `basePath` style for computing the target base URL. You can also override the base URL explicitly with the `base_url` argument when the spec's declared server isn't reachable from where your bridge runs.

### How does the generated MCP server authenticate to my upstream API?

Pass an `auth_header` tuple such as `("Authorization", "Bearer <token>")` to `OpenAPIProvider`. That header is attached to every outgoing HTTP request the generated tools make. This is separate from the MCP-side `AuthMiddleware` that authenticates agents calling your bridge — one secures the upstream hop, the other secures the agent-facing hop.

### Can I expose only some endpoints to agents?

Yes, with the `include` and `exclude` arguments, both keyed on operation IDs. Prefer an `include` allow-list so new or dangerous endpoints don't leak into your tool list automatically as the spec grows. Combine it with server-wide auth and rate-limit middleware for a defensible boundary.

## Next steps

Point `OpenAPIProvider` at your spec and expose your existing API to agents — no rewrite required. Follow the [Quick Start](../../getting-started/quickstart.md) to get Promptise installed, then use the [advanced MCP server patterns](../../mcp/server/advanced-patterns.md) guide to add filtering, base-URL overrides, and per-tool guards to your new bridge.
