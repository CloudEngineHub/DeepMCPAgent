---
title: "How to GDPR-Delete One User's Cached LLM Answers"
description: "A data-subject erasure request shouldn't force you to flush every customer's cache. Because mainstream LLM caches key entries by a prompt or content hash…"
keywords: "gdpr delete cached llm responses, purge_user llm cache, right to erasure ai agent, delete one user cached responses, per-user cache partition delete, gdpr llm cache compliance"
date: 2026-07-16
slug: gdpr-delete-cached-llm-responses
categories:
  - Cost & Efficiency
---

# How to GDPR-Delete One User's Cached LLM Answers

To **gdpr delete cached llm responses** for a single data subject, you should be able to name the user, call one function, and be done — without touching any other customer's cached data. That is not how most LLM caches work. A semantic cache keys each entry by a prompt or content hash so it can serve a saved answer for a similar query, and that hash carries no notion of *who* asked. So when a user exercises their Article 17 right to erasure, you are left with two bad options: flush the entire cache and destroy every customer's hit rate to erase one person, or write and maintain a bespoke key-scanning script that walks the store looking for entries that "belong" to that subject — entries the cache never labelled as theirs in the first place. Promptise Foundry closes that gap structurally: each subject lives in its own cache partition, and `SemanticCache.purge_user(user_id, tenant_id=...)` drops exactly that partition and nothing else.

This post shows the mechanism, the exact code to wire an erasure endpoint, and — honestly — what `purge_user` does and does not cover.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## Why erasing one user is hard when the cache has no caller dimension

Right to erasure (GDPR Article 17) is a per-subject operation. The request is always "delete the data you hold about *this person*," never "delete everyone's data." A compliant system therefore needs a way to enumerate or address one subject's records and remove only those.

A conventional LLM response cache is built for the opposite goal. Its key is designed to be shared as widely as possible, because a wider key means more hits and lower cost. The canonical key is something like `hash(prompt + model_params) → completion`, or for a semantic cache, `embedding(prompt)` matched within a similarity threshold against one vector store. Neither carries the caller's identity, because identity would fragment the key and reduce reuse. That trade-off is deliberate and, for cost, correct — right up until a data subject asks to be forgotten.

At that point the missing caller dimension becomes a liability. You cannot ask "which entries are this user's?" because no entry was ever tagged with a user. Your realistic choices are:

- **Flush everything** (`cache.clear()` or the equivalent). Correct for compliance, catastrophic for cost — you have just cold-started every other customer to erase one.
- **Build a custom scan.** Iterate the store, reconstruct which entries came from the subject (often impossible after the fact, because the raw prompt may itself be the personal data you're trying to purge), and delete the matches. This is code you write, test, and own forever, and it is exactly the kind of ad-hoc erasure logic auditors scrutinise.

The clean fix is to make identity part of the cache's structure *before* an erasure request ever arrives — so that "this user's entries" is a first-class, addressable partition rather than something you reverse-engineer under a 30-day deadline.

## What other frameworks do today

Be precise and fair here: the mainstream LLM caches are good at reuse, and every one of them can be namespaced by hand. The specific gap is that none of them partition by caller by default, so none of them ship a single-subject erasure call. Deleting one person means flushing all or scanning keys you built yourself.

- **LangChain.** Its cache layer (`RedisSemanticCache`, `RedisCache`, `GPTCache`, `InMemoryCache`, and friends) implements the `BaseCache` interface, whose mutation surface is `update()`, `lookup()`, and `clear()`. `RedisSemanticCache` keys entries on the prompt embedding plus the model string (`llm_string`) — there is no user or tenant field in the key. `clear()` flushes the whole store (optionally scoped to one `llm_string`), not one subject. Per-user isolation is something you assemble from separate cache instances or key prefixes and then erase yourself; the framework ships no `purge_user` equivalent.
- **GPTCache**, used directly or through **LlamaIndex's** GPTCache integration, has the same shape: an embedding of the prompt is matched against a shared store. Eviction is by policy (LRU/FIFO) or a full flush, and there is no built-in "delete everything for caller X" operation, because caller X was never part of the key.
- **AutoGen's `Cache`** is worth naming accurately because it is a different mechanism — an *exact-match* key-value cache keyed on the serialized request plus a manual `seed`. If, and only if, you deliberately assigned each subject a distinct seed, you can delete that seed's namespace. That is opt-in bookkeeping you maintain, not a per-user erasure API the framework hands you.
- **CrewAI's `CacheHandler`** caches *tool outputs* keyed on tool name plus arguments. There is no caller dimension at all — it is a per-crew execution cache — so single-subject erasure isn't expressible against it.

The honest summary: none of these lack the *ability* to delete data — `clear()` and manual scans exist. What they lack is a caller-scoped structure that makes deleting *exactly one subject* a single, safe call. In every case, per-subject erasure is code you design, build, and defend. Promptise's edge is not that erasure is possible; it's that per-subject erasure is **structural and first-class**, because the identity you'd otherwise have to reconstruct was baked into the cache key from the first write.

## How purge_user erases exactly one subject's partition

Promptise's `SemanticCache` defaults to `scope="per_user"`. Every cached entry is stored under the caller's own partition, and the [Semantic Cache docs](../../core/cache.md) describe the key layout in full:

- An untenanted caller's entries are keyed under `user:<id>`.
- A tenant-scoped caller (`CallerContext(tenant_id="acme")`) is keyed under an **injective, colon-prefixed hash** — `user:t:<sha256>` — derived from a length-prefixed `(tenant, user_id)` pair, so two tenants that both have a user called `alice` land in provably separate partitions. The tenanted and untenanted keyspaces are disjoint by construction.

Because the partition is addressable, erasure is a single call. `purge_user(user_id, *, tenant_id=None)` re-derives the exact same scope key the entries were written under and deletes precisely that key — the backends match on the **exact** scope key, never a prefix, so purging `user:12` can never touch `user:123`. It returns the number of entries removed and emits a `cache.purged` observability event you can log for your audit trail.

Here is the full flow, runnable end to end against a real model and MCP server:

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

    # Two different subjects each ask something. Each answer is stored in
    # that subject's OWN partition — user:alice and user:bob.
    alice = CallerContext(user_id="alice")
    bob = CallerContext(user_id="bob")

    await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What is my current plan?"}]},
        caller=alice,
    )
    await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What is my current plan?"}]},
        caller=bob,
    )

    # Alice sends a right-to-erasure request. Delete ONLY her partition.
    removed = await cache.purge_user("alice")
    print(f"Erased {removed} cached entries for alice")

    # Bob's cache is untouched — his next identical question is still a hit.
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What is my current plan?"}]},
        caller=bob,
    )
    print(result["messages"][-1].content)

    await agent.shutdown()


asyncio.run(main())
```

Alice's entries are gone; Bob's are intact. You did not flush the store, and you did not write a key scanner — you named the subject and the partition disappeared.

For a tenant-scoped deployment, pass the same `tenant_id` the entries were stored under so the derivation matches exactly:

```python
# Entries were written by CallerContext(user_id="alice", tenant_id="acme")
removed = await cache.purge_user("alice", tenant_id="acme")
```

Because the tenant-qualified key is injective, this erases the ACME `alice` and provably cannot touch a `alice` in any other tenant.

## Wire a GDPR erasure endpoint that deletes one partition in a single call

The erasure handler is just `purge_user` behind whatever transport your compliance workflow uses — an HTTP route, a queue consumer, or a role-guarded admin MCP tool. Keep the `SemanticCache` instance you passed to `build_agent()` in scope and call it directly:

```python
async def erase_subject(cache: SemanticCache, user_id: str,
                        tenant_id: str | None = None) -> dict:
    """Fulfil a GDPR Article 17 request for one data subject.

    Deletes exactly that subject's cache partition and returns an
    auditable receipt. No other subject's entries are affected.
    """
    removed = await cache.purge_user(user_id, tenant_id=tenant_id)
    return {
        "subject": user_id,
        "tenant": tenant_id,
        "cache_entries_removed": removed,
        "status": "erased",
    }
```

The returned count is your receipt: a concrete, loggable number for the erasure record you hand to your DPO or auditor. Because `purge_user` also fires a `cache.purged` event carrying `{"user_id", "entries_removed"}`, you can route erasures into the same [observability timeline](../../core/cache.md#observability) as the rest of your cache activity and prove the deletion happened.

One deliberate design choice keeps this honest: the same on-device embedding model that [tool optimization](../../core/tool-optimization.md) uses also powers the cache, so a subject's queries were never sent to a third-party embedding API in the first place. There is no external vector service you must *also* file an erasure request against — the personal data never left your environment, so purging your own partition is the whole job.

## What purge_user covers — and what it deliberately doesn't

Erasure is only trustworthy if you know its exact boundary, so state it plainly:

- **`purge_user` erases the `per_user` scope only.** A cache created with `scope="per_session"` keys entries by session, not user, so `purge_user` will not remove them — you erase those by their session scope instead. This is called out directly in the method's docstring so you don't assume broader coverage than it offers.
- **`scope="shared"` has no per-subject dimension by design.** Shared scope is for public, non-personal answers (FAQ, docs, weather) and requires `shared_data_acknowledged=True`. If entries could contain personal data, keep the default `per_user` scope so erasure stays addressable.
- **The cache is one store among several.** Erasing a subject's cache does not erase their [memory](../../core/cache.md), conversation history, or audit records — those are separate stores with their own erasure paths. `purge_user` is the cache seam of a broader erasure workflow, not the whole workflow.
- **Redis and in-memory behave identically.** Both backends delete by the exact scope key, so a purge on a Redis-backed cache removes the same one partition — and only that partition — across every worker sharing the store.

Getting the boundary right is the same discipline that keeps the cache from leaking in the first place: identity, not content, decides who a cached entry belongs to. That principle drives both erasure and isolation, which is why [Can a Paraphrase Leak Another Tenant's Cached Answer?](semantic-cache-cross-tenant-leak.md) and this post are two sides of one caller-scoped design.

## Frequently asked questions

### Does deleting one user's cache flush everyone else's?

No. `purge_user` re-derives the single scope key that subject's entries were stored under and deletes exactly that key. The backends match on the exact key, never a prefix, so `purge_user("user-12")` cannot affect `user-123`, and every other subject's cache — and its hit rate — is untouched. That is the whole point: single-subject erasure without a store-wide flush.

### How do I erase a subject in a multi-tenant deployment?

Pass the `tenant_id` the entries were written under: `await cache.purge_user("alice", tenant_id="acme")`. The tenant-qualified scope key is an injective `user:t:<sha256>` hash, so this erases the ACME `alice` and provably cannot touch an `alice` in any other tenant. If you erase without a `tenant_id` you target the untenanted `user:alice` partition, which is a disjoint keyspace — so use the same identity you cached under.

### What does purge_user return, and can I use it as an erasure receipt?

It returns an `int` — the number of entries removed — and emits a `cache.purged` observability event with `{"user_id", "entries_removed"}`. Log that count and event as your Article 17 receipt: it's a concrete record that the deletion ran and how much it removed.

### Does purge_user also delete the user's memory and conversation history?

No. `purge_user` erases the semantic cache partition only. Memory, conversation stores, and audit logs are separate systems with their own erasure paths. Treat `purge_user` as the cache step of a broader erasure runbook, not a one-call fix for every store that holds the subject's data.

### How is this different from just calling clear() like other frameworks?

`clear()` flushes the whole cache — correct for compliance, but it cold-starts every customer to erase one. `purge_user` deletes a single addressable partition because Promptise stored each subject in its own partition from the first write. You get compliant erasure without sacrificing everyone else's cache.

## Next steps

Read the [GDPR Compliance](../../core/cache.md) and per-user isolation sections of the Semantic Cache docs to see the exact `user:<id>` and tenant-qualified `user:t:<sha256>` key layout, then wire an erasure endpoint that calls `purge_user(user_id, tenant_id=...)` and logs the returned count as your receipt. To keep the aggressive caching that makes erasure worth doing, pair it with [How to Cut Token Cost for a Multi-Tenant AI Agent](cut-token-cost-multi-tenant-ai-agent.md); to confirm the same caller-scoped design also keeps tenants from reading each other's entries, work through [Can a Paraphrase Leak Another Tenant's Cached Answer?](semantic-cache-cross-tenant-leak.md).
