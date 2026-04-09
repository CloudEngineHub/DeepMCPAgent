# Semantic Cache

Cache LLM responses by query similarity. Save 30-50% on API costs by serving cached responses for semantically similar queries. All embedding runs locally by default.

```python
from promptise import build_agent, SemanticCache, CallerContext
from promptise.config import HTTPServerSpec

cache = SemanticCache()
cache.warmup()

agent = await build_agent(
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    model="openai:gpt-5-mini",
    cache=cache,
)

# First call → LLM, result cached
result = await agent.ainvoke(input, caller=CallerContext(user_id="user-42"))

# Second similar call → cache hit, no LLM call, instant response
result = await agent.ainvoke(input, caller=CallerContext(user_id="user-42"))
```

---

## How It Works

1. User sends a message
2. Input guardrails scan the message (block injection attacks)
3. Memory search runs (so the cache key includes memory context)
4. **Cache check** — embed the query, search for similar cached queries with matching context
5. **Cache hit** → run output guardrails on cached response → return instantly
6. **Cache miss** → continue to tools, LLM → output guardrails → **cache the post-guardrail response** → return

Cache check runs **after** input guardrails and memory search (so the cache key reflects current memory state) but **before** tool selection and LLM call. Cached responses are stored **after** output guardrails — only safe, redacted content is ever persisted in the cache.

---

## Security: Per-User Isolation

**Default scope is `per_user`** — every user gets an isolated cache partition. User A's cached responses are invisible to User B.

**No CallerContext = no caching.** If you don't pass `caller=CallerContext(user_id=...)`, caching is silently disabled for that request. This prevents accidental cross-user data leakage.

```python
# ✅ Cached (user isolated)
result = await agent.ainvoke(input, caller=CallerContext(user_id="user-42"))

# ❌ Not cached (no identity)
result = await agent.ainvoke(input)
```

Three scopes:

| Scope | Behavior | Use case |
|-------|----------|----------|
| `per_user` (default) | Each user has their own cache | Any personalized agent |
| `per_session` | Each session has its own cache | Conversation-specific |
| `shared` | All users share one cache | Public knowledge (weather, docs, FAQ) |

```python
# Shared scope — requires explicit acknowledgment
cache = SemanticCache(scope="shared", shared_data_acknowledged=True)
```

---

## Standalone / Shared Mode (No Multi-User)

If you're building a single-user app, internal tool, or public FAQ agent where there's no concept of "users," use `scope="shared"`:

```python
cache = SemanticCache(scope="shared", shared_data_acknowledged=True)

agent = await build_agent(
    servers={...}, model="openai:gpt-5-mini", cache=cache,
)

# No CallerContext needed — works immediately
result = await agent.ainvoke({"messages": [{"role": "user", "content": "What is Python?"}]})
```

With `scope="shared"`, caching works without `CallerContext`. Everyone shares the same cache. Use this when:

- Your agent answers public knowledge questions (docs, FAQ, weather)
- There's only one user (CLI tools, internal scripts)
- Responses never contain personalized data

!!! warning "Shared scope = no isolation"
    Every user sees everyone else's cached responses. Never use shared scope when the agent accesses user-specific data (accounts, orders, personal info).

---

## Multi-User Mode

For apps with multiple users (SaaS, customer support, multi-tenant), the default `per_user` scope isolates each user's cache automatically:

```python
cache = SemanticCache()  # scope="per_user" is the default

agent = await build_agent(..., cache=cache)

# Each user has their own cache partition
await agent.ainvoke(input, caller=CallerContext(user_id="alice"))  # Alice's cache
await agent.ainvoke(input, caller=CallerContext(user_id="bob"))    # Bob's cache (separate)
```

**How it works internally:**
- Cache key prefix is `user:{user_id}` — Alice's entries are keyed `user:alice`, Bob's are `user:bob`
- Similarity search only runs within a user's own partition — no cross-user matching possible
- If no `CallerContext` is provided, caching is silently disabled for that request (with a debug log: `"Cache: no CallerContext or user_id provided"`)
- `purge_user("alice")` removes all of Alice's cached entries (GDPR compliance)

**Per-session mode** isolates even further — each conversation session has its own cache:

```python
cache = SemanticCache(scope="per_session")

caller = CallerContext(
    user_id="alice",
    metadata={"session_id": "sess_abc123"},
)
await agent.ainvoke(input, caller=caller)
```

---

## Configuration

```python
cache = SemanticCache(
    backend="memory",                # "memory" or "redis"
    similarity_threshold=0.92,       # 0.0-1.0 (higher = stricter matching)
    default_ttl=3600,                # seconds
    scope="per_user",                # "per_user", "per_session", "shared"
    max_entries_per_user=1000,
    max_total_entries=100_000,
    invalidate_on_write=True,        # evict cache when write tools fire
    ttl_patterns={                   # regex → TTL for time-sensitive queries
        r"current|now|today|latest": 60,
        r"price|stock|rate": 30,
    },
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `backend` | `str` | `"memory"` | `"memory"` or `"redis"` |
| `redis_url` | `str` | `None` | Redis connection URL |
| `embedding` | `EmbeddingProvider \| str` | Local model | Embedding provider or model name |
| `similarity_threshold` | `float` | `0.92` | Min cosine similarity for cache hit |
| `default_ttl` | `int` | `3600` | Default time-to-live in seconds |
| `scope` | `str` | `"per_user"` | Cache isolation scope |
| `max_entries_per_user` | `int` | `1000` | Max entries per scope partition |
| `max_total_entries` | `int` | `100_000` | Max entries across all scopes |
| `encrypt_values` | `bool` | `False` | AES encryption at rest (Redis) |
| `ttl_patterns` | `dict` | `None` | Regex → TTL overrides |
| `invalidate_on_write` | `bool` | `True` | Evict cache on write tool calls |
| `cache_multi_turn` | `bool` | `False` | Cache multi-turn conversations |

---

## Cache Backends

### In-Memory (default)

Zero dependencies. Sub-millisecond lookups. Lost on restart. Single-process only.

```python
cache = SemanticCache(backend="memory")
```

### Redis

Shared across workers and servers. Survives restarts. Optional AES encryption at rest.

```python
cache = SemanticCache(
    backend="redis",
    redis_url="redis://localhost:6379",
    encrypt_values=True,  # AES encryption — set PROMPTISE_CACHE_KEY env var
)
```

Requires `pip install redis`. Encryption requires `pip install cryptography`.

Set `PROMPTISE_CACHE_KEY` to a Fernet key for persistent encryption across restarts. If not set, a key is auto-generated per process (cache won't survive restart).

**Graceful degradation:** If Redis is unreachable, cache operations fail silently (logged as warnings) and the agent continues normally — LLM is called directly.

---

## Embedding Providers

### Local (default)

Uses `sentence-transformers` — the same model used for semantic tool optimization. Zero API calls, runs locally.

```python
cache = SemanticCache()  # all-MiniLM-L6-v2
cache = SemanticCache(embedding="BAAI/bge-small-en-v1.5")
cache = SemanticCache(embedding="/models/local/custom")
```

### OpenAI / Azure OpenAI

```python
from promptise import OpenAIEmbeddingProvider

cache = SemanticCache(
    embedding=OpenAIEmbeddingProvider(
        model="text-embedding-3-small",
        api_key="${OPENAI_API_KEY}",
    ),
)
```

### Custom provider

Any object implementing the `EmbeddingProvider` protocol:

```python
class MyProvider:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return my_model.encode(texts)

cache = SemanticCache(embedding=MyProvider())
```

---

## Cache Key

The cache key determines when a hit occurs. It includes:

| Component | What it prevents |
|-----------|-----------------|
| Scope prefix (`user:42`) | Cross-user data leakage |
| Query embedding | Semantic similarity matching |
| Context fingerprint | Stale answers after memory/history changes |
| Model ID | Serving GPT responses as Claude responses |
| Instruction hash | Stale responses after prompt updates |

If any component changes, the cache misses and a fresh LLM call is made.

---

## Write Invalidation

When a tool with `read_only_hint=False` fires (create, update, delete), the cache for that scope is evicted. This prevents stale data:

```
"How many tickets are open?" → cached "47"
create_ticket() fires → cache evicted
"How many tickets are open?" → fresh LLM call → "48"
```

Disable with `invalidate_on_write=False` if your tools don't affect query results.

---

## GDPR Compliance

```python
# Delete all cached data for a user
count = await cache.purge_user("user-42")
```

---

## Observability

Cache events appear in the observability timeline:

- `cache.hit` — response served from cache (with similarity score, cache age)
- `cache.miss` — no cache hit, proceeding to LLM
- `cache.store` — new response stored in cache
- `cache.error` — cache operation failed (non-blocking)

---

## What's Next?

- [Guardrails](guardrails.md) — output guardrails always run on cached responses
- [Tool Optimization](tool-optimization.md) — shares the same embedding model
- [Building Agents](agents/building-agents.md) — the `cache` parameter on `build_agent()`
