---
title: "How to Compose Multiple MCP Servers Into One Gateway"
description: "You have three MCP servers and want agents to see one clean toolbelt. FastMCP can mount servers with prefixes, but you still don't get per-client visibility…"
keywords: "compose multiple mcp servers, mount mcp servers, namespace mcp tools, mcp gateway, mcp server composition"
date: 2026-07-16
slug: compose-multiple-mcp-servers
categories:
  - MCP
---

# How to Compose Multiple MCP Servers Into One Gateway

To compose multiple MCP servers into one gateway, you mount each team's server behind a namespace prefix so an agent connects to a single endpoint and discovers one clean toolbelt — `pay_charge`, `usr_get_user`, `rpt_revenue` — instead of juggling three URLs, three auth handshakes, and three tool lists it has to deduplicate itself. That much is table stakes; several frameworks can prefix-and-merge. The part that actually decides whether a gateway survives contact with production is what happens *at the seam*: can you filter what each client sees across the composed surface based on who is asking, and can two mounted servers ship different versions of the same tool without a name collision?

<!-- more -->

This post builds a real gateway from three separate servers with `mount()`, then layers the governance that a merged surface needs — per-client visibility, tag filtering, and versioned coexistence — and is honest about where other frameworks already draw that line and where Promptise Foundry makes it structural.

## Three servers, one endpoint, one problem

Start with the situation everyone actually has. Payments owns `charge` and `refund`. The users team owns `get_user`. Analytics owns `revenue`. Each is its own MCP server with its own deploy cadence, its own on-call rotation, and — critically — its own name for `get` or `export` or `status`. Point one agent at all three and you get three connections to manage, three tool lists that may collide on a bare name, and no single place to decide "this caller should not see the analytics tools at all."

The naive fix is to merge everything into one giant server. That trades three deploy cadences for one, couples unrelated teams into a single blast radius, and still leaves you hand-resolving name clashes. Composition is the opposite move: keep the servers independent, and stand a thin gateway in front that mounts each one behind a namespace. The teams stay decoupled; the agent sees one surface.

## Mount three servers into one gateway

`mount(parent, child, prefix=...)` copies a child server's tools, resources, prompts, and exception handlers into a parent, prefixing every tool name with `{prefix}_`. It preserves each tool's full definition — guards, roles, auth flags, approval requirements — so nothing about a tool's security posture is silently dropped when it crosses into the gateway. The following script is self-contained and runs as-is against the public `promptise.mcp.server` API:

```python
import asyncio

from promptise.mcp.server import MCPServer, TestClient, mount

# Team 1 — payments
payments = MCPServer(name="payments")


@payments.tool(tags=["public"])
async def charge(customer_id: str, amount_cents: int) -> dict:
    """Charge a customer's saved payment method."""
    return {"customer": customer_id, "charged": amount_cents}


@payments.tool(tags=["admin"])
async def refund(charge_id: str) -> dict:
    """Refund a charge."""
    return {"refunded": charge_id}


# Team 2 — users
users = MCPServer(name="users")


@users.tool(tags=["public"])
async def get_user(user_id: str) -> dict:
    """Look up a user profile."""
    return {"id": user_id, "name": "Ada Lovelace"}


# Team 3 — analytics
analytics = MCPServer(name="analytics")


@analytics.tool(tags=["admin"])
async def revenue(month: str) -> dict:
    """Report revenue for a month."""
    return {"month": month, "usd": 42_000}


# Compose all three into ONE gateway, each behind its own namespace.
gateway = MCPServer(name="api-gateway", version="1.0.0")
mount(gateway, payments, prefix="pay", tags=["billing"])
mount(gateway, users, prefix="usr", tags=["directory"])
mount(gateway, analytics, prefix="rpt", tags=["reporting"])


async def main() -> None:
    client = TestClient(gateway)

    # One clean, namespaced toolbelt discovered from a single endpoint.
    tools = await client.list_tools()
    print("gateway toolbelt:", sorted(t.name for t in tools))

    # Every tool stays callable through the gateway — no double hop.
    result = await client.call_tool("pay_charge", {"customer_id": "c-1", "amount_cents": 500})
    print("pay_charge      :", result[0].text)


asyncio.run(main())
```

Run it and the three servers collapse into one advertised surface:

```text
gateway toolbelt: ['pay_charge', 'pay_refund', 'rpt_revenue', 'usr_get_user']
pay_charge      : {"customer": "c-1", "charged": 500}
```

`refund` from payments and a future `refund` from a returns server can now coexist as `pay_refund` and `ret_refund` — the prefix is the namespace, so bare-name collisions between teams simply stop happening. Swap the `TestClient` for `gateway.run(transport="http", port=8080)` and the exact same composed surface is served over the wire to any MCP client. That is the whole composition story, and it is genuinely simple.

## What other frameworks do today

It is worth being precise here, because the interesting gap is not "who can prefix tool names" — several tools can — but what governance you can attach *across the merged surface*.

**FastMCP has real composition.** `mcp.mount(subserver, prefix="sub")` gives you live composition where calls route to the mounted server, and `import_server()` gives you a one-time static copy; both prefix tool names. So namespacing across composed servers is not a gap in FastMCP — it ships. FastMCP also has genuine tag support: you tag tools and set server-level `include_tags` / `exclude_tags` to control which tools are exposed, and its tool-transformation API (`Tool.from_tool(...)`) can rename a tool, hide or rename arguments, and rewrite descriptions. Those are real, useful primitives, and any honest comparison has to say so.

The precise delta is two-fold. First, FastMCP's tag exposure is **static and server-wide** — `include_tags` / `exclude_tags` are set when you build the server and apply the same way to everyone. What it does not give you as a first-class primitive is a **per-request, caller-aware** filter: a predicate that runs at discovery time against *this* client's roles and decides, on that request, whether `pay_refund` is even in the list. Second, FastMCP has no built-in **latest-alias-plus-pinned-version** primitive across the composed surface — you can register `search_v1` and `search_v2` as separate names, but resolving `search` to the newest while keeping `search@1.0` reachable is convention you hand-roll, as the [tool-versioning walkthrough](version-mcp-tools-without-breaking-clients.md) covers in depth.

**Agent frameworks don't play in this layer at all.** LangChain, CrewAI, and AutoGen consume MCP tools as *clients* — they connect to a server and pull its tools into an agent. They do not compose MCP *servers* into a gateway, because the gateway is a server-side concern and these frameworks sit on the other side of the wire. The composition question only becomes real once you are the one publishing the tools, which is exactly where Promptise's server SDK lives. For the broader stack-by-stack breakdown, see [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md).

So the framing is not "competitors can't compose." It is: composition plus a per-client, role-aware discovery filter plus versioned coexistence, all applied uniformly over the merged surface, is where Promptise makes the capability structural rather than something you assemble by hand.

## Govern the composed surface: visibility, tags, and versions

A gateway that shows every tool to every caller is a liability — the whole point of a single endpoint is that you can also make a single decision about who sees what. Promptise's transforms are discovery-time filters: each one takes the list of tool definitions and returns a filtered or rewritten list, and because they compose in order, you chain them into the exact view a given client should get. `TagFilterTransform` keeps only tools carrying a required tag; `VisibilityTransform` drops tools whose predicate fires against the calling context; and `VersionedToolRegistry` lets two mounted servers advertise `pay_charge@1.0` and `pay_charge@2.0` side by side. This second script is also fully runnable:

```python
import asyncio

from promptise.mcp.server import (
    ToolDef,
    RequestContext,
    TagFilterTransform,
    VisibilityTransform,
    VersionedToolRegistry,
)


async def charge(customer_id: str, amount_cents: int) -> dict:
    return {"customer": customer_id, "charged": amount_cents}


async def refund(charge_id: str) -> dict:
    return {"refunded": charge_id}


# The tools the gateway advertises, already namespaced by mount().
advertised = [
    ToolDef(name="pay_charge", description="Charge a customer.", handler=charge,
            input_schema={"type": "object"}, tags=["public", "billing"]),
    ToolDef(name="pay_refund", description="Refund a charge.", handler=refund,
            input_schema={"type": "object"}, tags=["admin", "billing"]),
    ToolDef(name="usr_get_user", description="Look up a user.", handler=charge,
            input_schema={"type": "object"}, tags=["public", "directory"]),
]


def surface_for(roles: set[str]) -> list[str]:
    """The discovery-time view one client gets across the composed surface."""
    ctx = RequestContext(server_name="api-gateway", tool_name="list_tools")
    ctx.state["roles"] = roles
    tools = TagFilterTransform(required_tags={"public", "admin"}).apply(advertised, ctx)
    tools = VisibilityTransform(
        hidden={"pay_refund": lambda c: "admin" not in (c.state.get("roles", set()) if c else set())}
    ).apply(tools, ctx)
    return sorted(t.name for t in tools)


# Two versions of the same gateway tool coexist under one base name.
vr = VersionedToolRegistry()
vr.register("pay_charge", "1.0", advertised[0])
vr.register("pay_charge", "2.0", advertised[0])


async def main() -> None:
    print("anonymous sees:", surface_for(set()))
    print("admin sees    :", surface_for({"admin"}))
    print("versions       :", vr.list_versions("pay_charge"))
    print("pinned v1 name :", vr.get("pay_charge@1.0").name)


asyncio.run(main())
```

The output shows the same gateway presenting two different surfaces to two different callers, and two tool versions living side by side:

```text
anonymous sees: ['pay_charge', 'usr_get_user']
admin sees    : ['pay_charge', 'pay_refund', 'usr_get_user']
versions       : ['1.0', '2.0']
pinned v1 name : pay_charge
```

The anonymous caller never sees `pay_refund`; the admin does. `pay_charge` resolves to the latest version while `pay_charge@1.0` stays pinned for callers that bound to the old contract. Note the honest boundary: transforms are a *discovery-time* control — they shape what `list_tools` returns, so an agent that never learned a hidden tool never calls it. They are not an authorization boundary. A caller who already has a tool name cached can still attempt the call, which is why destructive tools carry a real guard (`HasRole`, `RequireAuth`) that runs at call time regardless of what the discovery filter did. Use transforms to keep the toolbelt clean and relevant; use guards to make access decisions stick. The [Advanced Patterns](../../mcp/server/advanced-patterns.md) guide documents the full transform, composition, and versioning API in one place.

## Why a governed gateway beats one big server

Composition is not just a naming convenience; it is a failure-isolation strategy, and that is the reason to prefer it over merging everything into a single server.

- **Independent deploys, one surface.** Each team ships its own server on its own schedule. The gateway re-mounts them; agents keep seeing one stable endpoint. You decouple "who owns the tool" from "what the agent connects to."
- **Namespace isolation kills bare-name collisions.** Two teams can both ship a `status` or an `export` tool. Under the gateway they are `pay_status` and `rpt_export` — no coordination meeting required to avoid a clash.
- **One place to govern.** The per-client visibility, tag filtering, and versioning all attach at the gateway, so the "who sees what" decision lives in exactly one layer instead of being re-implemented in every downstream server.
- **The rest of the hardening stack composes too.** A gateway is still a normal `MCPServer`, so it carries the same caching, circuit breakers, per-tool rate limits, and audit logging as any other server — the [Production Features](../../mcp/server/production-features.md) overview walks through fitting those around a composed surface.

The mental model that keeps a big system healthy is the same one that keeps this gateway healthy: isolate each moving part so one server's outage, one team's schema change, or one over-broad tool cannot take down the whole surface. Composition gives you the seams; the transforms and versioned registry let you govern across them.

## Frequently asked questions

### Does composing MCP servers add a network hop per tool call?

No. `mount()` copies the child server's tool definitions into the parent's registry, so the gateway invokes the handler directly — there is no proxy round-trip to a separately running child process. The gateway is one server that happens to have been assembled from several. If you deliberately want live routing to independently deployed servers, that is a different topology (a reverse proxy in front of N servers), with different trade-offs.

### Can two mounted servers expose a tool with the same name?

Yes, as long as their prefixes differ. `mount(gateway, payments, prefix="pay")` and `mount(gateway, returns, prefix="ret")` turn two `refund` tools into `pay_refund` and `ret_refund`. If you need two versions of the *same* namespaced tool to coexist — `pay_charge@1.0` and `pay_charge@2.0` — that is what `VersionedToolRegistry` is for, with `pay_charge` resolving to the latest and the pinned name staying reachable.

### Do transforms make a tool unreachable, or just hidden?

Hidden. A transform changes what `list_tools` advertises at discovery time, which is enough to keep an agent from ever learning about a tool it should not use. It is not a security boundary: a client with a cached tool name can still attempt the call. Attach a per-tool guard (`HasRole`, `RequireAuth`) when you need the access decision enforced at call time, and treat transforms as the layer that keeps each client's toolbelt clean and relevant.

### How is this different from FastMCP's `mount()` and tag filtering?

FastMCP genuinely mounts sub-servers with prefixes and supports static, server-wide tag exposure via `include_tags` / `exclude_tags`, plus per-tool transformation with `Tool.from_tool(...)`. The difference is that Promptise makes two things first-class across the composed surface: a per-request, caller-aware `VisibilityTransform` that decides what *this* client sees on *this* request, and a `VersionedToolRegistry` where a latest alias and pinned `name@version` versions coexist without you hand-rolling the resolution logic. It is a difference of structure, not of whether basic composition is possible.

### Can I still add auth, caching, and rate limits to a gateway?

Yes. The gateway is an ordinary `MCPServer`, so `AuthMiddleware`, `CacheMiddleware`, `RateLimitMiddleware`, circuit breakers, and audit logging attach to it exactly as they would to any single server. Guards and auth flags declared on a child tool survive the mount, because `mount()` copies the full tool definition rather than just its name and schema.

## Next steps

Start by listing every MCP endpoint an agent currently connects to; each one is a candidate to mount behind a single gateway. Stand up a thin `MCPServer`, `mount()` your existing servers with clear prefixes, and confirm the merged toolbelt with `TestClient.list_tools()`. Then layer governance: `TagFilterTransform` to scope the surface, `VisibilityTransform` for per-client role-aware discovery, and `VersionedToolRegistry` where two servers need overlapping tool contracts. Follow the composition walkthrough in the [Advanced Patterns](../../mcp/server/advanced-patterns.md) guide to build your gateway, fit it into the rest of your hardening work with the [Production Features](../../mcp/server/production-features.md) overview, and if you are weighing frameworks, read [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md) to see where a governed composed surface changes the calculus.
