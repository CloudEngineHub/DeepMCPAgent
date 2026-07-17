---
title: "Carry a User's Tenant Across Agent Delegation"
description: "This is the data-isolation side of delegation, distinct from agent attribution: the moment an agent hands work to a peer, most frameworks drop the end-user's…"
keywords: "propagate tenant across agent delegation, caller context tenant cross-agent, keep data isolation after ask_peer, tenant survives agent handoff, downstream isolation delegated agent"
date: 2026-07-16
slug: propagate-tenant-across-agent-delegation
categories:
  - Multi-Tenancy
---

# Carry a User's Tenant Across Agent Delegation

To **propagate tenant across agent delegation** is to keep one promise: when your orchestrator agent hands work to a peer mid-request, the peer's cache, memory, and conversation history stay scoped to the *original* end-user's tenant — not to the process, and not to whatever principal the peer happens to think it is serving. This is the data-isolation side of delegation, and it is distinct from *attribution* (knowing which agent asked). Attribution answers "who delegated?"; isolation answers "whose data can the delegate touch?". Get attribution right and you still have a leak if the peer reads a shared vector store under the wrong tenant. This post shows how Promptise Foundry makes the tenant ride along automatically, so `ask_agent_<peer>` and `broadcast_to_agents` never silently widen the blast radius.

## The hop where isolation usually breaks

On a shared multi-tenant platform, a request arrives carrying an identity — say Acme's Alice. Your orchestrator agent scopes its own semantic cache, memory search, and conversation ownership to that tenant. So far so good. Then the orchestrator delegates a sub-task to a research peer. That single hop is where isolation quietly evaporates in most designs, because the handoff carries a *message*, not the *principal*. The peer starts a fresh invocation, sees no tenant, and falls back to the process default — which is "all tenants" if it shares a store.

The failure is invisible precisely because nothing errors. The peer returns a plausible answer; it just may be grounded in Globex's data while serving Acme's Alice. And on a shared store, "Alice" is not even unique: there is an Acme Alice and a Globex Alice, and if your surfaces key on the raw `user_id` they collide — the exact trap covered in [Same user_id, Two Tenants: Why That Isn't Isolation](same-user-id-across-two-tenants.md). A delegated hop that drops the tenant turns that latent collision into a live cross-tenant read.

The requirement, then, is that the tenant is *inherited* by every delegated agent, and that every per-user surface keys on a value that cannot collide across tenants — without you re-threading identity by hand at each call site.

## The mechanism: the tenant rides an async contextvar

Promptise carries per-request identity in a single object, `CallerContext`, which you pass once to the top-level `ainvoke()` or `chat()`. Its `tenant_id` is folded into a derived `isolation_key` — `"{tenant_id}::{user_id}"` — and that one key is what the semantic cache, memory scoping, and conversation ownership all partition on. Two tenants that share a `user_id` therefore land on provably disjoint keys. You can see the derivation with no model and no API key:

```python
from promptise import CallerContext

acme_alice   = CallerContext(user_id="alice", tenant_id="acme")
globex_alice = CallerContext(user_id="alice", tenant_id="globex")

# Every per-user surface — cache, memory, conversation ownership — keys on this.
assert acme_alice.isolation_key   == "acme::alice"
assert globex_alice.isolation_key == "globex::alice"
```

The continuity across delegation comes from *where* that identity lives. It is not stored on the agent instance (which would race across concurrent requests) — it is bound to an async `contextvars` variable for the duration of the invocation. When the orchestrator delegates, the `ask_agent_<peer>` tool runs inside that same async task and invokes the peer with no explicit caller. The peer's `ainvoke()` sees `caller is None` and inherits the ambient context. The whole guarantee is three lines of library internals:

```python
# Inside PromptiseAgent.ainvoke — you never write this; it runs for you.
if caller is None:
    caller = _caller_ctx_var.get()   # inherit the ambient tenant on delegation
_ctx_token = _caller_ctx_var.set(caller)
```

Because the peer inherits the *same* `CallerContext`, its memory search runs under `acme::alice`, its cache reads and writes stay in Acme's partition, and any conversation it persists is owned by Acme's Alice. The tenant survives the handoff structurally, not by convention. `CallerContext` and its per-request identity model are covered in the [Multi-User Identity guide](../../guides/multi-user-identity.md), and the delegation tools themselves in the [Cross-Agent Delegation reference](../../core/agents/cross-agent.md).

## Showcase: delegate to a peer, keep the tenant

Here is the feature end to end. The peer is a specialist with its *own* per-user memory; two tenants share the `user_id` `"alice"`. The orchestrator delegates to it, and the peer's memory search is scoped to whichever tenant the *top-level* request carried — with nothing re-passing the tenant at the delegation site. It runs against a real model (set `OPENAI_API_KEY`); the memory assertion at the end is deterministic and needs no network.

```python
import asyncio
from promptise import build_agent, CallerContext, InMemoryProvider
from promptise.memory import MemoryScope
from promptise.cross_agent import CrossAgent


async def main() -> None:
    # A peer specialist with its OWN per-user memory.
    peer_memory = InMemoryProvider(scope=MemoryScope.PER_USER)

    # Seed two tenants that share the user_id "alice".
    await peer_memory.add("Acme ships on Fridays",   user_id="acme::alice")
    await peer_memory.add("Globex ships on Mondays", user_id="globex::alice")

    researcher = await build_agent(
        servers={},
        model="openai:gpt-5-mini",
        instructions="Answer using only the delivery facts in your memory.",
        memory=peer_memory,
    )

    # The orchestrator can delegate to the peer as a tool.
    orchestrator = await build_agent(
        servers={},
        model="openai:gpt-5-mini",
        cross_agents={
            "researcher": CrossAgent(agent=researcher, description="Knows delivery schedules"),
        },
    )

    # One request, scoped to Acme's Alice. Nothing below re-passes the tenant.
    acme = CallerContext(user_id="alice", tenant_id="acme")
    result = await orchestrator.ainvoke(
        {"messages": [{"role": "user", "content": "Ask the researcher when we ship."}]},
        caller=acme,
    )
    print(result["messages"][-1].content)  # grounded in acme::alice memory only

    # The scoping the delegation relies on, shown directly and deterministically:
    hits = await peer_memory.search("ship", user_id=acme.isolation_key)
    assert all("Acme" in h.content for h in hits)  # Globex's row is never in scope

    await orchestrator.shutdown()
    await researcher.shutdown()


asyncio.run(main())
```

Swap `tenant_id="globex"` and re-run: the *same* orchestrator and the *same* peer now surface only Globex's Monday fact. You changed one value at the top of the request; every delegated hop followed. That is the whole point — isolation is a property of the request, not something you re-assert per call. Standing this up behind a JWT-authenticated MCP server, with the tenant claim extracted from the token, is walked through in the [Secure Multi-Tenant Agent Platform guide](../../guides/secure-multi-tenant-platform.md), and the retrieval side of the same key powers [Multi-Tenant RAG: Isolate Customer Data in a Shared Store](multi-tenant-rag.md).

## Why the tenant isn't a tool argument

Notice what the orchestrator does *not* do: it does not put `"tenant=acme"` into the message it sends the peer. The `ask_agent_<peer>` tool has an optional `context` string for hints and constraints, but the tenant deliberately does not travel there. If it did, the tenant would be model-visible and model-controllable — a prompt-injected instruction could ask the peer to "research as tenant globex," and the peer would have no way to know that is illegitimate. By riding an out-of-band contextvar instead, the tenant is inherited by the runtime, invisible to the LLM, and impossible to spoof through the conversation. Data-scoping identity and model-visible content live on different channels, which is what lets you delegate to a less-trusted or LLM-authored peer without handing it the keys to widen its own tenant scope.

## What other frameworks do today

To be fair to the ecosystem, multi-agent frameworks all *can* pass information between agents — the gap is specifically about a first-class *end-user tenant* that scopes downstream data surfaces automatically.

- **CrewAI** ships real delegation: its built-in "Delegate work to coworker" and "Ask question to coworker" tools let one agent hand a task to another, and they accept a `task` and a free-text `context`. What travels is that task and context — strings the LLM composes — not a structured end-user identity. So if a coworker reads a shared memory or vector store, scoping it to the *original* tenant is something you pass and re-apply yourself; there is no `tenant_id` that automatically keys the coworker's stores.
- **AutoGen** coordinates agents by passing messages (and, in 0.4, across processes via its distributed runtime). The unit of handoff is the message payload. That is a genuine and capable model — but a message is content, not a per-request tenant that rides along to scope a receiving agent's memory or cache back to the original end-user. You thread that yourself if you need per-tenant data isolation across the handoff.

None of that is a bug in those frameworks — it is the honest state of the art, and both give you the hooks to do it manually. Promptise's edge is not that others "can't." It is that tenant continuity is *structural*: one `CallerContext` is bound to the request, the runtime inherits it on every delegated hop, and every per-user surface keys on the same injective `isolation_key`. You do not reconstruct the tenant filter at each delegation — the delegate replays the exact identity the request arrived with.

## Frequently asked questions

**Does the peer inherit the tenant even though I never pass `caller` to it?**
Yes. The peer's `ainvoke()` sees no explicit caller and reads the ambient `CallerContext` from the async contextvar bound by the top-level invocation. That is the inheritance path in the mechanism section, and it is why delegation stays tenant-scoped without call-site plumbing.

**What if I *want* the peer to run as a different tenant?**
Pass an explicit `caller=` to that peer's invocation. An explicit `CallerContext` always wins over inheritance; the ambient tenant is only the default when you supply nothing.

**Does this work for `broadcast_to_agents`, not just single-peer asks?**
Yes. The broadcast tool fans out inside the same request scope, so each peer task inherits the same `CallerContext` and scopes its own surfaces to the original tenant.

**Is the tenant visible to the peer's LLM?**
No. The tenant rides an out-of-band contextvar, not the message. The `context` argument on `ask_agent_<peer>` is for model-visible hints only, which is what keeps the tenant unspoofable through the conversation.

**What happens with no tenant set — just a plain `user_id`?**
Then `isolation_key` is the plain `user_id` and the peer inherits that. The untenanted keyspace is disjoint from any tenanted one, so a tenant-less request can never collide with a tenanted user that shares the same id.

**Does inheritance survive concurrent requests through one shared agent?**
Yes. The caller lives in a `contextvars` variable, so each concurrent `ainvoke()` gets its own copy — one busy request cannot leak its tenant into another's delegated hop.

## Next steps

Pass a tenant-scoped `CallerContext` to your first agent once, and let delegation carry it the rest of the way: `ask_agent_<peer>` and `broadcast_to_agents` inherit the ambient tenant, so every downstream cache read, memory search, and persisted conversation stays scoped to the original principal. Start from the [Cross-Agent Delegation reference](../../core/agents/cross-agent.md) and the [Multi-User Identity guide](../../guides/multi-user-identity.md), then wire the tenant claim out of a JWT and into a full deployment with the [Secure Multi-Tenant Agent Platform guide](../../guides/secure-multi-tenant-platform.md).
