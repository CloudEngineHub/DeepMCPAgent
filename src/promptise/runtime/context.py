"""Unified agent context: state + memory + env + files.

:class:`AgentContext` is the single context object that an
:class:`~promptise.runtime.process.AgentProcess` provides to its agent
on every invocation.  It merges four concerns into one API:

* **State** — persistent key-value store with audit trail (Blackboard-like)
* **Memory** — semantic memory via any :class:`~promptise.memory.MemoryProvider`
* **Environment** — filtered snapshot of ``os.environ``
* **Files** — logical name → filesystem path mapping

The context is serializable via :meth:`to_dict` / :meth:`from_dict` for
checkpointing and distribution.  Every state write is also handed to the
hooks registered with :meth:`AgentContext.add_update_hook`, so an owner
can journal it as a ``context_update`` entry (the shape
:class:`~promptise.runtime.journal.ReplayEngine` replays) and recover
state written since the last checkpoint.  :func:`create_state_tools`
builds the tool an agent uses to write its own state.

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
import copy
import inspect
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("promptise.runtime.context")

#: Journal entry type for a state write (read by ``ReplayEngine``).
CONTEXT_UPDATE_ENTRY_TYPE = "context_update"

#: A state-write hook: called with the :class:`StateEntry` just recorded.
#: It may return an awaitable, which is scheduled on the running loop.
StateUpdateHook = Callable[["StateEntry"], "Awaitable[Any] | None"]


def _snapshot(value: Any) -> Any:
    """Return an independent copy of *value* for an audit-history entry.

    The live state store keeps the original (mutable) object so ``get()``
    returns a writable blackboard value; the history entry must instead be
    an immutable-after-record snapshot, so a later in-place mutation of the
    stored value cannot retroactively rewrite past, timestamped audit
    records. Values that cannot be deep-copied fall back to the original
    reference (best effort — no worse than the previous behavior).
    """
    try:
        return copy.deepcopy(value)
    except Exception:  # pragma: no cover — exotic non-copyable objects
        return value


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
        """Serialize to a JSON-compatible dict.

        This is also the ``data`` of a ``context_update`` journal entry:
        :class:`~promptise.runtime.journal.ReplayEngine` reads its ``key``
        and ``value``.
        """
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

    Every successful write (:meth:`put`, :meth:`update`) is recorded in
    the key's audit history and handed to the hooks registered with
    :meth:`add_update_hook`.  :meth:`restore_state` loads recovered state
    without firing them.

    Args:
        writable_keys: Keys that :meth:`put` and :meth:`update` may write
            (agent, API and trigger writes alike).  ``None`` or an empty
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
        self._update_hooks: list[StateUpdateHook] = []
        self._pending_hooks: set[asyncio.Future[Any]] = set()

        # Populate initial state
        for key, value in (initial_state or {}).items():
            self._state[key] = value
            entry = StateEntry(key=key, value=_snapshot(value), source="system")
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

    @property
    def writable_keys(self) -> list[str] | None:
        """The keys :meth:`put` may write (sorted), or ``None`` when all are."""
        return sorted(self._writable_keys) if self._writable_keys is not None else None

    def can_write(self, key: str) -> bool:
        """Whether *key* may be written by :meth:`put` / :meth:`update`."""
        return self._writable_keys is None or key in self._writable_keys

    def denied_keys(self, keys: Iterable[str]) -> list[str]:
        """The keys among *keys* that may not be written, in the given order."""
        return [key for key in keys if not self.can_write(key)]

    def put(
        self,
        key: str,
        value: Any,
        *,
        source: str = "agent",
    ) -> None:
        """Write a value to the state store.

        The write is recorded in the key's audit history and handed to
        the update hooks (see :meth:`add_update_hook`).

        Args:
            key: State key.
            value: Value to store.
            source: Origin of the write (``"agent"``, ``"api"``,
                ``"trigger"``, ...), kept in the audit history.

        Raises:
            KeyError: If *key* is not in the writable keys set (when
                writable keys are configured).
        """
        if not self.can_write(key):
            raise KeyError(self._not_writable_message([key]))
        self._fire_hooks([self._record(key, value, source)])

    def update(self, values: Mapping[str, Any], *, source: str = "agent") -> list[str]:
        """Write several keys at once, all or nothing.

        Every key is checked before anything is written: if one is not
        writable, none is.

        Args:
            values: Keys and the values to store.
            source: Origin of the writes.

        Returns:
            The keys written, in the given order.

        Raises:
            KeyError: If any key is not writable (the message names all
                of them).  Nothing is written.
        """
        denied = self.denied_keys(values)
        if denied:
            raise KeyError(self._not_writable_message(denied))
        entries = [self._record(key, value, source) for key, value in values.items()]
        self._fire_hooks(entries)
        return [entry.key for entry in entries]

    def restore_state(self, state: Mapping[str, Any], *, source: str = "recovery") -> None:
        """Load recovered state (from a checkpoint or ``ReplayEngine``).

        Bypasses :attr:`writable_keys` (the values were written legitimately
        before) and does not fire the update hooks, so restored values are
        not journaled a second time.  Each key gets a history entry with
        *source*.
        """
        for key, value in state.items():
            self._record(key, value, source)

    def _record(self, key: str, value: Any, source: str) -> StateEntry:
        self._state[key] = value
        entry = StateEntry(key=key, value=_snapshot(value), source=source)
        self._history.setdefault(key, []).append(entry)
        return entry

    def _not_writable_message(self, keys: list[str]) -> str:
        names = ", ".join(repr(k) for k in keys)
        noun = "Key" if len(keys) == 1 else "Keys"
        verb = "is" if len(keys) == 1 else "are"
        return f"{noun} {names} {verb} not writable. Allowed: {self.writable_keys}"

    # ------------------------------------------------------------------
    # Update hooks (journaling)
    # ------------------------------------------------------------------

    def add_update_hook(self, hook: StateUpdateHook) -> Callable[[], None]:
        """Call *hook* with the :class:`StateEntry` of every state write.

        Meant for journaling: an owner registers a hook that appends a
        ``context_update`` entry (:data:`CONTEXT_UPDATE_ENTRY_TYPE`) with
        ``entry.to_dict()`` as its data, which ``ReplayEngine`` replays
        after the last checkpoint.  The hook runs synchronously after the
        write; if it returns an awaitable, that is scheduled on the running
        event loop (await :meth:`flush_update_hooks` to wait for it).  A
        hook that raises is logged and never fails the write.

        Returns:
            A function that removes the hook.
        """
        self._update_hooks.append(hook)

        def remove() -> None:
            if hook in self._update_hooks:
                self._update_hooks.remove(hook)

        return remove

    async def flush_update_hooks(self) -> None:
        """Wait for the awaitables returned by update hooks to finish."""
        while self._pending_hooks:
            await asyncio.gather(*list(self._pending_hooks), return_exceptions=True)

    def _fire_hooks(self, entries: list[StateEntry]) -> None:
        for entry in entries:
            for hook in list(self._update_hooks):
                try:
                    result = hook(entry)
                except Exception:
                    logger.warning(
                        "AgentContext: update hook failed for %r", entry.key, exc_info=True
                    )
                    continue
                if inspect.isawaitable(result):
                    self._schedule(result, entry.key)

    def _schedule(self, awaitable: Awaitable[Any], key: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # put() called outside the event loop
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            logger.warning(
                "AgentContext: async update hook for %r skipped (no running event loop)", key
            )
            return
        future = asyncio.ensure_future(awaitable, loop=loop)
        self._pending_hooks.add(future)

        def done(fut: asyncio.Future[Any]) -> None:
            self._pending_hooks.discard(fut)
            if not fut.cancelled() and fut.exception() is not None:
                logger.warning(
                    "AgentContext: async update hook failed for %r",
                    key,
                    exc_info=fut.exception(),
                )

        future.add_done_callback(done)

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


# ----------------------------------------------------------------------
# Agent-facing state tool
# ----------------------------------------------------------------------

#: Name of the tool built by :func:`create_state_tools`.
STATE_TOOL_NAME = "update_state"


def create_state_tools(
    context: AgentContext,
    *,
    source: str = "agent",
    max_value_chars: int = 10_000,
    max_keys: int = 100,
) -> list[Any]:
    """Build the tool an agent uses to write its own state.

    Returns one LangChain ``StructuredTool``, ``update_state(key, value)``,
    which writes through :meth:`AgentContext.put`, so it is limited to the
    context's :attr:`~AgentContext.writable_keys` (offered to the model as
    an enum when they are configured), recorded in the audit history with
    *source*, and handed to the update hooks (journaled, when the owner
    journals).  Refusals come back to the model as ``"Error: ..."`` text,
    never as exceptions.

    The state is injected into every later prompt, so values are kept
    small: a value must be JSON-serializable and at most
    *max_value_chars* characters as JSON, and a context without
    ``writable_keys`` accepts at most *max_keys* keys in total.

    Args:
        context: The context the tool writes to.
        source: Audit source recorded for the tool's writes.
        max_value_chars: Largest value accepted (JSON length).
        max_keys: Most keys the state may hold after a write that adds a
            key (only checked when every key is writable).

    Returns:
        ``[update_state]``, ready to append to an agent's tools.

    Raises:
        ValueError: *max_value_chars* or *max_keys* is not positive.
    """
    from langchain_core.tools import StructuredTool
    from pydantic import Field, create_model

    if max_value_chars <= 0 or max_keys <= 0:
        raise ValueError("create_state_tools: max_value_chars and max_keys must be positive")

    allowed = context.writable_keys
    # A Literal key type reaches the model as an enum (LangChain rebuilds
    # the schema and drops json_schema_extra).
    key_type: Any = Literal.__getitem__(tuple(allowed)) if allowed else str
    key_help = (
        f"State key to write. One of: {', '.join(allowed)}."
        if allowed
        else "State key to write (short snake_case name)."
    )
    args_schema = create_model(
        "UpdateStateArgs",
        key=(key_type, Field(..., description=key_help)),
        value=(
            Any,
            Field(
                ...,
                description=(
                    "Value to store: a string, number, boolean, list or object. "
                    "It replaces the key's current value."
                ),
            ),
        ),
    )

    async def update_state(key: str, value: Any) -> str:
        if not context.can_write(key):
            return f"Error: state key {key!r} is not writable. Writable keys: {', '.join(allowed or [])}"
        try:
            encoded = json.dumps(value)
        except (TypeError, ValueError):
            return "Error: the value must be JSON (string, number, boolean, list or object)"
        if len(encoded) > max_value_chars:
            return (
                f"Error: the value is {len(encoded)} characters as JSON; "
                f"the limit is {max_value_chars}. Store a shorter summary."
            )
        if (
            allowed is None
            and key not in context.state_keys()
            and len(context.state_keys()) >= max_keys
        ):
            return f"Error: the state already holds {max_keys} keys; update an existing key instead"
        try:
            context.put(key, value, source=source)
        except KeyError as exc:
            return f"Error: {exc.args[0] if exc.args else exc}"
        return f"Stored state key {key!r}."

    description = (
        "Save a value in your persistent state. The state survives between runs "
        "and is shown to you at the start of every run as [Context State]. "
        "Use it for progress markers, counters and short facts you need next time."
    )
    if allowed:
        description += f" Writable keys: {', '.join(allowed)}."
    tool = StructuredTool(
        name=STATE_TOOL_NAME,
        description=description,
        args_schema=args_schema,
        coroutine=update_state,
        func=lambda **kw: "Error: update_state is async-only",
        # A key outside the enum fails schema validation: answer the model
        # with the allowed keys rather than raising.
        handle_validation_error=lambda exc: (
            "Error: invalid update_state arguments. "
            + (f"Writable keys: {', '.join(allowed)}. " if allowed else "")
            + "Pass a 'key' and a JSON 'value'."
        ),
    )
    return [tool]
