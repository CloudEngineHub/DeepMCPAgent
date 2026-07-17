---
title: "Conversation Persistence for LLM Agents in Python"
description: "Every chat app needs durable history, yet most tutorials hardcode one database and skip session ownership. This shows the same application code running…"
keywords: "conversation persistence, persist chat history Python, LLM chat history database, SQLite chat store, Postgres conversation store"
date: 2026-07-16
slug: conversation-persistence
categories:
  - Memory & RAG
---

# Conversation Persistence for LLM Agents in Python

Conversation persistence is the difference between a demo and a product: without it, every LLM agent forgets the last message the moment the process restarts, and a multi-user app happily serves one person's thread to another. Most tutorials hardcode a single database and quietly skip session ownership, so you inherit a rewrite the day you move from SQLite to Postgres — and a security bug you won't notice until someone reports it. This post shows how to persist chat history in Python behind one `ConversationStore` protocol, swap backends without touching application code, and enforce session ownership so users can only load their own conversations.

<!-- more -->

## Why conversation persistence is a first-class concern

An LLM agent is stateless. Each call to the model is a fresh request; the "memory" you see in ChatGPT is an illusion the application maintains by replaying prior messages back into the prompt. If you don't store those messages somewhere durable, three things break:

- **Restarts wipe context.** A deploy, a crash, or an autoscaler killing a pod erases every in-flight conversation.
- **Users collide.** Hold history in a module-level dict and two users on the same worker can read each other's threads.
- **You can't audit or resume.** Support can't reopen a ticket thread; users can't scroll back.

Durable conversation history is not the same thing as agent memory. A **conversation store** keeps the exact messages of a session in order and retrieves them by `session_id`. A **memory provider** stores semantic facts across sessions and retrieves them by similarity search. They complement each other, and Promptise lets you use both at once — the [memory guide](../../core/memory.md) covers the semantic side, and the [AI Agent Memory: The Complete Guide for Python Devs](ai-agent-memory.md) post explains when you need which. This article focuses on durable, ordered history: the exact transcript.

## One protocol, four backends: from SQLite to Postgres

Promptise defines a small `ConversationStore` protocol and ships four backends that implement it. Your agent code depends on the protocol, not the backend, so switching storage is a one-line change:

| Backend | Class | Best for |
|---|---|---|
| In-memory | `InMemoryConversationStore` | Tests, prototypes |
| SQLite | `SQLiteConversationStore` | Local dev, single-node apps |
| Postgres | `PostgresConversationStore` | Production, multi-node |
| Redis | `RedisConversationStore` | Ephemeral, high-throughput sessions |

Because they all satisfy the same protocol, the only line that changes between a laptop and a production cluster is the constructor. Everything downstream — the `chat()` calls, ownership checks, session listing — stays identical. That is the whole point of the [conversation store abstraction](../../core/conversations.md): pick a backend by operational need, not by how much rework you can stomach.

## Persist chat history in Python with `chat()`

Here is a complete, runnable example. `agent.chat()` loads history for the session, appends the new user message, invokes the model, and persists the updated transcript — automatically, on every call.

```python
import asyncio
from promptise import build_agent
from promptise.conversations import SQLiteConversationStore, generate_session_id


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You are a helpful support assistant.",
        conversation_store=SQLiteConversationStore("conversations.db"),
    )

    # Always mint session IDs server-side — never trust a client-supplied one.
    sid = generate_session_id()

    # First turn assigns the session to this user and persists it.
    await agent.chat("My order number is 10432.", session_id=sid, user_id="alice")

    # A later turn — even after a restart — reloads history from SQLite,
    # so the agent still knows the order number.
    reply = await agent.chat(
        "What was my order number?", session_id=sid, user_id="alice"
    )
    print(reply)  # -> references order 10432

    await agent.shutdown()


asyncio.run(main())
```

Set `OPENAI_API_KEY`, run it, then run it again: the second process still answers correctly because the transcript lives in `conversations.db`, not in memory. `generate_session_id()` produces an unguessable identifier — important because a predictable session ID is a hijacking vector once ownership is in play.

## Session ownership: stop users from loading each other's threads

Durability is only half of production-grade persistence. The other half is making sure `session_id` A belongs to user A. Promptise enforces this at the `chat()` boundary whenever you pass `user_id`:

- The **first** `chat()` on a new session assigns that session to the caller.
- Subsequent calls from the **same** user are allowed.
- A call from a **different** user raises `SessionAccessDenied`.

```python
from promptise.conversations import SessionAccessDenied, generate_session_id

sid = generate_session_id()

# Alice creates and owns the session.
await agent.chat("Hello", session_id=sid, user_id="alice")

# Bob guesses the ID and tries to read her thread.
try:
    await agent.chat("Show me the history", session_id=sid, user_id="bob")
except SessionAccessDenied as e:
    print(e)  # "User 'bob' denied access to session 'sess_...' (owned by 'alice')"
```

Ownership isn't only checked on `chat()`. It also applies to `get_session()`, `update_session()`, and `delete_session()`, while `list_sessions(user_id=...)` filters at the database level so a user only ever sees their own conversations. The store itself stays pure data — no auth logic leaks into your MongoDB or Postgres adapter — because enforcement lives in the agent layer. If you also propagate a per-request `CallerContext`, the same `user_id` flows through memory, guardrails, and observability; the [context engine](../../core/context-engine.md) is what assembles history, injected memory, and the system prompt into the final request before each LLM turn.

## Choosing an LLM chat history database backend

The right LLM chat history database depends on your operational shape, not on the framework:

- **`SQLiteConversationStore`** — single file, WAL mode for concurrent reads, `pip install aiosqlite`. Perfect for local development, CLIs, and single-node deployments. Use `":memory:"` in tests.
- **`PostgresConversationStore`** — pooled `asyncpg` connections, auto-created `sessions` and `messages` tables with indexes, `table_prefix` so it's safe to share a database with other apps. This is the default for multi-node production.
- **`RedisConversationStore`** — `redis.asyncio` with optional TTL expiry (`ttl=86400`) and a key prefix. Ideal when sessions are ephemeral or you're serving very high throughput and are fine with time-bounded history.
- **`InMemoryConversationStore`** — bounded by `max_sessions` and `max_messages_per_session`, zero dependencies, gone on restart. Tests and throwaway prototypes only.

Every backend accepts `max_messages_per_session` so you can cap a rolling window and keep prompts bounded. Migrating from SQLite to Postgres is a constructor swap:

```python
from promptise.conversations import PostgresConversationStore

store = PostgresConversationStore(
    "postgresql://user:pass@localhost/appdb",
    table_prefix="promptise_",
    pool_min=2,
    pool_max=10,
)
# Pass store=... into build_agent() exactly as before — no other code changes.
```

## When a raw database or another framework is a better fit

Promptise's conversation store earns its keep when the agent should own the read/write/ownership cycle and you want to move between backends without rewrites. It's an honest trade-off, though, and it isn't always the right layer.

- **You already have a message schema and ORM.** If your app persists chat in an existing `messages` table through SQLAlchemy or Prisma and you want that model to remain the source of truth, keep it and feed messages into the agent yourself. Adding a second store is redundant.
- **You need cross-turn graph checkpointing, not just transcripts.** If your requirement is durable execution of a branching workflow — replaying arbitrary node state, not conversation messages — a graph checkpointer such as LangGraph's is purpose-built for that shape. Promptise's store persists conversations; it is not a general workflow checkpointer.
- **You're storing one process's scratch state.** For non-user-facing, single-tenant background work, a plain file or Redis key is simpler than a session-scoped store.

For anything user-facing and multi-tenant, the built-in stores usually win on effort: you get durability, backend portability, and session ownership without writing or reviewing that code yourself.

## Frequently asked questions

### What's the difference between a conversation store and a memory provider?

A conversation store persists the exact messages of a session in order and retrieves them by `session_id` — it answers "what did they say three messages ago?" A memory provider stores semantic facts across sessions and retrieves them by similarity — it answers "what do I know about this user?" They work together, and you configure both on `build_agent()`. See the [memory documentation](../../core/memory.md) for the semantic side.

### How do I stop one user from reading another user's chat history?

Pass `user_id` to `chat()`. The first call assigns session ownership; later calls from a different user raise `SessionAccessDenied`. Ownership is also enforced on `get_session()`, `update_session()`, and `delete_session()`, and `list_sessions()` filters by user at the database level. Always generate session IDs with `generate_session_id()` so they can't be guessed.

### Can I switch from SQLite to Postgres later without rewriting my app?

Yes — that's the point of the `ConversationStore` protocol. Your agent code calls `chat()` and the session methods against the protocol, so moving to production is a one-line constructor change from `SQLiteConversationStore(...)` to `PostgresConversationStore(...)`. The [long-term memory guide](llm-long-term-memory.md) shows the same portability principle applied to knowledge storage.

## Next steps

Drop in `SQLiteConversationStore` today and swap to `PostgresConversationStore` when you scale — zero application-code changes. Start with the [Quick Start](../../getting-started/quickstart.md) to stand up an agent in a few lines, then read the [conversation persistence guide](../../core/conversations.md) for the full store API, custom backends, and the ownership model.
