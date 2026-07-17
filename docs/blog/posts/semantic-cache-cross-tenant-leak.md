---
title: "Can a Paraphrase Leak Another Tenant's Cached Answer?"
description: "A semantic cache does the one thing an exact-match cache can't: it matches by meaning. Tenant B types a paraphrase of a question tenant A already asked, the…"
keywords: "semantic cache cross-tenant leak, cache leak between customers, per-tenant semantic cache isolation, paraphrase cache hit wrong tenant, fail-closed llm cache, isolate llm cache per tenant"
date: 2026-07-16
slug: semantic-cache-cross-tenant-leak
categories:
  - Cost & Efficiency
---

# Can a Paraphrase Leak Another Tenant's Cached Answer?

A **semantic cache cross-tenant leak** is the failure mode that only similarity caching can produce: tenant B types a paraphrase of a question tenant A already asked, the two queries embed to nearly the same vector, and — without a caller-scoped partition — the lookup hands tenant B the completion Promptise stored for tenant A. An exact-match cache can never do this. It keys on the literal prompt string, so a reworded question misses and goes to the model. The moment you cache by *meaning* to save money, you also open a channel where meaning, not identity, decides who sees what. This post isolates that leak vector precisely, and shows how Promptise Foundry confines every similarity search to the caller's own tenant-qualified partition — by default, and fail-closed.

<!-- more -->

## Why similarity caching opens a door exact-match caching keeps shut

Think about what a cache hit *is* in each design.

An exact-match LLM cache stores `sha256(prompt + model_params) → completion`. A hit requires the incoming prompt to be byte-identical to a stored one. Reword a single token and you miss. That strictness is annoying for hit rates — it's why teams reach for semantic caching — but it has a quiet security property: two different tenants almost never type the *exact* same string, so accidental cross-tenant hits are vanishingly rare even without isolation.

A semantic cache throws that property away on purpose. It embeds the query, then returns any stored answer whose embedding sits within a cosine-similarity threshold. That is the whole point: "How much does the Pro plan cost per month?" and "What's the monthly price of Pro?" should hit the same entry. But similarity does not know about tenants. If tenant A asked one phrasing and tenant B asks the other, the embeddings land close, the threshold is met, and a shared store will return A's completion to B.

So the leak vector is specific and structural:

- **Exact-match caches** leak across tenants only on a literal string collision — rare.
- **Semantic caches** leak across tenants on a *meaning* collision — common, because paraphrase is the norm in natural language.

The fix is not to weaken the matching. It's to make sure the similarity search can only ever see one tenant's entries. That requires the cache key to carry identity, and it requires the system to refuse to cache when identity is missing.

## What other frameworks do today

Be fair here: the popular LLM caches are good at what they were built for, and every one of them *can* be namespaced by hand. The precise gap is that none partition per tenant by default, and none fail closed when caller identity is absent.

- **LangChain's `RedisSemanticCache`** (and the older `GPTCache` wrapper it exposes) matches by embedding similarity of the prompt against one shared vector store, keyed on the prompt embedding plus the model string (`llm_string`). There is no caller or tenant dimension in that key. You can run a separate cache instance or Redis key prefix per tenant, but you wire that yourself; the default is a single shared store.
- **GPTCache**, used directly or through **LlamaIndex's** GPTCache integration, has the same shape: an embedding of the prompt is matched against a shared store, and isolation is an opt-in you build with separate namespaces.
- **AutoGen's `Cache`** is a different mechanism worth naming accurately — it's an *exact-match* key-value cache keyed on the serialized request plus a manual `seed`. Two callers get separate partitions only if you hand them different seeds. There is no automatic tenant or user dimension, and because it isn't similarity-based, its leak surface is the string-collision one, not the paraphrase one.
- **CrewAI's `CacheHandler`** caches *tool outputs* keyed on tool name plus arguments. There's no tenant or user dimension at all — it's a per-crew execution cache, not a per-caller one.

The honest summary: the semantic caches (LangChain's, GPTCache, LlamaIndex's) genuinely carry the paraphrase-crosses-tenant risk against their default shared store; the others carry a narrower risk but still ship no tenant partition. In all of them, per-tenant isolation is something you remember to build. Miss it — or forget to pass identity on one code path — and the cache happily serves whatever the embedding matched. Promptise's edge is not that isolation is *possible*; it's that isolation is the **default**, and the absence of identity is treated as a reason to *not* cache rather than a reason to cache globally.

## How Promptise confines a similarity match to one tenant

Promptise's `SemanticCache` makes tenant isolation a structural property of the cache key, resting on three rules described in full in the [Semantic Cache docs](../../core/cache.md):

1. **`per_user` is the default scope.** You don't opt into isolation — you'd have to opt *out* of it by explicitly choosing `scope="shared"` (which itself requires `shared_data_acknowledged=True`). Every entry is keyed under the caller's partition, and the similarity search runs *only* within that partition. There is no code path where an embedding is compared across users.

2. **Fail-closed: no `CallerContext`, no caching.** If a request arrives without `caller=CallerContext(user_id=...)`, caching is silently disabled for that request (with a debug log: `"Cache: no CallerContext or user_id provided"`). Nothing is stored, nothing is matched. A forgotten identity can't degrade into a shared global cache — it degrades into no cache, which is the safe direction.

3. **The tenant-qualified partition is injective.** When the caller carries `CallerContext(tenant_id="acme")`, the scope key becomes tenant-qualified — an injective, colon-prefixed hash (`user:t:<sha256>`) that is *disjoint* from the untenanted `user:<id>` namespace. This closes the subtle case the plain per-user model doesn't: two different tenants that happen to reuse the same `user_id` (say both have a user called `alice`) still land in separate partitions, because `CallerContext.isolation_key` derives from `"{tenant_id}::{user_id}"`, not `user_id` alone. Same `user_id`, different tenant, provably different bucket.

Here's the leak scenario, run end-to-end. Two tenants, a shared `user_id`, and a deliberate paraphrase — the exact input that would produce a cross-tenant hit against a shared store:

```python
import asyncio
from promptise import build_agent, SemanticCache, CallerContext
from promptise.config import HTTPServerSpec


async def main():
    cache = SemanticCache()   # scope="per_user" is the default
    cache.warmup()            # pre-load the local embedding model at startup

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"billing": HTTPServerSpec(url="http://localhost:8000/mcp")},
        instructions="You are a support agent. Answer from the billing tools.",
        cache=cache,
    )

    # Tenant ACME, user "alice" asks a question.
    # The answer is stored in ACME's tenant-qualified partition.
    acme = CallerContext(user_id="alice", tenant_id="acme")
    await agent.ainvoke(
        {"messages": [{"role": "user",
                       "content": "What is my plan's monthly usage limit?"}]},
        caller=acme,
    )

    # Tenant GLOBEX has a DIFFERENT user who also happens to be "alice".
    # She types a paraphrase — embeddings land close to ACME's entry.
    globex = CallerContext(user_id="alice", tenant_id="globex")
    result = await agent.ainvoke(
        {"messages": [{"role": "user",
                       "content": "How many units does my plan allow each month?"}]},
        caller=globex,
    )

    # The similarity search only runs inside GLOBEX's partition, which is
    # disjoint from ACME's. ACME's cached answer is unreachable, so GLOBEX
    # gets a fresh, correctly-scoped LLM call — never ACME's completion.
    print(result["messages"][-1].content)

    await agent.shutdown()


asyncio.run(main())
```

The paraphrase would be a cache hit against a shared store keyed on the prompt embedding alone. In Promptise it isn't, because the embedding for GLOBEX is only ever compared against GLOBEX's entries. Identity gates the search *before* similarity ever runs.

The embedding itself runs locally — `SemanticCache` uses the same on-device model as [tool optimization](../../core/tool-optimization.md), so isolation costs you no extra API calls and works fully offline, which also means no query text leaves your environment to be embedded by a third party.

## Prove your cache fails closed

Isolation you can't observe is isolation you don't trust. The fail-closed rule is the easiest property to verify, so verify it directly: invoke once with no `CallerContext` and confirm nothing was stored.

```python
# Same agent as above. This request carries NO caller identity.
await agent.ainvoke(
    {"messages": [{"role": "user",
                   "content": "What is my plan's monthly usage limit?"}]},
)
# No caller → caching is skipped entirely. The debug log reads:
#   "Cache: no CallerContext or user_id provided"
# Nothing is written, so a later identical request cannot match it.
```

If you turn on `DEBUG` logging you'll see that line and no `cache.store` event — the observability timeline emits `cache.hit`, `cache.miss`, and `cache.store` events you can assert against. That's the fail-closed guarantee made visible: a code path that forgets identity produces *no* cached state, so there is nothing for a later request to match against, scoped or not.

## Where cache isolation fits in a multi-tenant platform

Per-tenant cache isolation is one seam in a larger surface. On a real multi-tenant deployment the same `tenant_id` on `CallerContext` needs to scope memory search, conversation ownership, and rate-limit buckets too — otherwise you've plugged one leak and left three. The [Secure Multi-Tenant Agent Platform guide](../../guides/secure-multi-tenant-platform.md) walks the full isolation model end-to-end, showing how a single `CallerContext` propagates a tenant boundary through every per-user surface at once.

The cache is also a cost lever, and the two goals reinforce each other: correct per-tenant partitioning is exactly what lets you cache aggressively *without* the leak, so the savings are real rather than a liability. If cost is what brought you here, [How to Cut Token Cost for a Multi-Tenant AI Agent](cut-token-cost-multi-tenant-ai-agent.md) covers the cache alongside the other levers. And because the cache key also fingerprints memory and conversation context, it interacts with how you manage history — if you truncate context carelessly you can both poison cache keys and drop facts the agent still needs, a trap [Why trim_messages Drops Facts Your Agent Still Needs](trim-messages-vs-facts-ledger.md) unpacks in detail.

## Frequently asked questions

### Can a paraphrase ever cross tenants in Promptise?

No — not through the semantic cache. The similarity search is scoped to the caller's partition before any embedding comparison happens, so a query from GLOBEX is only ever compared against GLOBEX's entries. Even a perfect paraphrase of ACME's question can't match ACME's cached answer, because ACME's entries are not in the set being searched. With `tenant_id` set, the partition is a `user:t:<sha256>` hash disjoint from every other tenant's, so two tenants reusing the same `user_id` still can't collide.

### What happens if I forget to pass a CallerContext?

The cache fails closed: caching is skipped for that request, nothing is written, and a debug log records `"Cache: no CallerContext or user_id provided"`. You lose the cache hit on that path — you never lose isolation. This is deliberately the opposite of caching globally when identity is missing, which is how a shared-store cache leaks.

### Is this different from just using per-user cache keys?

Yes, in the case that bites multi-tenant apps. Plain per-user keying (`user:alice`) collides if two tenants both have a user named `alice`. Promptise's tenant-qualified key derives from `CallerContext.isolation_key` (`"{tenant_id}::{user_id}"`) and hashes into a namespace disjoint from the untenanted one, so the ACME `alice` and the GLOBEX `alice` are provably separate partitions. See the per-user isolation section of the [Semantic Cache docs](../../core/cache.md) for the exact key layout.

### When is `scope="shared"` safe to use?

Only when responses contain no tenant- or user-specific data — public FAQ, docs, weather, a single-user CLI. Promptise makes you say so explicitly with `shared_data_acknowledged=True`, precisely so you can't back into a shared cache by accident. If the agent ever touches accounts, orders, or personal data, keep the default `per_user` scope.

## Next steps

Read the per-user isolation section of the [Semantic Cache docs](../../core/cache.md) to see the tenant-qualified key layout in full, then confirm your own deployment fails closed: invoke your agent once with no `CallerContext` and check that no `cache.store` event fired and nothing was persisted. When you're ready to extend the same tenant boundary across memory, conversations, and rate limits, work through the [Secure Multi-Tenant Agent Platform guide](../../guides/secure-multi-tenant-platform.md).
