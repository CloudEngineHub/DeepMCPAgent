---
title: "Delete One Tenant's Data for GDPR, Not the Rest"
description: "When a customer invokes GDPR right-to-erasure you must delete exactly their data from the cache, the vector store, and memory, and nothing else. This post…"
keywords: "delete one tenant's data gdpr, gdpr right to erasure vector store, purge one customer's data ai agent, tenant data deletion, purge_user tenant"
date: 2026-07-16
slug: delete-one-tenant-s-data-gdpr
categories:
  - Multi-Tenancy
---

# Delete One Tenant's Data for GDPR, Not the Rest

Delete one tenant's data for GDPR right-to-erasure and you inherit a deceptively hard requirement: remove exactly that customer's records from the semantic cache, the vector store, and long-term memory — and nothing belonging to anyone else. On a shared multi-tenant agent, "their data" is smeared across at least three stores, each keyed differently, and a right-to-erasure request gives you a legal deadline to prove you got all of it. This post shows how Promptise Foundry turns that into one call per surface — `cache.purge_user("alice", tenant_id="acme")` and `provider.purge_user(caller.isolation_key)` — where the tenant is baked into the key, so the purge matches exactly what was stored.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## Why "just delete the user" isn't enough

The naive mental model is "delete Alice." But on a shared platform there are two Alices: Acme's and Globex's. If your stores key on the raw `user_id`, those two collide, and a purge scoped to `"alice"` either erases too much (both customers) or, if you filter wrong, too little. That collision is exactly the isolation bug covered in [Same user_id, Two Tenants: Why That Isn't Isolation](same-user-id-across-two-tenants.md) — and a right-to-erasure request is where it turns from a leakage risk into an audit failure.

The second trap is surface count. An agent that has served real traffic has written the tenant's data to more than one place:

- **The semantic cache** — prior responses to Alice's queries, stored so a similar future query is served without a new LLM call.
- **The vector store / long-term memory** — facts the agent remembered about Alice across sessions.
- Optionally conversation history, which has its own erasure path.

Right-to-erasure under GDPR Article 17 doesn't care that the cache is "just a performance optimization." If it holds the data subject's personal data, it's in scope. Miss the cache and you can still surface an erased user's answer from a cache hit weeks later. The requirement is therefore *complete* erasure across every store, *scoped* to exactly one tenant's user, *provably*. That is three properties at once, and it's where hand-rolled deletes tend to drop one.

## The tenant is baked into the key

Promptise threads a single identity derivation through every per-user surface. `CallerContext` carries a `tenant_id`, and its `isolation_key` property is the join `"{tenant_id}::{user_id}"` (or the plain `user_id` when there is no tenant). The cache, memory scoping, and conversation ownership all key on that composite — never the raw `user_id`.

```python
from promptise import CallerContext

acme_alice   = CallerContext(user_id="alice", tenant_id="acme")
globex_alice = CallerContext(user_id="alice", tenant_id="globex")

assert acme_alice.isolation_key   == "acme::alice"
assert globex_alice.isolation_key == "globex::alice"
# Same user_id, different tenants — provably disjoint keys.
```

The `::` separator is reserved: `CallerContext` refuses to construct when a `tenant_id` contains a colon or a `user_id` contains `::`, which makes the join injective. Because storage and erasure share this one derivation, a tenant-scoped purge targets exactly the keyspace the writes used — there is no drift between "how it was stored" and "how you delete it." The semantic cache goes one step further and derives a length-prefixed `sha256` scope id for tenanted callers (`user:t:<hash>`), so two tenants that happen to share a `user_id` land in provably disjoint partitions. The details of that keyspace live in [the cache reference](../../core/cache.md) and [the memory reference](../../core/memory.md).

## One call per surface — a runnable erasure

Here is a self-contained example you can run with no API key and no model download. It uses the in-memory memory provider in `PER_USER` scope, stores rows for two tenants that share the `user_id` `"alice"`, then erases exactly one tenant's Alice by passing her `isolation_key` to `purge_user`.

```python
import asyncio
from promptise import CallerContext, InMemoryProvider
from promptise.memory import MemoryScope


async def main() -> None:
    # PER_USER scope stamps ownership on every stored row.
    memory = InMemoryProvider(scope=MemoryScope.PER_USER)

    acme_alice   = CallerContext(user_id="alice", tenant_id="acme")
    globex_alice = CallerContext(user_id="alice", tenant_id="globex")

    # Store under the tenant-qualified isolation key, exactly as the agent does.
    await memory.add("prefers window seats", user_id=acme_alice.isolation_key)
    await memory.add("allergic to shellfish", user_id=acme_alice.isolation_key)
    await memory.add("VIP loyalty tier",      user_id=globex_alice.isolation_key)

    # GDPR right-to-erasure for Acme's Alice — one call, tenant-scoped.
    removed = await memory.purge_user(acme_alice.isolation_key)
    print(f"erased {removed} rows for {acme_alice.isolation_key}")   # -> erased 2 rows

    # Globex's Alice is untouched: her key never matched the purge.
    survivors = await memory.search("loyalty", user_id=globex_alice.isolation_key)
    assert len(survivors) == 1
    print("globex::alice still has", survivors[0].content)


asyncio.run(main())
```

The same `purge_user` contract holds on the production providers, and the count you get back is your audit evidence. On `ChromaProvider`, `purge_user` deletes every document whose `_promptise_user_id` metadata matches the key; on `Mem0Provider` it delegates to Mem0's `delete_all(user_id=…)`. Because you pass the `isolation_key`, the metadata filter is tenant-qualified for free.

Now wire in the second surface — the semantic cache — for a complete, two-store erasure of one tenant's user:

```python
from promptise import SemanticCache, ChromaProvider
from promptise.memory import MemoryScope

cache  = SemanticCache(scope="per_user")               # per-user isolation on
memory = ChromaProvider(scope=MemoryScope.PER_USER, persist_directory="./memory")

async def erase(user_id: str, tenant_id: str) -> None:
    """Satisfy an Article 17 request for exactly one tenant's user."""
    from promptise import CallerContext
    key = CallerContext(user_id=user_id, tenant_id=tenant_id).isolation_key

    n_cache  = await cache.purge_user(user_id, tenant_id=tenant_id)  # semantic cache
    n_memory = await memory.purge_user(key)                          # vector store
    print(f"erased {n_cache} cached + {n_memory} memory rows for {tenant_id}/{user_id}")
```

Two surfaces, two calls, both tenant-scoped, both returning a count you can log to your audit trail. The cache takes `tenant_id` as a keyword because it re-derives the same hashed scope id internally; memory takes the pre-joined `isolation_key` because that's the literal owner stamped on each row. Nothing belonging to Globex's Alice, or to Acme's Bob, is in scope of either call. The full walkthrough of standing this up behind a JWT-authenticated MCP server is in the [Secure Multi-Tenant Agent Platform guide](../../guides/secure-multi-tenant-platform.md).

## What other frameworks do today

To be fair to the ecosystem: the deletion primitives exist, they're just per-store and unscoped, so the correctness burden lands on you.

- **LangChain** vector stores expose `VectorStore.delete(ids=[…])`, and integrations like Chroma also let you delete by a metadata `where` filter. That's real and it works — but it deletes by id or by a filter *you* construct, and there's no built-in notion of a tenant-qualified erasure key. Its LLM caches (`InMemoryCache`, `RedisSemanticCache`) implement `BaseCache.clear()`, which flushes the *whole* cache — there's no `purge_user`-style per-principal purge, so a single tenant's cached answers can't be dropped in isolation.
- **LlamaIndex** vector stores support `delete_ref_doc(ref_doc_id)` and, on several integrations, `delete_nodes(..., filters=…)` with metadata filters. Again real, again per-store, and again the tenant filter is yours to compose correctly every time.

None of that is wrong — it's the honest state of the art. The precise delta is this: there is no single tenant-scoped erasure primitive that spans the semantic cache *and* every memory store at once. You hand-compose a correct metadata-filtered delete per surface, remember that the cache is also in scope, and get the tenant key shape right in each place independently. Miss one store and you've left un-erased personal data that fails an audit — silently, because a partial delete raises no error.

Promptise's edge isn't that others "can't delete." It's that erasure is *structural*: one `isolation_key` derivation is shared by every write and every purge, and `purge_user` is a first-class method on the cache and on every memory provider with the same contract. You aren't reconstructing the tenant filter at delete time — you're replaying the exact key the data was stored under. For how that same key powers isolated retrieval in the first place, see [Multi-Tenant RAG: Isolate Customer Data in a Shared Store](multi-tenant-rag.md).

## Frequently asked questions

**Does `purge_user` delete other tenants that share the same `user_id`?**
No. You purge by `isolation_key` (`"acme::alice"`) for memory and by `(user_id, tenant_id)` for the cache. Both derive a tenant-qualified key, and the two tenants' keys are provably disjoint, so Globex's Alice is never in scope of an erasure for Acme's Alice.

**What if I forget to pass `tenant_id` to the cache?**
`cache.purge_user("alice")` with no `tenant_id` targets the *untenanted* keyspace, which is disjoint from any tenanted one — so it won't match tenant-scoped entries, and it won't accidentally erase a different tenant either. For tenanted data you must pass the `tenant_id` the entries were stored under; that's the same value on the `CallerContext` that served the requests.

**Is the semantic cache actually in scope for GDPR erasure?**
If it holds a data subject's personal data — including cached responses derived from it — treat it as in scope. That's exactly why `purge_user` is a first-class method on `SemanticCache`, not only on the memory providers.

**How do I prove the deletion happened?**
`purge_user` returns the count of entries removed on each surface. Log those counts (cache + memory) against the request id; that record is your erasure evidence.

**Does this cover conversation history too?**
Conversation persistence has its own ownership-scoped deletion path keyed on the same `isolation_key`. Erase memory and cache with `purge_user`, and delete the user's sessions through the conversation store to cover all three surfaces.

**What about `SHARED`-scope stores?**
For a `SHARED` memory provider there is no per-user ownership to honor, so `purge_user` is a no-op that returns `0`. Right-to-erasure requires `PER_USER` scope, which stamps ownership on every row — see [the memory reference](../../core/memory.md).

## Next steps

Turn on `PER_USER` scope for your memory provider and cache, then make erasure a two-line routine: `cache.purge_user("alice", tenant_id="acme")` and `provider.purge_user(caller.isolation_key)`. Because both replay the exact key the data was written under, you satisfy an Article 17 request for one tenant's user without touching anyone else's records — and you get a count back to file as proof. Start from the [cache reference](../../core/cache.md) and [memory reference](../../core/memory.md), then wire it into a full tenant-aware deployment with the [Secure Multi-Tenant Agent Platform guide](../../guides/secure-multi-tenant-platform.md).
