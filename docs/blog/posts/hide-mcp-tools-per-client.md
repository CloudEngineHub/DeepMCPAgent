---
title: "Show Different MCP Tools to Different Clients"
description: "A public MCP server shouldn't advertise admin tools to every caller. FastMCP gives you static tag filtering and tool transformation, but a different tool…"
keywords: "hide mcp tools per client, mcp tool visibility, role-based mcp tools, per-client tool list, visibility transform mcp"
date: 2026-07-16
slug: hide-mcp-tools-per-client
categories:
  - MCP
---

# Show Different MCP Tools to Different Clients

To hide MCP tools per client, you stop shipping one flat tool list to everyone and instead tailor the list each caller discovers at `list_tools` time — the anonymous integration sees `search_orders` and `get_order`, while an authenticated operator additionally sees `issue_refund` and `delete_customer`. A public MCP server that advertises its destructive admin tools to every caller has already lost the argument: even if a guard stops the call, you have leaked the shape of your privileged surface to anyone who runs discovery, and you have handed a confused agent tools it will eventually try to use. The clean fix is to make the *advertised* toolset a function of *who is asking*.

<!-- more -->

This post builds that per-client view with three primitives — `TagFilterTransform`, `VisibilityTransform`, and per-tool guards (`HasRole` / `HasScope`) — shows the exact enforcement boundary between "hidden" and "blocked," and is precise about where other frameworks already draw part of this line and where Promptise Foundry makes per-caller visibility a first-class layer.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## The problem: one public server, two very different callers

Picture an orders API exposed over MCP. It has four tools. Two are read-only and safe for any integration to see: `search_orders`, `get_order`. Two are privileged and irreversible: `issue_refund` moves money, `delete_customer` erases a record. Every one of your callers connects to the same endpoint — a partner's read-only bot, your internal ops agent, a support copilot.

If `list_tools` returns all four tools to all three callers, you have two distinct problems, and they are not the same problem:

- **A disclosure problem.** The partner bot now knows `delete_customer` exists, what arguments it takes, and roughly what it does. That is reconnaissance you handed out for free at discovery time.
- **A correctness problem.** An LLM that can *see* `issue_refund` will, under the wrong prompt, *try* `issue_refund`. Tools an agent should never touch are best kept out of its context entirely, not dangled in front of it and then rejected after the fact.

The naive fix — spin up a second "admin" server on a second port and route privileged callers there — doubles your deploy surface, splits your audit trail, and still shows the same list to everyone who reaches a given port. What you actually want is one server that presents a *different* tool list depending on the calling identity, decided fresh on each discovery request.

## Tailor the tool list per caller at discovery time

A Promptise transform is a pure function over the tool list: `apply(tools, ctx) -> tools`. It takes the definitions the server would advertise plus the current request context, and returns the filtered or rewritten list this caller should see. Because they compose in order — each transform sees the previous one's output — you chain them into the exact discovery-time view a given caller gets. `TagFilterTransform` keeps only tools carrying an allowed audience tag; `VisibilityTransform` drops named tools whose predicate fires against the calling context. The following script is self-contained and runs as-is against the public `promptise.mcp.server` API:

```python
import asyncio

from promptise.mcp.server import (
    ToolDef,
    RequestContext,
    HasRole,
    TagFilterTransform,
    VisibilityTransform,
)


async def _noop() -> dict:
    return {}


# The full toolbelt the server implements, each tool tagged by audience.
catalog = [
    ToolDef(name="search_orders", description="Search a customer's orders.",
            handler=_noop, input_schema={"type": "object"}, tags=["public"]),
    ToolDef(name="get_order", description="Fetch one order by id.",
            handler=_noop, input_schema={"type": "object"}, tags=["public"]),
    ToolDef(name="issue_refund", description="Refund an order.",
            handler=_noop, input_schema={"type": "object"}, tags=["admin"],
            auth=True, guards=[HasRole("admin")]),
    ToolDef(name="delete_customer", description="Erase a customer record.",
            handler=_noop, input_schema={"type": "object"}, tags=["admin"],
            auth=True, guards=[HasRole("admin")]),
]


def surface_for(roles: set[str]) -> list[str]:
    """The tool list ONE caller discovers, tailored at list_tools time."""
    ctx = RequestContext(server_name="orders-api", tool_name="list_tools")
    ctx.state["roles"] = roles

    # 1. Tag gate: only tools whose audience this caller is allowed to see.
    allowed_tags = {"public", "admin"} if "admin" in roles else {"public"}
    tools = TagFilterTransform(required_tags=allowed_tags).apply(catalog, ctx)

    # 2. Per-tool visibility predicate: belt-and-suspenders for named tools.
    tools = VisibilityTransform(hidden={
        "issue_refund": lambda c: "admin" not in c.state.get("roles", set()),
        "delete_customer": lambda c: "admin" not in c.state.get("roles", set()),
    }).apply(tools, ctx)

    return sorted(t.name for t in tools)


async def can_call(tool_name: str, roles: set[str]) -> bool:
    """Discovery hides a tool; the guard is what actually STOPS the call."""
    tool = next(t for t in catalog if t.name == tool_name)
    ctx = RequestContext(server_name="orders-api", tool_name=tool_name)
    ctx.state["roles"] = roles
    for guard in tool.guards:
        if not await guard.check(ctx):
            return False
    return True


async def main() -> None:
    print("anonymous discovers:", surface_for(set()))
    print("admin discovers    :", surface_for({"admin"}))
    print("anon call refund?  :", await can_call("issue_refund", set()))
    print("admin call refund? :", await can_call("issue_refund", {"admin"}))


asyncio.run(main())
```

Run it and the same server presents two different surfaces to two different callers:

```text
anonymous discovers: ['get_order', 'search_orders']
admin discovers    : ['delete_customer', 'get_order', 'issue_refund', 'search_orders']
anon call refund?  : False
admin call refund? : True
```

The anonymous caller never learns `issue_refund` or `delete_customer` exist. The admin sees the full toolbelt. Nothing about the tools changed — only the *view* did, and it was decided on this request against this caller's roles. Swap `ctx.state["roles"]` for `ctx.client.roles`, which `AuthMiddleware` populates from the verified JWT, and the same logic runs against a cryptographically authenticated identity instead of a test fixture.

Why two transforms and not one? `TagFilterTransform` is your coarse audience gate — tag whole groups of tools `public`, `admin`, `billing`, and admit a caller to the tags their role earns. `VisibilityTransform` is the surgical override for a specific named tool whose rule does not fit a tag bucket — "hide `export_pii` unless the caller is on the compliance team," expressed as an arbitrary predicate. They compose cleanly, so you reach for the tag gate first and the per-tool predicate only where you need it.

## Discovery hides, guards enforce

The most important line in that script is the honest one: `surface_for` and `can_call` are **different layers**, and you need both. This is the distinction that separates a clean toolbelt from an actual access-control decision.

A transform shapes what `list_tools` advertises. That is enough to stop a well-behaved agent from ever learning a tool exists — and keeping a dangerous tool out of an LLM's context is a real, valuable safety win. But it is **not a security boundary.** A caller who already cached `issue_refund` from a previous session, or who simply guesses the name, can still send the call. Hiding a tool does not make it unreachable; it makes it undiscovered.

That is why the privileged tools in the catalog carry `guards=[HasRole("admin")]`. The guard runs at call time, reads roles from the verified `ctx.client` (falling back to `ctx.state["roles"]`), and denies the call regardless of what discovery did. The `can_call` output makes the boundary concrete: the anonymous caller is refused `issue_refund` even though nothing stopped it from *attempting* the call. For OAuth2 deployments, swap `HasRole` for `HasScope("refunds:write")` and the same enforcement keys off the JWT `scope` claim instead of roles.

The mental model that keeps this correct:

- **Transforms** decide *what a caller sees* — a relevance and least-disclosure control at discovery time.
- **Guards** decide *what a caller may do* — the authorization boundary at call time.

Use transforms to keep every client's toolbelt clean and free of tools it has no business knowing about; use guards to make the access decision actually stick. The [Auth & Security](../../mcp/server/auth-security.md) guide covers how `AuthMiddleware`, roles, scopes, and the guard chain fit together end to end, including how the JWT payload populates `ctx.client`.

## What other frameworks do today

It is worth being precise here, because the interesting gap is not "who can filter tools at all" — the honest answer is that more than one framework can — but *whether the filter can vary per caller on each request without you writing the plumbing.*

**FastMCP has real, static filtering.** You can tag components and set server-level `include_tags` / `exclude_tags` to control which tools are exposed, and its tool-transformation API (`Tool.from_tool(...)`) can rename a tool, hide or rename its arguments, and rewrite its description. Those are genuine, useful primitives, and any fair comparison has to say so plainly. The precise delta is that these controls are **static and server-wide**: `include_tags` / `exclude_tags` are fixed when you build the server and apply the same way to every caller, and a transformed tool is transformed identically for everyone. FastMCP does not ship a first-class, per-request filter that runs at discovery time against *this* client's roles or scopes and returns a different list to a different caller. To get "anonymous sees two tools, admin sees four" you drop down and hand-roll your own `list_tools` middleware that inspects the request and rebuilds the list. That is exactly the plumbing Promptise's transform-plus-context model turns into a few composed lines. For the broader stack-by-stack breakdown, see [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md).

**Agent frameworks don't play in this layer at all.** LangChain, CrewAI, and AutoGen consume MCP tools as *clients* — they connect to a server and pull its tools into an agent's context. They expose whatever list the server hands them; per-client server-side visibility is not their job, because they sit on the other side of the wire. The decision about which caller sees which tools only exists once you are the one *publishing* the tools, which is precisely where Promptise's server SDK lives.

So the framing is not "competitors can't filter tools." It is: a per-request, caller-aware discovery filter — composed with a call-time guard so hidden and blocked are enforced independently — is a structural layer in Promptise rather than middleware you assemble and maintain yourself. The same structural-vs-hand-rolled distinction shows up in [tool versioning](version-mcp-tools-without-breaking-clients.md), where a latest alias and pinned `name@version` contracts coexist without you writing the resolution logic.

## Wiring it into a real server

In the runnable script the transforms operate on a `ToolDef` list directly, which is what makes it self-contained. In a live server the same three ingredients attach to real tools:

- **Tag every tool by audience.** `@server.tool(tags=["public"])` or `tags=["admin"]` on the decorator gives `TagFilterTransform` something to gate on. Choose tags that map to how you actually segment callers — `public` / `internal` / `admin`, or per-team buckets like `billing` and `support`.
- **Guard the privileged tools.** `@server.tool(auth=True, roles=["admin"])` or `guards=[HasScope("refunds:write")]` puts the enforcement decision on the tool itself, so it travels with the tool even if it is later mounted into a gateway or composed into a larger surface.
- **Compute the per-caller view at discovery time.** Run your `TagFilterTransform` and `VisibilityTransform` chain against the current request's roles or scopes to produce the list that caller should see, exactly as `surface_for` does. `VisibilityTransform` and `TagFilterTransform` are documented together with the full transform API in the [Advanced Patterns](../../mcp/server/advanced-patterns.md) guide.

Kept in that order, the design stays honest: tags and predicates decide relevance and disclosure; guards decide authorization. One caller's over-broad prompt can never reach a tool it was not shown *and* is not permitted to call.

## Frequently asked questions

### Does hiding a tool with a transform make it impossible to call?

No. A transform changes what `list_tools` advertises, which stops a well-behaved agent from ever learning the tool exists — a real safety and least-disclosure win. It is not a security boundary: a caller with a cached or guessed tool name can still attempt the call. Attach a per-tool guard (`HasRole`, `HasScope`, `RequireAuth`) so the access decision is enforced at call time regardless of what discovery showed, and treat the transform as the layer that keeps each client's toolbelt clean.

### How do I vary the list by an authenticated identity instead of a test value?

`AuthMiddleware` verifies the incoming JWT and populates `ctx.client` with the caller's `roles` and `scopes`. Your transform predicates read from there — `ctx.client.roles` — rather than the `ctx.state["roles"]` fixture used in the runnable example. The [Auth & Security](../../mcp/server/auth-security.md) guide walks through the token verification and claim mapping that fills `ctx.client`.

### When should I use `TagFilterTransform` versus `VisibilityTransform`?

Reach for `TagFilterTransform` when you can segment whole groups of tools by audience — tag them `public` / `admin` / `billing` and admit each caller to the tags their role earns. Reach for `VisibilityTransform` when one specific named tool has a rule that does not fit a tag bucket, since its predicate can be arbitrary logic against the request context. They compose, so a common pattern is a coarse tag gate followed by a couple of per-tool overrides.

### Can I use scopes instead of roles?

Yes. Swap `HasRole("admin")` for `HasScope("refunds:write")` on the guard, and gate your transform on `ctx.client.scopes`. Scopes come from the JWT `scope` claim and suit OAuth2 client-credentials deployments; roles suit human-team or API-key models. The two are independent, so you can mix them per tool.

### Does this work when I compose several servers into one gateway?

Yes. Guards and auth flags declared on a tool survive `mount()`, so a privileged tool stays guarded after it is namespaced into a gateway, and you run the same transform chain over the merged surface to produce each caller's view. Composition and per-client visibility are designed to layer together rather than compete.

## Next steps

Restrict tools per client by starting from the tool you would least like an anonymous caller to see: tag it `admin`, add a `HasRole` or `HasScope` guard, and confirm it disappears from the discovered list for a non-privileged context while the guard still refuses the call. Then generalize — tag the rest of your tools by audience, build the `TagFilterTransform` + `VisibilityTransform` chain that maps roles to allowed tags, and verify each caller's surface with the runnable pattern above. Follow the visibility-transform walkthrough in the [Advanced Patterns](../../mcp/server/advanced-patterns.md) guide, wire it to real identities with [Auth & Security](../../mcp/server/auth-security.md), and if you are weighing frameworks, read [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md) to see where a per-caller discovery layer changes the calculus.
