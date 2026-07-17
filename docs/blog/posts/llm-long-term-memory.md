---
title: "LLM Long-Term Memory in Python: A Practical Guide"
description: "Top results stop at 'store embeddings in a vector DB' and leave out the hard half: retrieval. This walks the full loop — a swappable provider protocol that…"
keywords: "LLM long-term memory, vector memory in Python, agent memory with ChromaDB, persistent LLM memory, Mem0 vs Chroma memory"
date: 2026-07-16
slug: llm-long-term-memory
categories:
  - Memory & RAG
---

# LLM Long-Term Memory in Python: A Practical Guide

Adding **LLM long-term memory** to a Python agent is where most tutorials go quiet. They show you how to embed a string and push it into a vector database, then stop — as if storage were the whole job. It isn't. The hard half is retrieval: deciding *what* to pull back, *when* to inject it, and *how* to keep the loop identical whether you're running an in-memory stub in tests or a persistent store in production. By the end of this guide you'll have a working memory loop that auto-searches and injects relevant context before every model call, and you'll be able to swap backends by changing a single line.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## Why storing embeddings isn't the hard part

An LLM is stateless. Each call sees only the messages you hand it, so anything the agent "learned" three turns ago — the user's timezone, a project deadline, a stated preference — is gone unless you re-supply it. Long-term memory is the layer that survives across invocations, sessions, and process restarts.

The naive version is easy: embed text, store the vector, done. But that leaves the reader with three unsolved problems:

- **Retrieval timing.** Memory only helps if it's fetched *before* the model runs, not after.
- **Relevance.** Dumping every stored fact into the prompt wastes tokens and drowns the signal. You want the few entries that match the current query.
- **Injection.** Even relevant results are useless unless they land in the prompt in a form the model actually reads.

Promptise Foundry treats these as first-class concerns. The [memory guide](../../core/memory.md) frames memory as an integration layer with one job: before every invocation, search the store and inject what's relevant — no explicit tool call, no manual prompt surgery.

## The async MemoryProvider protocol: one interface, swappable backends

Everything in Promptise memory sits behind a small async protocol. A provider implements `search`, `add`, `delete`, `purge_user`, and `close` — nothing more. Because the interface is fixed, the agent doesn't care which backend is behind it:

- **`InMemoryProvider`** — substring search, no persistence. Perfect for tests and local prototyping.
- **`ChromaProvider`** — local vector similarity search with real embeddings, persisted to disk.
- **`Mem0Provider`** — hybrid vector + optional graph retrieval for enterprise setups.

`search()` returns a ranked list of `MemoryResult` objects, each carrying `.content`, a `.score` between 0 and 1, a `.memory_id`, and `.metadata`. That uniform shape is what lets you prototype against one provider and ship on another without touching your agent code. It's the same design philosophy behind Promptise's conversation and RAG layers — small protocols, interchangeable implementations.

## Agent memory with ChromaDB: the auto-injection loop

Here's the full loop — storage *and* retrieval — using ChromaProvider for real **vector memory in Python**. This is the feature that most guides skip: `build_agent()` wraps your provider so that every `ainvoke` automatically searches memory and prepends the relevant hits to the prompt.

```python
import asyncio
from promptise import build_agent
from promptise.memory import ChromaProvider  # swap for InMemoryProvider in tests


async def main():
    # Persistent vector memory. Change this one line to InMemoryProvider()
    # in tests — the retrieval loop below stays identical.
    memory = ChromaProvider(
        collection_name="agent_memory",
        persist_directory=".promptise/chroma",
    )

    # Seed a couple of long-lived facts (persisted to disk).
    await memory.add("The user prefers metric units and concise answers.")
    await memory.add("Project Atlas ships on 2026-09-01; owner is Priya.")

    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You are a helpful project assistant.",
        memory=memory,           # auto-search + inject before every call
        memory_auto_store=True,   # persist new facts from each turn
    )

    # No manual retrieval: the agent searches memory, injects the hits,
    # and answers using the seeded facts.
    result = await agent.ainvoke(
        {"messages": [{"role": "user",
                       "content": "When does Atlas ship, and who owns it?"}]},
    )
    print(result["messages"][-1].content)

    await agent.shutdown()


asyncio.run(main())
```

Two things earn their keep here. First, `memory=memory` turns on **agent memory with ChromaDB** auto-injection — before the model sees the question, Promptise runs a similarity search and slips the top matches into the system prompt. Second, `memory_auto_store=True` closes the loop the other way: salient facts from each turn are written back, so the agent remembers across runs and even across process restarts, because Chroma persisted them to `.promptise/chroma`.

Want to store facts deliberately instead of automatically? Leave `memory_auto_store` off and call `memory.add(...)` yourself. Either way, the retrieval side is unchanged — that's the point of the protocol.

## Persistent LLM memory in production: scoping and forgetting

Shared memory is fine for a single-owner assistant, but the moment real users show up you need isolation. Providers accept a `scope`. Set it to per-user and every `search`, `add`, and `delete` is partitioned by `user_id`, so Alice never retrieves Bob's data.

```python
from promptise.memory import ChromaProvider, MemoryScope

memory = ChromaProvider(
    collection_name="user_memory",
    persist_directory=".promptise/chroma",
    scope=MemoryScope.PER_USER,
)

# Each caller only ever sees their own entries.
await memory.add("Allergic to penicillin.", user_id="alice")
hits = await memory.search("allergies", user_id="alice")
for hit in hits:
    print(round(hit.score, 3), hit.content)

# GDPR "right to be forgotten" — remove everything a user owns.
removed = await memory.purge_user("alice")
print(f"purged {removed} entries")
```

Inside an agent you don't thread `user_id` through by hand. Pass a `CallerContext` on invocation and Promptise propagates the identity into the memory search for you:

```python
from promptise import CallerContext

await agent.ainvoke(
    {"messages": [{"role": "user", "content": "What am I allergic to?"}]},
    caller=CallerContext(user_id="alice", roles=["patient"]),
)
```

That `purge_user()` call is what makes this **persistent LLM memory** compliant rather than a liability — deletion is a supported operation, not an afterthought. For the conversation transcripts that live alongside long-term memory (session history, ownership enforcement), see the [conversations guide](../../core/conversations.md); the two layers are complementary — memory holds durable facts, conversations hold turn-by-turn history.

## Mem0 vs Chroma memory: which provider to pick

The honest answer is that the backends solve slightly different problems, so **Mem0 vs Chroma memory** is a fit question, not a ranking.

- **Choose `ChromaProvider` when** you want local-first vector memory with zero external services. It runs in-process, persists to a directory, and uses a sensible default embedding model. Great for single-node deployments, air-gapped environments, and anyone who'd rather not run another server.
- **Choose `Mem0Provider` when** you need hybrid retrieval — vector plus an optional knowledge graph — or managed, cross-node memory for a fleet of agents. Mem0 is the better fit when relationships between facts matter as much as similarity, or when you've already standardized on it operationally.
- **Choose `InMemoryProvider` when** you're writing tests. It has no dependencies and no persistence, so your suite stays fast and hermetic. It uses substring matching, not real embeddings — never ship it.

Because all three implement the same protocol, this decision is reversible. Prototype with `InMemoryProvider`, validate with `ChromaProvider`, and if you later outgrow local storage, move to `Mem0Provider` without rewriting your agent. Long-term memory is one axis of a bigger context strategy; for the wider picture — what to remember versus what to retrieve on demand — read the [RAG guide](../../core/rag.md), and for a design-level tour of the options see [AI Agent Memory: The Complete Guide for Python Devs](ai-agent-memory.md). If you're wrestling with *how much* to inject per call, [Context Window Management for LLM Agents, Explained](context-window-management.md) covers the budgeting side.

## Frequently asked questions

### What is the difference between long-term memory and a context window?

The context window is the fixed span of tokens a model can read in a single call — it resets every request. Long-term memory is an external store that outlives any one call. Promptise bridges them by searching memory before each invocation and injecting only the relevant results into the window, so you get durable recall without blowing your token budget.

### Do I need a vector database for LLM long-term memory?

Not to start. `InMemoryProvider` gives you the full retrieval loop with zero dependencies for tests and prototypes. You move to a vector store like ChromaDB when you need semantic (not substring) matching and persistence across restarts — and because the `MemoryProvider` protocol is identical, that switch is a one-line change.

### How does the agent decide which memories to inject?

On every invocation, Promptise runs the current user message as a similarity search against the provider, ranks the results by score, and injects the top matches into the system prompt. You control the pool by scoping providers per user and by choosing what you `add()`; you don't write retrieval glue by hand.

## Next steps

Start with `InMemoryProvider` in your tests, then switch one line to `ChromaProvider` for production — the retrieval loop stays identical. Grab the [Quick Start](../../getting-started/quickstart.md) to stand up your first agent, then work through the [memory guide](../../core/memory.md) to wire persistent vector memory, per-user scoping, and auto-injection into it.
