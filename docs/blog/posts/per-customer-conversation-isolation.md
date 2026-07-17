---
title: "Per-Customer Chat History: Enforcing Session Ownership"
description: "Persisting chat threads is easy; stopping one customer from reading another's thread by guessing a session id is the hard part, and single-user ownership…"
keywords: "per-customer conversation isolation, session ownership enforcement multi-tenant, stop guessing another customer's session id, sessionaccessdenied tenant, who owns this conversation thread"
date: 2026-07-16
slug: per-customer-conversation-isolation
categories:
  - Multi-Tenancy
---

# Per-Customer Chat History: Enforcing Session Ownership

**Per-customer conversation isolation** is the difference between persisting a chat thread and actually keeping one customer from reading another's — and the second is the part most stacks quietly skip. Wiring up a database to store messages by `session_id` is a solved problem: pick SQLite, Postgres, or Redis and you have durable history. The hard problem starts the moment a second customer exists, because a session id is just a string, and any authenticated caller who can produce that string can ask for the thread behind it. This post shows how Promptise Foundry turns "who is allowed to read this thread" from a check you have to remember into an invariant enforced on every read, write, and delete — keyed on the `tenant::user` isolation key so an enumerated session id from another customer is denied, not served.

## Persisting a thread is easy; owning it is the hard part

Storage answers the question "what messages are in session `thread-42`?" It does not answer "is this caller allowed to see session `thread-42`?" Those are different questions, and the second is an authorization question that lives above the store.

The failure mode is concrete. Your `chat()` endpoint takes a `session_id` from the request, loads the messages, and returns them. If that lookup is nothing more than `SELECT ... WHERE session_id = ?`, then the only thing standing between customer A and customer B's transcript is the secrecy of the id. Session ids leak: they show up in logs, in URLs, in client-side state, in support tickets. And even when they don't leak, they can be guessed or enumerated if you generated them sequentially. The result is a broken-object-level-authorization bug — the most common serious API vulnerability there is — wearing a chat-history costume.

So the real requirement is not "persist threads." It is: **every operation on a thread verifies that the caller owns it**, and the ownership key is one an attacker in another tenant cannot forge.

## What "who owns this conversation thread" really means

Say Acme and Globex both use your product, and both happen to have a user called `alice`. Acme's Alice opens a support thread and pastes a database rotation schedule into it. Now Globex's Alice — a completely different human at a different company — sends a request with `session_id="thread-42"`, either because she guessed it or because it leaked.

Two naive designs both fail here:

- **No ownership at all.** The store returns `thread-42` to anyone who names it. Globex's Alice reads Acme's secret. This is the missing-`WHERE`-clause bug.
- **Single-user ownership only.** You record that `thread-42` is owned by `alice` and check `owner == caller.user_id`. Globex's Alice *is* `alice`. The check passes. She still reads Acme's secret.

Single-user ownership is not enough once two tenants can share a `user_id`. The owner has to be a compound identity that includes the tenant, so that `acme`'s Alice and `globex`'s Alice are different principals even though their user id is identical. That is the crux of per-customer isolation, and it is the same reasoning that drives [multi-tenant RAG isolation in a shared vector store](multi-tenant-rag.md) — the isolation key, not the storage layer, is where correctness lives. We go deeper on the specific `alice`-collision trap in [Same user_id, Two Tenants: Why That Isn't Isolation](same-user-id-across-two-tenants.md).

## Session ownership enforcement, keyed on tenant::user

Promptise ties conversation ownership to `CallerContext`. When you pass a `caller` with both a `user_id` and a `tenant_id`, the agent derives a single `isolation_key` — `"{tenant_id}::{user_id}"` — and that composite string, not the bare `user_id`, becomes the session owner. The derivation is injective by construction: a `tenant_id` may not contain `:` and a `user_id` may not contain `::`, so the tenanted keyspace (always containing `::`) is provably disjoint from raw user ids. An attacker cannot forge `acme::alice` by naming their own user `acme::alice` — that value is rejected at construction time.

On the first `chat()` call, the session is assigned to the caller's isolation key. On every subsequent call, the store checks ownership *before* it loads a single message. Here is the whole thing end to end — a runnable script whose only requirement is `OPENAI_API_KEY`:

```python
import asyncio

from promptise import build_agent, CallerContext
from promptise.conversations import SQLiteConversationStore, SessionAccessDenied


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You are a per-customer support assistant.",
        conversation_store=SQLiteConversationStore("support.db"),
    )

    # Same user_id, two different customers -> two disjoint isolation keys.
    acme = CallerContext(user_id="alice", tenant_id="acme")
    globex = CallerContext(user_id="alice", tenant_id="globex")

    # Acme's Alice opens a thread and stores something sensitive.
    # This assigns ownership of "thread-42" to the key "acme::alice".
    await agent.chat(
        "Note for later: our DB password rotates Fridays at 02:00 UTC.",
        session_id="thread-42",
        caller=acme,
    )

    # Globex's Alice guesses the session id and tries to read it.
    try:
        await agent.chat(
            "What did I note earlier?",
            session_id="thread-42",
            caller=globex,
        )
    except SessionAccessDenied as exc:
        print(f"Denied: {exc}")
        print("attempted:", exc.attempted_user_id)  # globex::alice
        print("owner:    ", exc.owner_user_id)       # acme::alice

    await agent.shutdown()


asyncio.run(main())
```

Globex's Alice does not get an empty thread or a generic reply — she gets a `SessionAccessDenied` (a subclass of `PermissionError`) carrying `session_id`, `attempted_user_id`, and `owner_user_id`, so your API layer can turn it into a clean `403` and your audit trail records exactly who tried to reach whose thread. The full lifecycle — ownership check, history load, invoke, persist, assign-on-new — is documented in the [conversation persistence reference](../../core/conversations.md).

If your tenant identity arrives as a JWT claim rather than a hand-built `CallerContext`, the same `tenant_id` is extracted server-side by `AuthMiddleware` and threaded through with a single `require_tenant=True` flag; the [Multi-Tenancy guide](../../mcp/server/multi-tenancy.md) covers that path, and the [secure multi-tenant platform guide](../../guides/secure-multi-tenant-platform.md) walks the whole token-to-storage chain across both the agent and the MCP server.

## What other frameworks do today

It is worth being precise about where the line actually falls, because durable chat history is common and the gap is narrow but real.

LangGraph ships first-class **checkpointers** — `MemorySaver`, `SqliteSaver`, `PostgresSaver` and their async variants — that persist graph state and retrieve it by `thread_id` (with an optional `checkpoint_ns` namespace). That is genuine, well-built durability: you pass `config={"configurable": {"thread_id": "thread-42"}}` and get the thread's state back. What the checkpointer does *not* include is a caller-ownership check. Whoever presents the `thread_id` receives the state; there is no built-in notion of "which principal owns this thread," and no tenant dimension in the key. Authorization is therefore left entirely to your application code — which means cross-customer access is exactly one missing `WHERE` clause and one guessed `thread_id` away, and the correctness of your isolation rests on every endpoint remembering to add that clause.

Promptise's edge is not that this is impossible elsewhere — you can absolutely build ownership on top of a LangGraph checkpointer. The difference is that in Promptise it is **structural**: ownership is enforced inside `chat()`, `get_session()`, `update_session()`, and `delete_session()`, and the owning identity is the `tenant::user` isolation key rather than a bare user id. There is no per-handler check to forget, and no code path that loads a thread without first verifying the caller. The capability is an invariant, not a convention you re-implement per route.

## Every read, write, and delete is ownership-checked

The enforcement is not special-cased to `chat()`. Every operation that touches a session honors the same rule, and listing filters at the database level so a caller never even sees another customer's session ids:

```python
# Ownership is verified before the operation runs.
info = await agent.get_session("thread-42", user_id="acme::alice")

# list_sessions filters by owner in the SQL WHERE clause —
# other customers' sessions are never returned, not just hidden.
mine = await agent.list_sessions(user_id="acme::alice", limit=20, offset=0)

# Delete and metadata updates are ownership-checked too.
await agent.delete_session("thread-42", user_id="acme::alice")
await agent.update_session(
    "thread-42",
    calling_user_id="acme::alice",  # verified before the change applies
    title="Rotation schedule",
)
```

Three behaviors make this safe by default rather than safe-if-you-remember:

- **Fail closed.** The ownership check runs *before* messages are loaded. If the store is unreachable and ownership cannot be verified, the call raises rather than proceeding — an unverifiable request is a denied request, never a served one.
- **Unowned stays open, owned stays locked.** A session with no owner (created without a `user_id`) is accessible; the moment a session has an owner, only that exact isolation key passes. There is no "close enough."
- **Database-level scoping.** `list_sessions(user_id=...)` compiles to a `WHERE user_id = ?` filter, so pagination and listing can never leak another customer's session ids to enumerate in the first place.

Pass the raw `user_id` for a single-tenant app, or pass the `tenant::user` isolation key (which `chat(caller=...)` derives for you automatically) for a multi-tenant one. The mechanism is identical; only the shape of the key changes.

## Frequently asked questions

### Who owns this conversation thread?

The first caller to write to it. On the initial `chat()` call, Promptise assigns the session to that caller's identity — the plain `user_id` in a single-tenant app, or the `tenant::user` isolation key when a `CallerContext.tenant_id` is set. Every later read, write, update, or delete compares the caller's key against that recorded owner.

### What does SessionAccessDenied mean in a multi-tenant setup?

`SessionAccessDenied` (a `PermissionError`) is raised when a caller's isolation key does not match the session's owner. In a multi-tenant setup the owner is `tenant::user`, so `globex::alice` is denied access to a thread owned by `acme::alice` even though both users are named `alice`. The exception carries `session_id`, `attempted_user_id`, and `owner_user_id` for your API response and audit log.

### Can a customer read another's thread by guessing a session id?

No. Guessing the id is not enough — the caller must also present the isolation key that owns it. A request from another customer for a valid-but-unowned session id raises `SessionAccessDenied` before any message is loaded. This is why you can stop treating session ids as secrets and stop guessing another customer's session id as a viable attack.

### Isn't single-user ownership enough?

Not once two tenants can share a `user_id`. Single-user ownership checks `owner == user_id`, which passes for two different people who both happen to be `alice`. Session ownership enforcement for a multi-tenant app has to key on `tenant::user` so the tenant is part of the principal, which is exactly what `CallerContext.isolation_key` provides.

## Next steps

Call `agent.chat(..., caller=CallerContext(user_id, tenant_id))` and every session read, write, update, and delete is ownership-checked against the `tenant::user` key automatically — no per-handler authorization to write or remember. Start with the [conversation persistence reference](../../core/conversations.md) to wire up a `SQLiteConversationStore` or `PostgresConversationStore`, then read the [Multi-Tenancy guide](../../mcp/server/multi-tenancy.md) to pull the tenant from a JWT claim, and follow the [secure multi-tenant platform guide](../../guides/secure-multi-tenant-platform.md) to see the full token-to-storage isolation boundary in one place.
