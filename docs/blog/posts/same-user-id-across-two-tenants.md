---
title: "Same user_id, Two Tenants: Why That Isn't Isolation"
description: "Two customers can both have a user named 'alice', and if memory, cache, and threads key on user_id alone their data collides silently. Beyond that collision…"
keywords: "same user_id across two tenants, user id collision multi-tenant, why user_id isn't tenant isolation, injective isolation key tenant user, prevent tenant impersonation isolation key"
date: 2026-07-16
slug: same-user-id-across-two-tenants
categories:
  - Multi-Tenancy
---

# Same user_id, Two Tenants: Why That Isn't Isolation

The problem with the **same user_id across two tenants** is that it looks fine right up until it doesn't: two of your customers, Acme and Globex, each have a user literally named `alice`, and the day both are active, whatever you key memory, cache, and conversation history on had better distinguish them. If that key is `user_id` alone, it doesn't — and the failure is silent. No exception, no log line, just Acme's `alice` occasionally reading Globex's `alice`'s data. This post is about why a per-user key is not a per-tenant key, what an *injective* isolation key is, and the subtler risk most treatments skip: not just accidental collision, but deliberate **forgery** of another tenant's namespace.

<!-- more -->

## The collision you won't catch in a demo

In a demo you have one tenant, so `user_id` is unique and everything works. The moment you onboard a second customer, `user_id` stops being globally unique — it's only unique *within* a tenant. Your identity provider issues `alice` inside Acme and `alice` inside Globex, and both are correct. The bug is entirely on your side, in how you derive storage keys.

Walk the surfaces that hold per-user state in a typical agent:

- **Long-term memory** — a vector store scoped by `user_id`. Search for `alice`'s memories and you get both Acmes' and Globex's, blended.
- **Semantic cache** — an entry keyed by `user_id`. Acme's `alice` asks a question, Globex's `alice` asks something similar, and the cache serves Acme's cached answer across the tenant boundary.
- **Conversation history / threads** — a session owned by `user_id`. Ownership checks pass for the wrong tenant.

Each of these is a *silent* leak. Nothing crashes, because `alice == alice` is a perfectly valid string comparison. That's what makes it dangerous: it survives code review, it survives your test suite (which almost certainly uses one tenant), and it surfaces in production as a support ticket that reads "why can I see another company's data?" Cross-tenant leakage is the single worst incident class a multi-customer platform can ship.

## What isolation actually requires: an injective key

Isolation is a property of the key, not of the store. You need a function that maps `(tenant, user)` to a storage key such that two *different* principals never map to the *same* key — an **injective** map. Prefixing "usually" isn't it; a convention that every call site remembers to write `f"{tenant}:{user}"` fails the first time one site forgets.

Promptise Foundry makes the derivation a single, first-class property. Every request carries a `CallerContext`, and its `isolation_key` is the one thing every per-user surface keys on:

```python
from promptise import CallerContext

# Two customers, each with a user literally named "alice"
acme_alice   = CallerContext(user_id="alice", tenant_id="acme")
globex_alice = CallerContext(user_id="alice", tenant_id="globex")

# Same user_id — but the isolation keys are disjoint.
assert acme_alice.isolation_key   == "acme::alice"
assert globex_alice.isolation_key == "globex::alice"
assert acme_alice.isolation_key != globex_alice.isolation_key
```

When a `tenant_id` is set, `isolation_key` is `"{tenant_id}::{user_id}"`; with no tenant it's the plain `user_id`. That single derivation is what the semantic cache, memory scoping, and conversation ownership all read — so the same `user_id` under two tenants produces two disjoint keyspaces, structurally, with no per-call-site prefixing to forget. The [multi-tenancy reference](../../mcp/server/multi-tenancy.md) lists every surface that inherits this key: cache, memory, conversation ownership, rate-limit buckets, and audit entries.

## The subtler failure: forging a tenant's namespace

Accidental collision is the obvious risk. The one people miss is *forgery*: a crafted `user_id` that impersonates a real tenant's namespace. Suppose you compose keys as `f"{tenant}::{user}"` but only reject the literal `"::"` substring inside each part. That's still not injective, for two reasons.

First, the separator can be **synthesized at the boundary**. With `tenant="ac"` + `user=":me::alice"`... it's easy to construct two different `(tenant, user)` pairs whose naive join produces the identical string. A tenant ending in `:` and a user starting with `:` collapse the boundary.

Second — and this is the impersonation case — an *untenanted* caller with `user_id="acme::alice"` derives the key `"acme::alice"`, which is byte-for-byte identical to the *tenanted* pair `(tenant="acme", user="alice")`. One principal reads another's data with no tenant assigned at all.

Promptise closes both at construction time. `CallerContext.__post_init__` forbids any colon in `tenant_id` (so the first `:` in the key unambiguously begins the `::` separator, killing the boundary-synthesis collision) and forbids `"::"` anywhere in `user_id` (so the tenanted keyspace — which always contains `::` — stays provably disjoint from the untenanted one, a bare `user_id`). Both violations raise `ValueError` before the object exists:

```python
from promptise import CallerContext

# A raw user_id can't forge a tenant's namespace: construction fails closed.
try:
    CallerContext(user_id="acme::alice")          # no tenant, crafted id
except ValueError as e:
    print("rejected:", e)   # user_id must not contain '::' — it is the separator

# And a tenant can't smuggle the separator in either.
try:
    CallerContext(user_id="alice", tenant_id="ac:me")
except ValueError as e:
    print("rejected:", e)   # tenant_id must not contain ':'

# Single colons in user_id are still fine — real SSO subjects work unchanged.
ok = CallerContext(user_id="google:114...:alice", tenant_id="acme")
assert ok.isolation_key == "acme::google:114...:alice"
```

Both snippets above run with nothing but `pip install promptise` — no API key, no network. That's the point: the injectivity guarantee is a property of the type, checkable in a unit test, not a runtime hope. A raw `user_id` of `acme::alice` can *never* become a valid isolation key, so it can never collide with the tenant `acme`. The keyspace is disjoint by construction.

## What other frameworks do today

To be fair about the delta, most agent frameworks let you scope state per user — they simply don't make tenant isolation a property of the key.

- **LangGraph** keys durable state on a single `thread_id` (`config={"configurable": {"thread_id": ...}}`), and its long-term `BaseStore` uses namespace tuples *you compose yourself*, e.g. `(user_id, "memories")`. There's a `checkpoint_ns`, but it's for subgraphs, not tenancy. So there's no built-in tenant dimension and no reserved separator: composing `(tenant, user)` safely — and keeping it injective — is on you.
- **CrewAI** scopes memory (and its Mem0 integration) by a single `user_id`. Real and useful, but the same shape: no built-in tenant axis in the key, so two customers sharing a `user_id` collide unless you hand-roll a composite.
- **AutoGen** memory (`ListMemory`, Mem0 memory) is likewise keyed per user id, with no tenant dimension baked into the derivation.

None of this means those frameworks *can't* isolate tenants — you can always build a safe composite key by hand. The precise delta is that in each of them the isolation key is a raw string *you* assemble at every call site, so (1) there's no tenant dimension unless you add one, and (2) nothing structurally prevents a crafted `user_id` from colliding with — or impersonating — another tenant's namespace. Promptise's contribution is to make the composition first-class (`isolation_key`) *and* the separator reserved (a construction-time `ValueError`), turning "we always prefix correctly" from a convention that fails silently into an invariant the type enforces. If you're weighing the broader trade-offs, the honest field guide in [Where Does tenant_id Come From? JWT Claim vs API Key](tenant-id-from-jwt-claim.md) covers where the tenant value should originate in the first place.

## Wire it once, and every surface inherits the key

You don't thread the key anywhere. Attach a `CallerContext` to each request and memory search, the semantic cache, and conversation ownership all key on `isolation_key` automatically:

```python
import asyncio
from promptise import build_agent, CallerContext
from promptise.config import HTTPServerSpec
from promptise.conversations import SQLiteConversationStore
from promptise.memory import ChromaProvider
from promptise.cache import SemanticCache

async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"crm": HTTPServerSpec(url="https://mcp.internal/crm/mcp")},
        conversation_store=SQLiteConversationStore("chat.db"),
        memory=ChromaProvider(persist_directory="./memory"),
        cache=SemanticCache(),
        instructions="You are a CRM assistant. Use tools to answer questions.",
    )

    acme_alice   = CallerContext(user_id="alice", tenant_id="acme")
    globex_alice = CallerContext(user_id="alice", tenant_id="globex")

    # Same name, same question — two disjoint keyspaces, no leak.
    await agent.chat("Remember my quota is 500 units.",
                     session_id="s-acme", caller=acme_alice)
    reply = await agent.chat("What's my quota?",
                             session_id="s-globex", caller=globex_alice)
    print(reply)   # Globex's alice has no memory of Acme's alice

    await agent.shutdown()

asyncio.run(main())
```

Because `ChromaProvider` in `PER_USER` scope reads the ambient `CallerContext` and passes its key into every `search`/`add`, Globex's `alice` searches `globex::alice` and finds nothing Acme wrote — the [memory guide](../../core/memory.md) documents that fail-closed scoping in full. The semantic cache scopes entries the same way, so one tenant is never served another's cached answer, and it keeps its usual 30–50% cost reduction; isolation and savings aren't a trade-off. For the end-to-end picture — token issuance, server-side guards, and per-tenant storage patterns wired together — the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md) is the reference build, and [Multi-Tenant RAG: Isolate Customer Data in a Shared Store](multi-tenant-rag.md) drills into keeping the vector index itself partitioned.

## Frequently asked questions

### Isn't a per-user key already tenant isolation?

Only if `user_id` is globally unique, and in a multi-tenant system it isn't — it's unique per tenant. Two customers can each have a user named `alice`. Keying on `user_id` alone maps both to the same storage key, which silently blends their memory, cache, and conversation history. You need the `(tenant, user)` pair, and it has to be injective.

### Why forbid `::` in user_id instead of just escaping it?

Escaping pushes the correctness burden onto every read and write path. Forbidding `"::"` in `user_id` (and any colon in `tenant_id`) at construction makes the tenanted keyspace — which always contains `::` — provably disjoint from the untenanted one. A raw `user_id` like `acme::alice` can never become a valid isolation key, so it can't collide with or forge the tenant `acme`. Single colons still work, so SSO subjects like `google:11445` are unaffected.

### What if a caller has no tenant_id?

Then `isolation_key` is the plain `user_id`, which is the correct behavior for single-tenant or internal use. Because `user_id` can never contain `"::"`, that untenanted key can never accidentally equal a tenanted `"{tenant}::{user}"` key. Mixed tenanted and untenanted callers coexist safely in the same store.

### Does this apply to the MCP server side too?

Yes. On the server, `AuthMiddleware` extracts the tenant from a signed JWT claim into `ClientContext.tenant_id`, and the same "tenant is part of the key" invariant governs rate-limit buckets and audit entries. See the [multi-tenancy reference](../../mcp/server/multi-tenancy.md) for the server-side guards (`RequireTenant`, `HasTenant`) and `require_tenant=True`.

## Next steps

If any per-user surface in your agent keys on `user_id` alone, treat it as a latent cross-tenant leak and fix the key, not the store. Construct `CallerContext(user_id=..., tenant_id=...)`, let `isolation_key` key every per-user surface with no manual prefixing and no forgeable overlap, and add a one-line unit test that asserts your two `alice`s produce disjoint keys. Start from the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md), confirm the storage-side scoping in the [memory guide](../../core/memory.md), and lock down the server boundary with the [multi-tenancy reference](../../mcp/server/multi-tenancy.md).
