"""Multi-user isolation tests for :mod:`promptise.observability`.

Validates that the collector:

* Stamps every event with ``user_id`` / ``session_id`` from the current
  :class:`~promptise.agent.CallerContext`.
* Supports explicit ``user_id`` override on :meth:`ObservabilityCollector.record`.
* Filters entries by ``user_ids`` / ``session_ids`` via :meth:`query`.
* Returns only the target tenant's events via :meth:`for_user`.
* Drops only the target tenant's entries via :meth:`purge_user`.
"""

from __future__ import annotations

import asyncio

import pytest

from promptise.agent import CallerContext, _caller_ctx_var
from promptise.observability import (
    ObservabilityCollector,
    TimelineEntry,
    TimelineEventCategory,
    TimelineEventType,
)

# ---------------------------------------------------------------------------
# Auto-propagation from CallerContext
# ---------------------------------------------------------------------------


class TestCallerContextAutoPropagation:
    def test_user_id_is_stamped_from_caller(self) -> None:
        c = ObservabilityCollector()
        token = _caller_ctx_var.set(CallerContext(user_id="alice"))
        try:
            entry = c.record(TimelineEventType.TOOL_CALL, details="x")
        finally:
            _caller_ctx_var.reset(token)

        assert entry.user_id == "alice"

    def test_session_id_is_stamped_from_caller_metadata(self) -> None:
        c = ObservabilityCollector()
        token = _caller_ctx_var.set(
            CallerContext(user_id="alice", metadata={"session_id": "sess-1"})
        )
        try:
            entry = c.record(TimelineEventType.LLM_TURN)
        finally:
            _caller_ctx_var.reset(token)

        assert entry.user_id == "alice"
        assert entry.session_id == "sess-1"

    def test_explicit_user_id_overrides_contextvar(self) -> None:
        c = ObservabilityCollector()
        token = _caller_ctx_var.set(CallerContext(user_id="alice"))
        try:
            entry = c.record(TimelineEventType.TOOL_CALL, user_id="override")
        finally:
            _caller_ctx_var.reset(token)

        assert entry.user_id == "override"

    def test_no_caller_means_user_id_is_none(self) -> None:
        c = ObservabilityCollector()
        # No contextvar set.
        entry = c.record(TimelineEventType.TOOL_CALL)
        assert entry.user_id is None
        assert entry.session_id is None


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerializationIncludesUserId:
    def test_to_dict_includes_user_id_and_session_id(self) -> None:
        entry = TimelineEntry(
            entry_id="e1",
            timestamp=1.0,
            event_type=TimelineEventType.TOOL_CALL,
            category=TimelineEventCategory.TRANSPARENCY,
            user_id="alice",
            session_id="sess-1",
        )
        d = entry.to_dict()
        assert d["user_id"] == "alice"
        assert d["session_id"] == "sess-1"

    def test_to_dict_omits_nothing_when_user_is_none(self) -> None:
        c = ObservabilityCollector()
        entry = c.record(TimelineEventType.TOOL_CALL)
        d = entry.to_dict()
        assert "user_id" in d
        assert d["user_id"] is None


# ---------------------------------------------------------------------------
# Query / filter / for_user
# ---------------------------------------------------------------------------


class TestQueryFilteringByUser:
    def _populate(self, collector: ObservabilityCollector) -> None:
        for user, text in [("alice", "a1"), ("bob", "b1"), ("alice", "a2")]:
            token = _caller_ctx_var.set(CallerContext(user_id=user))
            try:
                collector.record(TimelineEventType.TOOL_CALL, details=text)
            finally:
                _caller_ctx_var.reset(token)

    def test_query_user_ids_filters_to_that_user(self) -> None:
        c = ObservabilityCollector()
        self._populate(c)

        alice_only = c.query(user_ids=["alice"])
        assert [e.details for e in alice_only] == ["a1", "a2"]

    def test_query_user_ids_supports_multiple(self) -> None:
        c = ObservabilityCollector()
        self._populate(c)
        both = c.query(user_ids=["alice", "bob"])
        assert len(both) == 3

    def test_query_user_ids_unknown_user_returns_empty(self) -> None:
        c = ObservabilityCollector()
        self._populate(c)
        assert c.query(user_ids=["mallory"]) == []

    def test_for_user_shortcut(self) -> None:
        c = ObservabilityCollector()
        self._populate(c)

        alice_entries = c.for_user("alice")
        assert all(e.user_id == "alice" for e in alice_entries)
        assert len(alice_entries) == 2

    def test_for_user_empty_id_returns_empty(self) -> None:
        c = ObservabilityCollector()
        self._populate(c)
        assert c.for_user("") == []

    def test_get_users_returns_unique_sorted(self) -> None:
        c = ObservabilityCollector()
        self._populate(c)
        assert c.get_users() == ["alice", "bob"]

    def test_query_session_ids_filters(self) -> None:
        c = ObservabilityCollector()
        token_a = _caller_ctx_var.set(CallerContext(user_id="alice", metadata={"session_id": "s1"}))
        try:
            c.record(TimelineEventType.TOOL_CALL, details="a-s1")
        finally:
            _caller_ctx_var.reset(token_a)

        token_b = _caller_ctx_var.set(CallerContext(user_id="alice", metadata={"session_id": "s2"}))
        try:
            c.record(TimelineEventType.TOOL_CALL, details="a-s2")
        finally:
            _caller_ctx_var.reset(token_b)

        s1 = c.query(session_ids=["s1"])
        assert [e.details for e in s1] == ["a-s1"]


# ---------------------------------------------------------------------------
# GDPR purge
# ---------------------------------------------------------------------------


class TestPurgeUser:
    def test_purge_removes_only_target(self) -> None:
        c = ObservabilityCollector()
        for user in ["alice", "bob", "alice", "carol"]:
            token = _caller_ctx_var.set(CallerContext(user_id=user))
            try:
                c.record(TimelineEventType.TOOL_CALL)
            finally:
                _caller_ctx_var.reset(token)

        removed = c.purge_user("alice")
        assert removed == 2

        remaining_users = c.get_users()
        assert remaining_users == ["bob", "carol"]

    def test_purge_empty_user_id_is_noop(self) -> None:
        c = ObservabilityCollector()
        token = _caller_ctx_var.set(CallerContext(user_id="alice"))
        try:
            c.record(TimelineEventType.TOOL_CALL)
        finally:
            _caller_ctx_var.reset(token)

        assert c.purge_user("") == 0
        assert len(c.get_timeline()) == 1

    def test_purge_preserves_events_with_null_user_id(self) -> None:
        c = ObservabilityCollector()
        # System event outside of any invocation.
        c.record(TimelineEventType.HEALTH_CHECK)
        # Alice event.
        token = _caller_ctx_var.set(CallerContext(user_id="alice"))
        try:
            c.record(TimelineEventType.TOOL_CALL)
        finally:
            _caller_ctx_var.reset(token)

        assert c.purge_user("alice") == 1
        remaining = c.get_timeline()
        assert len(remaining) == 1
        assert remaining[0].event_type == TimelineEventType.HEALTH_CHECK


# ---------------------------------------------------------------------------
# Concurrent callers — contextvar isolation
# ---------------------------------------------------------------------------


class TestConcurrentRecording:
    @pytest.mark.asyncio
    async def test_concurrent_tasks_stamp_correct_user_id(self) -> None:
        c = ObservabilityCollector()

        async def as_user(user: str, n: int) -> None:
            token = _caller_ctx_var.set(CallerContext(user_id=user))
            try:
                for _ in range(n):
                    await asyncio.sleep(0)
                    c.record(TimelineEventType.TOOL_CALL, details=user)
            finally:
                _caller_ctx_var.reset(token)

        await asyncio.gather(
            as_user("alice", 5),
            as_user("bob", 5),
            as_user("carol", 5),
        )

        # Each user sees only their own events.
        for u in ["alice", "bob", "carol"]:
            events = c.for_user(u)
            assert len(events) == 5
            assert all(e.details == u for e in events)
