---
title: "Does the user's identity survive agent delegation?"
description: "When an orchestrator hands a task to a peer via ask_peer or broadcast, the human principal is usually dropped — the peer runs unattributed and its memory…"
keywords: "propagate user identity across agent delegation, caller context across sub-agents, multi-agent user isolation, principal survives delegation, ask_peer identity propagation, cross-tenant leakage delegation"
date: 2026-07-16
slug: propagate-user-identity-across-agent-delegation
categories:
  - Identity
---

# Does the user's identity survive agent delegation?

The honest answer for most agent stacks is: not unless you thread it by hand. To **propagate user identity across agent delegation** — so that when an orchestrator acting on Alice's behalf hands a subtask to a peer via `ask_peer` or `broadcast`, the peer still runs *as Alice* — you normally have to pass the principal down every hop yourself. Miss it on one call and the peer runs unattributed: its memory search, its semantic cache, its conversation ownership, and its audit trail all detach from the real user. In a multi-tenant product, "unattributed" quietly becomes "cross-tenant," and that is the exact leak a security reviewer will find.

This post looks at that one hop — the delegation call — across LangGraph, CrewAI, AutoGen, and Promptise Foundry, and shows how Promptise inherits the ambient `CallerContext` into the peer automatically so `tenant::user` isolation holds by default, plus how `delegated_by` lands on the peer's timeline to answer "who caused this?"

## The hop where the principal usually gets dropped

Picture a support orchestrator. Alice, a user in tenant `acme`, asks it to "summarize my last three tickets and draft a reply." The orchestrator delegates the retrieval to a `researcher` peer and the drafting to a `writer` peer. Three agents now touch Alice's data.

Everything upstream is easy to get right, because the request arrives with identity attached — a bearer token, a session, a `user_id`. The failure mode is the *sub-call*. When the orchestrator invokes a peer, that peer is a fresh agent invocation with its own memory retriever, its own cache lookup, and its own audit records. If nothing carries Alice's principal into that invocation, the peer does the sensible-but-wrong thing: it runs as *nobody*. Its memory search isn't scoped to Alice, so it can surface another user's stored notes. Its cache key isn't scoped to Alice, so it can serve — or store — an answer under a shared bucket. Its audit line says a tool ran, but not on whose behalf.

None of this shows up in a demo, because a demo has one user. It shows up the first time two tenants share the orchestrator. So the real question isn't "can I pass identity down?" — you always can. It's "does the principal survive the hop *by default*, so a forgotten parameter can't turn into a leak?"

## What other frameworks do today

To be fair, every mature framework gives you a way to move context between agents. The gap is narrower and more specific than "they can't do it."

- **LangGraph** automatically threads its `RunnableConfig` — including the `configurable` dict where teams stash things like `user_id` and `thread_id` — into nested-graph and subgraph calls. That propagation is real and reliable. What it doesn't do is treat that `user_id` as a *principal* that re-scopes state: the checkpointer keys on `thread_id`, and any per-user memory store or retriever only isolates by user if *you* read `config["configurable"]["user_id"]` inside it and filter accordingly. The config travels; the enforcement is yours to write on every store.
- **CrewAI** lets agents hand work to each other through delegation tools (the "Delegate work to coworker" tool, and the manager in a hierarchical process). That's genuine message passing between agents. But the payload is a task and context string, not a user principal, and CrewAI's memory is scoped to the crew, not to an end user — so per-user isolation across a delegated task is something you layer on.
- **AutoGen** moves messages between agents through group chats and, in the 0.4 line, a message-routing runtime. Again, real inter-agent communication — but the thing being routed is a conversation message, not a per-user identity that automatically re-scopes each agent's memory, cache, and audit.

So the precise delta is this: LangGraph threads config, CrewAI and AutoGen pass messages, and **none of them define a principal that, on its own, re-scopes the peer's memory search, semantic cache, guardrail tagging, conversation ownership, and audit to the original user.** Preventing cross-user or cross-tenant leakage across the delegated hop is on you. That's not a knock on those tools — it's a design choice. Promptise makes the opposite choice: the principal is a first-class, ambient object, and isolation across delegation is the default rather than a thing you remember to wire.

## How Promptise inherits the CallerContext on ask_peer and broadcast

In Promptise, the principal is a [`CallerContext`](../../guides/multi-user-identity.md) you attach to an invocation with `caller=`. The key behavior: when a peer agent is invoked *without* an explicit caller, it reads the **ambient** caller from an async-safe contextvar the orchestrator already set. Delegation happens in-process, so that contextvar is still in scope — the peer inherits Alice automatically.

Here is the whole loop. The orchestrator is invoked as Alice; it delegates to a `researcher` peer through the auto-generated `ask_agent_researcher` tool; the peer's memory and cache come out scoped to Alice without a single identity parameter on the delegation call.

```python
import asyncio
from promptise import build_agent, CallerContext
from promptise.config import HTTPServerSpec
from promptise.cross_agent import CrossAgent
from promptise.memory import ChromaProvider
from promptise.cache import SemanticCache


async def main():
    # A specialist peer with its own per-user memory + cache.
    researcher = await build_agent(
        model="openai:gpt-5-mini",
        servers={"search": HTTPServerSpec(url="http://localhost:8001/mcp")},
        instructions="You are a research specialist. Use tools to find facts.",
        memory=ChromaProvider(persist_directory="./researcher-memory"),
        cache=SemanticCache(),
    )

    # The orchestrator delegates to the peer via cross_agents.
    orchestrator = await build_agent(
        model="openai:gpt-5-mini",
        servers={"tickets": HTTPServerSpec(url="http://localhost:8002/mcp")},
        instructions="Delegate research to the researcher, then answer.",
        cross_agents={
            "researcher": CrossAgent(agent=researcher, description="Finds facts"),
        },
    )

    alice = CallerContext(user_id="u-alice", tenant_id="acme")

    result = await orchestrator.ainvoke(
        {"messages": [{"role": "user",
                       "content": "Research my last tickets and summarize them."}]},
        caller=alice,   # identity for THIS request only
    )
    print(result["messages"][-1].content)

    await orchestrator.shutdown()
    await researcher.shutdown()


asyncio.run(main())
```

When the orchestrator calls `ask_agent_researcher`, the researcher's `ainvoke` runs with no `caller` argument. Internally it falls back to the ambient principal — the equivalent of `caller = get_current_caller()` — which is still `alice`. So the researcher's memory search, its cache lookup, and any conversation it persists are all keyed to Alice's identity, exactly as if she had called it directly. You didn't pass `alice` to the peer; the framework did.

## Why the isolation_key makes it structural, not a convention

Automatic inheritance only matters if what gets inherited actually *enforces* isolation. In Promptise, every per-user surface keys on one derived value — `CallerContext.isolation_key` — and it is deliberately not just the raw `user_id`:

```python
from promptise import CallerContext

acme_alice   = CallerContext(user_id="alice", tenant_id="acme")
globex_alice = CallerContext(user_id="alice", tenant_id="globex")

acme_alice.isolation_key    # -> "acme::alice"
globex_alice.isolation_key  # -> "globex::alice"
```

Two different tenants can each have a user called `alice`, and they get *different* isolation keys — so the semantic cache, memory scoping, and conversation ownership can never collide them. The `::` join is enforced to be injective: `tenant_id` is rejected if it contains a colon, and `user_id` is rejected if it contains `::`, so a crafted pair can't synthesize a separator and impersonate another tenant's key. That's the difference between isolation as a *convention* ("remember to filter by user_id") and isolation as a *structural invariant* baked into the one derivation everything routes through.

Now stack the two facts together. The peer inherits the ambient `CallerContext` across the delegation hop, and the peer's cache/memory/conversation all key on `isolation_key`. That means the peer of an `acme::alice` request physically cannot read `globex::alice` state — not because you filtered correctly on the sub-call, but because the sub-call never had a chance to run as the wrong principal. The full field-by-field walkthrough of what each surface keys on lives in the [Multi-User Identity guide](../../guides/multi-user-identity.md), and the end-to-end tenant story is in the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md).

## delegated_by answers "who caused this?"

Preserving the *human* principal keeps data isolation intact. There's a second identity in play, and Promptise carries it too: the *agent* that did the delegating.

If you give the orchestrator an [agent identity](../../identity/guide.md) and wire the peer with `cross_agents=`, the delegating agent's identity claims ride along on a separate contextvar around the peer call. The peer's observability timeline then stamps **`delegated_by`** on every entry it records during that delegated run — the delegating agent's descriptors, no credential. So an auditor reading the researcher's timeline sees both facts: it ran on Alice's behalf (the principal), and it was *caused* by the `support-orchestrator` agent (the delegator). That answers "who caused this?", not just "who ran it?" — the accountability question that message-passing between anonymous agents can't cleanly reconstruct after the fact.

Because the two identities travel on independent channels, they compose cleanly: the human principal re-scopes data, and `delegated_by` records provenance. For how the acting-agent identity itself is minted and presented, see [How does an AI agent authenticate to an API? (not API keys)](how-does-an-ai-agent-authenticate-to-an-api.md) and [Give each AI agent its own identity, not a shared key](give-each-ai-agent-its-own-identity.md).

## When you actually want a different principal on the peer

Inheritance is the safe default, but it isn't a straitjacket. Sometimes a peer *should* run as a different principal — a background enrichment agent that operates under a service account, or a hop that deliberately drops user scopes before crossing a trust boundary. Because the peer only inherits the ambient caller *when you don't provide one*, you override it by invoking the peer with an explicit `CallerContext` (or by resetting it) for that call. The default protects you from the common mistake — silently losing the user — while still letting you make an intentional, visible choice to re-scope when the architecture calls for it. The point isn't "always inherit"; it's "never drop the principal by accident."

## Frequently asked questions

### Does the peer automatically run as the original user?

Yes. When the orchestrator is invoked with `caller=alice`, that principal is stored in an async-safe contextvar. A peer invoked through `ask_peer`/`broadcast` without its own `caller` reads that ambient value, so it runs as Alice — its memory, cache, and conversation scope to her `isolation_key` with no parameter on the delegation call.

### How is this different from LangGraph passing config into subgraphs?

LangGraph reliably threads its `config` (including `configurable`) into subgraphs, but that config is an opaque bag — your stores must read `user_id` out of it and enforce isolation themselves. Promptise's `CallerContext` *is* the principal, and one derived `isolation_key` re-scopes cache, memory, and conversation ownership automatically, so isolation doesn't depend on each store getting the filtering right.

### Can two tenants with the same user_id collide across a delegated hop?

No. Every per-user surface keys on `isolation_key`, which is `"{tenant_id}::{user_id}"` when a tenant is set. `acme::alice` and `globex::alice` are distinct keys, and the join is validated to be injective, so a peer of an `acme` request can't read `globex` state even if both tenants have a user named `alice`.

### What does delegated_by record, and does it forward a credential?

`delegated_by` is stamped on the peer's observability timeline with the delegating agent's identity claims (descriptors only — no token or secret). It answers "who caused this delegated work?" and is independent of the human principal, so you get both attribution (which agent delegated) and isolation (which user it acted for).

## Next steps

Keep the principal intact across hops so a forgotten parameter can never become a cross-tenant leak: attach a `CallerContext` at the top, wire peers with `cross_agents=`, and let the ambient principal and `isolation_key` do the enforcing. Start with the [Multi-User Identity guide](../../guides/multi-user-identity.md) for the field-by-field trust boundary, then the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md) to see delegation isolation inside a full tenant architecture.
