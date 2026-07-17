---
title: "How to Roll Out a Breaking MCP Tool Change Safely"
description: "You need to change a tool's schema without a flag-day migration across every connected client. Because most MCP servers expose one definition per tool name…"
keywords: "roll out breaking mcp tool change, mcp tool deprecation, side-by-side tool versions, gradual client migration mcp, search@1.0 mcp"
date: 2026-07-16
slug: roll-out-breaking-mcp-tool-change
categories:
  - MCP
---

# How to Roll Out a Breaking MCP Tool Change Safely

To roll out a breaking MCP tool change safely, you publish the new schema as a *new version* beside the old one and let each client migrate on its own clock — instead of editing the live tool in place and forcing every connected agent to move the instant you deploy. That second path is the default one, and it is why a one-line schema edit so often turns into an incident. An MCP tool's `input_schema` is a published contract: agents discovered it, built function calls to match, and will keep sending v1-shaped arguments until *they* change. Add a required field, rename a property, or tighten a type, and every in-flight caller starts getting rejected before your handler even runs.

This is a walkthrough for doing it the safe way. We use Promptise Foundry's `VersionedToolRegistry` to serve `search@2.0` beside `search@1.0`, keep `search` pointing at the latest, and deprecate the old contract on a timeline your clients control — not on your deploy schedule.

## Why one schema edit is normally a flag-day migration

The trap is that a tool's schema is a *distributed* dependency, not a local one. On a normal server, one tool name maps to exactly one definition. When you want to add a `filters` argument to `search`, the obvious move is to edit the function:

```python
# Before — the shipped contract
async def search(query: str) -> list[dict]: ...

# After — one required argument added, and every v1 caller is now broken
async def search(query: str, filters: dict) -> list[dict]: ...
```

The moment that deploys, `filters` is required. Every agent that learned the v1 schema is still sending `{"query": "..."}`, validation rejects the call, and — because agents retry and reformulate — a single rejected call often fans out into a burst of them, each one burning tokens against a contract that no longer exists. Nothing crashed on your side. You changed the terms out from under callers who never agreed to them.

The only way this is *not* an outage is if v1 can keep answering while callers migrate. That is exactly what "just ship v2" assumes and what one-definition-per-name cannot give you. Without side-by-side serving, every breaking change is all-or-nothing: a flag day where all clients cut over at once or something breaks. Rolling out safely means removing that constraint before you touch the schema.

## What other frameworks do today

It is worth being precise here, because the gap is not that other frameworks are careless — it is a difference in where the tool boundary lives.

In **LangChain**, **CrewAI**, and **AutoGen**, tools are ordinary in-process Python callables: a `@tool`-decorated function, a `BaseTool` subclass, a `FunctionTool`. The agent imports them and calls them directly inside its own process. There is no wire protocol between caller and tool, so there is nothing to version-negotiate — change the signature and the calling code either matches it or it does not, and you find out at your next deploy. That is not a defect; it is what in-process functions are. It does mean these frameworks give you no protocol-level way to keep an old tool shape reachable for callers that already bound to it. The moment any of them consume a tool *over MCP* instead (LangChain via `langchain-mcp-adapters`, AutoGen via `mcp_server_tools`), the versioning question doesn't vanish — it moves to the MCP server, which is where it belongs.

On the server side, **FastMCP** registers each tool by name: `@mcp.tool()` maps one name to one function, and the base MCP spec lists tools by unique name with no per-tool version field. You can absolutely emulate versioning by registering two separately named functions — `search_v1` and `search_v2` — and that works. What FastMCP has no first-class primitive for is serving `search` as an alias that always points at the latest *alongside* a pinned `search@1.0`, with semantic-version resolution deciding which one "latest" means. You'd hand-roll the alias, own the "which version is newest" logic, and keep the two names from drifting yourself. That is the exact delta: the capability is reachable by convention, but it is not structural. For the wider stack comparison, see [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md).

Promptise's contribution is to make that convention a real registry primitive — latest-alias-plus-pinned-versions as a first-class thing — so you don't rebuild it per project. The rest of this post shows the rollout that primitive enables.

## Roll out v2 beside v1 with VersionedToolRegistry

`VersionedToolRegistry` is an overlay registry: you register multiple `ToolDef`s under one base name with explicit version strings, it resolves the bare name to the newest one, and it keeps every pinned `name@version` reachable. The following script is fully self-contained and runs as-is against the public `promptise.mcp.server` API — it models the whole rollout, from shipping v2 to reading a deprecation dashboard.

```python
import asyncio

from promptise.mcp.server import ToolDef, VersionedToolRegistry


# v1 — the shipped contract. Agents in production learned this shape.
async def search_v1(query: str) -> list[dict]:
    """Full-text search. v1: query only."""
    return [{"id": "doc-1", "query": query}]


# v2 — adds a required `index` and optional `filters`. Breaking: v1 payloads
# no longer validate against it, so it must NOT replace v1 in place.
async def search_v2(query: str, index: str, filters: dict | None = None) -> list[dict]:
    """Full-text search. v2: scoped to an index, with structured filters."""
    return [{"id": "doc-1", "query": query, "index": index, "filters": filters or {}}]


def build(handler, schema) -> ToolDef:
    return ToolDef(name="search", description=handler.__doc__, handler=handler, input_schema=schema)


v1 = build(search_v1, {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
})
v2 = build(search_v2, {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "index": {"type": "string"},
        "filters": {"type": "object"},
    },
    "required": ["query", "index"],
})

registry = VersionedToolRegistry()
registry.register("search", "1.0", v1)   # already in production
registry.register("search", "2.0", v2)   # the rollout — ships as the new latest


async def main() -> None:
    # Deprecation dashboard: what is live, and which is "latest"?
    print("versioned         :", registry.has("search"))
    print("versions live     :", registry.list_versions("search"))
    print("latest -> v2      :", registry.get("search") is v2)
    print("search@1.0 -> v1  :", registry.get("search@1.0") is v1)

    # Existing v1 caller: untouched by the rollout.
    print("v1 caller         :", await registry.get("search@1.0").handler(query="invoices"))
    # Migrated caller: opts in to v2 when its team is ready.
    print("v2 caller         :", await registry.get("search").handler(query="invoices", index="finance"))

    # A shipped contract can never be clobbered by accident.
    try:
        registry.register("search", "1.0", v2)
    except ValueError as exc:
        print("clobber blocked   :", exc)


asyncio.run(main())
```

Running it prints exactly the coexistence a safe rollout depends on:

```text
versioned         : True
versions live     : ['1.0', '2.0']
latest -> v2      : True
search@1.0 -> v1  : True
v1 caller         : [{'id': 'doc-1', 'query': 'invoices'}]
v2 caller         : [{'id': 'doc-1', 'query': 'invoices', 'index': 'finance', 'filters': {}}]
clobber blocked   : Tool 'search' version '1.0' is already registered
```

Two properties do the load-bearing work. First, `search` resolves by *semantic* version comparison, not registration order or string sorting — so `2.0` beats `1.0`, and `1.10` correctly beats `1.9`. New agents discovering `search` pick up v2 automatically; agents pinned to `search@1.0` keep hitting the old contract. Second, `register()` refuses to overwrite an existing `base_name` + `version` pair — the `clobber blocked` line — so you physically cannot replace a live v1 contract by accident. The rollout is additive by construction. The same overlay approach is documented alongside transforms, composition, and OpenAPI bridging in the [Advanced Patterns](../../mcp/server/advanced-patterns.md) guide.

## Deprecate on a timeline your clients control

Serving both versions is the mechanism. A safe *rollout* is the discipline you wrap around it. Here is the sequence that turns a breaking change into a zero-downtime migration:

- **Never edit a shipped schema in place.** Adding a required field, renaming a property, narrowing a type, or removing a property is breaking. Register a new `ToolDef` under a new version instead of mutating the old one — and let the registry's clobber protection enforce it for you.
- **Ship v2 as the new latest, keep v1 live.** New discovery resolves `search` → v2 automatically; agents pinned to `search@1.0` keep working untouched. You have now decoupled "deployed" from "migrated," which is the entire point.
- **Announce a deprecation window, then watch who still pins v1.** `registry.list_versions("search")` gives you the live inventory for a deprecation dashboard; pair it with your server's [audit trail](../../mcp/server/production-features.md) to see which callers still hit `search@1.0`. Migration becomes a number you can watch trend toward zero.
- **Retire v1 only when that number is zero** (or your published window closes). Removing a version is a deliberate act at the end of the timeline, not a side effect of your deploy. Because `search` is an alias, retiring v1 doesn't change what `search` means for anyone already tracking latest.
- **Reserve new versions for genuinely breaking changes.** A purely additive tweak — a new *optional* field with a safe default — doesn't need a new version at all; agents that ignore it are unaffected. Bump the version when, and only when, a v1-shaped argument payload would stop validating.

This is the same failure-isolation mindset that keeps the rest of a production MCP server healthy: contain change so one moving part can't take down the whole. For the deeper "why the schema *is* the contract" story behind this, see [Why a Small MCP Tool Change Broke Every Connected Agent](version-mcp-tools-without-breaking-clients.md).

## Frequently asked questions

### Does the MCP protocol version tools for me?

No. The base MCP spec lists tools by unique name and has no per-tool version field — a session negotiates one protocol version, not one per tool. The `search@1.0` pin is a naming convention layered on top of that flat list, resolved by `VersionedToolRegistry`: `get("search@1.0")` splits on the last `@`, finds the `search` group, and returns the `1.0` definition, while `get("search")` returns that group's latest. You need a registry primitive to express latest-alias-plus-pinned-versions cleanly; the protocol won't hand it to you.

### What actually counts as a breaking change I need to version?

Anything that can make a previously valid call invalid: adding a required parameter, renaming a property, removing a property, tightening a type, or making an optional field required. Adding a new *optional* parameter with a safe default is backward compatible — callers that never send it are unaffected — so that alone does not warrant a new version. The reliable test: would a v1-shaped argument payload still validate against the new schema? If not, roll it out as a new version.

### Can I do this with FastMCP or an in-process framework like LangChain?

You can approximate it. With FastMCP, register two separately named tools (`search_v1`, `search_v2`) and maintain the "which is latest" pointer yourself — it works, but the alias and semver-resolution logic are yours to own. With LangChain, CrewAI, or AutoGen, tools are in-process functions with no wire contract to version, so the question only becomes real once those agents call tools over MCP. Promptise's difference is that latest-alias-plus-pinned-version is a first-class registry primitive, not something you hand-roll per project.

### How does `latest` decide which version wins?

By semantic numeric comparison of the version strings. Each version is split on `.` into integer segments, so `1.10` correctly ranks above `1.9` and `2.0` above `1.0` — registration order and string sorting don't matter. `registry.get("search")` returns the highest; `registry.list_versions("search")` returns all of them in that same ascending order, which is exactly what you want for a migration audit.

## Next steps

Start by auditing your own server for the latent trap: any tool whose schema you have edited in place since it shipped is a break waiting for the next agent that pinned the old shape. Move those changes behind versions, register v2 as the new latest with `VersionedToolRegistry`, keep v1 live, and retire it only when your callers have migrated. Ship your breaking change safely by following the versioning steps in the [Advanced Patterns](../../mcp/server/advanced-patterns.md) guide, fit versioning into the rest of your hardening work with the [Production Features](../../mcp/server/production-features.md) overview, and if you're weighing your stack, read [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md) to see where first-class tool versioning changes the calculus.
