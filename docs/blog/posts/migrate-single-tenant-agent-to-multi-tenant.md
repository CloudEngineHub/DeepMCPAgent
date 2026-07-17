---
title: "Migrate a Single-Tenant Agent to Multi-Tenant"
description: "Retrofitting tenancy onto a LangChain or LlamaIndex app means touching every vector query, cache lookup, and thread id by hand. This how-to walks the…"
keywords: "migrate single-tenant agent to multi-tenant, retrofit tenant isolation ai agent, add multi-tenancy without rewrite, single-tenant to multi-tenant saas agent, tenant_id one seam"
date: 2026-07-16
slug: migrate-single-tenant-agent-to-multi-tenant
categories:
  - Multi-Tenancy
---

# Migrate a Single-Tenant Agent to Multi-Tenant

To **migrate a single-tenant agent to multi-tenant**, you don't have to rewrite it — you have to find the one place where identity enters the system and make the tenant part of it. The trap is believing there are many such places. In a typical agent, per-customer state lives in at least three stores — long-term memory (a vector index), a semantic cache, and conversation history — and the naive migration path is to hunt down every read and write against all three and splice in a tenant filter. That's the retrofit tax, and it's where cross-tenant leaks come from: not the filters you add, but the one you forget. This how-to walks the Promptise Foundry migration seam end to end, where adding a tenant means setting a single field.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## The retrofit tax in a single-tenant agent

A single-tenant agent is honest about its assumption: there is one customer, so `user_id` is globally unique and every store can key on it directly. Memory partitions by `user_id`. The cache scopes entries by `user_id`. Conversation sessions are owned by `user_id`. It all works, and it keeps working right up until you onboard a second customer.

The moment you do, `user_id` stops being globally unique — it's only unique *within* a tenant. Your SSO issues a user named `alice` inside Acme and another `alice` inside Globex, and both are legitimate. Now every store that keyed on `user_id` alone maps both `alice`s to the same partition. Acme's `alice` can read Globex's memories, be served Globex's cached answers, and resume Globex's conversations. Nothing crashes, because `alice == alice` is a valid string comparison. That is the worst failure mode a multi-customer platform can ship: a *silent* one.

To **retrofit tenant isolation into an AI agent** the naive way, you open every call site:

- Each vector-store `search` and `add` gains a tenant metadata filter.
- Each cache `get`/`store` gains a tenant-qualified key.
- Each session lookup and ownership check gains a tenant comparison.

Dozens of call sites, each one a place to get the composition wrong, and every miss is a leak that survives code review and a single-tenant test suite. The goal of a good migration is to shrink that surface from "everywhere identity is used" to **one seam**.

## The migration seam: add tenant_id to CallerContext

In Promptise, every invocation already carries a `CallerContext` — the per-request identity you pass to `chat()` and `ainvoke()`. It has a `user_id`; migrating to multi-tenant is a one-field change: add `tenant_id`.

```diff
- caller = CallerContext(user_id="alice")
+ caller = CallerContext(user_id="alice", tenant_id="acme")
```

That single field flips a derived property, `isolation_key`, which is the value every per-user surface scopes on. When a tenant is set, `isolation_key` is `"{tenant_id}::{user_id}"`; with no tenant, it stays the plain `user_id` — so your single-tenant code keeps working unchanged, and tenanted callers get a disjoint keyspace. The derivation is injective by construction, and the type enforces it: a colon in `tenant_id` or a `::` in `user_id` raises `ValueError` before the object exists, so a crafted `user_id` can never forge another tenant's namespace.

This snippet is fully runnable with nothing but `pip install promptise` — no API key, no network, no MCP server. It's the whole guarantee, checkable in a unit test:

```python
from promptise import CallerContext

# BEFORE: single-tenant. user_id is assumed globally unique.
solo = CallerContext(user_id="alice")
assert solo.isolation_key == "alice"

# AFTER: two customers, each with a user literally named "alice".
acme_alice   = CallerContext(user_id="alice", tenant_id="acme")
globex_alice = CallerContext(user_id="alice", tenant_id="globex")

# Same user_id — but the isolation keys are disjoint keyspaces.
assert acme_alice.isolation_key   == "acme::alice"
assert globex_alice.isolation_key == "globex::alice"
assert acme_alice.isolation_key != globex_alice.isolation_key

# The separator is reserved, so a raw user_id can't impersonate a tenant.
try:
    CallerContext(user_id="acme::alice")          # crafted, no tenant
    raise AssertionError("should have been rejected")
except ValueError as e:
    print("rejected:", e)   # user_id must not contain '::' — it is the separator

# Real SSO subjects (single colons) are unaffected.
sso = CallerContext(user_id="google:114253", tenant_id="acme")
assert sso.isolation_key == "acme::google:114253"
print("migration seam verified")
```

That is the entire behavioral contract of the migration. Everything else in this post is about *why setting that field is enough* — why you don't then have to go edit memory, cache, and conversation code by hand.

## Migrate cache, memory, and conversations without touching a provider

Here's the part that makes this a seam rather than a starting point: the providers never touch `tenant_id` directly. The agent reads the ambient `CallerContext` for the in-flight request and derives each store's scope from it internally. So when you **add multi-tenancy without a rewrite**, the diff is confined to how you *construct* the caller — not how memory, cache, or conversations are configured or called.

Take a single-tenant agent and its multi-tenant version side by side. The `build_agent(...)` call is **identical**; only the `CallerContext` changed:

```python
import asyncio
from promptise import build_agent, CallerContext
from promptise.config import HTTPServerSpec
from promptise.memory import ChromaProvider, MemoryScope
from promptise.cache import SemanticCache
from promptise.conversations import SQLiteConversationStore

async def main():
    # This wiring does NOT change during the migration.
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"crm": HTTPServerSpec(url="https://mcp.internal/crm/mcp")},
        memory=ChromaProvider(persist_directory="./mem", scope=MemoryScope.PER_USER),
        cache=SemanticCache(),                       # per-user scope by default
        conversation_store=SQLiteConversationStore("chat.db"),
        instructions="You are a CRM assistant. Use tools to answer questions.",
    )

    # The ONLY change: the caller now carries a tenant.
    acme_alice   = CallerContext(user_id="alice", tenant_id="acme")
    globex_alice = CallerContext(user_id="alice", tenant_id="globex")

    await agent.chat("Remember my quota is 500 units.",
                     session_id="s-acme", caller=acme_alice)
    reply = await agent.chat("What's my quota?",
                             session_id="s-globex", caller=globex_alice)
    print(reply)   # Globex's alice has no memory of Acme's alice

    await agent.shutdown()

asyncio.run(main())
```

Trace where the tenant goes, and notice it goes there on its own:

- **Long-term memory.** Before searching or storing, the agent passes `caller.isolation_key` (now `acme::alice`) into the provider as its `user_id`. `ChromaProvider` in `PER_USER` scope partitions on whatever key it's handed — it neither knows nor needs to know that the key now encodes a tenant. Globex's `alice` searches `globex::alice` and finds nothing Acme wrote. The [memory guide](../../core/memory.md) documents this fail-closed per-user scoping in full.
- **Semantic cache.** For each request, the cache derives an injective, tenant-qualified scope key from the same `(tenant_id, user_id)` pair — a length-prefixed hash, so two distinct tenants can never collide even under adversarial ids. One tenant is never served another's cached answer, and the cache keeps its usual 30–50% cost reduction; isolation and savings are not a trade-off. See the [cache reference](../../core/cache.md) for the scope model and `purge_user()`.
- **Conversation ownership.** `chat()` keys session ownership on `caller.isolation_key`, so a session created under `acme::alice` can never be resumed by `globex::alice`, even though both users are named `alice`.

One derivation feeds all three. That is what "isolation guaranteed at the scoping layer" means in practice: you migrate the identity you already pass, and cache, memory, and conversation ownership re-scope together, because none of them own the composition — the `CallerContext` does. There is exactly **one tenant_id, one seam**.

## What other frameworks do today

To be fair about the delta: the mainstream frameworks give you real, capable primitives for per-user scoping. What they don't give you is a single place to add the tenant *once*.

- **LangGraph** persists durable state under a `thread_id` you supply in `config={"configurable": {"thread_id": ...}}`, and its long-term `BaseStore` uses namespace tuples *you* compose, e.g. `(user_id, "memories")`. There is a `checkpoint_ns`, but it scopes subgraph checkpoints, not tenants. So the primitives are there — but the tenant dimension isn't built in, and you assemble the namespace at every store call and every `thread_id` yourself.
- **LlamaIndex** vector stores support `MetadataFilters`, so the standard multi-tenant pattern is to stamp a `tenant_id` on each node's metadata and pass a filter on *every* query engine and retriever. It also bounds chat history with a `token_limit` on `ChatMemoryBuffer`/`Memory`. Both are real — but the tenant filter is applied per retrieval call, and any response or LLM cache you add is namespaced separately, by you.
- **LangChain** retrievers accept `search_kwargs={"filter": {...}}` for per-query tenant filtering, and its LLM cache (`set_llm_cache(InMemoryCache())` or the Redis cache) is process-global by default — not per-user or per-tenant unless you namespace it. So retrofitting tenancy means threading a filter into each retriever call and replacing or partitioning the global cache.

None of this means those frameworks *can't* isolate tenants — you can always build a correct composite by hand. The precise delta is that in each of them the tenant is a value *you* thread at every call site, across three subsystems that don't share a scoping layer: a metadata filter here, a namespace tuple there, a cache key somewhere else. There is no single seam where adding the tenant re-scopes memory, cache, and conversation ownership at once, and no reserved separator stopping a crafted id from colliding with a real tenant's namespace. Promptise's contribution is to make the composition first-class (`isolation_key`), the separator reserved (a construction-time `ValueError`), and the threading automatic — turning "we always filter by tenant" from a convention that fails silently into an invariant the type enforces. For the origin question that precedes all of this — *where the tenant value should come from* — the companion post [Same user_id, Two Tenants: Why That Isn't Isolation](same-user-id-across-two-tenants.md) covers the injective-key argument in depth.

## Verify it: two tenants, one user_id, zero leaks

A migration isn't done until you've proven it. The cheapest, most durable check runs entirely offline — it's a property of the type, so it belongs in your unit suite, not a manual QA pass:

```python
from promptise import CallerContext

def test_same_user_id_stays_isolated():
    """Two tenants sharing a user_id land in disjoint keyspaces."""
    acme   = CallerContext(user_id="alice", tenant_id="acme")
    globex = CallerContext(user_id="alice", tenant_id="globex")
    assert acme.isolation_key != globex.isolation_key
    # An untenanted caller can never collide with a tenanted one, either.
    assert CallerContext(user_id="alice").isolation_key not in (
        acme.isolation_key, globex.isolation_key
    )
```

Because memory scoping, the cache scope key, and conversation ownership all derive from the same `(tenant_id, user_id)` pair, this one assertion certifies the seam for all three surfaces at once. When you're ready to prove it against live stores end to end — token issuance, server-side guards, and per-tenant storage wired together — run the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md), which walks the full **single-tenant to multi-tenant SaaS agent** build with `TestClient` (no cloud, no keys). If the store you're most worried about is the vector index, [Multi-Tenant RAG: Isolate Customer Data in a Shared Store](multi-tenant-rag.md) drills into keeping that partition airtight.

## Frequently asked questions

### Do I have to change my memory, cache, or conversation code to add a tenant?

No. That's the point of the seam. You add `tenant_id` to the `CallerContext` you already construct per request. The agent reads the ambient caller and threads `isolation_key` into memory search/store, derives the cache scope from the same `(tenant, user)` pair, and keys conversation ownership on it — all internally. `ChromaProvider`, `SemanticCache`, and `SQLiteConversationStore` need no changes; they scope on whatever key they're handed.

### Will my existing single-tenant sessions and memories still work after migrating?

Yes. A `CallerContext` with no `tenant_id` produces an `isolation_key` equal to the plain `user_id` — exactly what your single-tenant agent already used. Because `user_id` can never contain the `::` separator (construction rejects it), that untenanted key can never accidentally equal a tenanted `"{tenant}::{user}"` key. Tenanted and untenanted callers coexist safely in the same store, so you can migrate incrementally.

### Where should the tenant_id actually come from?

From a trusted claim, not from user input. On the MCP server side, `AuthMiddleware` extracts the tenant from a signed JWT claim into `ClientContext.tenant_id`; on the agent side you construct `CallerContext(user_id=..., tenant_id=...)` from that verified identity. Never let a client name its own tenant. The [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md) shows the full token-to-context flow.

### Is the semantic cache still safe if two tenants share a user_id?

Yes. The cache derives its scope key from a length-prefixed hash of `(tenant_id, user_id)`, which is injective — two distinct tenants can't collide even if their user ids are identical, and the tenanted keyspace is disjoint from the untenanted one. See the [cache reference](../../core/cache.md) for the scope model, and note that `purge_user()` takes the same `tenant_id` so a per-tenant GDPR purge matches exactly what was stored.

## Next steps

Migrating a single-tenant agent to multi-tenant is a scoping change, not a rewrite: find where identity enters (`CallerContext`), make the tenant part of it, and let `isolation_key` re-scope memory, cache, and conversation ownership together. Concretely — add `tenant_id` to your `CallerContext`, keep your `build_agent(...)` wiring exactly as it is, and add the one-line unit test above that asserts two same-`user_id` tenants produce disjoint keys. Then run the multi-tenant guide's end-to-end example to confirm the isolation against live stores. Start with the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md), confirm the storage-side behavior in the [memory guide](../../core/memory.md) and [cache reference](../../core/cache.md), and read [Same user_id, Two Tenants: Why That Isn't Isolation](same-user-id-across-two-tenants.md) for the property that makes the seam safe.
