---
title: "AI Agent Memory: The Complete Guide for Python Devs"
description: "Most 'agent memory' articles are thin pitches for a single vector DB. This hub maps the entire memory stack — short-term conversation history, long-term…"
keywords: "AI agent memory, LLM agent memory, persistent memory for AI agents, vector memory Python, agent memory architecture"
date: 2026-07-16
slug: ai-agent-memory
categories:
  - Memory & RAG
---

# AI Agent Memory: The Complete Guide for Python Devs

AI agent memory is not one feature — it is a stack of four distinct layers, and most guides only ever cover one of them. They pick a vector database, wire up a `search()` call, and declare the problem solved. That leaves you with an agent that recalls old facts but re-reads your entire conversation on every turn, recomputes identical answers, and blows past the context window the moment history gets long. This guide maps the whole stack: short-term conversation history, long-term vector memory, the semantic cache, and context budgeting. For each layer you'll see exactly which problem it solves and the runnable Python that turns it on in Promptise Foundry.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## The four layers of agent memory architecture

"Memory" is an overloaded word. When people say an LLM agent needs memory, they usually mean one of four things — and conflating them is why so many agents feel half-broken. A clean agent memory architecture separates them:

- **Short-term (conversation history)** — what was said in *this* session. Lives in the message list; bounded by the context window.
- **Long-term (vector memory)** — durable facts that outlive a session: preferences, past decisions, learned context. Retrieved by semantic similarity.
- **Semantic cache** — not knowledge at all, but a shortcut. Reuses a previous *answer* when a new question is close enough.
- **Context budget** — the referee that decides how much of the above actually fits into the prompt before the model is called.

Each layer has its own failure mode. Skip long-term memory and your agent is an amnesiac. Skip the cache and you pay for the same completion twice. Skip context budgeting and a long chat silently truncates the user's newest message. You want all four, and you want them to cooperate rather than fight over the same tokens.

## Short-term memory: the conversation buffer

Short-term memory is the cheapest layer because the model already has it: the running list of messages you pass to `ainvoke()`. The only real decisions are how many turns to keep and what to do when the buffer outgrows the window. In Promptise, a single agent built with `build_agent()` keeps this history in the message list you hand it, and the long-running [Agent Runtime](../../runtime/context.md) adds a bounded `ConversationBuffer` so a process that runs for days doesn't accumulate unbounded state.

The trap is treating short-term history as free. It is the first thing to overflow a small model's window, which is exactly why the context-budget layer (below) trims conversation history *before* it touches your identity prompt or tool definitions.

## Long-term memory: vector memory in Python

This is the layer people mean when they say "persistent memory for AI agents" — durable recall that survives restarts and spans sessions. Promptise ships three memory providers behind one async protocol, so you pick a backend by changing a constructor, not your application code:

| Provider | Search | Persistence | Best for |
|---|---|---|---|
| `InMemoryProvider` | Substring match | None | Tests, local dev |
| `ChromaProvider` | Local vector similarity | Optional (`persist_directory`) | Production semantic recall |
| `Mem0Provider` | Hybrid vector + graph | Managed by Mem0 | Multi-user, knowledge graphs |

The part that makes this feel like real memory rather than a database you have to babysit is **automatic search-and-inject**. When you attach a provider via `build_agent(memory=...)`, Promptise wraps the agent in a `MemoryAgent`. Before every invocation it takes the user's query, searches the provider, and injects the top matches as a sanitized system message — no memory tool for the model to remember to call, no retrieval code in your handler.

Here is the whole thing end to end with persistent, per-user vector memory in Python:

```python
import asyncio
from promptise import build_agent, CallerContext
from promptise.memory import ChromaProvider, MemoryScope

async def main():
    # Persistent, per-user vector memory — survives restarts, isolated by user_id
    memory = ChromaProvider(
        collection_name="agent_memory",
        persist_directory=".promptise/chroma",
        scope=MemoryScope.PER_USER,
    )

    # Store a durable fact once (normally written by your app or an earlier turn)
    await memory.add("Deploys go to AWS eu-central-1.", user_id="alice")

    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You are a helpful assistant. Use what you remember about the user.",
        memory=memory,
    )

    # No retrieval code, no memory tool — the agent searches and injects for you.
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Which region do my services run in?"}]},
        caller=CallerContext(user_id="alice"),
    )
    print(result["messages"][-1].content)   # → answer mentions eu-central-1

    await agent.shutdown()

asyncio.run(main())
```

Notice the fact was stored as `"Deploys go to AWS eu-central-1."` but retrieved by a differently worded question about regions. That is vector similarity doing its job — `ChromaProvider` embeds locally with `all-MiniLM-L6-v2`, so no extra API key is required. Swap it for `InMemoryProvider` in tests or `Mem0Provider` for hybrid graph search and every other line stays identical.

Two production details are worth calling out. First, `MemoryScope.PER_USER` makes memory fail-closed: the `user_id` from `CallerContext` propagates automatically, and a lookup without an owner raises rather than leaking another tenant's data. Second, injected content is sanitized against prompt injection before it reaches the model. Both behaviors, plus the full provider protocol and GDPR `purge_user()`, are documented in the [Memory Providers guide](../../core/memory.md). For patterns like summarizing old sessions into durable facts, the companion post [LLM Long-Term Memory in Python: A Practical Guide](llm-long-term-memory.md) goes deeper.

## Semantic cache: don't recompute what you already answered

The cache is the layer everyone forgets, and it is pure savings. Where long-term memory recalls *facts*, the semantic cache recalls *answers*: if a new query is semantically close to one you already served, you return the stored response instead of calling the model again. Promptise's `SemanticCache` reports a **30–50% cost reduction** on workloads with repeated or near-duplicate questions, and its embedding runs locally by default.

```python
from promptise import build_agent, SemanticCache

cache = SemanticCache()
cache.warmup()

agent = await build_agent(
    model="openai:gpt-5-mini",
    cache=cache,
)
```

The safety design matters here. The default scope is `per_user`, so one user's cached answers are invisible to another's — and if you invoke without a `CallerContext`, caching is silently skipped to prevent cross-user leakage. Cached responses are re-scanned by output guardrails on the way out, so redaction is never bypassed by a cache hit. The full ordering, Redis backend, and encrypted-at-rest option live in the [Semantic Cache guide](../../core/cache.md).

## Context budgeting: making memory fit the window

You can have perfect recall and still ship a broken agent if all that memory doesn't fit. The context budget is the referee that assembles every layer — identity, tools, memory, strategies, conversation history — counts the tokens, and trims by priority when the window is tight. Promptise's optional `ContextEngine` handles this deterministically:

- It knows the model's context window (auto-detected) and counts tokens exactly with `tiktoken` for OpenAI.
- It assigns a priority to every layer. Identity, the current user message, and tool definitions are marked required and never dropped.
- When the budget is exceeded it trims from the bottom up: conversation history first (oldest pairs), then learned strategies, then memory — never the user's actual question.

```python
from promptise import build_agent, ContextEngine

engine = ContextEngine(model="openai:gpt-5-mini")
engine.add_layer("company_policy", priority=7, content="We follow GDPR strictly.")

agent = await build_agent(model="openai:gpt-5-mini", context_engine=engine)
```

This is what stops the classic silent-truncation bug where a long chat quietly loses the newest turn. See the [Context Engine guide](../../core/context-engine.md) for the full 13-layer priority table and per-layer token reports, and [Context Window Management for LLM Agents, Explained](context-window-management.md) for the strategy behind the numbers.

## When a plain vector DB is the better fit

Be honest with yourself about scope. If you are building a pure retrieval-augmented search box — embed a corpus, query it, render results, no autonomous tool use — then a standalone vector database with a thin retrieval wrapper is simpler, and you don't need an agent framework's memory stack at all. Promptise's four layers earn their keep when memory has to *cooperate* with tool calls, per-user isolation, guardrails, caching, and a token budget under one identity. For a document search feature, that is overhead you can skip. For a stateful, multi-tenant LLM agent, assembling those layers by hand is where the real cost hides.

## Frequently asked questions

### What is the difference between AI agent memory and RAG?

RAG retrieves documents from a corpus to ground a single answer; agent memory persists facts about the interaction itself — user preferences, past decisions, session state — across many turns and sessions. They overlap in that both use vector search, but memory is written by the agent's own experience, while RAG reads from a curated knowledge base. Promptise supports both, and the semantic cache adds a third path: reusing whole answers.

### Do I need a vector database for LLM agent memory?

Not to start. `InMemoryProvider` gives you working memory with zero dependencies for tests and prototypes. You graduate to `ChromaProvider` for local semantic search with persistence, or `Mem0Provider` for hybrid graph search — and because all three share one protocol, upgrading is a one-line constructor swap, not a rewrite.

### How does per-user memory isolation work?

Set `scope=MemoryScope.PER_USER` on the provider. The `user_id` from `CallerContext` propagates automatically through the `MemoryAgent`, every read and write is scoped to that owner, and a lookup with no owner fails closed instead of leaking. `purge_user(user_id)` deletes everything for one user to satisfy GDPR erasure requests.

## Next steps

Read the [Memory Providers guide](../../core/memory.md) and add persistent, auto-injected memory to your agent in about five lines of config. If you're just getting set up, start with the [Quick Start](../../getting-started/quickstart.md), then layer the [semantic cache](../../core/cache.md) on top to stop paying twice for the same answer.
