---
title: "Semantic Caching for LLMs: Cut API Costs 30-50%"
description: "Goes past the theory of similarity-based caching to the two things that make it safe in production — per-user cache isolation (no CallerContext, no caching)…"
keywords: "semantic cache for LLMs, LLM response caching, reduce LLM API costs, cache LLM completions, GPT semantic cache"
date: 2026-07-16
slug: semantic-cache-for-llms
categories:
  - Memory & RAG
---

# Semantic Caching for LLMs: Cut API Costs 30-50%

A semantic cache for LLMs serves a stored answer when a new question *means* the same thing as one you already answered — not just when the two strings match byte for byte. That is the difference between a cache that almost never hits (exact-match) and one that quietly absorbs the "how do refunds work?" / "what's your refund policy?" / "can I get my money back?" long tail that real users type. This post skips the tutorial-grade theory and focuses on the two things that decide whether similarity-based caching is safe in production, then shows you how to turn it on in Promptise Foundry with one argument and measure your real hit rate before you commit.

## How LLM response caching works when it matches by meaning

Exact-match caching stores a response under a hash of the prompt. Change one word and you miss. That is fine for deterministic API responses and useless for natural language, where the same intent arrives in a hundred surface forms.

Semantic caching swaps the hash for an embedding. Each incoming query is embedded into a vector, and the cache looks for a previously stored query whose vector is close enough — above a cosine `similarity_threshold`. If it finds one, it returns that stored response with no LLM call at all. Promptise Foundry computes those embeddings **locally by default** (the same `sentence-transformers` model used for [semantic tool optimization](../../core/tool-optimization.md)), so the cache lookup itself costs zero API dollars and adds sub-millisecond overhead.

The published payoff for this in Promptise is a **30–50% cost reduction** on workloads with repetitive queries — support bots, internal knowledge agents, documentation Q&A. Your mileage depends entirely on how repetitive your traffic actually is, which is exactly why you should measure rather than assume.

## Turn on the semantic cache with one build_agent() argument

You do not wire up a vector store, an embedding pipeline, or an invalidation job. You construct a `SemanticCache` and pass it to `build_agent()`. Everything below is a complete, runnable script — set your `OPENAI_API_KEY` and point `servers` at a live MCP endpoint.

```python
import asyncio
from promptise import build_agent, SemanticCache, CallerContext
from promptise.config import HTTPServerSpec


async def main():
    cache = SemanticCache(
        scope="per_user",           # default: isolate every user's cache
        similarity_threshold=0.92,  # min cosine similarity for a hit
    )
    cache.warmup()                  # load the local embedding model up front

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"docs": HTTPServerSpec(url="http://localhost:8000/mcp")},
        cache=cache,
        guardrails=True,            # output guardrails re-scan every cached hit
    )

    alice = CallerContext(user_id="alice")
    first_q = {"messages": [{"role": "user", "content": "What is our refund policy?"}]}

    # First call → LLM runs, response cached after output guardrails
    first = await agent.ainvoke(first_q, caller=alice)
    print(first["messages"][-1].content)

    # Semantically similar call → cache hit, no LLM call, instant answer
    similar_q = {"messages": [{"role": "user", "content": "How do refunds work here?"}]}
    hit = await agent.ainvoke(similar_q, caller=alice)
    print(hit["messages"][-1].content)

    await agent.shutdown()


asyncio.run(main())
```

`cache.warmup()` loads the embedding model before your first request so you don't pay a cold-start penalty on live traffic. From there, the cache lives inside the agent's request pipeline — it runs after input guardrails and memory search but before tool selection and the LLM call. The full [SemanticCache reference](../../core/cache.md) documents every knob; the two that matter most for correctness are covered next.

## The two things that make GPT semantic cache safe in production

Most "add a cache" tutorials stop at the hit/miss logic. In a multi-user system that is where the danger *starts*. Promptise builds in two guarantees so a shared cache never becomes a data-leak or a policy-bypass.

### Per-user isolation: no CallerContext, no caching

The default scope is `per_user`. Every user gets an isolated cache partition keyed by `user:{user_id}`, and similarity search only ever runs inside the caller's own partition — there is no code path that can match Alice's query against Bob's stored answer.

The strict half of this rule is the important one: **if you don't pass a `CallerContext` with a `user_id`, caching is silently disabled for that request.** That is deliberate. Rather than risk one anonymous request leaking a personalized answer to the next person, the cache simply steps aside and calls the LLM directly.

```python
# Cached — identity present, isolated to Alice's partition
await agent.ainvoke(inp, caller=CallerContext(user_id="alice"))

# NOT cached — no identity, LLM is called directly (no leak risk)
await agent.ainvoke(inp)
```

If your agent has no concept of users — a CLI tool, an internal script, or a public FAQ bot where every answer is identical for everyone — opt into `SemanticCache(scope="shared", shared_data_acknowledged=True)`. The explicit acknowledgment flag exists so "everyone shares one cache" is always a decision you made on purpose, never a default you tripped over. Multi-tenant callers get an extra guarantee: with `CallerContext(tenant_id="acme")` the key is tenant-qualified, so two tenants that happen to use the same `user_id` can never collide.

### Post-guardrail storage: only safe content is ever cached

The second guarantee is about *what* lands in the cache. Promptise stores the response **after** output guardrails run, not before. So the value written to the cache is already scanned for PII, redacted, and policy-checked. And on a cache *hit*, the stored response is re-scanned through output guardrails again before it is returned — a policy you tightened yesterday still applies to an answer cached last week.

That ordering closes a subtle hole: a naive cache that stored raw model output would happily replay yesterday's un-redacted answer forever. Because Promptise caches only post-guardrail content and re-scans on the way out, the cache can never become a way to smuggle unsafe content past your controls.

## Tuning: similarity threshold, TTL, and write invalidation

Once it is safe, the next question is accuracy. A `SemanticCache` exposes the levers that decide when a hit is *correct*, not just similar:

- **`similarity_threshold`** (default `0.92`) — higher is stricter. Raise it if you see wrong-but-close answers being served; lower it to catch more of the paraphrase long tail.
- **`ttl_patterns`** — regex → TTL overrides, so time-sensitive queries expire fast while stable ones live long.
- **`invalidate_on_write`** (default `True`) — when a tool with `read_only_hint=False` fires (create, update, delete), the scope's cache is evicted so you never serve a stale count.
- **`default_ttl`**, **`max_entries_per_user`**, and a `backend` of `"memory"` or `"redis"` for sharing the cache across workers.

```python
cache = SemanticCache(
    similarity_threshold=0.93,
    default_ttl=3600,
    ttl_patterns={
        r"current|now|today|latest": 60,   # freshness-sensitive → short TTL
        r"price|stock|rate": 30,
    },
    invalidate_on_write=True,
)
```

There is also a memory interaction worth understanding. The cache key includes a fingerprint of the memory context injected into the prompt, so when your agent's [long-term memory](../../core/memory.md) changes, the fingerprint changes and the cache misses rather than serving an answer built on stale context. If you are pairing the cache with retrieval and persistent memory, [AI Agent Memory: The Complete Guide for Python Devs](ai-agent-memory.md) walks through how those layers fit together.

## When a semantic cache is the wrong choice

Caching is not free wins for every workload, and pretending otherwise would waste your time. Skip it — or scope it narrowly — when:

- **Answers must always be fresh.** Live dashboards, real-time prices, "what changed in the last minute" queries. A cache here trades correctness for a hit rate you don't want. Use tight `ttl_patterns` at most, or leave the cache off for those routes.
- **Every query is unique.** If users rarely ask the same thing twice, your hit rate approaches zero and you pay embedding overhead for nothing.
- **Outputs are intentionally non-deterministic.** Creative generation, brainstorming, and "give me three different variations" flows *want* a new answer each time.

The honest test is empirical: enable the cache, watch the `cache.hit` / `cache.miss` / `cache.store` events in the observability timeline, and read your real hit rate off live traffic. If it is low, the cache is quietly stepping aside on every miss and costing you almost nothing — but you should still turn it off rather than carry an abstraction that isn't earning its place.

## Frequently asked questions

### Does a semantic cache for LLMs return wrong answers?

It can if the threshold is too loose. Two questions can be close in embedding space but need different answers, and a low `similarity_threshold` will serve one for the other. Promptise defaults to a conservative `0.92`; raise it if you observe near-miss hits, and remember that write invalidation and TTL patterns catch the *stale*-answer class of errors separately from the *wrong*-match class.

### How is this different from LLM response caching by exact match?

Exact-match caching only hits when the prompt string is identical, so it misses almost all natural-language variation. Semantic caching embeds each query and matches by cosine similarity, so paraphrases hit the same entry. That is what turns a near-zero hit rate into the 30–50% cost reduction range on repetitive workloads.

### Do I need an external vector database to cache LLM completions?

No. The default `backend="memory"` needs zero dependencies and does sub-millisecond lookups in-process, and embeddings run on a local model — no external embedding API. Switch to `backend="redis"` only when you need the cache shared across workers or surviving restarts, optionally with AES encryption at rest.

## Next steps

Enable `SemanticCache` with a single `build_agent()` argument and measure your real hit rate before committing — the honest way to know whether caching pays off for *your* traffic. Start from the [Quick Start](../../getting-started/quickstart.md) to get an agent running, then read the full [Semantic Cache guide](../../core/cache.md) to tune scope, thresholds, and invalidation for production.
