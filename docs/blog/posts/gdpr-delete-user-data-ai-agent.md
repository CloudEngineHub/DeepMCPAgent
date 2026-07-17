---
title: "GDPR Right to Erasure: Purge a User From Your Agent"
description: "A subject deletion request means erasing one user from everywhere the agent persisted them — long-term memory, the semantic cache, and the observability…"
keywords: "GDPR delete user data AI agent, right to erasure LLM, purge user data agent, forget user memory agent, GDPR AI agent compliance"
date: 2026-07-16
slug: gdpr-delete-user-data-ai-agent
categories:
  - Air-Gapped & Sovereign
---

# GDPR Right to Erasure: Purge a User From Your Agent

When a subject-access team hands you a **GDPR delete user data AI agent** request, the hard part is never the primary database — you already know how to run a `DELETE` there. The hard part is every *other* place the agent quietly kept a copy of that person: the long-term memory it embedded from their conversations, the semantic cache holding their answered questions, and the observability timeline recording who did what. A modern agent is not one store. It is three or four persistence layers wired behind one `build_agent()` call, and a right-to-erasure request has to reach all of them or it is not complete. This post shows how to erase one user from every store the agent wrote to — with a single call per layer, keyed to the exact principal so you delete that person and no one else.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## The three stores an agent keeps a user in

Point an agent at a memory provider, a cache, and observability, and you have signed up to persist user-derived data in three independent places, each with its own retention semantics:

- **Long-term memory.** Every fact the agent remembers about a user is embedded and stored — in Chroma, in Mem0, or in memory. Under `PER_USER` scope each entry is stamped with the caller's identity so it can be searched back later. That stamp is exactly what makes it personal data.
- **The semantic cache.** To save 30-50% on LLM calls, the agent caches answers by query similarity, partitioned per user by default. A cached response to *"what's my current balance?"* is that user's data sitting in a fast store, waiting to be served again.
- **The observability timeline.** Every LLM turn and tool call is recorded with the authenticated `user_id` and `session_id` for attribution and audit. That is a per-person activity log by construction.

Delete the row in your app database and all three of these still hold the user. An erasure request that stops at the primary store is a compliance gap you will have to explain at audit time. The framework has to give you a way to reach each layer — and to reach *only the right person* inside it.

## One erasure call across memory, cache, and telemetry

Promptise exposes `purge_user()` on every layer that persists per-user data — each memory provider, the `SemanticCache`, and the observability collector. Each call removes exactly that user's data from that store and returns the number of entries it dropped, so you get a receipt you can log against the erasure ticket. Here is the whole flow: normal traffic writes to all three stores under one principal, then a deletion request clears them.

```python
import asyncio

from promptise import build_agent, SemanticCache, CallerContext
from promptise.memory import InMemoryProvider, MemoryScope


async def main():
    # One store per persistence layer the agent writes to.
    memory = InMemoryProvider(scope=MemoryScope.PER_USER)
    cache = SemanticCache()          # scope="per_user" by default
    cache.warmup()

    agent = await build_agent(
        servers={},                  # or your own on-prem MCP servers
        model="openai:gpt-5-mini",
        instructions="You are a support assistant.",
        memory=memory,
        memory_auto_store=True,      # persist the exchange to long-term memory
        cache=cache,
        observe=True,                # record a telemetry timeline
    )

    # A tenant-qualified caller — the principal every store keys on.
    caller = CallerContext(user_id="alice", tenant_id="acme")

    # Normal traffic writes to all three stores under acme::alice.
    await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Remember my order #4471."}]},
        caller=caller,
    )

    # --- The right-to-erasure request arrives ---
    principal = caller.isolation_key                          # "acme::alice"

    mem_removed = await memory.purge_user(principal)          # long-term memory
    cache_removed = await cache.purge_user(                   # semantic cache
        caller.user_id, tenant_id=caller.tenant_id
    )
    tele_removed = agent.collector.purge_user(caller.user_id)  # telemetry buffer

    print(f"erased -> memory={mem_removed} cache={cache_removed} telemetry={tele_removed}")

    await agent.shutdown()


asyncio.run(main())
```

Three calls, three receipts, three stores emptied of one person. `memory.purge_user()` and `cache.purge_user()` are async (they may touch Chroma, Mem0, or Redis); `agent.collector.purge_user()` operates on an in-process ring buffer and is synchronous. Nothing here is bespoke deletion logic you had to reverse-engineer from each backend's internals — it is one named operation per layer, documented alongside the layer it erases: the [memory reference](../../core/memory.md), the [cache reference](../../core/cache.md), and the [observability reference](../../core/observability.md).

## Tenant-qualified keys erase exactly the right principal

Erasure is only correct if it targets the right person. In a multi-tenant deployment, two different real people in two different tenants can share the same `user_id` — a `"alice"` at Acme and a `"alice"` at Globex. Purge by the raw `user_id` alone and you risk deleting the wrong Alice, or failing to delete data stored under a tenant-qualified key.

Promptise resolves this with one derivation: `CallerContext.isolation_key`. When a tenant is set it is `"{tenant_id}::{user_id}"`; otherwise it is the plain `user_id`. This single key is what memory scoping, the semantic cache, and conversation ownership all store under, so isolation is a structural invariant rather than a convention you have to remember. The keyspace is deliberately injective: the tenanted namespace always contains `::`, keeping it disjoint from raw untenanted ids, and the cache goes further, hashing the tenant-qualified id into a `user:t:<sha256>` prefix so two tenants sharing a `user_id` can never collide in the same partition.

That is why the erasure code passes `caller.isolation_key` to memory and `(user_id, tenant_id)` to the cache: each call reconstructs the exact partition the entries were written under. You erase `acme::alice` and leave `globex::alice` untouched — provably, by key construction, not by a `WHERE tenant = ?` clause you hope you got right everywhere.

## What other frameworks do today

Let's be precise and fair, because this is where the differentiation is real. The individual stores in other stacks are *not* deletion-proof — several ship genuine primitives:

- **LangChain / LangGraph.** Its long-term memory `BaseStore` supports key-level `delete`, and LangGraph checkpointers expose thread-level deletion of persisted state — real, useful operations. But its LLM response cache (`set_llm_cache` with `InMemoryCache`, `SQLiteCache`, or `RedisCache`) is keyed by the `(prompt, llm_string)` pair; it has *no concept of a user*. So there is no per-user cache purge at all — only a global `.clear()` that wipes every user's cached responses. You erase one person from memory item-by-item, and you cannot erase them from the cache without flushing everyone.
- **CrewAI.** `crew.reset_memories(...)` resets short-term, long-term, entity, and knowledge memory — but it resets by memory *type*, globally, not by subject. There is no "delete this one user" slice.
- **AutoGen.** Its `Memory` protocol offers `clear()`, which empties the whole memory store rather than a single user's entries.
- **Mem0.** Credit where it is due: Mem0 ships a genuine per-user delete — `delete_all(user_id=...)` — and Promptise's own `Mem0Provider.purge_user()` delegates straight to it. But that covers the memory layer only. It does not touch a semantic response cache or a telemetry timeline, because those are not Mem0's job.

The honest delta is not that competitors *lack* deletion. It is that no mainstream agent framework exposes a **single right-to-erasure primitive that spans memory, the response cache, and telemetry with one consistent, tenant-qualified key.** Deleting a user everywhere is per-backend glue you write yourself — and then have to *prove complete* at audit time, across stores whose keys don't even agree on what a "user" is. Promptise's edge is structural: `purge_user()` is a first-class operation on each persistence layer, all keyed on the same `isolation_key`, so "erase this person" is three named calls with receipts rather than an archaeology project.

## What purge_user does not reach

Honesty matters more than a clean sweep, so here is exactly where the boundaries are — the same limits documented in the source:

- **Flushed telemetry sinks are external.** `collector.purge_user()` clears the in-memory ring buffer of recent events. It does *not* reach data already flushed to an external sink — a JSON file on disk, a webhook target, an OTLP backend. Those are owned by their destinations and must be erased there. The buffer is your live timeline; downstream stores follow their own retention.
- **The cache purges the `per_user` scope.** A cache built with `scope="per_session"` keys entries by session, not user, so `purge_user()` won't find them — erase those by their session scope instead. The default `per_user` scope is the GDPR-relevant one.
- **`SHARED` memory has no owner to purge.** A memory provider in `SHARED` scope has no per-user ownership, so `purge_user()` is a no-op that returns `0`. Anything you need to erase per person must be stored under `PER_USER` scope.
- **Conversation transcripts are a fourth surface.** If you persist chat history with a `ConversationStore`, those transcripts are a separate store keyed by session and owner; delete them there. The three-store sweep above covers memory, cache, and telemetry — wire the conversation store into the same erasure routine when you use one.

None of these are surprises at 2 a.m. because they are documented on the method that owns each store. That is the difference between a right-to-erasure workflow you can attest to and one you *hope* was complete.

## Frequently asked questions

### Does purge_user delete the user from my primary database too?

No, and it shouldn't. `purge_user()` erases the stores the *agent* owns — long-term memory, the semantic cache, the telemetry buffer. Your application database, CRM, and any downstream warehouse are yours to delete in your own erasure routine. Think of these three calls as the agent-layer half of a complete **GDPR AI agent compliance** workflow, not a replacement for it.

### How do I forget one user's memory without touching anyone else?

Store memory under `PER_USER` scope (`InMemoryProvider(scope=MemoryScope.PER_USER)`, or `ChromaProvider` / `Mem0Provider` with per-user scoping) and call `purge_user(caller.isolation_key)`. Per-user scope stamps ownership on every entry, so the purge drops only that principal's rows. This is how you **forget a user's memory** in an agent without a global wipe — see the [memory reference](../../core/memory.md) for how each provider honors ownership.

### What about a right-to-erasure request for data already written to disk?

The telemetry ring buffer is cleared immediately, but anything already flushed to an external sink — a JSON audit file, a webhook receiver, an OTLP backend — lives outside the agent and must be erased at its destination as part of the same ticket. This is a deliberate, documented boundary: the collector never silently claims to have deleted data it handed off to another system.

### Is one purge_user pass enough for right to erasure on an LLM agent?

For the agent's own stores, yes — memory, cache, and telemetry are covered by the three calls above, plus a fourth for a `ConversationStore` if you use one. The point of a built-in **right to erasure for an LLM** agent is that these are named operations with return-count receipts, not hand-written deletion you have to audit per backend. Combine them with your primary-store deletion and you have an end-to-end erasure you can attest to.

## Next steps

Wire the three calls above into your subject-deletion handler: call `purge_user()` on your memory provider (with `caller.isolation_key`), on the `SemanticCache` (with `user_id` and `tenant_id`), and on `agent.collector` — logging each returned count against the erasure ticket. The [memory](../../core/memory.md), [cache](../../core/cache.md), and [observability](../../core/observability.md) references document the scope semantics for every backend you might swap in.

If data residency is the reason erasure is on your desk, keep the whole stack on your own hardware too: [Why AI Agent Frameworks Fail in Air-Gapped Networks](air-gapped-ai-agent.md) maps the hidden cloud dependencies that leak user data off-host, and the [Air-Gapped AI Agent Framework: The On-Prem Guide](air-gapped-agent-framework.md) walks through locking down model, memory, guardrails, and telemetry so *nothing* about a user ever leaves the boundary you can prove you control.
