"""Per-session state management for MCP servers.

Maintains state that persists across multiple tool calls within
the same client session.  Each MCP session (connection) gets its
own isolated state store.

Example::

    from promptise.mcp.server import MCPServer, Depends, SessionState

    server = MCPServer(name="api")

    @server.tool()
    async def add_item(
        item: str,
        session: SessionState = Depends(SessionState),
    ) -> dict:
        items = session.get("items", [])
        items.append(item)
        session.set("items", items)
        return {"count": len(items)}

    @server.tool()
    async def get_items(
        session: SessionState = Depends(SessionState),
    ) -> list[str]:
        return session.get("items", [])
"""

from __future__ import annotations

import threading
from typing import Any


class SessionState:
    """Key-value state scoped to the current MCP session.

    Each client session gets its own isolated state.  State persists
    across tool calls within the same session and is automatically
    cleaned up when the session ends.

    Use via dependency injection::

        @server.tool()
        async def counter(session: SessionState = Depends(SessionState)) -> int:
            count = session.get("count", 0) + 1
            session.set("count", count)
            return count
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value from session state."""
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a value in session state."""
        self._data[key] = value

    def delete(self, key: str) -> None:
        """Remove a key from session state."""
        self._data.pop(key, None)

    def clear(self) -> None:
        """Clear all session state."""
        self._data.clear()

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def keys(self) -> list[str]:
        """Return all keys in session state."""
        return list(self._data.keys())

    def to_dict(self) -> dict[str, Any]:
        """Return a snapshot of all state."""
        return dict(self._data)


class SessionManager:
    """Manages per-session state stores.

    Maps session IDs to ``SessionState`` instances.  Thread-safe for
    use across async tasks.

    The framework creates one ``SessionManager`` per ``MCPServer``
    and looks up the current session by ID in the request context.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str) -> SessionState:
        """Get or create a ``SessionState`` for the given session ID."""
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionState()
            return self._sessions[session_id]

    def remove(self, session_id: str) -> None:
        """Remove a session's state (called on disconnect)."""
        with self._lock:
            self._sessions.pop(session_id, None)

    @property
    def active_sessions(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)
