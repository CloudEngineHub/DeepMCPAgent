"""Unified agent context: state + memory + env + files.

:class:`AgentContext` is the single context object that an
:class:`~promptise.runtime.process.AgentProcess` provides to its agent
on every invocation.  It merges four concerns into one API:

* **State** — persistent key-value store with audit trail (Blackboard-like)
* **Memory** — semantic memory via any :class:`~promptise.memory.MemoryProvider`
* **Environment** — filtered snapshot of ``os.environ``
* **Files** — logical name → filesystem path mapping

The context is serializable via :meth:`to_dict` / :meth:`from_dict` for
checkpointing and distribution.

Example::

    ctx = AgentContext(
        initial_state={"counter": 0},
        writable_keys=["counter"],
        env_prefix="AGENT_",
    )
    ctx.put("counter", 1)
    assert ctx.get("counter") == 1
    assert len(ctx.state_history("counter")) == 2  # initial + put
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StateEntry:
    """Audit record for a single state write.

    Attributes:
        key: The key that was written.
        value: The value that was stored.
        timestamp: Unix timestamp of the write.
        source: Origin of the write (``"system"``, ``"agent"``,
            ``"trigger"``, etc.).
    """

    key: str
    value: Any
    timestamp: float = field(default_factory=time.time)
    source: str = "system"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "key": self.key,
            "value": self.value,
            "timestamp": self.timestamp,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateEntry:
        """Deserialize from a dict."""
        return cls(
            key=data["key"],
            value=data["value"],
            timestamp=data.get("timestamp", 0.0),
            source=data.get("source", "system"),
        )


class AgentContext:
    """Unified context layer for an agent process.

    Provides:

    * ``.get()`` / ``.put()`` — persistent key-value state with audit trail
    * ``.memory`` — optional :class:`~promptise.memory.MemoryProvider`
    * ``.env`` — filtered environment variables
    * ``.files`` — mounted file paths

    Args:
        writable_keys: Keys the agent is allowed to write to.  An empty
            list means **all** keys are writable.
        memory_provider: Optional
            :class:`~promptise.memory.MemoryProvider` instance.
        file_mounts: Mapping of logical name → filesystem path.
        env_prefix: Only expose env vars starting with this prefix.
        initial_state: Pre-populated key-value state.
    """

    def __init__(
        self,
        *,
        writable_keys: list[str] | None = None,
        memory_provider: Any | None = None,
        file_mounts: dict[str, str] | None = None,
        env_prefix: str = "AGENT_",
        initial_state: dict[str, Any] | None = None,
    ) -> None:
        self._writable_keys: set[str] | None = set(writable_keys) if writable_keys else None
        self._memory_provider = memory_provider
        self._file_mounts: dict[str, str] = dict(file_mounts or {})
        self._env_prefix = env_prefix

        # State store
        self._state: dict[str, Any] = {}
        self._history: dict[str, list[StateEntry]] = {}
        self._lock = asyncio.Lock()

        # Populate initial state
        for key, value in (initial_state or {}).items():
            self._state[key] = value
            entry = StateEntry(key=key, value=value, source="system")
            self._history.setdefault(key, []).append(entry)

    # ------------------------------------------------------------------
    # State (Blackboard-like)
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Read a value from the state store.

        Args:
            key: State key.
            default: Value to return if key is absent.

        Returns:
            The stored value or *default*.
        """
        return self._state.get(key, default)

    def put(
        self,
        key: str,
        value: Any,
        *,
        source: str = "agent",
    ) -> None:
        """Write a value to the state store.

        Args:
            key: State key.
            value: Value to store.
            source: Origin of the write.

        Raises:
            KeyError: If *key* is not in the writable keys set (when
                writable keys are configured).
        """
        if self._writable_keys is not None and key not in self._writable_keys:
            raise KeyError(f"Key {key!r} is not writable. Allowed: {sorted(self._writable_keys)}")
        self._state[key] = value
        entry = StateEntry(key=key, value=value, source=source)
        self._history.setdefault(key, []).append(entry)

    def state_snapshot(self) -> dict[str, Any]:
        """Return a shallow copy of the current state."""
        return dict(self._state)

    def state_history(self, key: str) -> list[StateEntry]:
        """Return the audit trail for a given key.

        Args:
            key: State key.

        Returns:
            Chronological list of :class:`StateEntry` records.
        """
        return list(self._history.get(key, []))

    def state_keys(self) -> list[str]:
        """Return all keys in the current state."""
        return list(self._state.keys())

    def clear_state(self) -> None:
        """Remove all state and history."""
        self._state.clear()
        self._history.clear()

    # ------------------------------------------------------------------
    # Memory (MemoryProvider protocol)
    # ------------------------------------------------------------------

    @property
    def memory(self) -> Any | None:
        """The underlying :class:`~promptise.memory.MemoryProvider`, or ``None``."""
        return self._memory_provider

    async def search_memory(
        self,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[Any]:
        """Search semantic memory for relevant context.

        Args:
            query: Search query text.
            limit: Max results to return.
            min_score: Min relevance score (0.0–1.0).

        Returns:
            List of :class:`~promptise.memory.MemoryResult` objects,
            or empty list if no provider is configured.
        """
        if self._memory_provider is None:
            return []
        results = await self._memory_provider.search(query, limit=limit)
        if min_score > 0:
            results = [r for r in results if r.score >= min_score]
        return results

    async def add_memory(
        self,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Store a new memory.

        Args:
            content: Text content to store.
            metadata: Optional metadata.

        Returns:
            Memory ID string, or ``None`` if no provider is configured.
        """
        if self._memory_provider is None:
            return None
        return await self._memory_provider.add(content, metadata=metadata or {})

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory by ID.

        Returns:
            ``True`` if deleted, ``False`` if no provider or not found.
        """
        if self._memory_provider is None:
            return False
        return await self._memory_provider.delete(memory_id)

    # ------------------------------------------------------------------
    # Environment
    # ------------------------------------------------------------------

    @property
    def env(self) -> dict[str, str]:
        """Filtered snapshot of environment variables.

        Only variables whose name starts with :attr:`env_prefix` are
        included.  The prefix is **not** stripped from the key.
        """
        return {k: v for k, v in os.environ.items() if k.startswith(self._env_prefix)}

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    @property
    def files(self) -> dict[str, str]:
        """Mapping of logical mount name → filesystem path."""
        return dict(self._file_mounts)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize context state for checkpointing or distribution.

        Note:
            The memory provider is **not** serialized — it must be
            re-attached when restoring from a checkpoint.
        """
        return {
            "state": dict(self._state),
            "history": {k: [e.to_dict() for e in entries] for k, entries in self._history.items()},
            "writable_keys": (sorted(self._writable_keys) if self._writable_keys else None),
            "env_prefix": self._env_prefix,
            "file_mounts": dict(self._file_mounts),
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        memory_provider: Any | None = None,
    ) -> AgentContext:
        """Reconstruct an :class:`AgentContext` from a serialized dict.

        Args:
            data: Dict produced by :meth:`to_dict`.
            memory_provider: Optional provider to re-attach.

        Returns:
            Restored :class:`AgentContext` instance.
        """
        ctx = cls(
            writable_keys=data.get("writable_keys"),
            memory_provider=memory_provider,
            file_mounts=data.get("file_mounts", {}),
            env_prefix=data.get("env_prefix", "AGENT_"),
        )
        # Restore state directly (bypass writable_keys check)
        ctx._state = dict(data.get("state", {}))
        # Restore history
        for key, entries in data.get("history", {}).items():
            ctx._history[key] = [StateEntry.from_dict(e) for e in entries]
        return ctx

    def __repr__(self) -> str:
        return (
            f"AgentContext(keys={len(self._state)}, "
            f"memory={'yes' if self._memory_provider else 'no'}, "
            f"mounts={len(self._file_mounts)})"
        )
