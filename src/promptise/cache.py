"""Semantic Cache — cache LLM responses by query similarity.

Reduces LLM API costs by 30-50% by serving cached responses for
semantically similar queries.  All embedding runs locally by default.

Example::

    from promptise import build_agent, SemanticCache

    agent = await build_agent(
        ...,
        cache=SemanticCache(),
    )

    # First call → LLM, result cached
    # Second similar call → cache hit, no LLM call
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import numpy as np  # type: ignore[import-not-found]

logger = logging.getLogger("promptise.cache")

# Lazy numpy: imported on first SemanticCache use, not at module load time.
# This keeps the base install free of a heavy numpy dependency for users
# who don't enable semantic caching. Install with `pip install "promptise[all]"`
# or `pip install numpy` if you want to use SemanticCache.
_np: Any = None


def _get_np() -> Any:
    """Lazy import numpy. Raises clear error if missing."""
    global _np
    if _np is None:
        try:
            import numpy as np_module
            _np = np_module
        except ImportError as e:
            raise ImportError(
                "SemanticCache requires numpy. Install with: "
                'pip install numpy  (or pip install "promptise[all]")'
            ) from e
    return _np

__all__ = [
    "SemanticCache",
    "EmbeddingProvider",
    "LocalEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "InMemoryCacheBackend",
    "RedisCacheBackend",
    "CacheEntry",
    "CacheStats",
]


# ═══════════════════════════════════════════════════════════════════════
# Protocols & Data Types
# ═══════════════════════════════════════════════════════════════════════


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for embedding providers.

    Implement this to plug in any embedding model or API::

        class MyProvider:
            async def embed(self, texts: list[str]) -> list[list[float]]:
                return my_model.encode(texts)
    """

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class CacheEntry:
    """A single cached response.

    Attributes:
        query_text: The original user query.
        response_text: The extracted response text.
        output: The full LangGraph output dict (for returning to caller).
        embedding: The query embedding vector.
        scope_key: Isolation scope (e.g. ``"user:user-42"``).
        context_fingerprint: Hash of memory + history + prompt context.
        model_id: LLM model that generated this response.
        instruction_hash: Hash of the system instructions.
        checksum: SHA-256 of response_text for corruption detection.
        created_at: Monotonic timestamp of creation.
        ttl: Time-to-live in seconds.
        metadata: Extra info (tools_used, token count, etc.).
    """

    query_text: str
    response_text: str
    output: Any
    embedding: list[float]
    scope_key: str
    context_fingerprint: str
    model_id: str
    instruction_hash: str
    checksum: str
    created_at: float
    ttl: int
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        """Check if this entry has expired.

        Uses ``time.time()`` (wall clock) consistently across all backends.
        Both InMemory and Redis backends set ``created_at`` with ``time.time()``.
        """
        return (time.time() - self.created_at) > self.ttl

    def verify_checksum(self) -> bool:
        """Verify response integrity."""
        return self.checksum == hashlib.sha256(self.response_text.encode()).hexdigest()


@dataclass
class CacheStats:
    """Cache performance statistics.

    Attributes:
        hits: Number of cache hits.
        misses: Number of cache misses.
        stores: Number of entries stored.
        evictions: Number of entries evicted.
        hit_rate: Proportion of requests served from cache.
    """

    hits: int = 0
    misses: int = 0
    stores: int = 0
    evictions: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


# ═══════════════════════════════════════════════════════════════════════
# Embedding Providers
# ═══════════════════════════════════════════════════════════════════════

# Global embedding model cache — per cache.py module.
# Note: tool_optimization.py has its own model loading; they are NOT
# shared.  This avoids coupling between modules while keeping models
# cached within each module's lifetime.
_embedding_model_cache: dict[str, Any] = {}


class LocalEmbeddingProvider:
    """Local embedding via sentence-transformers.

    Uses the same model loading pattern as tool optimization.
    If the same model is already loaded (e.g. for semantic tool
    selection), the instance is shared — no duplicate memory.

    Args:
        model: Model name or local directory path.

    Example::

        provider = LocalEmbeddingProvider()  # all-MiniLM-L6-v2
        provider = LocalEmbeddingProvider(model="BAAI/bge-small-en-v1.5")
        provider = LocalEmbeddingProvider(model="/models/local/embeddings")
    """

    DEFAULT_MODEL = "all-MiniLM-L6-v2"

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self._model_name = model
        self._encode_fn: Any | None = None

    def warmup(self) -> None:
        """Pre-load the embedding model."""
        self._get_encode_fn()

    def _get_encode_fn(self) -> Any:
        if self._encode_fn is not None:
            return self._encode_fn

        if self._model_name in _embedding_model_cache:
            model = _embedding_model_cache[self._model_name]
            self._encode_fn = model.encode
            return self._encode_fn

        try:
            import warnings

            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*resume_download.*")
                warnings.filterwarnings("ignore", message=".*UNEXPECTED.*")
                from sentence_transformers import SentenceTransformer

                model = SentenceTransformer(self._model_name)
            _embedding_model_cache[self._model_name] = model
            self._encode_fn = model.encode
            logger.info("Loaded embedding model: %s", self._model_name)
            return self._encode_fn
        except ImportError:
            raise ImportError(
                "sentence-transformers required for local embeddings. "
                "Install with: pip install sentence-transformers"
            )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using the local model."""
        encode_fn = self._get_encode_fn()
        loop = asyncio.get_running_loop()
        embeddings = await loop.run_in_executor(
            None, lambda: encode_fn(texts, normalize_embeddings=True).tolist()
        )
        return embeddings


class OpenAIEmbeddingProvider:
    """Embedding via OpenAI or Azure OpenAI API.

    Args:
        model: Model name (e.g. ``"text-embedding-3-small"``).
        api_key: OpenAI API key.
        base_url: Custom base URL (for Azure or proxies).
        azure_endpoint: Azure OpenAI endpoint URL.
        azure_deployment: Azure deployment name.

    Example::

        # OpenAI
        provider = OpenAIEmbeddingProvider(
            model="text-embedding-3-small",
            api_key="${OPENAI_API_KEY}",
        )

        # Azure OpenAI
        provider = OpenAIEmbeddingProvider(
            model="text-embedding-3-small",
            azure_endpoint="https://xxx.openai.azure.com",
            azure_deployment="my-embedding",
            api_key="${AZURE_OPENAI_KEY}",
        )
    """

    def __init__(
        self,
        *,
        model: str = "text-embedding-3-small",
        api_key: str | None = None,
        base_url: str | None = None,
        azure_endpoint: str | None = None,
        azure_deployment: str | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._azure_endpoint = azure_endpoint
        self._azure_deployment = azure_deployment

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts via OpenAI API."""
        try:
            import httpx
        except ImportError:
            raise ImportError("httpx required for OpenAI embeddings: pip install httpx")

        # Resolve API key from env if needed
        api_key = self._api_key
        if api_key and "${" in api_key:
            from .env_resolver import resolve_env_vars

            api_key = resolve_env_vars(api_key)
        if not api_key:
            raise ValueError(
                "OpenAIEmbeddingProvider: api_key is empty. "
                "Set the environment variable or pass the key directly."
            )

        # Build URL
        if self._azure_endpoint:
            url = (
                f"{self._azure_endpoint}/openai/deployments/"
                f"{self._azure_deployment or self._model}/embeddings"
                f"?api-version=2024-02-01"
            )
            headers = {"api-key": api_key or ""}
        else:
            url = f"{self._base_url or 'https://api.openai.com'}/v1/embeddings"
            headers = {"Authorization": f"Bearer {api_key}"}

        headers["Content-Type"] = "application/json"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers=headers,
                json={"input": texts, "model": self._model},
            )
            resp.raise_for_status()
            data = resp.json()
            return [item["embedding"] for item in data["data"]]


# ═══════════════════════════════════════════════════════════════════════
# Cache Backends
# ═══════════════════════════════════════════════════════════════════════


class InMemoryCacheBackend:
    """In-memory cache with numpy-based similarity search.

    Stores entries per scope with LRU eviction. Thread-safe via asyncio
    (single event loop). No persistence — lost on restart.

    Args:
        max_entries_per_scope: Max entries per scope partition.
        max_total_entries: Max entries across all scopes.
    """

    def __init__(
        self,
        *,
        max_entries_per_scope: int = 1000,
        max_total_entries: int = 100_000,
    ) -> None:
        self._max_per_scope = max_entries_per_scope
        self._max_total = max_total_entries
        # scope_key → list of CacheEntry
        self._entries: dict[str, list[CacheEntry]] = {}
        # scope_key → numpy array of embeddings (N x D)
        self._embeddings: dict[str, Any] = {}
        self._stats = CacheStats()
        self._total_entries = 0

    async def search(
        self,
        scope_key: str,
        embedding: list[float],
        threshold: float,
    ) -> CacheEntry | None:
        """Find the best matching entry above threshold."""
        entries = self._entries.get(scope_key, [])
        if not entries:
            return None

        emb_matrix = self._embeddings.get(scope_key)
        if emb_matrix is None or len(emb_matrix) == 0:
            return None

        # Remove expired entries first
        self._evict_expired(scope_key)
        entries = self._entries.get(scope_key, [])
        if not entries:
            return None

        np = _get_np()
        emb_matrix = self._embeddings[scope_key]
        query_vec = np.array(embedding, dtype=np.float32)
        scores = np.dot(emb_matrix, query_vec)

        best_idx = int(np.argmax(scores))
        best_score = float(scores[best_idx])

        if best_score >= threshold:
            entry = entries[best_idx]
            if not entry.verify_checksum():
                logger.warning("Cache: checksum mismatch, treating as miss")
                self._remove_entry(scope_key, best_idx)
                return None
            self._stats.hits += 1
            return entry

        return None

    async def store(self, scope_key: str, entry: CacheEntry) -> None:
        """Store an entry, evicting LRU if at capacity."""
        np = _get_np()
        if scope_key not in self._entries:
            self._entries[scope_key] = []
            self._embeddings[scope_key] = np.empty((0, len(entry.embedding)), dtype=np.float32)

        entries = self._entries[scope_key]

        # Evict oldest if at per-scope limit
        while len(entries) >= self._max_per_scope:
            self._remove_entry(scope_key, 0)
            self._stats.evictions += 1

        # Evict oldest globally if at total limit
        while self._total_entries >= self._max_total:
            self._evict_oldest_global()
            self._stats.evictions += 1

        entries.append(entry)
        emb_vec = np.array([entry.embedding], dtype=np.float32)
        self._embeddings[scope_key] = np.vstack([self._embeddings[scope_key], emb_vec])
        self._total_entries += 1
        self._stats.stores += 1

    async def invalidate(self, scope_key: str, pattern: str | None = None) -> int:
        """Evict entries matching a pattern (or all for scope)."""
        entries = self._entries.get(scope_key, [])
        if not entries:
            return 0

        if pattern is None:
            count = len(entries)
            self._total_entries -= count
            del self._entries[scope_key]
            del self._embeddings[scope_key]
            return count

        # Pattern match against tool names in metadata
        # Escape regex metacharacters first, then convert glob * to .*
        safe_pattern = re.escape(pattern).replace(r"\*", ".*")
        regex = re.compile(f"^{safe_pattern}$")
        to_remove = []
        for i, e in enumerate(entries):
            tools = e.metadata.get("tools_used", [])
            if any(regex.match(t) for t in tools):
                to_remove.append(i)

        for idx in reversed(to_remove):
            self._remove_entry(scope_key, idx)
        return len(to_remove)

    async def purge_user(self, user_id: str) -> int:
        """Remove all entries for a user (GDPR compliance)."""
        # Exact match — not prefix! "user:12" must NOT match "user:123"
        exact_key = f"user:{user_id}"
        count = 0
        keys_to_remove = [k for k in self._entries if k == exact_key]
        for key in keys_to_remove:
            count += len(self._entries[key])
            self._total_entries -= len(self._entries[key])
            del self._entries[key]
            del self._embeddings[key]
        return count

    async def stats(self) -> CacheStats:
        return self._stats

    def _remove_entry(self, scope_key: str, idx: int) -> None:
        np = _get_np()
        entries = self._entries[scope_key]
        entries.pop(idx)
        emb = self._embeddings[scope_key]
        self._embeddings[scope_key] = np.delete(emb, idx, axis=0)
        self._total_entries -= 1

    def _evict_expired(self, scope_key: str) -> None:
        entries = self._entries.get(scope_key, [])
        to_remove = [i for i, e in enumerate(entries) if e.expired]
        for idx in reversed(to_remove):
            self._remove_entry(scope_key, idx)

    def _evict_oldest_global(self) -> None:
        """Evict the oldest entry across all scopes."""
        oldest_key = None
        oldest_time = float("inf")
        for key, entries in self._entries.items():
            if entries and entries[0].created_at < oldest_time:
                oldest_time = entries[0].created_at
                oldest_key = key
        if oldest_key:
            self._remove_entry(oldest_key, 0)


# ═══════════════════════════════════════════════════════════════════════
# SemanticCache
# ═══════════════════════════════════════════════════════════════════════


class RedisCacheBackend:
    """Redis-backed cache with vector similarity search.

    Stores cache entries as JSON in Redis hashes. Embeddings are stored
    per scope and similarity is computed by fetching all embeddings for
    a scope and running numpy dot product locally. This avoids requiring
    the RediSearch module while keeping similarity search functional.

    Optional AES encryption at rest via ``encrypt_values=True``.
    Encryption key is read from ``PROMPTISE_CACHE_KEY`` env var or
    auto-generated per process.

    Args:
        redis_url: Redis connection URL (e.g. ``redis://localhost:6379``).
        max_entries_per_scope: Max entries per scope partition.
        max_total_entries: Max entries across all scopes.
        encrypt_values: Encrypt cached response values at rest.
    """

    def __init__(
        self,
        *,
        redis_url: str = "redis://localhost:6379",
        max_entries_per_scope: int = 1000,
        max_total_entries: int = 100_000,
        encrypt_values: bool = False,
    ) -> None:
        self._max_per_scope = max_entries_per_scope
        self._max_total = max_total_entries
        self._stats = CacheStats()
        self._encrypt = encrypt_values
        self._fernet: Any | None = None
        self._redis: Any | None = None
        self._redis_url = redis_url

        if encrypt_values:
            self._init_encryption()

    def _init_encryption(self) -> None:
        """Initialize Fernet encryption from env or auto-generate."""
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            raise ImportError("cryptography required for encrypted cache: pip install cryptography")
        import os

        key = os.environ.get("PROMPTISE_CACHE_KEY")
        if not key:
            key = Fernet.generate_key().decode()
            logger.warning(
                "PROMPTISE_CACHE_KEY not set — auto-generated encryption key. "
                "Cache will not survive restarts. Set the env var for persistence."
            )
        self._fernet = Fernet(key if isinstance(key, bytes) else key.encode())

    async def _get_redis(self) -> Any:
        """Lazy Redis connection with reconnection on failure."""
        if self._redis is not None:
            # Verify the connection is alive (with timeout to prevent hangs)
            try:
                await asyncio.wait_for(self._redis.ping(), timeout=5.0)
                return self._redis
            except Exception:
                logger.warning("Redis cache: connection lost, reconnecting")
                try:
                    await self._redis.aclose()
                except Exception:
                    pass
                self._redis = None

        try:
            import redis.asyncio as aioredis
        except ImportError:
            raise ImportError("redis[asyncio] required for Redis cache backend: pip install redis")
        self._redis = aioredis.from_url(self._redis_url, decode_responses=False)
        return self._redis

    def _scope_entries_key(self, scope_key: str) -> str:
        """Redis key for the hash storing entries for a scope."""
        return f"promptise:cache:entries:{scope_key}"

    def _scope_embeddings_key(self, scope_key: str) -> str:
        """Redis key for the list storing embeddings for a scope."""
        return f"promptise:cache:embeddings:{scope_key}"

    def _scope_order_key(self, scope_key: str) -> str:
        """Redis key for the sorted set tracking insertion order (LRU)."""
        return f"promptise:cache:order:{scope_key}"

    def _encrypt_value(self, data: bytes) -> bytes:
        if self._fernet:
            return self._fernet.encrypt(data)
        return data

    def _decrypt_value(self, data: bytes) -> bytes:
        if self._fernet:
            return self._fernet.decrypt(data)
        return data

    async def search(
        self,
        scope_key: str,
        embedding: list[float],
        threshold: float,
    ) -> CacheEntry | None:
        """Find the best matching entry above threshold."""
        r = await self._get_redis()

        # Get all entry IDs for this scope
        entries_key = self._scope_entries_key(scope_key)
        entry_ids = await r.hkeys(entries_key)
        if not entry_ids:
            return None

        # Get all embeddings
        emb_key = self._scope_embeddings_key(scope_key)
        raw_embeddings = await r.hgetall(emb_key)
        if not raw_embeddings:
            return None

        # Build numpy matrix and compute similarity
        np = _get_np()
        query_vec = np.array(embedding, dtype=np.float32)
        best_score = -1.0
        best_id: bytes | None = None

        for eid in entry_ids:
            raw_emb = raw_embeddings.get(eid)
            if raw_emb is None:
                continue
            emb_vec = np.frombuffer(raw_emb, dtype=np.float32)
            score = float(np.dot(emb_vec, query_vec))
            if score > best_score:
                best_score = score
                best_id = eid

        if best_score < threshold or best_id is None:
            return None

        # Load the entry
        raw_entry = await r.hget(entries_key, best_id)
        if raw_entry is None:
            return None

        try:
            decrypted = self._decrypt_value(raw_entry)
            entry_data = json.loads(decrypted)
        except Exception:
            logger.warning("Redis cache: failed to deserialize entry, treating as miss")
            await r.hdel(entries_key, best_id)
            return None

        # Deserialize LangGraph output
        raw_output = entry_data["output"]
        if isinstance(raw_output, dict) and raw_output.get("_fallback"):
            output = {"messages": []}  # Minimal output on fallback
        else:
            try:
                from langchain_core.load import load

                output = load(raw_output)
            except Exception:
                output = raw_output  # Use as-is if load fails

        entry = CacheEntry(
            query_text=entry_data["query_text"],
            response_text=entry_data["response_text"],
            output=output,
            embedding=embedding,  # Use the query embedding (we matched)
            scope_key=scope_key,
            context_fingerprint=entry_data["context_fingerprint"],
            model_id=entry_data["model_id"],
            instruction_hash=entry_data["instruction_hash"],
            checksum=entry_data["checksum"],
            created_at=entry_data["created_at"],
            ttl=entry_data["ttl"],
            metadata=entry_data.get("metadata", {}),
        )

        # Check TTL (use wall clock for Redis — persists across restarts)
        if (time.time() - entry.created_at) > entry.ttl:
            await r.hdel(entries_key, best_id)
            await r.hdel(emb_key, best_id)
            await r.zrem(self._scope_order_key(scope_key), best_id)
            return None

        # Verify checksum
        if not entry.verify_checksum():
            logger.warning("Redis cache: checksum mismatch, evicting corrupted entry")
            await r.hdel(entries_key, best_id)
            await r.hdel(emb_key, best_id)
            return None

        self._stats.hits += 1
        return entry

    async def store(self, scope_key: str, entry: CacheEntry) -> None:
        """Store an entry in Redis."""
        r = await self._get_redis()
        import secrets

        entry_id = secrets.token_hex(8).encode()

        entries_key = self._scope_entries_key(scope_key)
        emb_key = self._scope_embeddings_key(scope_key)
        order_key = self._scope_order_key(scope_key)

        # Evict oldest if at per-scope limit
        count = await r.hlen(entries_key)
        while count >= self._max_per_scope:
            oldest = await r.zrange(order_key, 0, 0)
            if not oldest:
                break
            await r.hdel(entries_key, oldest[0])
            await r.hdel(emb_key, oldest[0])
            await r.zrem(order_key, oldest[0])
            count -= 1
            self._stats.evictions += 1

        # Serialize entry (use wall clock for Redis)
        # LangGraph output contains AIMessage/ToolMessage objects that aren't
        # JSON-serializable. Use LangChain's dumpd() for safe serialization.
        try:
            from langchain_core.load import dumpd

            serialized_output = dumpd(entry.output)
        except Exception as exc:
            # Fallback: store response_text only (lose tool call details)
            logger.warning("Cache: output serialization failed (%s), storing text-only fallback", type(exc).__name__)
            serialized_output = {"_fallback": True, "response_text": entry.response_text}

        entry_data = {
            "query_text": entry.query_text,
            "response_text": entry.response_text,
            "output": serialized_output,
            "context_fingerprint": entry.context_fingerprint,
            "model_id": entry.model_id,
            "instruction_hash": entry.instruction_hash,
            "checksum": entry.checksum,
            "created_at": time.time(),  # Wall clock for Redis persistence
            "ttl": entry.ttl,
            "metadata": entry.metadata,
        }

        raw_entry = json.dumps(entry_data).encode()
        encrypted = self._encrypt_value(raw_entry)

        # Store entry + embedding + order
        await r.hset(entries_key, entry_id, encrypted)
        np = _get_np()
        emb_bytes = np.array(entry.embedding, dtype=np.float32).tobytes()
        await r.hset(emb_key, entry_id, emb_bytes)
        await r.zadd(order_key, {entry_id: time.time()})

        # NOTE: We do NOT set expire() on the shared hash/sorted-set keys.
        # These hold ALL entries for a scope — setting TTL would clobber
        # earlier entries with longer TTLs. TTL is enforced per-entry
        # during search() via created_at + ttl check.

        self._stats.stores += 1

    async def invalidate(self, scope_key: str, pattern: str | None = None) -> int:
        """Evict entries for a scope."""
        r = await self._get_redis()
        entries_key = self._scope_entries_key(scope_key)

        if pattern is None:
            count = await r.hlen(entries_key)
            await r.delete(entries_key)
            await r.delete(self._scope_embeddings_key(scope_key))
            await r.delete(self._scope_order_key(scope_key))
            return count

        # Pattern-based invalidation: load all entries, match, delete
        all_entries = await r.hgetall(entries_key)
        safe_pattern = re.escape(pattern).replace(r"\*", ".*")
        regex = re.compile(f"^{safe_pattern}$")
        to_remove = []

        for eid, raw in all_entries.items():
            try:
                decrypted = self._decrypt_value(raw)
                data = json.loads(decrypted)
                tools = data.get("metadata", {}).get("tools_used", [])
                if any(regex.match(t) for t in tools):
                    to_remove.append(eid)
            except Exception:
                to_remove.append(eid)  # Remove corrupted entries

        emb_key = self._scope_embeddings_key(scope_key)
        order_key = self._scope_order_key(scope_key)
        for eid in to_remove:
            await r.hdel(entries_key, eid)
            await r.hdel(emb_key, eid)
            await r.zrem(order_key, eid)

        return len(to_remove)

    async def purge_user(self, user_id: str) -> int:
        """Remove all entries for a user (GDPR compliance)."""
        r = await self._get_redis()
        exact_key = f"user:{user_id}"
        entries_key = self._scope_entries_key(exact_key)
        count = await r.hlen(entries_key)
        if count > 0:
            await r.delete(entries_key)
            await r.delete(self._scope_embeddings_key(exact_key))
            await r.delete(self._scope_order_key(exact_key))
        return count

    async def stats(self) -> CacheStats:
        return self._stats

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._redis is not None:
            await self._redis.close()
            self._redis = None


class SemanticCache:
    """Semantic cache for agent responses.

    Caches LLM responses by query similarity using local or cloud
    embeddings.  Reduces API costs by 30-50% for workloads with
    repetitive queries.

    **Security:** Default scope is ``per_user`` — each user gets an
    isolated cache partition.  No ``CallerContext`` = no caching.
    Cached responses always pass through output guardrails.

    Args:
        backend: ``"memory"`` (default) or ``"redis"``.
        redis_url: Redis connection URL (when backend is ``"redis"``).
        embedding: An :class:`EmbeddingProvider`, a model name string,
            or ``None`` for the default local model.
        similarity_threshold: Minimum cosine similarity for a cache hit.
        default_ttl: Default time-to-live in seconds.
        scope: Cache isolation: ``"per_user"`` (default),
            ``"per_session"``, or ``"shared"``.
        max_entries_per_user: Max entries per scope partition.
        max_total_entries: Max entries across all scopes.
        encrypt_values: Encrypt cached values at rest (Redis only).
        ttl_patterns: Regex → TTL overrides for time-sensitive queries.
        invalidate_on_write: Evict cache when write tools fire.
        cache_multi_turn: Cache multi-turn conversations (default: off).
        shared_data_acknowledged: Required when scope is ``"shared"``.

    Example::

        # One-liner
        cache = SemanticCache()

        # Full config
        cache = SemanticCache(
            backend="redis",
            redis_url="redis://localhost:6379",
            similarity_threshold=0.92,
            scope="per_user",
            ttl_patterns={r"current|now|today": 60},
        )

        agent = await build_agent(..., cache=cache)
    """

    def __init__(
        self,
        *,
        backend: str = "memory",
        redis_url: str | None = None,
        embedding: EmbeddingProvider | str | None = None,
        similarity_threshold: float = 0.92,
        default_ttl: int = 3600,
        scope: str = "per_user",
        max_entries_per_user: int = 1000,
        max_total_entries: int = 100_000,
        encrypt_values: bool = False,
        ttl_patterns: dict[str, int] | None = None,
        invalidate_on_write: bool = True,
        cache_multi_turn: bool = False,
        shared_data_acknowledged: bool = False,
    ) -> None:
        self._threshold = similarity_threshold
        self._default_ttl = default_ttl
        self._scope = scope
        self._ttl_patterns = {re.compile(k): v for k, v in (ttl_patterns or {}).items()}
        self._invalidate_on_write = invalidate_on_write
        self._cache_multi_turn = cache_multi_turn

        # Warn if shared scope without acknowledgment
        if scope == "shared" and not shared_data_acknowledged:
            logger.warning(
                "SemanticCache: scope='shared' without shared_data_acknowledged=True. "
                "Cached responses will be shared across ALL users. Ensure no "
                "personalized data is cached. Set shared_data_acknowledged=True "
                "to suppress this warning."
            )

        # Resolve embedding provider
        if embedding is None:
            self._embedding = LocalEmbeddingProvider()
        elif isinstance(embedding, str):
            self._embedding = LocalEmbeddingProvider(model=embedding)
        else:
            self._embedding = embedding

        # Resolve backend
        if backend == "memory":
            self._backend = InMemoryCacheBackend(
                max_entries_per_scope=max_entries_per_user,
                max_total_entries=max_total_entries,
            )
        elif backend == "redis":
            if not redis_url:
                raise ValueError("redis_url is required when backend='redis'")
            self._backend = RedisCacheBackend(
                redis_url=redis_url,
                max_entries_per_scope=max_entries_per_user,
                max_total_entries=max_total_entries,
                encrypt_values=encrypt_values,
            )
        else:
            raise ValueError(f"Unknown cache backend: {backend!r}")

    def warmup(self) -> None:
        """Pre-load the embedding model.

        Call at startup to avoid download/load latency on first cache check.
        """
        if isinstance(self._embedding, LocalEmbeddingProvider):
            self._embedding.warmup()

    # ── Core API ─────────────────────────────────────────────────────

    async def check(
        self,
        query_text: str,
        *,
        context_fingerprint: str = "",
        caller: Any | None = None,
        model_id: str | None = None,
        instruction_hash: str = "",
    ) -> CacheEntry | None:
        """Check for a cached response.

        Returns the cached entry if a semantically similar query was
        previously cached with the same context, model, and instructions.
        Returns ``None`` on cache miss.

        Args:
            query_text: The user's query.
            context_fingerprint: Hash of memory + history context.
            caller: :class:`CallerContext` for scope isolation.
            model_id: LLM model identifier.
            instruction_hash: Hash of the system instructions.
        """
        scope_key = self._build_scope_key(caller)
        if scope_key is None:
            self._backend._stats.misses += 1
            logger.debug(
                "Cache: no CallerContext or user_id provided — caching disabled "
                "for this request. Pass caller=CallerContext(user_id=...) to "
                "enable caching, or use scope='shared' for public data."
            )
            return None

        if not query_text.strip():
            self._backend._stats.misses += 1
            return None

        # Embed the query
        try:
            embeddings = await self._embedding.embed([query_text])
            if not embeddings:
                logger.warning("Cache: embedding returned empty list, skipping")
                self._backend._stats.misses += 1
                return None
            query_emb = embeddings[0]
        except Exception:
            logger.warning("Cache: embedding failed, skipping cache check", exc_info=True)
            self._backend._stats.misses += 1
            return None

        # Search backend
        entry = await self._backend.search(scope_key, query_emb, self._threshold)

        if entry is None:
            self._backend._stats.misses += 1
            return None

        # Verify context match — different context = stale answer
        if (
            entry.context_fingerprint != context_fingerprint
            or entry.model_id != (model_id or "")
            or entry.instruction_hash != instruction_hash
        ):
            self._backend._stats.misses += 1
            return None

        # Valid cache hit
        logger.debug(
            "Cache hit for query %r (scope=%s, age=%.0fs)",
            query_text[:50],
            scope_key,
            time.monotonic() - entry.created_at,
        )
        return entry

    async def store(
        self,
        query_text: str,
        response_text: str,
        output: Any,
        *,
        context_fingerprint: str = "",
        caller: Any | None = None,
        model_id: str | None = None,
        instruction_hash: str = "",
        tools_used: list[str] | None = None,
    ) -> None:
        """Store a response in the cache.

        Args:
            query_text: The user's query.
            response_text: The extracted response text.
            output: The full LangGraph output dict.
            context_fingerprint: Hash of memory + history context.
            caller: :class:`CallerContext` for scope isolation.
            model_id: LLM model identifier.
            instruction_hash: Hash of the system instructions.
            tools_used: List of tool names called during this invocation.
        """
        scope_key = self._build_scope_key(caller)
        if scope_key is None:
            return  # No identity → don't cache

        if not query_text.strip() or not response_text.strip():
            return

        # Embed the query
        try:
            embeddings = await self._embedding.embed([query_text])
            if not embeddings:
                logger.warning("Cache: embedding returned empty list, skipping store")
                return
            query_emb = embeddings[0]
        except Exception:
            logger.warning("Cache: embedding failed, skipping store", exc_info=True)
            return

        # Compute TTL
        ttl = self._resolve_ttl(query_text)

        # Build entry
        entry = CacheEntry(
            query_text=query_text,
            response_text=response_text,
            output=output,
            embedding=query_emb,
            scope_key=scope_key,
            context_fingerprint=context_fingerprint,
            model_id=model_id or "",
            instruction_hash=instruction_hash,
            checksum=hashlib.sha256(response_text.encode()).hexdigest(),
            created_at=time.time(),  # Wall clock — consistent with Redis backend and .expired
            ttl=ttl,
            metadata={"tools_used": tools_used or []},
        )

        await self._backend.store(scope_key, entry)
        logger.debug(
            "Cache stored for query %r (scope=%s, ttl=%ds)",
            query_text[:50],
            scope_key,
            ttl,
        )

    async def invalidate_for_write(self, tool_name: str, caller: Any | None = None) -> None:
        """Invalidate cache entries after a write operation.

        Called automatically when a tool with ``read_only_hint=False``
        fires during an agent invocation.
        """
        if not self._invalidate_on_write:
            return

        scope_key = self._build_scope_key(caller)
        if scope_key is None:
            return

        count = await self._backend.invalidate(scope_key)
        if count > 0:
            logger.debug(
                "Cache invalidated %d entries for scope %s (write tool: %s)",
                count,
                scope_key,
                tool_name,
            )

    async def purge_user(self, user_id: str) -> int:
        """Remove all cached entries for a user.

        Use for GDPR right-to-erasure compliance.

        Args:
            user_id: The user whose cache to purge.

        Returns:
            Number of entries removed.
        """
        count = await self._backend.purge_user(user_id)
        logger.info("Cache purged %d entries for user %s", count, user_id)
        # Emit cache.purged event if notifier is available
        notifier = getattr(self, "_event_notifier", None)
        if notifier is not None:
            try:
                from promptise.events import emit_event

                emit_event(
                    notifier,
                    "cache.purged",
                    "info",
                    {"user_id": user_id, "entries_removed": count},
                )
            except Exception:
                pass
        return count

    async def stats(self) -> CacheStats:
        """Get cache performance statistics."""
        return await self._backend.stats()

    async def close(self) -> None:
        """Close the cache backend (Redis connections, etc.).

        Called automatically by ``agent.shutdown()``.
        """
        if hasattr(self._backend, "close"):
            await self._backend.close()

    # ── Internals ────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_scope_id(value: str) -> str:
        """Sanitize a scope identifier — reject empty, replace delimiters."""
        if not value or not value.strip():
            return ""
        # Replace colons and slashes to prevent scope key confusion
        return value.strip().replace(":", "_").replace("/", "_")

    def _build_scope_key(self, caller: Any | None) -> str | None:
        """Build the scope isolation key from caller context.

        Returns None if caching should be disabled for this request.
        """
        if self._scope == "shared":
            logger.debug("Cache: using shared scope (no per-user isolation)")
            return "shared"

        if caller is None:
            return None

        user_id = getattr(caller, "user_id", None)

        if self._scope == "per_user":
            if user_id is None:
                return None
            safe_id = self._sanitize_scope_id(user_id)
            if not safe_id:
                return None
            return f"user:{safe_id}"

        if self._scope == "per_session":
            session_id = (getattr(caller, "metadata", None) or {}).get("session_id")
            if session_id:
                safe_sid = self._sanitize_scope_id(session_id)
                if safe_sid:
                    return f"session:{safe_sid}"
            # Fall back to user scope
            if user_id is None:
                return None
            safe_id = self._sanitize_scope_id(user_id)
            if not safe_id:
                return None
            return f"user:{safe_id}"

        return None

    def _resolve_ttl(self, query_text: str) -> int:
        """Resolve TTL using pattern overrides, then default."""
        query_lower = query_text.lower()
        for pattern, ttl in self._ttl_patterns.items():
            if pattern.search(query_lower):
                return ttl
        return self._default_ttl


# ═══════════════════════════════════════════════════════════════════════
# Helper functions for agent integration
# ═══════════════════════════════════════════════════════════════════════


def compute_context_fingerprint(
    *,
    memory_results: list[Any] | None = None,
    conversation_length: int = 0,
    instruction_hash: str = "",
    tool_set_hash: str = "",
) -> str:
    """Compute a fingerprint of the current context.

    Used as part of the cache key to ensure stale context doesn't
    produce stale cache hits.  Hashes the actual memory content —
    not just the count — so new memories invalidate stale cache entries.
    """
    # Hash actual memory content, not just count
    mem_hash = "none"
    if memory_results:
        mem_texts = []
        for r in memory_results:
            if hasattr(r, "content"):
                mem_texts.append(str(r.content)[:200])
            elif hasattr(r, "text"):
                mem_texts.append(str(r.text)[:200])
            else:
                mem_texts.append(str(r)[:200])
        mem_hash = hashlib.sha256("|".join(mem_texts).encode()).hexdigest()[:16]

    parts = [
        f"mem:{mem_hash}",
        f"conv:{conversation_length}",
        f"inst:{instruction_hash[:16]}",
        f"tools:{tool_set_hash[:16]}",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def compute_instruction_hash(instructions: str | None) -> str:
    """Hash the system instructions for cache key inclusion."""
    if not instructions:
        return "default"
    return hashlib.sha256(instructions.encode()).hexdigest()[:16]
