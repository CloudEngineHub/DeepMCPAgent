---
title: "Why a Small MCP Tool Change Broke Every Connected Agent"
description: "A single renamed field in an MCP tool silently breaks every agent already calling it, because the schema is the contract. Agent-framework tools are…"
keywords: "version mcp tools without breaking clients, mcp tool versioning, breaking change mcp schema, backward compatible mcp tools, versioned tool registry"
date: 2026-07-16
slug: version-mcp-tools-without-breaking-clients
categories:
  - MCP
---

# Why a Small MCP Tool Change Broke Every Connected Agent

To version MCP tools without breaking clients, you have to treat a tool's schema as a published API contract — because that is exactly what every connected agent already assumes it is. An agent does not read your source code. It reads the `input_schema` your server advertises, builds a function call to match, and trusts that the shape it saw at discovery time is the shape you will accept at call time. Rename one field from `customer` to `customer_id`, tighten `filters` from optional to required, or drop a property some prompt still references, and every agent already in flight starts emitting arguments your handler rejects. Nothing crashed on your side. You just changed the contract out from under callers who never agreed to the new terms.

<!-- more -->

This post walks through why that failure is structural, what other frameworks do about it today, and how Promptise Foundry's `VersionedToolRegistry` lets `search` and `search@1.0` live side by side so a schema change stops being an outage.

## The schema is the contract, and one field breaks it

Here is the exact sequence that turns a one-line change into an incident. You ship `search(query: str)`. Three agent teams wire `search` into their prompts and go to production. Weeks later you add filtering, and because it feels like a small, additive improvement, you edit the existing function in place:

```python
# Before
async def search(query: str) -> list[dict]: ...

# After — looks harmless, is not
async def search(query: str, filters: dict) -> list[dict]: ...
```

The moment you deploy, `filters` is a required parameter. Every agent that learned the v1 schema is still sending `{"query": "..."}` with no `filters` key. Validation rejects the call before your handler ever runs. From the agent's side this reads as a tool that suddenly, inexplicably, refuses valid-looking input — and because agents retry and reformulate, a rejected call often turns into a burst of rejected calls, each one burning tokens and latency on a contract that no longer exists.

The root cause is that a tool's schema is a distributed dependency, not a local one. Once agents discover it, changing it is a breaking change by definition, and "just deploy v2" only works if v1 can keep answering while callers migrate at their own pace. Without that, the safe rename does not exist — every schema edit is all-or-nothing.

## What other frameworks do today

It is worth being precise about where the gap actually is, because it is not that competitors are careless.

In **LangChain**, **CrewAI**, and **AutoGen**, tools are ordinary in-process Python callables — a `@tool`-decorated function, a `BaseTool` subclass, a `FunctionTool`. The agent imports them and invokes them directly inside its own process. There is no wire protocol between the tool and the caller, so there is nothing to version-negotiate: when you change the function signature, the calling code either matches the new signature or it does not, and you find out at your next deploy. That is not a defect — it is what in-process functions are. But it also means these frameworks give you no protocol-level mechanism to keep an old tool shape reachable for callers that already bound to it. The moment any of them consume a tool over MCP instead (LangChain via `langchain-mcp-adapters`, AutoGen via `mcp_server_tools`), the versioning question doesn't disappear — it moves to the MCP server, which is where it belongs and where this whole discussion lives.

On the server side, **FastMCP** registers each tool by name: `@mcp.tool()` maps one name to one function, and the MCP protocol itself lists tools by unique name with no per-tool version field in the base spec. You can absolutely emulate versioning by registering two separately named functions — `search_v1` and `search_v2` — and that works. What FastMCP has no first-class primitive for is serving `search` (an alias that always points at the latest) alongside `search@1.0` (a pinned older contract) from a single base name, with semantic-version resolution deciding which one "latest" means. You would hand-roll the alias, own the "which version is newest" logic, and keep the two names from drifting yourself. That is the exact delta: the capability is reachable by convention, but it is not structural. For a broader head-to-head, the [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md) post lays out where each framework draws the line.

Promptise's contribution is to make that convention a real registry primitive so you don't reinvent it per project.

## Serve `search` and `search@1.0` side by side

`VersionedToolRegistry` is an overlay registry: you register multiple `ToolDef`s under one base name with explicit version strings, and it resolves `search` to the newest one while keeping every pinned `name@version` reachable. The following example is fully self-contained and runs as-is against the public `promptise.mcp.server` API:

```python
import asyncio

from promptise.mcp.server import ToolDef, VersionedToolRegistry


# v1: the original contract. Agents in production hard-code this shape.
async def search_v1(query: str) -> list[dict]:
    """Full-text search. v1: query string only."""
    return [{"id": "doc-1", "query": query}]


# v2: adds a `filters` argument. A different signature, same base name.
async def search_v2(query: str, filters: dict | None = None) -> list[dict]:
    """Full-text search. v2: adds structured filtering."""
    return [{"id": "doc-1", "query": query, "filters": filters or {}}]


v1 = ToolDef(
    name="search",
    description="Full-text search (v1).",
    handler=search_v1,
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)

v2 = ToolDef(
    name="search",
    description="Full-text search (v2, adds filters).",
    handler=search_v2,
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "filters": {"type": "object"},
        },
        "required": ["query"],
    },
)

registry = VersionedToolRegistry()
registry.register("search", "1.0", v1)
registry.register("search", "2.0", v2)


async def main() -> None:
    # `search` resolves to the latest version...
    latest = registry.get("search")
    # ...while the old contract stays reachable, pinned by version.
    pinned = registry.get("search@1.0")

    print("versions available:", registry.list_versions("search"))
    print("latest is v2      :", latest is v2)
    print("search@1.0 is v1  :", pinned is v1)

    # A v1 client keeps calling the old signature — nothing breaks.
    print("v1 client         :", await pinned.handler(query="invoices"))
    # A v2 client opts in to the new argument when it is ready.
    print("v2 client         :", await latest.handler(query="invoices", filters={"year": 2026}))


asyncio.run(main())
```

Run it and you get exactly the coexistence you want:

```text
versions available: ['1.0', '2.0']
latest is v2      : True
search@1.0 is v1  : True
v1 client         : [{'id': 'doc-1', 'query': 'invoices'}]
v2 client         : [{'id': 'doc-1', 'query': 'invoices', 'filters': {'year': 2026}}]
```

The agent that learned v1 keeps calling the v1 signature and gets a correct answer. A new agent — or the same one, once its team is ready — pins `search@2.0` or just tracks `search` to pick up the latest. Neither one is forced to move on your deploy schedule. The renamed field is no longer an outage; it is a new version that older callers can ignore.

## How version resolution works

Two mechanics do the work, and both are worth understanding before you rely on them.

**The `latest` alias uses semantic ordering, not registration order.** When you ask for `search`, the registry returns the highest version by numeric segment comparison — so `2.0` beats `1.0`, `1.10` correctly beats `1.9` (not the string-sorted opposite), and `3.0.0` beats `2.9.9`. Register versions in any order; "latest" is always the genuinely newest one. `registry.list_versions("search")` returns them sorted the same way, which is what you want for a deprecation dashboard or a migration audit.

**Pinning is a naming convention over MCP's flat tool list.** MCP advertises tools by unique name with no native per-tool version field, so Promptise expresses the pin as a suffix: `get("search@1.0")` splits on the last `@`, finds the `search` group, and returns the `1.0` definition. `get("search")` with no suffix returns the group's latest. Registering the same base name and version twice raises immediately rather than silently overwriting a live contract — you cannot clobber v1 by accident. This is the same overlay approach the [Advanced Patterns](../../mcp/server/advanced-patterns.md) guide documents alongside transforms, composition, and OpenAPI bridging, and it slots into the broader [production features](../../mcp/server/production-features.md) stack next to caching, circuit breakers, and per-tool rate limits.

## A safe deprecation workflow

The registry gives you the mechanism; here is the discipline that turns it into a zero-downtime rollout.

- **Never edit a shipped schema in place.** Any change that adds a required field, renames a property, narrows a type, or removes a property is breaking. Register it as a new version instead of mutating the old `ToolDef`.
- **Ship v2 as the new latest, keep v1 live.** New discovery picks up `search` → v2 automatically; existing agents pinned to `search@1.0` keep working untouched. You have decoupled "deployed" from "migrated."
- **Watch who still pins the old version.** Use your audit trail to see which callers still hit `search@1.0`. When that number reaches zero — or your deprecation window closes — you can retire v1 by removing it from the registry, and only then.
- **Reserve major versions for breaking changes.** A purely additive, backward-compatible tweak (a new *optional* field with a safe default) does not need a new version at all; agents that ignore it are unaffected. Bump the version when, and only when, the old arguments would stop working.

This is the same backpressure mindset that keeps the rest of a production MCP server healthy: isolate change so one moving part cannot take down the whole. It pairs naturally with the failure-isolation patterns in [One Stalled MCP Tool Can Exhaust Your Connection Pool](mcp-tool-connection-pool-exhaustion.md) — versioning contains schema change the way a circuit breaker contains a failing dependency.

## Frequently asked questions

### Does the MCP protocol support tool versions natively?

No. The base MCP spec lists tools by unique name and has no per-tool version field — a session negotiates one protocol version, not one version per tool. Promptise's `search@1.0` pin is a naming convention layered on top of that flat list, resolved by `VersionedToolRegistry`. That is why "just version the tool" isn't something the protocol hands you for free; you need a registry primitive to express latest-alias-plus-pinned-versions cleanly.

### What counts as a breaking change to an MCP tool schema?

Anything that can make a previously valid call invalid: adding a required parameter, renaming a property, removing a property, tightening a type, or making an optional field required. Adding a new *optional* parameter with a safe default is backward compatible — agents that never send it are unaffected — so that alone does not need a new version. When in doubt, ask whether a v1-shaped argument payload would still validate against the new schema. If it would not, version it.

### Can I do this with FastMCP or an in-process framework like LangChain?

You can approximate it. With FastMCP, register two separately named tools (`search_v1`, `search_v2`) and manage the "which is latest" pointer yourself — it works, but the alias and semver logic are yours to maintain. With LangChain, CrewAI, or AutoGen, tools are in-process functions with no wire contract to version, so the question only becomes real once those agents call tools over MCP. Promptise's difference is that the latest-alias-plus-pinned-version behavior is a first-class registry primitive rather than something you hand-roll per project.

### How does `latest` decide which version wins?

By semantic numeric comparison of the version strings, not registration order or string sorting. Each version is split on `.` into integer segments, so `1.10` correctly ranks above `1.9`, and `2.0` above `1.0`. `registry.get("search")` returns the highest; `registry.list_versions("search")` returns all of them in that same ascending order.

## Next steps

Start by auditing your own server for the trap: any tool whose schema you have edited in place since it shipped is a latent break waiting for the next agent that pinned the old shape. Move those changes behind versions, ship v2 as the new latest, and keep v1 live until your callers have migrated. See coexisting tool versions in action in the [Advanced Patterns](../../mcp/server/advanced-patterns.md) guide, fit versioning into the rest of your hardening work with the [Production Features](../../mcp/server/production-features.md) overview, and if you are weighing your stack, read the [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md) comparison to see where first-class tool versioning changes the calculus.
