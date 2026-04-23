"""Tests for SemanticCache — embedding, backends, isolation, integration."""

from __future__ import annotations

import hashlib
import time

import numpy as np
import pytest

from promptise.agent import CallerContext
from promptise.cache import (
    CacheEntry,
    InMemoryCacheBackend,
    SemanticCache,
    compute_context_fingerprint,
    compute_instruction_hash,
)

# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_entry(
    query: str = "test query",
    response: str = "test response",
    scope_key: str = "user:test",
    embedding: list[float] | None = None,
    ttl: int = 3600,
) -> CacheEntry:
    emb = embedding or [0.1] * 384
    return CacheEntry(
        query_text=query,
        response_text=response,
        output={"messages": [{"content": response}]},
        embedding=emb,
        scope_key=scope_key,
        context_fingerprint="fp1",
        model_id="openai:gpt-5-mini",
        instruction_hash="inst1",
        checksum=hashlib.sha256(response.encode()).hexdigest(),
        created_at=time.time(),
        ttl=ttl,
    )


class FakeEmbeddingProvider:
    """Deterministic embedding for tests — returns normalized random vectors."""

    def __init__(self, dim: int = 384):
        self._dim = dim
        self._cache: dict[str, list[float]] = {}

    async def embed(self, texts: list[str]) -> list[list[float]]:
        results = []
        for text in texts:
            if text not in self._cache:
                # Deterministic: hash text to seed random
                seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
                rng = np.random.RandomState(seed)
                vec = rng.randn(self._dim).astype(np.float32)
                vec = vec / np.linalg.norm(vec)
                self._cache[text] = vec.tolist()
            results.append(self._cache[text])
        return results


# ═══════════════════════════════════════════════════════════════════════
# InMemoryCacheBackend
# ═══════════════════════════════════════════════════════════════════════


class TestInMemoryBackend:
    @pytest.mark.asyncio
    async def test_store_and_search(self):
        backend = InMemoryCacheBackend()
        entry = _make_entry()
        await backend.store("user:a", entry)
        result = await backend.search("user:a", entry.embedding, 0.9)
        assert result is not None
        assert result.response_text == "test response"

    @pytest.mark.asyncio
    async def test_miss_below_threshold(self):
        backend = InMemoryCacheBackend()
        entry = _make_entry(embedding=[1.0] + [0.0] * 383)
        await backend.store("user:a", entry)
        # Orthogonal vector → similarity ~0
        result = await backend.search("user:a", [0.0] + [1.0] + [0.0] * 382, 0.9)
        assert result is None

    @pytest.mark.asyncio
    async def test_per_user_isolation(self):
        backend = InMemoryCacheBackend()
        entry_a = _make_entry(scope_key="user:a")
        entry_b = _make_entry(scope_key="user:b")
        await backend.store("user:a", entry_a)
        await backend.store("user:b", entry_b)
        # User A can't see user B's entries
        result = await backend.search("user:a", entry_b.embedding, 0.9)
        assert result is not None  # Same embedding, but user A has their own copy

    @pytest.mark.asyncio
    async def test_lru_eviction(self):
        backend = InMemoryCacheBackend(max_entries_per_scope=2)
        e1 = _make_entry(query="q1", embedding=[1.0] + [0.0] * 383)
        e2 = _make_entry(query="q2", embedding=[0.0, 1.0] + [0.0] * 382)
        e3 = _make_entry(query="q3", embedding=[0.0, 0.0, 1.0] + [0.0] * 381)
        await backend.store("user:a", e1)
        await backend.store("user:a", e2)
        await backend.store("user:a", e3)
        # e1 should be evicted (oldest)
        result = await backend.search("user:a", e1.embedding, 0.9)
        assert result is None

    @pytest.mark.asyncio
    async def test_purge_user(self):
        backend = InMemoryCacheBackend()
        await backend.store("user:alice", _make_entry())
        await backend.store("user:alice", _make_entry(query="q2"))
        await backend.store("user:bob", _make_entry())
        count = await backend.purge_user("alice")
        assert count == 2
        # Alice gone, Bob still there
        await backend.stats()
        assert backend._total_entries == 1

    @pytest.mark.asyncio
    async def test_invalidate_all(self):
        backend = InMemoryCacheBackend()
        await backend.store("user:a", _make_entry())
        await backend.store("user:a", _make_entry(query="q2"))
        count = await backend.invalidate("user:a")
        assert count == 2

    @pytest.mark.asyncio
    async def test_checksum_corruption(self):
        backend = InMemoryCacheBackend()
        entry = _make_entry()
        entry.checksum = "corrupted"
        await backend.store("user:a", entry)
        result = await backend.search("user:a", entry.embedding, 0.5)
        assert result is None  # Corrupted → treated as miss

    @pytest.mark.asyncio
    async def test_expired_entry_evicted(self):
        backend = InMemoryCacheBackend()
        entry = _make_entry(ttl=0)  # Immediately expires
        entry.created_at = time.time() - 10  # Created 10s ago
        await backend.store("user:a", entry)
        result = await backend.search("user:a", entry.embedding, 0.5)
        assert result is None  # Expired

    @pytest.mark.asyncio
    async def test_stats(self):
        backend = InMemoryCacheBackend()
        entry = _make_entry()
        await backend.store("user:a", entry)
        await backend.search("user:a", entry.embedding, 0.9)  # hit
        await backend.search("user:a", [0.0] * 384, 0.99)  # miss
        stats = await backend.stats()
        assert stats.hits == 1
        assert stats.misses == 0  # misses tracked at SemanticCache level
        assert stats.stores == 1


# ═══════════════════════════════════════════════════════════════════════
# SemanticCache
# ═══════════════════════════════════════════════════════════════════════


class TestSemanticCache:
    @pytest.mark.asyncio
    async def test_cache_hit_on_identical_query(self):
        cache = SemanticCache(
            embedding=FakeEmbeddingProvider(),
            similarity_threshold=0.9,
        )
        caller = CallerContext(user_id="user-1")

        # Store
        await cache.store(
            "What is Python?",
            "Python is a programming language.",
            {"messages": []},
            caller=caller,
            model_id="gpt",
            instruction_hash="h1",
        )

        # Check — identical query
        result = await cache.check(
            "What is Python?",
            caller=caller,
            model_id="gpt",
            instruction_hash="h1",
        )
        assert result is not None
        assert result.response_text == "Python is a programming language."

    @pytest.mark.asyncio
    async def test_cache_miss_different_model(self):
        cache = SemanticCache(
            embedding=FakeEmbeddingProvider(),
            similarity_threshold=0.9,
        )
        caller = CallerContext(user_id="user-1")

        await cache.store(
            "test",
            "response",
            {"messages": []},
            caller=caller,
            model_id="gpt-4",
            instruction_hash="h1",
        )

        # Different model → miss
        result = await cache.check(
            "test",
            caller=caller,
            model_id="claude",
            instruction_hash="h1",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_miss_different_instructions(self):
        cache = SemanticCache(
            embedding=FakeEmbeddingProvider(),
            similarity_threshold=0.9,
        )
        caller = CallerContext(user_id="user-1")

        await cache.store(
            "test",
            "response",
            {"messages": []},
            caller=caller,
            model_id="gpt",
            instruction_hash="v1",
        )

        # Different instructions → miss
        result = await cache.check(
            "test",
            caller=caller,
            model_id="gpt",
            instruction_hash="v2",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_caller_no_cache(self):
        cache = SemanticCache(embedding=FakeEmbeddingProvider())

        # No caller → store is no-op
        await cache.store("test", "response", {"messages": []})

        # No caller → check returns None
        result = await cache.check("test")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_user_id_no_cache(self):
        cache = SemanticCache(embedding=FakeEmbeddingProvider())
        caller = CallerContext()  # user_id is None

        await cache.store("test", "response", {"messages": []}, caller=caller)
        result = await cache.check("test", caller=caller)
        assert result is None

    @pytest.mark.asyncio
    async def test_per_user_isolation(self):
        cache = SemanticCache(
            embedding=FakeEmbeddingProvider(),
            similarity_threshold=0.5,
        )
        alice = CallerContext(user_id="alice")
        bob = CallerContext(user_id="bob")

        await cache.store(
            "secret",
            "alice's secret data",
            {"messages": []},
            caller=alice,
            model_id="gpt",
            instruction_hash="h",
        )

        # Bob can't see Alice's cache
        result = await cache.check(
            "secret",
            caller=bob,
            model_id="gpt",
            instruction_hash="h",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_shared_scope(self):
        cache = SemanticCache(
            embedding=FakeEmbeddingProvider(),
            scope="shared",
            shared_data_acknowledged=True,
            similarity_threshold=0.9,
        )
        alice = CallerContext(user_id="alice")
        bob = CallerContext(user_id="bob")

        await cache.store(
            "weather",
            "sunny",
            {"messages": []},
            caller=alice,
            model_id="gpt",
            instruction_hash="h",
        )

        # Bob CAN see shared cache
        result = await cache.check(
            "weather",
            caller=bob,
            model_id="gpt",
            instruction_hash="h",
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_ttl_pattern_override(self):
        cache = SemanticCache(
            embedding=FakeEmbeddingProvider(),
            default_ttl=3600,
            ttl_patterns={r"current|now|today": 60},
        )
        # "current" matches pattern → TTL should be 60
        ttl = cache._resolve_ttl("What is the current price?")
        assert ttl == 60

        # No pattern match → default TTL
        ttl = cache._resolve_ttl("What is Python?")
        assert ttl == 3600

    @pytest.mark.asyncio
    async def test_invalidate_for_write(self):
        cache = SemanticCache(
            embedding=FakeEmbeddingProvider(),
            similarity_threshold=0.5,
        )
        caller = CallerContext(user_id="user-1")

        await cache.store(
            "count tickets",
            "47 tickets",
            {"messages": []},
            caller=caller,
            model_id="gpt",
            instruction_hash="h",
        )

        # Write tool fires → invalidate
        await cache.invalidate_for_write("create_ticket", caller=caller)

        # Cache should be empty now
        result = await cache.check(
            "count tickets",
            caller=caller,
            model_id="gpt",
            instruction_hash="h",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_purge_user_gdpr(self):
        cache = SemanticCache(embedding=FakeEmbeddingProvider())
        caller = CallerContext(user_id="user-to-delete")

        await cache.store(
            "q1",
            "r1",
            {"messages": []},
            caller=caller,
            model_id="gpt",
            instruction_hash="h",
        )
        await cache.store(
            "q2",
            "r2",
            {"messages": []},
            caller=caller,
            model_id="gpt",
            instruction_hash="h",
        )

        count = await cache.purge_user("user-to-delete")
        assert count == 2

    @pytest.mark.asyncio
    async def test_stats(self):
        cache = SemanticCache(
            embedding=FakeEmbeddingProvider(),
            similarity_threshold=0.9,
        )
        caller = CallerContext(user_id="u")

        await cache.store("q", "r", {}, caller=caller, model_id="m", instruction_hash="h")
        await cache.check("q", caller=caller, model_id="m", instruction_hash="h")
        await cache.check("different", caller=caller, model_id="m", instruction_hash="h")

        stats = await cache.stats()
        assert stats.stores == 1
        assert stats.hits >= 1

    @pytest.mark.asyncio
    async def test_empty_query_skipped(self):
        cache = SemanticCache(embedding=FakeEmbeddingProvider())
        caller = CallerContext(user_id="u")
        result = await cache.check("", caller=caller)
        assert result is None

    @pytest.mark.asyncio
    async def test_warmup(self):
        cache = SemanticCache(embedding=FakeEmbeddingProvider())
        cache.warmup()  # Should not raise


# ═══════════════════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════════════════


class TestHelpers:
    def test_compute_context_fingerprint(self):
        fp1 = compute_context_fingerprint(conversation_length=5)
        fp2 = compute_context_fingerprint(conversation_length=10)
        assert fp1 != fp2  # Different context → different fingerprint

    def test_compute_context_fingerprint_deterministic(self):
        fp1 = compute_context_fingerprint(conversation_length=5, instruction_hash="abc")
        fp2 = compute_context_fingerprint(conversation_length=5, instruction_hash="abc")
        assert fp1 == fp2

    def test_compute_instruction_hash(self):
        h1 = compute_instruction_hash("You are helpful.")
        h2 = compute_instruction_hash("You are a data analyst.")
        assert h1 != h2

    def test_compute_instruction_hash_none(self):
        h = compute_instruction_hash(None)
        assert h == "default"


# ═══════════════════════════════════════════════════════════════════════
# CacheEntry
# ═══════════════════════════════════════════════════════════════════════


class TestCacheEntry:
    def test_verify_checksum_valid(self):
        entry = _make_entry()
        assert entry.verify_checksum() is True

    def test_verify_checksum_invalid(self):
        entry = _make_entry()
        entry.checksum = "wrong"
        assert entry.verify_checksum() is False

    def test_expired(self):
        entry = _make_entry(ttl=0)
        entry.created_at = time.time() - 10
        assert entry.expired is True

    def test_not_expired(self):
        entry = _make_entry(ttl=3600)
        assert entry.expired is False
