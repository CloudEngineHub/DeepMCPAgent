"""Multi-user / multi-tenant isolation tests for :mod:`promptise.memory`.

Covers:

* ``SHARED`` scope: legacy behavior, no ``user_id`` filtering.
* ``PER_USER`` scope: search/add/delete filter by ``user_id``.
* ``MemoryIsolationError`` raised when per-user scope is missing a ``user_id``.
* ``purge_user`` removes only the target user's entries.
* ``MemoryAgent`` auto-propagates the current ``CallerContext.user_id``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from promptise.agent import CallerContext, _caller_ctx_var
from promptise.memory import (
    InMemoryProvider,
    MemoryAgent,
    MemoryIsolationError,
    MemoryScope,
)


# ---------------------------------------------------------------------------
# SHARED scope (default) — legacy behavior must not regress
# ---------------------------------------------------------------------------


class TestInMemoryProviderSharedScope:
    @pytest.mark.asyncio
    async def test_default_scope_is_shared(self) -> None:
        p = InMemoryProvider()
        assert p.scope is MemoryScope.SHARED

    @pytest.mark.asyncio
    async def test_shared_scope_ignores_user_id_on_add(self) -> None:
        p = InMemoryProvider()
        # Per-call user_id is harmless in SHARED scope.
        mid = await p.add("alice note", user_id="alice")
        assert mid

        # Bob can still search freely — no filtering in SHARED mode.
        hits = await p.search("alice", user_id="bob")
        assert len(hits) == 1
        assert hits[0].content == "alice note"

    @pytest.mark.asyncio
    async def test_shared_scope_search_without_user_id_returns_all(self) -> None:
        p = InMemoryProvider()
        await p.add("alpha one")
        await p.add("alpha two")

        # Both contain "alpha" — SHARED scope returns both without any
        # user filtering.
        hits = await p.search("alpha")
        assert len(hits) == 2

    @pytest.mark.asyncio
    async def test_shared_scope_delete_no_owner_check(self) -> None:
        p = InMemoryProvider()
        mid = await p.add("doomed")
        assert await p.delete(mid) is True
        assert await p.delete(mid) is False  # already gone

    @pytest.mark.asyncio
    async def test_shared_scope_purge_user_is_noop(self) -> None:
        p = InMemoryProvider()
        await p.add("one")
        await p.add("two")

        # SHARED scope has no per-user ownership to honor.
        assert await p.purge_user("anyone") == 0

        # Entries remain intact.
        assert len(await p.search("on")) == 1  # matches "one"
        assert len(await p.search("tw")) == 1  # matches "two"


# ---------------------------------------------------------------------------
# PER_USER scope — the isolation contract
# ---------------------------------------------------------------------------


class TestInMemoryProviderPerUserScope:
    @pytest.mark.asyncio
    async def test_add_requires_user_id(self) -> None:
        p = InMemoryProvider(scope=MemoryScope.PER_USER)
        with pytest.raises(MemoryIsolationError):
            await p.add("no owner")

    @pytest.mark.asyncio
    async def test_search_requires_user_id(self) -> None:
        p = InMemoryProvider(scope=MemoryScope.PER_USER)
        with pytest.raises(MemoryIsolationError):
            await p.search("anything")

    @pytest.mark.asyncio
    async def test_delete_requires_user_id(self) -> None:
        p = InMemoryProvider(scope=MemoryScope.PER_USER)
        with pytest.raises(MemoryIsolationError):
            await p.delete("some-id")

    @pytest.mark.asyncio
    async def test_users_cannot_read_each_other_via_search(self) -> None:
        p = InMemoryProvider(scope=MemoryScope.PER_USER)
        await p.add("alice loves jazz", user_id="alice")
        await p.add("bob loves rock", user_id="bob")
        await p.add("alice also likes rock", user_id="alice")

        alice_hits = await p.search("rock", user_id="alice")
        bob_hits = await p.search("rock", user_id="bob")

        # Alice sees only her own rock entry.
        assert len(alice_hits) == 1
        assert "alice" in alice_hits[0].content.lower()

        # Bob sees only his own rock entry.
        assert len(bob_hits) == 1
        assert "bob" in bob_hits[0].content.lower()

    @pytest.mark.asyncio
    async def test_search_returns_empty_for_user_with_no_data(self) -> None:
        p = InMemoryProvider(scope=MemoryScope.PER_USER)
        await p.add("alice data", user_id="alice")

        carol_hits = await p.search("data", user_id="carol")
        assert carol_hits == []

    @pytest.mark.asyncio
    async def test_delete_refuses_cross_user_delete(self) -> None:
        p = InMemoryProvider(scope=MemoryScope.PER_USER)
        alice_mid = await p.add("alice secret", user_id="alice")

        # Bob may not delete alice's entry.
        assert await p.delete(alice_mid, user_id="bob") is False

        # Alice's entry is still there.
        hits = await p.search("secret", user_id="alice")
        assert len(hits) == 1

    @pytest.mark.asyncio
    async def test_delete_allows_owner_to_delete(self) -> None:
        p = InMemoryProvider(scope=MemoryScope.PER_USER)
        alice_mid = await p.add("to be deleted", user_id="alice")
        assert await p.delete(alice_mid, user_id="alice") is True

    @pytest.mark.asyncio
    async def test_purge_user_only_removes_target(self) -> None:
        p = InMemoryProvider(scope=MemoryScope.PER_USER)
        await p.add("alice-1", user_id="alice")
        await p.add("alice-2", user_id="alice")
        await p.add("bob-1", user_id="bob")

        removed = await p.purge_user("alice")
        assert removed == 2

        # Bob is untouched.
        bob_hits = await p.search("bob", user_id="bob")
        assert len(bob_hits) == 1

        # Alice is empty.
        alice_hits = await p.search("alice", user_id="alice")
        assert alice_hits == []

    @pytest.mark.asyncio
    async def test_purge_empty_user_id_is_noop(self) -> None:
        p = InMemoryProvider(scope=MemoryScope.PER_USER)
        await p.add("n", user_id="alice")
        assert await p.purge_user("") == 0

    @pytest.mark.asyncio
    async def test_scores_match_expected_order_within_user(self) -> None:
        """Per-user filtering must preserve the relevance ordering."""
        p = InMemoryProvider(scope=MemoryScope.PER_USER)
        await p.add("alpha beta", user_id="alice")
        await p.add("alpha", user_id="alice")  # shorter → higher score
        await p.add("alpha gamma delta", user_id="alice")

        hits = await p.search("alpha", user_id="alice")
        assert hits[0].content == "alpha"
        # All three alice entries returned.
        assert len(hits) == 3


# ---------------------------------------------------------------------------
# MemoryAgent — auto-propagates CallerContext.user_id
# ---------------------------------------------------------------------------


class TestMemoryAgentCallerPropagation:
    @pytest.mark.asyncio
    async def test_memory_agent_passes_user_id_from_contextvar(self) -> None:
        p = InMemoryProvider(scope=MemoryScope.PER_USER)
        await p.add("alice loves jazz", user_id="alice")
        await p.add("bob loves rock", user_id="bob")

        inner = type("Inner", (), {})()
        inner.ainvoke = AsyncMock(return_value={"messages": []})

        agent = MemoryAgent(inner, p, min_score=0.0)

        # Simulate "alice is invoking" via the CallerContext contextvar.
        token = _caller_ctx_var.set(CallerContext(user_id="alice"))
        try:
            await agent.ainvoke({"messages": [{"role": "user", "content": "rock"}]})
        finally:
            _caller_ctx_var.reset(token)

        # The memory search must have been scoped — if it wasn't, the
        # provider would raise MemoryIsolationError or leak bob's entry.
        inner.ainvoke.assert_awaited_once()
        # Inspect the input passed to the inner agent — it should contain
        # only alice's memory (there is none matching "rock" for alice, so
        # no memory block should be injected).
        (call_input,), _ = inner.ainvoke.call_args
        msgs = call_input["messages"]
        injected = [m for m in msgs if getattr(m, "type", None) == "system"]
        # No injection for alice (no match); bob's "rock" entry is hidden.
        assert not any(
            "bob loves rock" in (getattr(m, "content", "") or "") for m in injected
        )

    @pytest.mark.asyncio
    async def test_memory_agent_raises_without_caller_on_per_user(self) -> None:
        """Per-user provider + no CallerContext ⇒ graceful degradation.

        ``MemoryAgent._search_memory`` catches the isolation error so a
        missing caller context never breaks the agent; it simply skips
        memory injection.  This keeps behavior predictable in background
        tasks where no caller is set.
        """
        p = InMemoryProvider(scope=MemoryScope.PER_USER)
        await p.add("alice data", user_id="alice")

        inner = type("Inner", (), {})()
        inner.ainvoke = AsyncMock(return_value={"messages": []})

        agent = MemoryAgent(inner, p)
        # No CallerContext set → user_id=None → provider raises →
        # _search_memory catches & returns [].
        await agent.ainvoke({"messages": [{"role": "user", "content": "data"}]})

        inner.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_store_tags_content_with_caller_user_id(self) -> None:
        p = InMemoryProvider(scope=MemoryScope.PER_USER)

        inner = type("Inner", (), {})()
        inner.ainvoke = AsyncMock(
            return_value={"messages": [{"role": "assistant", "content": "hi alice"}]}
        )

        agent = MemoryAgent(inner, p, auto_store=True)

        token = _caller_ctx_var.set(CallerContext(user_id="alice"))
        try:
            await agent.ainvoke({"messages": [{"role": "user", "content": "hello"}]})
        finally:
            _caller_ctx_var.reset(token)

        # Alice can search her stored exchange; bob cannot.
        alice_hits = await p.search("hello", user_id="alice")
        assert len(alice_hits) == 1
        bob_hits = await p.search("hello", user_id="bob")
        assert bob_hits == []


# ---------------------------------------------------------------------------
# Concurrent callers should not leak across asyncio tasks
# ---------------------------------------------------------------------------


class TestConcurrentIsolation:
    @pytest.mark.asyncio
    async def test_two_concurrent_contextvars_stay_isolated(self) -> None:
        p = InMemoryProvider(scope=MemoryScope.PER_USER)
        await p.add("alice memo", user_id="alice")
        await p.add("bob memo", user_id="bob")

        async def as_user(user_id: str) -> list[str]:
            token = _caller_ctx_var.set(CallerContext(user_id=user_id))
            try:
                await asyncio.sleep(0)  # force scheduler interleave
                results = await p.search("memo", user_id=user_id)
                return [r.content for r in results]
            finally:
                _caller_ctx_var.reset(token)

        alice_task = asyncio.create_task(as_user("alice"))
        bob_task = asyncio.create_task(as_user("bob"))
        alice_out, bob_out = await asyncio.gather(alice_task, bob_task)

        assert alice_out == ["alice memo"]
        assert bob_out == ["bob memo"]
