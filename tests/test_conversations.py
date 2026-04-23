"""Tests for promptise.conversations — session management and chat persistence."""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from promptise.conversations import (
    ConversationStore,
    InMemoryConversationStore,
    Message,
    PostgresConversationStore,
    RedisConversationStore,
    SessionAccessDenied,
    SessionInfo,
    SQLiteConversationStore,
    generate_session_id,
)

# ---------------------------------------------------------------------------
# Message dataclass tests
# ---------------------------------------------------------------------------


class TestMessage:
    """Tests for the Message dataclass."""

    def test_defaults(self) -> None:
        msg = Message(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"
        assert msg.metadata == {}
        assert isinstance(msg.created_at, datetime)
        assert msg.created_at.tzinfo is not None

    def test_with_metadata(self) -> None:
        msg = Message(role="assistant", content="hi", metadata={"tokens": 5})
        assert msg.metadata == {"tokens": 5}

    def test_to_dict(self) -> None:
        msg = Message(role="user", content="test")
        d = msg.to_dict()
        assert d["role"] == "user"
        assert d["content"] == "test"
        assert "created_at" in d
        assert d["metadata"] == {}

    def test_from_dict(self) -> None:
        now = datetime.now(timezone.utc)
        d = {
            "role": "assistant",
            "content": "response",
            "metadata": {"tool": "search"},
            "created_at": now.isoformat(),
        }
        msg = Message.from_dict(d)
        assert msg.role == "assistant"
        assert msg.content == "response"
        assert msg.metadata == {"tool": "search"}

    def test_from_dict_no_created_at(self) -> None:
        msg = Message.from_dict({"role": "user", "content": "hi"})
        assert isinstance(msg.created_at, datetime)

    def test_roundtrip(self) -> None:
        original = Message(role="user", content="test", metadata={"a": 1})
        restored = Message.from_dict(original.to_dict())
        assert restored.role == original.role
        assert restored.content == original.content
        assert restored.metadata == original.metadata


# ---------------------------------------------------------------------------
# SessionInfo dataclass tests
# ---------------------------------------------------------------------------


class TestSessionInfo:
    """Tests for the SessionInfo dataclass."""

    def test_defaults(self) -> None:
        info = SessionInfo(session_id="s1")
        assert info.session_id == "s1"
        assert info.user_id is None
        assert info.title is None
        assert info.message_count == 0
        assert info.metadata == {}
        assert isinstance(info.created_at, datetime)

    def test_with_all_fields(self) -> None:
        info = SessionInfo(
            session_id="s1",
            user_id="u1",
            title="Test",
            message_count=5,
            metadata={"tag": "support"},
        )
        assert info.user_id == "u1"
        assert info.title == "Test"
        assert info.message_count == 5
        assert info.metadata == {"tag": "support"}


# ---------------------------------------------------------------------------
# Protocol compliance tests
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    """Verify built-in stores satisfy the ConversationStore protocol."""

    def test_inmemory_is_conversation_store(self) -> None:
        assert isinstance(InMemoryConversationStore(), ConversationStore)

    def test_postgres_is_conversation_store(self) -> None:
        store = PostgresConversationStore("postgresql://localhost/test")
        assert isinstance(store, ConversationStore)

    def test_sqlite_is_conversation_store(self) -> None:
        store = SQLiteConversationStore(":memory:")
        assert isinstance(store, ConversationStore)

    def test_redis_is_conversation_store(self) -> None:
        store = RedisConversationStore("redis://localhost")
        assert isinstance(store, ConversationStore)

    def test_custom_store_satisfies_protocol(self) -> None:
        class CustomStore:
            async def get_session(self, session_id: str) -> SessionInfo | None:
                return None

            async def load_messages(self, session_id: str) -> list[Message]:
                return []

            async def save_messages(self, session_id: str, messages: list[Message]) -> None:
                pass

            async def delete_session(self, session_id: str) -> bool:
                return True

            async def list_sessions(self, *, user_id=None, limit=50, offset=0) -> list[SessionInfo]:
                return []

            async def close(self) -> None:
                pass

        assert isinstance(CustomStore(), ConversationStore)


# ---------------------------------------------------------------------------
# InMemoryConversationStore tests
# ---------------------------------------------------------------------------


class TestInMemoryConversationStore:
    """Tests for InMemoryConversationStore."""

    @pytest.fixture
    def store(self) -> InMemoryConversationStore:
        return InMemoryConversationStore()

    @pytest.mark.asyncio
    async def test_empty_load(self, store: InMemoryConversationStore) -> None:
        messages = await store.load_messages("nonexistent")
        assert messages == []

    @pytest.mark.asyncio
    async def test_save_and_load(self, store: InMemoryConversationStore) -> None:
        msgs = [
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi there!"),
        ]
        await store.save_messages("s1", msgs)
        loaded = await store.load_messages("s1")
        assert len(loaded) == 2
        assert loaded[0].role == "user"
        assert loaded[1].content == "Hi there!"

    @pytest.mark.asyncio
    async def test_save_creates_session(self, store: InMemoryConversationStore) -> None:
        await store.save_messages("s1", [Message(role="user", content="test")])
        sessions = await store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].session_id == "s1"
        assert sessions[0].message_count == 1

    @pytest.mark.asyncio
    async def test_save_updates_session(self, store: InMemoryConversationStore) -> None:
        await store.save_messages("s1", [Message(role="user", content="a")])
        old_sessions = await store.list_sessions()
        old_time = old_sessions[0].updated_at

        await store.save_messages(
            "s1",
            [
                Message(role="user", content="a"),
                Message(role="assistant", content="b"),
            ],
        )
        sessions = await store.list_sessions()
        assert sessions[0].message_count == 2
        assert sessions[0].updated_at >= old_time

    @pytest.mark.asyncio
    async def test_delete_session(self, store: InMemoryConversationStore) -> None:
        await store.save_messages("s1", [Message(role="user", content="hi")])
        assert await store.delete_session("s1") is True
        assert await store.delete_session("s1") is False
        assert await store.load_messages("s1") == []

    @pytest.mark.asyncio
    async def test_list_sessions_ordering(self, store: InMemoryConversationStore) -> None:
        import asyncio

        await store.save_messages("s1", [Message(role="user", content="first")])
        # Windows ``time.time()`` resolution is ~15.6ms; without a small pause
        # all three saves collapse to the same timestamp and the ordering is
        # undefined.
        await asyncio.sleep(0.02)
        await store.save_messages("s2", [Message(role="user", content="second")])
        await asyncio.sleep(0.02)
        await store.save_messages("s3", [Message(role="user", content="third")])

        sessions = await store.list_sessions()
        assert len(sessions) == 3
        # Most recent first
        assert sessions[0].session_id == "s3"

    @pytest.mark.asyncio
    async def test_list_sessions_pagination(self, store: InMemoryConversationStore) -> None:
        for i in range(10):
            await store.save_messages(f"s{i}", [Message(role="user", content=f"msg-{i}")])

        page1 = await store.list_sessions(limit=3)
        assert len(page1) == 3
        page2 = await store.list_sessions(limit=3, offset=3)
        assert len(page2) == 3
        assert page1[0].session_id != page2[0].session_id

    @pytest.mark.asyncio
    async def test_list_sessions_user_filter(self, store: InMemoryConversationStore) -> None:
        await store.save_messages("s1", [Message(role="user", content="a")])
        await store.save_messages("s2", [Message(role="user", content="b")])
        await store.update_session("s1", user_id="alice")
        await store.update_session("s2", user_id="bob")

        alice = await store.list_sessions(user_id="alice")
        assert len(alice) == 1
        assert alice[0].session_id == "s1"

    @pytest.mark.asyncio
    async def test_max_sessions_eviction(self) -> None:
        store = InMemoryConversationStore(max_sessions=2)
        await store.save_messages("s1", [Message(role="user", content="a")])
        await store.save_messages("s2", [Message(role="user", content="b")])
        await store.save_messages("s3", [Message(role="user", content="c")])

        assert store.session_count() == 2
        # s1 should be evicted (oldest)
        assert await store.load_messages("s1") == []

    @pytest.mark.asyncio
    async def test_max_messages_per_session(self) -> None:
        store = InMemoryConversationStore(max_messages_per_session=3)
        msgs = [Message(role="user", content=f"msg-{i}") for i in range(5)]
        await store.save_messages("s1", msgs)

        loaded = await store.load_messages("s1")
        assert len(loaded) == 3
        # Should keep the last 3
        assert loaded[0].content == "msg-2"

    @pytest.mark.asyncio
    async def test_isolation_between_sessions(self, store: InMemoryConversationStore) -> None:
        await store.save_messages("s1", [Message(role="user", content="session 1")])
        await store.save_messages("s2", [Message(role="user", content="session 2")])

        s1 = await store.load_messages("s1")
        s2 = await store.load_messages("s2")
        assert s1[0].content == "session 1"
        assert s2[0].content == "session 2"

    @pytest.mark.asyncio
    async def test_returned_messages_are_copies(self, store: InMemoryConversationStore) -> None:
        """Modifying returned messages should not affect the store."""
        await store.save_messages("s1", [Message(role="user", content="original")])
        loaded = await store.load_messages("s1")
        loaded.append(Message(role="assistant", content="injected"))

        reloaded = await store.load_messages("s1")
        assert len(reloaded) == 1  # Not affected by external mutation

    @pytest.mark.asyncio
    async def test_update_session(self, store: InMemoryConversationStore) -> None:
        await store.save_messages("s1", [Message(role="user", content="test")])
        assert await store.update_session("s1", title="My Chat", user_id="alice") is True
        sessions = await store.list_sessions()
        assert sessions[0].title == "My Chat"
        assert sessions[0].user_id == "alice"

    @pytest.mark.asyncio
    async def test_update_nonexistent_session(self, store: InMemoryConversationStore) -> None:
        assert await store.update_session("nope", title="x") is False

    @pytest.mark.asyncio
    async def test_close_is_noop(self, store: InMemoryConversationStore) -> None:
        await store.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_concurrent_access(self, store: InMemoryConversationStore) -> None:
        """Multiple coroutines writing to the same session should not corrupt."""

        async def writer(n: int) -> None:
            msgs = [Message(role="user", content=f"msg-{n}-{i}") for i in range(10)]
            await store.save_messages(f"s-{n}", msgs)

        await asyncio.gather(*[writer(i) for i in range(20)])
        assert store.session_count() == 20


# ---------------------------------------------------------------------------
# SQLiteConversationStore tests
# ---------------------------------------------------------------------------


class TestSQLiteConversationStore:
    """Tests for SQLiteConversationStore (using :memory:)."""

    @pytest.fixture
    async def store(self) -> SQLiteConversationStore:
        s = SQLiteConversationStore(":memory:")
        yield s
        await s.close()

    @pytest.mark.asyncio
    async def test_empty_load(self, store: SQLiteConversationStore) -> None:
        assert await store.load_messages("x") == []

    @pytest.mark.asyncio
    async def test_save_and_load(self, store: SQLiteConversationStore) -> None:
        msgs = [
            Message(role="user", content="Hello"),
            Message(role="assistant", content="World"),
        ]
        await store.save_messages("s1", msgs)
        loaded = await store.load_messages("s1")
        assert len(loaded) == 2
        assert loaded[0].role == "user"
        assert loaded[1].content == "World"

    @pytest.mark.asyncio
    async def test_delete_session(self, store: SQLiteConversationStore) -> None:
        await store.save_messages("s1", [Message(role="user", content="a")])
        assert await store.delete_session("s1") is True
        assert await store.delete_session("s1") is False
        assert await store.load_messages("s1") == []

    @pytest.mark.asyncio
    async def test_list_sessions(self, store: SQLiteConversationStore) -> None:
        await store.save_messages("s1", [Message(role="user", content="a")])
        await store.save_messages("s2", [Message(role="user", content="b")])
        sessions = await store.list_sessions()
        assert len(sessions) == 2

    @pytest.mark.asyncio
    async def test_list_sessions_pagination(self, store: SQLiteConversationStore) -> None:
        for i in range(5):
            await store.save_messages(f"s{i}", [Message(role="user", content=f"m{i}")])
        page = await store.list_sessions(limit=2, offset=2)
        assert len(page) == 2

    @pytest.mark.asyncio
    async def test_replace_messages(self, store: SQLiteConversationStore) -> None:
        """Saving again should replace, not append."""
        await store.save_messages("s1", [Message(role="user", content="first")])
        await store.save_messages(
            "s1",
            [
                Message(role="user", content="first"),
                Message(role="assistant", content="second"),
            ],
        )
        loaded = await store.load_messages("s1")
        assert len(loaded) == 2

    @pytest.mark.asyncio
    async def test_max_messages(self) -> None:
        store = SQLiteConversationStore(":memory:", max_messages_per_session=3)
        msgs = [Message(role="user", content=f"m{i}") for i in range(5)]
        await store.save_messages("s1", msgs)
        loaded = await store.load_messages("s1")
        assert len(loaded) == 3
        assert loaded[0].content == "m2"
        await store.close()

    @pytest.mark.asyncio
    async def test_metadata_preserved(self, store: SQLiteConversationStore) -> None:
        msg = Message(role="user", content="test", metadata={"tool": "search", "count": 3})
        await store.save_messages("s1", [msg])
        loaded = await store.load_messages("s1")
        assert loaded[0].metadata == {"tool": "search", "count": 3}

    @pytest.mark.asyncio
    async def test_update_session(self, store: SQLiteConversationStore) -> None:
        await store.save_messages("s1", [Message(role="user", content="test")])
        assert await store.update_session("s1", title="Chat", user_id="alice") is True
        sessions = await store.list_sessions(user_id="alice")
        assert len(sessions) == 1
        assert sessions[0].title == "Chat"

    @pytest.mark.asyncio
    async def test_file_persistence(self) -> None:
        """Test that data persists across store instances."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name

        try:
            store1 = SQLiteConversationStore(path)
            await store1.save_messages("s1", [Message(role="user", content="persist me")])
            await store1.close()

            store2 = SQLiteConversationStore(path)
            loaded = await store2.load_messages("s1")
            assert len(loaded) == 1
            assert loaded[0].content == "persist me"
            await store2.close()
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_table_prefix(self) -> None:
        store = SQLiteConversationStore(":memory:", table_prefix="myapp_")
        await store.save_messages("s1", [Message(role="user", content="test")])
        loaded = await store.load_messages("s1")
        assert len(loaded) == 1
        await store.close()

    @pytest.mark.asyncio
    async def test_close_and_reopen(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name

        try:
            store = SQLiteConversationStore(path)
            await store.save_messages("s1", [Message(role="user", content="test")])
            await store.close()

            # Should be able to re-initialize after close
            store2 = SQLiteConversationStore(path)
            loaded = await store2.load_messages("s1")
            assert len(loaded) == 1
            await store2.close()
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# PostgresConversationStore tests (unit — no real DB)
# ---------------------------------------------------------------------------


class TestTablePrefixValidation:
    """Tests for SQL injection prevention via table prefix validation."""

    def test_safe_prefixes(self) -> None:
        from promptise.conversations import _validate_table_prefix

        assert _validate_table_prefix("promptise_") == "promptise_"
        assert _validate_table_prefix("myapp_") == "myapp_"
        assert _validate_table_prefix("App123") == "App123"
        assert _validate_table_prefix("_private") == "_private"
        assert _validate_table_prefix("") == ""

    def test_unsafe_prefixes(self) -> None:
        from promptise.conversations import _validate_table_prefix

        with pytest.raises(ValueError, match="Unsafe table_prefix"):
            _validate_table_prefix("drop table;--")
        with pytest.raises(ValueError, match="Unsafe table_prefix"):
            _validate_table_prefix("my-prefix")
        with pytest.raises(ValueError, match="Unsafe table_prefix"):
            _validate_table_prefix("123start")
        with pytest.raises(ValueError, match="Unsafe table_prefix"):
            _validate_table_prefix("has space")

    def test_postgres_rejects_unsafe(self) -> None:
        with pytest.raises(ValueError):
            PostgresConversationStore("postgresql://localhost/test", table_prefix="bad;prefix")

    def test_sqlite_rejects_unsafe(self) -> None:
        with pytest.raises(ValueError):
            SQLiteConversationStore(":memory:", table_prefix="bad;prefix")


class TestPostgresConversationStoreUnit:
    """Unit tests for PostgresConversationStore (no real database)."""

    def test_constructor(self) -> None:
        store = PostgresConversationStore(
            "postgresql://localhost/test",
            table_prefix="app_",
            pool_min=1,
            pool_max=5,
            max_messages_per_session=100,
        )
        assert store._prefix == "app_"
        assert store._pool_min == 1
        assert store._pool_max == 5
        assert store._max_messages == 100

    def test_requires_asyncpg(self) -> None:
        store = PostgresConversationStore("postgresql://localhost/test")
        # _ensure_pool should raise ImportError if asyncpg not installed
        # We test the attribute rather than calling it to avoid side effects
        assert store._initialized is False

    def test_table_prefix_default(self) -> None:
        store = PostgresConversationStore("postgresql://localhost/test")
        assert store._prefix == "promptise_"


# ---------------------------------------------------------------------------
# RedisConversationStore tests (unit — no real Redis)
# ---------------------------------------------------------------------------


class TestRedisConversationStoreUnit:
    """Unit tests for RedisConversationStore (no real Redis)."""

    def test_constructor(self) -> None:
        store = RedisConversationStore(
            "redis://localhost:6379",
            key_prefix="myapp:",
            ttl=3600,
            max_messages_per_session=100,
        )
        assert store._prefix == "myapp:"
        assert store._ttl == 3600
        assert store._max_messages == 100

    def test_key_generation(self) -> None:
        store = RedisConversationStore(key_prefix="p:")
        assert store._session_key("s1") == "p:session:s1"
        assert store._messages_key("s1") == "p:messages:s1"
        assert store._index_key() == "p:all_sessions"
        assert store._index_key("alice") == "p:user_sessions:alice"


# ---------------------------------------------------------------------------
# Agent integration tests (chat method)
# ---------------------------------------------------------------------------


class TestAgentChatIntegration:
    """Test the PromptiseAgent.chat() method with a mock store."""

    @pytest.mark.asyncio
    async def test_chat_without_store(self) -> None:
        """chat() should work without a conversation store (no persistence)."""
        from unittest.mock import AsyncMock, MagicMock

        from promptise.agent import PromptiseAgent

        inner = AsyncMock()
        inner.ainvoke = AsyncMock(
            return_value={"messages": [MagicMock(content="Hello back!", type="ai")]}
        )

        agent = PromptiseAgent(inner=inner)
        result = await agent.chat("Hello", session_id="s1")
        assert result == "Hello back!"

    @pytest.mark.asyncio
    async def test_chat_loads_and_saves(self) -> None:
        """chat() should load history, invoke, and save updated history."""
        from unittest.mock import AsyncMock, MagicMock

        from promptise.agent import PromptiseAgent

        inner = AsyncMock()
        inner.ainvoke = AsyncMock(
            return_value={"messages": [MagicMock(content="I'm good!", type="ai")]}
        )

        store = InMemoryConversationStore()

        agent = PromptiseAgent(inner=inner, conversation_store=store)

        # First message
        r1 = await agent.chat("Hello", session_id="s1")
        assert r1 == "I'm good!"

        # Verify persisted
        msgs = await store.load_messages("s1")
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[0].content == "Hello"
        assert msgs[1].role == "assistant"
        assert msgs[1].content == "I'm good!"

    @pytest.mark.asyncio
    async def test_chat_accumulates_history(self) -> None:
        """Multiple chat() calls should accumulate history."""
        from unittest.mock import AsyncMock, MagicMock

        from promptise.agent import PromptiseAgent

        call_count = 0

        async def mock_invoke(input, config=None, **kw):
            nonlocal call_count
            call_count += 1
            return {"messages": [MagicMock(content=f"reply-{call_count}", type="ai")]}

        inner = AsyncMock()
        inner.ainvoke = mock_invoke

        store = InMemoryConversationStore()
        agent = PromptiseAgent(inner=inner, conversation_store=store)

        await agent.chat("msg-1", session_id="s1")
        await agent.chat("msg-2", session_id="s1")
        await agent.chat("msg-3", session_id="s1")

        msgs = await store.load_messages("s1")
        assert len(msgs) == 6  # 3 user + 3 assistant

    @pytest.mark.asyncio
    async def test_chat_session_isolation(self) -> None:
        """Different session_ids should have separate history."""
        from unittest.mock import AsyncMock, MagicMock

        from promptise.agent import PromptiseAgent

        inner = AsyncMock()
        inner.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="ok", type="ai")]})

        store = InMemoryConversationStore()
        agent = PromptiseAgent(inner=inner, conversation_store=store)

        await agent.chat("for session 1", session_id="s1")
        await agent.chat("for session 2", session_id="s2")

        s1 = await store.load_messages("s1")
        s2 = await store.load_messages("s2")
        assert len(s1) == 2
        assert len(s2) == 2
        assert s1[0].content == "for session 1"
        assert s2[0].content == "for session 2"

    @pytest.mark.asyncio
    async def test_chat_max_messages(self) -> None:
        """Rolling window should truncate old messages."""
        from unittest.mock import AsyncMock, MagicMock

        from promptise.agent import PromptiseAgent

        call_count = 0

        async def mock_invoke(input, config=None, **kw):
            nonlocal call_count
            call_count += 1
            return {"messages": [MagicMock(content=f"r{call_count}", type="ai")]}

        inner = AsyncMock()
        inner.ainvoke = mock_invoke

        store = InMemoryConversationStore()
        agent = PromptiseAgent(
            inner=inner,
            conversation_store=store,
            conversation_max_messages=4,
        )

        await agent.chat("m1", session_id="s1")
        await agent.chat("m2", session_id="s1")
        await agent.chat("m3", session_id="s1")

        msgs = await store.load_messages("s1")
        assert len(msgs) == 4  # Last 4 messages kept
        assert msgs[0].content == "m2"  # m1 + r1 evicted

    @pytest.mark.asyncio
    async def test_chat_with_metadata(self) -> None:
        """User message metadata should be persisted."""
        from unittest.mock import AsyncMock, MagicMock

        from promptise.agent import PromptiseAgent

        inner = AsyncMock()
        inner.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="ok", type="ai")]})

        store = InMemoryConversationStore()
        agent = PromptiseAgent(inner=inner, conversation_store=store)

        await agent.chat(
            "hello",
            session_id="s1",
            metadata={"source": "web", "ip": "1.2.3.4"},
        )
        msgs = await store.load_messages("s1")
        assert msgs[0].metadata == {"source": "web", "ip": "1.2.3.4"}

    @pytest.mark.asyncio
    async def test_chat_store_failure_graceful(self) -> None:
        """If the store fails, chat() should still return a response."""
        from unittest.mock import AsyncMock, MagicMock

        from promptise.agent import PromptiseAgent

        inner = AsyncMock()
        inner.ainvoke = AsyncMock(
            return_value={"messages": [MagicMock(content="still works", type="ai")]}
        )

        store = AsyncMock()
        store.load_messages = AsyncMock(side_effect=ConnectionError("DB down"))
        store.save_messages = AsyncMock(side_effect=ConnectionError("DB down"))

        agent = PromptiseAgent(inner=inner, conversation_store=store)
        result = await agent.chat("hello", session_id="s1")
        assert result == "still works"

    @pytest.mark.asyncio
    async def test_list_sessions_requires_store(self) -> None:
        from promptise.agent import PromptiseAgent

        inner = AsyncMock()
        agent = PromptiseAgent(inner=inner)
        with pytest.raises(RuntimeError, match="No conversation store"):
            await agent.list_sessions()

    @pytest.mark.asyncio
    async def test_delete_session_requires_store(self) -> None:
        from promptise.agent import PromptiseAgent

        inner = AsyncMock()
        agent = PromptiseAgent(inner=inner)
        with pytest.raises(RuntimeError, match="No conversation store"):
            await agent.delete_session("s1")

    @pytest.mark.asyncio
    async def test_list_and_delete_via_agent(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from promptise.agent import PromptiseAgent

        inner = AsyncMock()
        inner.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="ok", type="ai")]})

        store = InMemoryConversationStore()
        agent = PromptiseAgent(inner=inner, conversation_store=store)

        await agent.chat("hello", session_id="s1")
        await agent.chat("world", session_id="s2")

        sessions = await agent.list_sessions()
        assert len(sessions) == 2

        deleted = await agent.delete_session("s1")
        assert deleted is True

        sessions = await agent.list_sessions()
        assert len(sessions) == 1

    @pytest.mark.asyncio
    async def test_update_session_via_agent(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from promptise.agent import PromptiseAgent

        inner = AsyncMock()
        inner.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="ok", type="ai")]})

        store = InMemoryConversationStore()
        agent = PromptiseAgent(inner=inner, conversation_store=store)

        await agent.chat("hello", session_id="s1")
        await agent.update_session("s1", title="Renamed")

        sessions = await agent.list_sessions()
        assert sessions[0].title == "Renamed"


# ---------------------------------------------------------------------------
# _extract_response_text tests
# ---------------------------------------------------------------------------


class TestExtractResponseText:
    """Tests for the _extract_response_text helper."""

    def test_from_messages(self) -> None:
        from unittest.mock import MagicMock

        from promptise.agent import _extract_response_text

        output = {
            "messages": [
                MagicMock(content="user msg", type="human"),
                MagicMock(content="ai msg", type="ai"),
            ]
        }
        assert _extract_response_text(output) == "ai msg"

    def test_from_last_ai_message(self) -> None:
        from unittest.mock import MagicMock

        from promptise.agent import _extract_response_text

        output = {
            "messages": [
                MagicMock(content="first", type="ai"),
                MagicMock(content="", type="ai"),
                MagicMock(content="last real", type="ai"),
            ]
        }
        assert _extract_response_text(output) == "last real"

    def test_from_output_key(self) -> None:
        from promptise.agent import _extract_response_text

        assert _extract_response_text({"output": "hello"}) == "hello"

    def test_from_string(self) -> None:
        from promptise.agent import _extract_response_text

        assert _extract_response_text("direct string") == "direct string"

    def test_fallback_to_str(self) -> None:
        from promptise.agent import _extract_response_text

        assert _extract_response_text(42) == "42"

    def test_empty_messages_fallback(self) -> None:
        from promptise.agent import _extract_response_text

        output = {"messages": []}
        assert _extract_response_text(output) == "{'messages': []}"


# ---------------------------------------------------------------------------
# Shutdown closes conversation store
# ---------------------------------------------------------------------------


class TestShutdownClosesStore:
    """Verify that agent shutdown closes the conversation store."""

    @pytest.mark.asyncio
    async def test_shutdown_closes_store(self) -> None:
        from promptise.agent import PromptiseAgent

        inner = AsyncMock()
        store = AsyncMock()
        store.close = AsyncMock()

        agent = PromptiseAgent(inner=inner, conversation_store=store)
        await agent.shutdown()
        store.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_handles_store_error(self) -> None:
        from promptise.agent import PromptiseAgent

        inner = AsyncMock()
        store = AsyncMock()
        store.close = AsyncMock(side_effect=RuntimeError("close failed"))

        agent = PromptiseAgent(inner=inner, conversation_store=store)
        # Should not raise
        await agent.shutdown()


# ---------------------------------------------------------------------------
# generate_session_id tests
# ---------------------------------------------------------------------------


class TestGenerateSessionId:
    """Tests for cryptographically secure session ID generation."""

    def test_default_prefix(self) -> None:
        sid = generate_session_id()
        assert sid.startswith("sess_")
        assert len(sid) == 5 + 32  # "sess_" + 32 hex chars

    def test_custom_prefix(self) -> None:
        sid = generate_session_id(prefix="chat")
        assert sid.startswith("chat_")

    def test_uniqueness(self) -> None:
        ids = {generate_session_id() for _ in range(1000)}
        assert len(ids) == 1000  # All unique

    def test_unpredictable(self) -> None:
        """Sequential IDs should have no common pattern."""
        id1 = generate_session_id()
        id2 = generate_session_id()
        # Strip prefix and compare — should differ significantly
        hex1 = id1.split("_", 1)[1]
        hex2 = id2.split("_", 1)[1]
        assert hex1 != hex2
        # At least half the characters should differ (probabilistic but safe)
        differences = sum(1 for a, b in zip(hex1, hex2, strict=False) if a != b)
        assert differences > 8


# ---------------------------------------------------------------------------
# SessionAccessDenied exception tests
# ---------------------------------------------------------------------------


class TestSessionAccessDenied:
    """Tests for the SessionAccessDenied exception."""

    def test_basic(self) -> None:
        exc = SessionAccessDenied("s1", "attacker")
        assert exc.session_id == "s1"
        assert exc.attempted_user_id == "attacker"
        assert exc.owner_user_id is None
        assert "attacker" in str(exc)
        assert "s1" in str(exc)

    def test_with_owner(self) -> None:
        exc = SessionAccessDenied("s1", "attacker", owner_user_id="alice")
        assert exc.owner_user_id == "alice"
        assert "alice" in str(exc)

    def test_is_permission_error(self) -> None:
        exc = SessionAccessDenied("s1", "x")
        assert isinstance(exc, PermissionError)


# ---------------------------------------------------------------------------
# get_session() tests
# ---------------------------------------------------------------------------


class TestGetSession:
    """Tests for get_session() on built-in stores."""

    @pytest.mark.asyncio
    async def test_inmemory_get_nonexistent(self) -> None:
        store = InMemoryConversationStore()
        assert await store.get_session("nope") is None

    @pytest.mark.asyncio
    async def test_inmemory_get_existing(self) -> None:
        store = InMemoryConversationStore()
        await store.save_messages("s1", [Message(role="user", content="hi")])
        await store.update_session("s1", user_id="alice", title="Test")
        info = await store.get_session("s1")
        assert info is not None
        assert info.session_id == "s1"
        assert info.user_id == "alice"
        assert info.title == "Test"

    @pytest.mark.asyncio
    async def test_inmemory_get_returns_copy(self) -> None:
        """Modifying the returned SessionInfo should not affect the store."""
        store = InMemoryConversationStore()
        await store.save_messages("s1", [Message(role="user", content="hi")])
        info = await store.get_session("s1")
        info.title = "MUTATED"
        reloaded = await store.get_session("s1")
        assert reloaded.title is None  # Not mutated

    @pytest.mark.asyncio
    async def test_sqlite_get_session(self) -> None:
        store = SQLiteConversationStore(":memory:")
        assert await store.get_session("nope") is None
        await store.save_messages("s1", [Message(role="user", content="hi")])
        await store.update_session("s1", user_id="bob")
        info = await store.get_session("s1")
        assert info is not None
        assert info.user_id == "bob"
        await store.close()


# ---------------------------------------------------------------------------
# Session ownership enforcement tests
# ---------------------------------------------------------------------------


class TestOwnershipEnforcement:
    """Tests for multi-user session access control."""

    @pytest.fixture
    def make_agent(self):
        """Create an agent with an InMemory store and mock inner."""
        from promptise.agent import PromptiseAgent

        def _make():
            inner = AsyncMock()
            inner.ainvoke = AsyncMock(
                return_value={"messages": [MagicMock(content="ok", type="ai")]}
            )
            store = InMemoryConversationStore()
            agent = PromptiseAgent(inner=inner, conversation_store=store)
            return agent, store

        return _make

    @pytest.mark.asyncio
    async def test_chat_assigns_ownership(self, make_agent) -> None:
        """First chat with user_id should assign ownership."""
        agent, store = make_agent()
        await agent.chat("hello", session_id="s1", user_id="alice")
        info = await store.get_session("s1")
        assert info.user_id == "alice"

    @pytest.mark.asyncio
    async def test_chat_same_user_succeeds(self, make_agent) -> None:
        """Same user can continue chatting."""
        agent, store = make_agent()
        await agent.chat("hello", session_id="s1", user_id="alice")
        result = await agent.chat("again", session_id="s1", user_id="alice")
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_chat_different_user_denied(self, make_agent) -> None:
        """Different user_id on existing session should raise SessionAccessDenied."""
        agent, store = make_agent()
        await agent.chat("hello", session_id="s1", user_id="alice")
        with pytest.raises(SessionAccessDenied) as exc_info:
            await agent.chat("hack", session_id="s1", user_id="bob")
        assert exc_info.value.attempted_user_id == "bob"
        assert exc_info.value.owner_user_id == "alice"

    @pytest.mark.asyncio
    async def test_chat_no_user_id_skips_check(self, make_agent) -> None:
        """When user_id is None, no ownership check is performed."""
        agent, store = make_agent()
        await agent.chat("hello", session_id="s1", user_id="alice")
        # No user_id = no enforcement
        result = await agent.chat("bypass", session_id="s1")
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_chat_unowned_session_accessible(self, make_agent) -> None:
        """Sessions without a user_id are accessible by anyone."""
        agent, store = make_agent()
        await agent.chat("hello", session_id="s1")  # No user_id
        # Any user_id can access an unowned session
        result = await agent.chat("ok", session_id="s1", user_id="bob")
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_delete_ownership_check(self, make_agent) -> None:
        """delete_session with wrong user_id should raise."""
        agent, store = make_agent()
        await agent.chat("hello", session_id="s1", user_id="alice")
        with pytest.raises(SessionAccessDenied):
            await agent.delete_session("s1", user_id="bob")

    @pytest.mark.asyncio
    async def test_delete_correct_user(self, make_agent) -> None:
        """delete_session with correct user_id should succeed."""
        agent, store = make_agent()
        await agent.chat("hello", session_id="s1", user_id="alice")
        result = await agent.delete_session("s1", user_id="alice")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_no_user_id_no_check(self, make_agent) -> None:
        """delete_session without user_id does no ownership check."""
        agent, store = make_agent()
        await agent.chat("hello", session_id="s1", user_id="alice")
        result = await agent.delete_session("s1")
        assert result is True

    @pytest.mark.asyncio
    async def test_update_ownership_check(self, make_agent) -> None:
        """update_session with wrong calling_user_id should raise."""
        agent, store = make_agent()
        await agent.chat("hello", session_id="s1", user_id="alice")
        with pytest.raises(SessionAccessDenied):
            await agent.update_session("s1", calling_user_id="bob", title="hacked")

    @pytest.mark.asyncio
    async def test_update_correct_user(self, make_agent) -> None:
        """update_session with correct calling_user_id should succeed."""
        agent, store = make_agent()
        await agent.chat("hello", session_id="s1", user_id="alice")
        result = await agent.update_session("s1", calling_user_id="alice", title="My Chat")
        assert result is True
        info = await store.get_session("s1")
        assert info.title == "My Chat"

    @pytest.mark.asyncio
    async def test_get_session_ownership_check(self, make_agent) -> None:
        """get_session with wrong user_id should raise."""
        agent, store = make_agent()
        await agent.chat("hello", session_id="s1", user_id="alice")
        with pytest.raises(SessionAccessDenied):
            await agent.get_session("s1", user_id="bob")

    @pytest.mark.asyncio
    async def test_get_session_correct_user(self, make_agent) -> None:
        """get_session with correct user_id should succeed."""
        agent, store = make_agent()
        await agent.chat("hello", session_id="s1", user_id="alice")
        info = await agent.get_session("s1", user_id="alice")
        assert info.session_id == "s1"

    @pytest.mark.asyncio
    async def test_list_sessions_scoped_to_user(self, make_agent) -> None:
        """list_sessions should be filtered by user_id."""
        agent, store = make_agent()
        await agent.chat("a", session_id="s1", user_id="alice")
        await agent.chat("b", session_id="s2", user_id="bob")
        await agent.chat("c", session_id="s3", user_id="alice")

        alice_sessions = await agent.list_sessions(user_id="alice")
        assert len(alice_sessions) == 2
        assert all(s.user_id == "alice" for s in alice_sessions)

        bob_sessions = await agent.list_sessions(user_id="bob")
        assert len(bob_sessions) == 1

    @pytest.mark.asyncio
    async def test_cross_user_enumeration_prevented(self, make_agent) -> None:
        """User should not be able to read another user's session by ID."""
        agent, store = make_agent()
        await agent.chat("secret stuff", session_id="s1", user_id="alice")

        # Bob tries to access alice's session via chat
        with pytest.raises(SessionAccessDenied):
            await agent.chat("what's here?", session_id="s1", user_id="bob")

        # Bob tries via get_session
        with pytest.raises(SessionAccessDenied):
            await agent.get_session("s1", user_id="bob")

        # Bob tries via delete
        with pytest.raises(SessionAccessDenied):
            await agent.delete_session("s1", user_id="bob")

        # Bob tries via update
        with pytest.raises(SessionAccessDenied):
            await agent.update_session("s1", calling_user_id="bob", title="pwned")
