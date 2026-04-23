"""Conversation persistence — session management and chat history storage.

Provides a :class:`ConversationStore` protocol and built-in implementations
for persisting conversation history across sessions.  The agent's
:meth:`~promptise.agent.PromptiseAgent.chat` method uses the store
automatically — load history, append the new exchange, persist.

Built-in stores:

* :class:`InMemoryConversationStore` — dict-backed, no persistence.  Testing only.
* :class:`PostgresConversationStore` — ``asyncpg`` backed.  Production.
* :class:`SQLiteConversationStore` — ``aiosqlite`` backed.  Local dev / single-node.
* :class:`RedisConversationStore` — ``redis.asyncio`` backed.  Ephemeral / caching.

Example — PostgreSQL persistence::

    from promptise import build_agent
    from promptise.conversations import PostgresConversationStore

    store = PostgresConversationStore("postgresql://user:pass@localhost/myapp")
    agent = await build_agent(
        servers={...},
        model="openai:gpt-5-mini",
        conversation_store=store,
    )
    response = await agent.chat("Hello!", session_id="conv-abc")
    response = await agent.chat("What did I say?", session_id="conv-abc")

Example — custom store::

    from promptise.conversations import ConversationStore, SessionInfo, Message

    class MyStore:
        async def load_messages(self, session_id: str) -> list[Message]: ...
        async def save_messages(self, session_id: str, messages: list[Message]) -> None: ...
        async def delete_session(self, session_id: str) -> bool: ...
        async def list_sessions(self, *, user_id=None, limit=50, offset=0) -> list[SessionInfo]: ...
        async def close(self) -> None: ...
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("promptise.conversations")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionAccessDenied(PermissionError):
    """Raised when a user attempts to access a session they do not own.

    Attributes:
        session_id: The session that was accessed.
        attempted_user_id: The user who tried to access it.
        owner_user_id: The actual owner (if known).
    """

    def __init__(
        self,
        session_id: str,
        attempted_user_id: str,
        owner_user_id: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.attempted_user_id = attempted_user_id
        self.owner_user_id = owner_user_id
        owner_info = f" (owned by {owner_user_id!r})" if owner_user_id else ""
        super().__init__(
            f"User {attempted_user_id!r} denied access to session {session_id!r}{owner_info}"
        )


# ---------------------------------------------------------------------------
# Session ID generation
# ---------------------------------------------------------------------------


def generate_session_id(prefix: str = "sess") -> str:
    """Generate a cryptographically secure session identifier.

    Returns a 32-character hex token prefixed with the given string,
    e.g. ``"sess_a1b2c3d4e5f6..."``.  Unpredictable and non-enumerable.

    Args:
        prefix: Short prefix for readability.  Defaults to ``"sess"``.

    Returns:
        A unique session ID like ``"sess_a1b2c3d4e5f67890abcdef1234567890"``.
    """
    return f"{prefix}_{secrets.token_hex(16)}"


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """A single conversation message.

    Attributes:
        role: The message role (``"user"``, ``"assistant"``, ``"system"``, ``"tool"``).
        content: The message text.
        metadata: Optional metadata (tool calls, token counts, latency, etc.).
        created_at: UTC timestamp.  Defaults to now.
    """

    _VALID_ROLES = {"user", "assistant", "system", "tool"}

    role: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.role not in self._VALID_ROLES:
            raise ValueError(
                f"Invalid message role {self.role!r}. Must be one of: {', '.join(sorted(self._VALID_ROLES))}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "role": self.role,
            "content": self.content,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        """Deserialize from a plain dict."""
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = datetime.now(timezone.utc)
        return cls(
            role=data["role"],
            content=data["content"],
            metadata=data.get("metadata", {}),
            created_at=created_at,
        )


@dataclass
class SessionInfo:
    """Metadata about a conversation session.

    Attributes:
        session_id: Unique session identifier.
        user_id: Optional user identifier for multi-user applications.
        title: Optional human-readable title.
        message_count: Number of messages in the session.
        created_at: When the session was first created.
        updated_at: When the session was last updated.
        metadata: Application-specific metadata (tags, source, etc.).
        expires_at: Optional expiry time for data-retention enforcement.
            ``None`` means the session never expires automatically.
    """

    session_id: str
    user_id: str | None = None
    title: str | None = None
    message_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# ConversationStore protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ConversationStore(Protocol):
    """Interface for conversation persistence backends.

    All methods are async.  Implementations must handle their own
    thread-safety and connection management.  Call :meth:`close` when
    the store is no longer needed.

    **Security note:** Stores do NOT enforce session ownership internally.
    Ownership checks happen at the ``PromptiseAgent`` layer via
    ``_enforce_ownership()`` before calling store methods. If you use stores
    directly (outside of ``build_agent()``), you MUST verify session ownership
    yourself by calling ``get_session()`` and checking ``user_id`` before
    ``load_messages()`` or ``delete_session()``.

    Implementing a custom store requires four data methods plus
    :meth:`close`::

        class MyStore:
            async def load_messages(self, session_id: str) -> list[Message]: ...
            async def save_messages(self, session_id: str, messages: list[Message]) -> None: ...
            async def delete_session(self, session_id: str) -> bool: ...
            async def list_sessions(self, *, user_id=None, limit=50, offset=0) -> list[SessionInfo]: ...
            async def close(self) -> None: ...
    """

    async def get_session(self, session_id: str) -> SessionInfo | None:
        """Return session metadata, or ``None`` if the session does not exist.

        Used for ownership checks before loading messages.
        """
        ...

    async def load_messages(self, session_id: str) -> list[Message]:
        """Load all messages for a session, ordered by creation time.

        Returns an empty list if the session does not exist.
        """
        ...

    async def save_messages(self, session_id: str, messages: list[Message]) -> None:
        """Persist the full message list for a session.

        Creates the session if it does not exist.  Replaces all messages
        if the session already exists.
        """
        ...

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages.

        Returns ``True`` if the session existed and was deleted.
        """
        ...

    async def list_sessions(
        self,
        *,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionInfo]:
        """List sessions, optionally filtered by user.

        Returns sessions ordered by ``updated_at`` descending (most
        recent first).
        """
        ...

    async def close(self) -> None:
        """Release resources (connections, file handles)."""
        ...


# ---------------------------------------------------------------------------
# InMemoryConversationStore (testing / development)
# ---------------------------------------------------------------------------


class InMemoryConversationStore:
    """Dict-backed conversation store for testing and prototyping.

    No persistence.  All data lives in-process and is lost when the
    process exits.

    Args:
        max_sessions: Maximum sessions to keep.  Oldest evicted first.
            ``0`` means unlimited.
        max_messages_per_session: Maximum messages per session.  Oldest
            messages are dropped when the limit is reached.  ``0`` means
            unlimited.
    """

    def __init__(
        self,
        *,
        max_sessions: int = 0,
        max_messages_per_session: int = 0,
        session_ttl: int | None = None,
    ) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._messages: dict[str, list[Message]] = {}
        self._max_sessions = max_sessions
        self._max_messages = max_messages_per_session
        self._session_ttl = session_ttl  # seconds; None = never expire
        self._lock = asyncio.Lock()

    def _is_session_expired(self, info: SessionInfo) -> bool:
        """Check if a session has exceeded its TTL."""
        if info.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= info.expires_at

    async def cleanup_expired(self) -> int:
        """Remove all expired sessions.  Returns the count of sessions removed.

        Call periodically for data-retention enforcement.
        """
        async with self._lock:
            expired = [
                sid for sid, info in self._sessions.items() if self._is_session_expired(info)
            ]
            for sid in expired:
                del self._sessions[sid]
                self._messages.pop(sid, None)
            return len(expired)

    async def get_session(self, session_id: str) -> SessionInfo | None:
        """Return session metadata or ``None``."""
        async with self._lock:
            info = self._sessions.get(session_id)
            return (
                SessionInfo(
                    session_id=info.session_id,
                    user_id=info.user_id,
                    title=info.title,
                    message_count=info.message_count,
                    created_at=info.created_at,
                    updated_at=info.updated_at,
                    metadata=dict(info.metadata),
                )
                if info
                else None
            )

    async def load_messages(self, session_id: str) -> list[Message]:
        """Load all messages for a session.  Returns empty for expired sessions."""
        async with self._lock:
            info = self._sessions.get(session_id)
            if info is not None and self._is_session_expired(info):
                del self._sessions[session_id]
                self._messages.pop(session_id, None)
                return []
            return list(self._messages.get(session_id, []))

    async def save_messages(self, session_id: str, messages: list[Message]) -> None:
        """Persist messages, creating the session if needed."""
        async with self._lock:
            # Enforce per-session message limit
            if self._max_messages > 0 and len(messages) > self._max_messages:
                messages = messages[-self._max_messages :]

            now = datetime.now(timezone.utc)
            if session_id not in self._sessions:
                # Evict oldest session if at capacity
                if self._max_sessions > 0 and len(self._sessions) >= self._max_sessions:
                    oldest_id = min(
                        self._sessions,
                        key=lambda k: self._sessions[k].updated_at,
                    )
                    del self._sessions[oldest_id]
                    self._messages.pop(oldest_id, None)

                expires_at = (
                    now + timedelta(seconds=self._session_ttl)
                    if self._session_ttl is not None
                    else None
                )
                self._sessions[session_id] = SessionInfo(
                    session_id=session_id,
                    created_at=now,
                    updated_at=now,
                    message_count=len(messages),
                    expires_at=expires_at,
                )
            else:
                self._sessions[session_id].updated_at = now
                self._sessions[session_id].message_count = len(messages)

            self._messages[session_id] = list(messages)

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session and its messages."""
        async with self._lock:
            existed = session_id in self._sessions
            self._sessions.pop(session_id, None)
            self._messages.pop(session_id, None)
            return existed

    async def list_sessions(
        self,
        *,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionInfo]:
        """List sessions ordered by most recently updated.  Expired sessions excluded."""
        async with self._lock:
            # Lazy cleanup of expired sessions
            expired = [
                sid for sid, info in self._sessions.items() if self._is_session_expired(info)
            ]
            for sid in expired:
                del self._sessions[sid]
                self._messages.pop(sid, None)

            sessions = list(self._sessions.values())
            if user_id is not None:
                sessions = [s for s in sessions if s.user_id == user_id]
            sessions.sort(key=lambda s: s.updated_at, reverse=True)
            return sessions[offset : offset + limit]

    async def close(self) -> None:
        """No-op for in-memory store."""

    def session_count(self) -> int:
        """Return the number of active sessions."""
        return len(self._sessions)

    async def update_session(
        self,
        session_id: str,
        *,
        user_id: str | None = ...,  # type: ignore[assignment]
        title: str | None = ...,  # type: ignore[assignment]
        metadata: dict[str, Any] | None = ...,  # type: ignore[assignment]
    ) -> bool:
        """Update session metadata.

        Only provided fields are updated.  Returns ``True`` if the
        session existed.
        """
        async with self._lock:
            info = self._sessions.get(session_id)
            if info is None:
                return False
            if user_id is not ...:
                info.user_id = user_id
            if title is not ...:
                info.title = title
            if metadata is not ...:
                info.metadata = metadata or {}
            info.updated_at = datetime.now(timezone.utc)
            return True


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SAFE_PREFIX_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_table_prefix(prefix: str) -> str:
    """Validate that a table prefix is safe for SQL interpolation.

    Only alphanumeric characters and underscores are allowed.  The prefix
    must start with a letter or underscore.

    Raises:
        ValueError: If the prefix contains unsafe characters.
    """
    if not prefix:
        return ""
    if not _SAFE_PREFIX_RE.match(prefix):
        raise ValueError(
            f"Unsafe table_prefix: {prefix!r}. "
            f"Only alphanumeric characters and underscores allowed, "
            f"must start with a letter or underscore."
        )
    return prefix


# ---------------------------------------------------------------------------
# PostgresConversationStore
# ---------------------------------------------------------------------------

# SQL statements — defined as constants for clarity and reuse.
_PG_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS {prefix}sessions (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT,
    title        TEXT,
    metadata     JSONB NOT NULL DEFAULT '{}',
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_PG_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS {prefix}messages (
    id           BIGSERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES {prefix}sessions(session_id) ON DELETE CASCADE,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{{}}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_PG_CREATE_IDX_MESSAGES = """
CREATE INDEX IF NOT EXISTS idx_{prefix}messages_session
    ON {prefix}messages(session_id, created_at)
"""

_PG_CREATE_IDX_SESSIONS_USER = """
CREATE INDEX IF NOT EXISTS idx_{prefix}sessions_user
    ON {prefix}sessions(user_id, updated_at DESC)
"""


class PostgresConversationStore:
    """PostgreSQL-backed conversation store using ``asyncpg``.

    Auto-creates tables and indexes on first use.  Requires
    ``pip install asyncpg``.

    Args:
        dsn: PostgreSQL connection string
            (e.g. ``"postgresql://user:pass@localhost/mydb"``).
        table_prefix: Prefix for table names.  Defaults to ``"promptise_"``
            so multiple applications can share one database.
        pool_min: Minimum connection pool size.
        pool_max: Maximum connection pool size.
        max_messages_per_session: Rolling window limit.  ``0`` = unlimited.
    """

    def __init__(
        self,
        dsn: str,
        *,
        table_prefix: str = "promptise_",
        pool_min: int = 2,
        pool_max: int = 10,
        max_messages_per_session: int = 0,
    ) -> None:
        self._dsn = dsn
        self._prefix = _validate_table_prefix(table_prefix)
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._max_messages = max_messages_per_session
        self._pool: Any = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure_pool(self) -> Any:
        """Lazily create the connection pool and run migrations."""
        if self._initialized:
            return self._pool

        async with self._init_lock:
            if self._initialized:
                return self._pool

            try:
                import asyncpg
            except ImportError as exc:
                raise ImportError(
                    "PostgresConversationStore requires asyncpg. "
                    "Install it with: pip install asyncpg"
                ) from exc

            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=self._pool_min,
                max_size=self._pool_max,
            )
            await self._migrate()
            self._initialized = True
            return self._pool

    async def _migrate(self) -> None:
        """Create tables and indexes if they don't exist."""
        p = self._prefix
        async with self._pool.acquire() as conn:
            await conn.execute(_PG_CREATE_SESSIONS.format(prefix=p))
            await conn.execute(_PG_CREATE_MESSAGES.format(prefix=p))
            await conn.execute(_PG_CREATE_IDX_MESSAGES.format(prefix=p))
            await conn.execute(_PG_CREATE_IDX_SESSIONS_USER.format(prefix=p))

    async def get_session(self, session_id: str) -> SessionInfo | None:
        """Return session metadata or ``None``."""
        pool = await self._ensure_pool()
        p = self._prefix
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT session_id, user_id, title, metadata, message_count, "  # nosec B608 - table prefix validated in __init__; user data parameterized
                f"created_at, updated_at FROM {p}sessions WHERE session_id = $1",
                session_id,
            )
            if row is None:
                return None
            return SessionInfo(
                session_id=row["session_id"],
                user_id=row["user_id"],
                title=row["title"],
                message_count=row["message_count"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                metadata=_pg_json_loads(row["metadata"]),
            )

    async def load_messages(self, session_id: str) -> list[Message]:
        """Load all messages for a session."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT role, content, metadata, created_at "  # nosec B608 - table prefix validated in __init__; user data parameterized
                f"FROM {self._prefix}messages "
                f"WHERE session_id = $1 ORDER BY created_at, id",
                session_id,
            )
            return [
                Message(
                    role=row["role"],
                    content=row["content"],
                    metadata=_pg_json_loads(row["metadata"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def save_messages(self, session_id: str, messages: list[Message]) -> None:
        """Persist messages, creating the session if needed."""
        if self._max_messages > 0 and len(messages) > self._max_messages:
            messages = messages[-self._max_messages :]

        pool = await self._ensure_pool()
        p = self._prefix

        async with pool.acquire() as conn:
            async with conn.transaction():
                # Upsert session
                await conn.execute(
                    f"INSERT INTO {p}sessions (session_id, message_count, updated_at) "  # nosec B608 - table prefix validated in __init__; user data parameterized
                    f"VALUES ($1, $2, now()) "
                    f"ON CONFLICT (session_id) DO UPDATE SET "
                    f"  message_count = $2, updated_at = now()",
                    session_id,
                    len(messages),
                )

                # Replace messages: delete existing and bulk insert
                await conn.execute(
                    f"DELETE FROM {p}messages WHERE session_id = $1",  # nosec B608 - table prefix validated in __init__; user data parameterized
                    session_id,
                )

                if messages:
                    import json

                    await conn.executemany(
                        f"INSERT INTO {p}messages "  # nosec B608 - table prefix validated in __init__; user data parameterized
                        f"(session_id, role, content, metadata, created_at) "
                        f"VALUES ($1, $2, $3, $4::jsonb, $5)",
                        [
                            (
                                session_id,
                                m.role,
                                m.content,
                                json.dumps(m.metadata),
                                m.created_at,
                            )
                            for m in messages
                        ],
                    )

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages (cascade)."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"DELETE FROM {self._prefix}sessions WHERE session_id = $1",  # nosec B608 - table prefix validated in __init__; user data parameterized
                session_id,
            )
            return result == "DELETE 1"

    async def list_sessions(
        self,
        *,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionInfo]:
        """List sessions ordered by most recently updated."""
        pool = await self._ensure_pool()
        p = self._prefix

        args: tuple[Any, ...]
        if user_id is not None:
            query = (
                f"SELECT session_id, user_id, title, metadata, message_count, "  # nosec B608 - table prefix validated in __init__; user data parameterized
                f"created_at, updated_at FROM {p}sessions "
                f"WHERE user_id = $1 ORDER BY updated_at DESC LIMIT $2 OFFSET $3"
            )
            args = (user_id, limit, offset)
        else:
            query = (
                f"SELECT session_id, user_id, title, metadata, message_count, "  # nosec B608 - table prefix validated in __init__; user data parameterized
                f"created_at, updated_at FROM {p}sessions "
                f"ORDER BY updated_at DESC LIMIT $1 OFFSET $2"
            )
            args = (limit, offset)

        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return [
                SessionInfo(
                    session_id=row["session_id"],
                    user_id=row["user_id"],
                    title=row["title"],
                    message_count=row["message_count"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    metadata=_pg_json_loads(row["metadata"]),
                )
                for row in rows
            ]

    async def update_session(
        self,
        session_id: str,
        *,
        user_id: str | None = ...,  # type: ignore[assignment]
        title: str | None = ...,  # type: ignore[assignment]
        metadata: dict[str, Any] | None = ...,  # type: ignore[assignment]
    ) -> bool:
        """Update session metadata fields.  Only provided fields are changed."""
        pool = await self._ensure_pool()
        sets: list[str] = ["updated_at = now()"]
        args: list[Any] = []
        idx = 1

        if user_id is not ...:
            idx += 1
            sets.append(f"user_id = ${idx}")
            args.append(user_id)
        if title is not ...:
            idx += 1
            sets.append(f"title = ${idx}")
            args.append(title)
        if metadata is not ...:
            import json

            idx += 1
            sets.append(f"metadata = ${idx}::jsonb")
            args.append(json.dumps(metadata or {}))

        if not args:
            return False

        query = f"UPDATE {self._prefix}sessions SET {', '.join(sets)} WHERE session_id = $1"  # nosec B608 - table prefix validated in __init__; SET clause built from allowlist; user data parameterized
        async with pool.acquire() as conn:
            result = await conn.execute(query, session_id, *args)
            return result == "UPDATE 1"

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._initialized = False


# ---------------------------------------------------------------------------
# SQLiteConversationStore
# ---------------------------------------------------------------------------


class SQLiteConversationStore:
    """SQLite-backed conversation store using ``aiosqlite``.

    Auto-creates tables on first use.  Requires
    ``pip install aiosqlite``.

    Args:
        path: Path to the SQLite database file.  Use ``":memory:"`` for
            in-memory storage with full SQL semantics.
        table_prefix: Prefix for table names.
        max_messages_per_session: Rolling window limit.  ``0`` = unlimited.
    """

    def __init__(
        self,
        path: str = "conversations.db",
        *,
        table_prefix: str = "promptise_",
        max_messages_per_session: int = 0,
    ) -> None:
        self._path = path
        self._prefix = _validate_table_prefix(table_prefix)
        self._max_messages = max_messages_per_session
        self._db: Any = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure_db(self) -> Any:
        """Lazily open the database and run migrations."""
        if self._initialized:
            return self._db

        async with self._init_lock:
            if self._initialized:
                return self._db

            try:
                import aiosqlite
            except ImportError as exc:
                raise ImportError(
                    "SQLiteConversationStore requires aiosqlite. "
                    "Install it with: pip install aiosqlite"
                ) from exc

            self._db = await aiosqlite.connect(self._path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA foreign_keys=ON")
            await self._migrate()
            self._initialized = True
            return self._db

    async def _migrate(self) -> None:
        """Create tables and indexes."""
        p = self._prefix
        await self._db.execute(
            f"CREATE TABLE IF NOT EXISTS {p}sessions ("
            f"  session_id TEXT PRIMARY KEY,"
            f"  user_id TEXT,"
            f"  title TEXT,"
            f"  metadata TEXT NOT NULL DEFAULT '{{}}',"
            f"  message_count INTEGER NOT NULL DEFAULT 0,"
            f"  created_at TEXT NOT NULL,"
            f"  updated_at TEXT NOT NULL"
            f")"
        )
        await self._db.execute(
            f"CREATE TABLE IF NOT EXISTS {p}messages ("
            f"  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            f"  session_id TEXT NOT NULL REFERENCES {p}sessions(session_id) ON DELETE CASCADE,"
            f"  role TEXT NOT NULL,"
            f"  content TEXT NOT NULL,"
            f"  metadata TEXT NOT NULL DEFAULT '{{}}',"
            f"  created_at TEXT NOT NULL"
            f")"
        )
        await self._db.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{p}messages_session "
            f"ON {p}messages(session_id, created_at)"
        )
        await self._db.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{p}sessions_user "
            f"ON {p}sessions(user_id, updated_at DESC)"
        )
        await self._db.commit()

    async def get_session(self, session_id: str) -> SessionInfo | None:
        """Return session metadata or ``None``."""
        import json

        db = await self._ensure_db()
        p = self._prefix
        cursor = await db.execute(
            f"SELECT session_id, user_id, title, metadata, message_count, "  # nosec B608 - table prefix validated in __init__; user data parameterized
            f"created_at, updated_at FROM {p}sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return SessionInfo(
            session_id=row["session_id"],
            user_id=row["user_id"],
            title=row["title"],
            message_count=row["message_count"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )

    async def load_messages(self, session_id: str) -> list[Message]:
        """Load all messages for a session."""
        import json

        db = await self._ensure_db()
        cursor = await db.execute(
            f"SELECT role, content, metadata, created_at "  # nosec B608 - table prefix validated in __init__; user data parameterized
            f"FROM {self._prefix}messages "
            f"WHERE session_id = ? ORDER BY created_at, id",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [
            Message(
                role=row["role"],
                content=row["content"],
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    async def save_messages(self, session_id: str, messages: list[Message]) -> None:
        """Persist messages, creating the session if needed."""
        import json

        if self._max_messages > 0 and len(messages) > self._max_messages:
            messages = messages[-self._max_messages :]

        db = await self._ensure_db()
        p = self._prefix
        now = datetime.now(timezone.utc).isoformat()

        # Upsert session
        await db.execute(
            f"INSERT INTO {p}sessions (session_id, message_count, created_at, updated_at) "  # nosec B608 - table prefix validated in __init__; user data parameterized
            f"VALUES (?, ?, ?, ?) "
            f"ON CONFLICT(session_id) DO UPDATE SET "
            f"  message_count = ?, updated_at = ?",
            (session_id, len(messages), now, now, len(messages), now),
        )

        # Replace messages
        await db.execute(
            f"DELETE FROM {p}messages WHERE session_id = ?",  # nosec B608 - table prefix validated in __init__; user data parameterized
            (session_id,),
        )

        if messages:
            await db.executemany(
                f"INSERT INTO {p}messages "  # nosec B608 - table prefix validated in __init__; user data parameterized
                f"(session_id, role, content, metadata, created_at) "
                f"VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        session_id,
                        m.role,
                        m.content,
                        json.dumps(m.metadata),
                        m.created_at.isoformat(),
                    )
                    for m in messages
                ],
            )

        await db.commit()

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages."""
        db = await self._ensure_db()
        p = self._prefix
        # Messages cascade via FK
        cursor = await db.execute(
            f"DELETE FROM {p}sessions WHERE session_id = ?",  # nosec B608 - table prefix validated in __init__; user data parameterized
            (session_id,),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def list_sessions(
        self,
        *,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionInfo]:
        """List sessions ordered by most recently updated."""
        import json

        db = await self._ensure_db()
        p = self._prefix

        if user_id is not None:
            cursor = await db.execute(
                f"SELECT session_id, user_id, title, metadata, message_count, "  # nosec B608 - table prefix validated in __init__; user data parameterized
                f"created_at, updated_at FROM {p}sessions "
                f"WHERE user_id = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            )
        else:
            cursor = await db.execute(
                f"SELECT session_id, user_id, title, metadata, message_count, "  # nosec B608 - table prefix validated in __init__; user data parameterized
                f"created_at, updated_at FROM {p}sessions "
                f"ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )

        rows = await cursor.fetchall()
        return [
            SessionInfo(
                session_id=row["session_id"],
                user_id=row["user_id"],
                title=row["title"],
                message_count=row["message_count"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            )
            for row in rows
        ]

    async def update_session(
        self,
        session_id: str,
        *,
        user_id: str | None = ...,  # type: ignore[assignment]
        title: str | None = ...,  # type: ignore[assignment]
        metadata: dict[str, Any] | None = ...,  # type: ignore[assignment]
    ) -> bool:
        """Update session metadata fields."""
        import json

        db = await self._ensure_db()
        sets: list[str] = ["updated_at = ?"]
        args: list[Any] = [datetime.now(timezone.utc).isoformat()]

        if user_id is not ...:
            sets.append("user_id = ?")
            args.append(user_id)
        if title is not ...:
            sets.append("title = ?")
            args.append(title)
        if metadata is not ...:
            sets.append("metadata = ?")
            args.append(json.dumps(metadata or {}))

        if len(args) == 1:
            return False

        args.append(session_id)
        cursor = await db.execute(
            f"UPDATE {self._prefix}sessions SET {', '.join(sets)} WHERE session_id = ?",  # nosec B608 - table prefix validated in __init__; SET clause built from allowlist; user data parameterized
            args,
        )
        await db.commit()
        return cursor.rowcount > 0

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None
            self._initialized = False


# ---------------------------------------------------------------------------
# RedisConversationStore
# ---------------------------------------------------------------------------


class RedisConversationStore:
    """Redis-backed conversation store using ``redis.asyncio``.

    Stores sessions and messages as JSON in Redis hashes and sorted sets.
    Ideal for ephemeral sessions, caching, or high-throughput scenarios.

    Requires ``pip install redis``.

    Args:
        url: Redis connection URL (e.g. ``"redis://localhost:6379"``).
        key_prefix: Prefix for all Redis keys.
        ttl: Default TTL in seconds for session data.  ``0`` = no expiry.
        max_messages_per_session: Rolling window limit.  ``0`` = unlimited.
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379",
        *,
        key_prefix: str = "promptise:",
        ttl: int = 0,
        max_messages_per_session: int = 0,
    ) -> None:
        self._url = url
        self._prefix = key_prefix
        self._ttl = ttl
        self._max_messages = max_messages_per_session
        self._redis: Any = None
        self._init_lock = asyncio.Lock()

    def _session_key(self, session_id: str) -> str:
        return f"{self._prefix}session:{session_id}"

    def _messages_key(self, session_id: str) -> str:
        return f"{self._prefix}messages:{session_id}"

    def _index_key(self, user_id: str | None = None) -> str:
        if user_id:
            return f"{self._prefix}user_sessions:{user_id}"
        return f"{self._prefix}all_sessions"

    async def _ensure_redis(self) -> Any:
        """Lazily connect to Redis."""
        if self._redis is not None:
            return self._redis

        async with self._init_lock:
            if self._redis is not None:
                return self._redis

            try:
                import redis.asyncio as aioredis
            except ImportError as exc:
                raise ImportError(
                    "RedisConversationStore requires redis. Install it with: pip install redis"
                ) from exc

            self._redis = aioredis.from_url(self._url, decode_responses=True)
            return self._redis

    async def get_session(self, session_id: str) -> SessionInfo | None:
        """Return session metadata or ``None``."""
        import json

        r = await self._ensure_redis()
        data = await r.hgetall(self._session_key(session_id))
        if not data:
            return None
        return SessionInfo(
            session_id=session_id,
            user_id=data.get("user_id") or None,
            title=data.get("title") or None,
            message_count=int(data.get("message_count", 0)),
            created_at=datetime.fromisoformat(data["created_at"])
            if "created_at" in data
            else datetime.now(timezone.utc),
            updated_at=datetime.fromisoformat(data["updated_at"])
            if "updated_at" in data
            else datetime.now(timezone.utc),
            metadata=json.loads(data["metadata"]) if data.get("metadata") else {},
        )

    async def load_messages(self, session_id: str) -> list[Message]:
        """Load all messages for a session from a Redis sorted set."""
        import json

        r = await self._ensure_redis()
        raw_messages = await r.lrange(self._messages_key(session_id), 0, -1)
        return [Message.from_dict(json.loads(m)) for m in raw_messages]

    async def save_messages(self, session_id: str, messages: list[Message]) -> None:
        """Persist messages as a Redis list."""
        import json

        if self._max_messages > 0 and len(messages) > self._max_messages:
            messages = messages[-self._max_messages :]

        r = await self._ensure_redis()
        now = datetime.now(timezone.utc)
        msg_key = self._messages_key(session_id)
        sess_key = self._session_key(session_id)

        pipe = r.pipeline()

        # Check if session exists
        existing = await r.exists(sess_key)

        # Store session info
        session_data = {
            "session_id": session_id,
            "message_count": str(len(messages)),
            "updated_at": now.isoformat(),
        }
        if not existing:
            session_data["created_at"] = now.isoformat()

        pipe.hset(sess_key, mapping=session_data)

        # Replace messages
        pipe.delete(msg_key)
        if messages:
            pipe.rpush(msg_key, *[json.dumps(m.to_dict()) for m in messages])

        # Update index (sorted set scored by timestamp)
        score = now.timestamp()
        pipe.zadd(self._index_key(), {session_id: score})

        # Get user_id to also index by user
        if existing:
            user_id = await r.hget(sess_key, "user_id")
            if user_id:
                pipe.zadd(self._index_key(user_id), {session_id: score})

        # TTL
        if self._ttl > 0:
            pipe.expire(sess_key, self._ttl)
            pipe.expire(msg_key, self._ttl)

        await pipe.execute()

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session from Redis."""
        r = await self._ensure_redis()
        sess_key = self._session_key(session_id)

        existed = await r.exists(sess_key)
        if not existed:
            return False

        # Get user_id before deletion for index cleanup
        user_id = await r.hget(sess_key, "user_id")

        pipe = r.pipeline()
        pipe.delete(sess_key)
        pipe.delete(self._messages_key(session_id))
        pipe.zrem(self._index_key(), session_id)
        if user_id:
            pipe.zrem(self._index_key(user_id), session_id)
        await pipe.execute()
        return True

    async def list_sessions(
        self,
        *,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionInfo]:
        """List sessions from Redis sorted set index."""
        r = await self._ensure_redis()
        index_key = self._index_key(user_id)

        # Get session IDs from sorted set (newest first)
        session_ids = await r.zrevrange(index_key, offset, offset + limit - 1)

        results: list[SessionInfo] = []
        for sid in session_ids:
            data = await r.hgetall(self._session_key(sid))
            if not data:
                continue
            results.append(
                SessionInfo(
                    session_id=sid,
                    user_id=data.get("user_id"),
                    title=data.get("title"),
                    message_count=int(data.get("message_count", 0)),
                    created_at=datetime.fromisoformat(data["created_at"])
                    if "created_at" in data
                    else datetime.now(timezone.utc),
                    updated_at=datetime.fromisoformat(data["updated_at"])
                    if "updated_at" in data
                    else datetime.now(timezone.utc),
                    metadata={},
                )
            )
        return results

    async def update_session(
        self,
        session_id: str,
        *,
        user_id: str | None = ...,  # type: ignore[assignment]
        title: str | None = ...,  # type: ignore[assignment]
        metadata: dict[str, Any] | None = ...,  # type: ignore[assignment]
    ) -> bool:
        """Update session metadata in Redis hash."""
        r = await self._ensure_redis()
        sess_key = self._session_key(session_id)

        if not await r.exists(sess_key):
            return False

        updates: dict[str, str] = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        old_user_id = await r.hget(sess_key, "user_id")

        if user_id is not ...:
            updates["user_id"] = user_id or ""
        if title is not ...:
            updates["title"] = title or ""
        if metadata is not ...:
            import json

            updates["metadata"] = json.dumps(metadata) if metadata else ""

        if len(updates) == 1:
            return False

        pipe = r.pipeline()
        pipe.hset(sess_key, mapping=updates)

        # Update user index if user_id changed
        new_uid = updates.get("user_id")
        if new_uid is not None and new_uid != old_user_id:
            score = datetime.now(timezone.utc).timestamp()
            if old_user_id:
                pipe.zrem(self._index_key(old_user_id), session_id)
            if new_uid:
                pipe.zadd(self._index_key(new_uid), {session_id: score})

        await pipe.execute()
        return True

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pg_json_loads(val: Any) -> dict[str, Any]:
    """Safely parse a PostgreSQL JSONB value.

    asyncpg returns ``dict`` for JSONB columns, but some edge cases
    (e.g., raw strings) need a ``json.loads`` pass.
    """
    if isinstance(val, dict):
        return val
    if val is None:
        return {}
    import json

    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return {}
