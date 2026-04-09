"""Runtime lifecycle hooks.

Runtime hooks let you react to high-level events in an agent process
without modifying the agent itself. Unlike the reasoning-graph hooks in
:mod:`promptise.engine.hooks` (which fire for every node/tool in a
graph), runtime hooks fire at *process-level* moments: session start,
user prompts, permission checks, subagent starts, context compaction,
file changes, config changes, task creation/completion.

Typical uses:

* Send a Slack message when a session starts or a critical permission
  is denied.
* Audit-log every user prompt before the agent sees it.
* Mutate a user prompt to inject standing context.
* Trigger an external shell script (see :class:`ShellHook`) on a
  specific event.
* Block an action by raising from a hook.

Hooks are async callables keyed by event type. A hook can be
``once=True``, in which case it fires a single time and is then
automatically deregistered — useful for one-shot notifications,
first-run setup, or "next time X happens, do Y" patterns.

Example::

    from promptise.runtime.hooks import HookManager, HookEvent

    hooks = HookManager()

    @hooks.on(HookEvent.USER_PROMPT_SUBMIT)
    async def audit(ctx):
        await audit_log.write(ctx.data["prompt"])

    @hooks.on(HookEvent.SESSION_START, once=True)
    async def welcome(ctx):
        print("First session of the run!")

    # Inside the process, at the right moment:
    await hooks.dispatch(HookEvent.SESSION_START, data={"session_id": "s1"})
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class HookEvent(str, Enum):
    """Lifecycle events that a :class:`HookManager` can dispatch.

    These span process startup, user interaction, permission checks,
    subagent spawns, context-window compaction, file-system changes,
    config reloads, and task bookkeeping. They are deliberately coarse:
    if you need fine-grained per-node / per-tool introspection, use
    :mod:`promptise.engine.hooks` instead (reasoning graph hooks).
    """

    #: Fires when the process enters the RUNNING state for the first time
    #: in a run. Use for setup, greetings, audit start.
    SESSION_START = "session_start"

    #: Fires when the process is about to stop (STOPPING state). Use for
    #: cleanup, teardown audit, final flush.
    SESSION_END = "session_end"

    #: Fires every time a user (or trigger) submits a new prompt. Data
    #: contains the full prompt text; hooks may mutate it.
    USER_PROMPT_SUBMIT = "user_prompt_submit"

    #: Fires before a tool that requires approval is executed. Data
    #: contains the tool name, arguments, and caller context.
    PERMISSION_REQUEST = "permission_request"

    #: Fires when an approval request is denied (by policy, user, or
    #: classifier). Useful for alerting.
    PERMISSION_DENIED = "permission_denied"

    #: Fires when the agent spawns a subagent (via cross-agent delegation
    #: or open-mode meta-tools).
    SUBAGENT_START = "subagent_start"

    #: Fires when a spawned subagent finishes.
    SUBAGENT_STOP = "subagent_stop"

    #: Fires immediately before the conversation buffer is compacted
    #: (e.g. summary injection, truncation).
    PRE_COMPACT = "pre_compact"

    #: Fires after compaction completes. Data contains the new buffer
    #: size and summary if any.
    POST_COMPACT = "post_compact"

    #: Fires when a watched file on disk changes. Produced by
    #: FileWatchTrigger or manual dispatch.
    FILE_CHANGED = "file_changed"

    #: Fires when the process's effective configuration changes at
    #: runtime (open mode, manifest reload, etc.).
    CONFIG_CHANGE = "config_change"

    #: Fires whenever a new background task / invocation is enqueued.
    TASK_CREATED = "task_created"

    #: Fires when a background task / invocation finishes, success or
    #: failure. Data contains outcome + duration.
    TASK_COMPLETED = "task_completed"


@dataclass
class HookContext:
    """Context passed to every hook callback.

    Attributes:
        event: The event that is firing.
        process_id: Identifier of the :class:`AgentProcess` dispatching
            the hook. May be empty for top-level dispatch.
        timestamp: UTC timestamp of dispatch.
        data: Free-form event payload. Hook implementations may read
            and (for some events, e.g. USER_PROMPT_SUBMIT) mutate this
            dict to alter the subsequent runtime behavior.
        metadata: Additional per-dispatch metadata, not typically
            mutated by hooks.
    """

    event: HookEvent
    process_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def set(self, key: str, value: Any) -> None:
        """Shorthand for ``self.data[key] = value``."""
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """Shorthand for ``self.data.get(key, default)``."""
        return self.data.get(key, default)


#: A hook callback — must be async and take a single HookContext.
HookCallable = Callable[[HookContext], Awaitable[None]]


class HookBlocked(Exception):
    """Raised by a hook to block the action that triggered it.

    The dispatcher catches this specifically and returns a
    ``blocked=True`` outcome so callers can abort the in-progress
    operation (e.g. cancel a tool call, refuse a user prompt).
    """

    def __init__(self, reason: str = "blocked by hook") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Registration:
    """Internal: one registered hook binding."""

    hook_id: str
    event: HookEvent
    callback: HookCallable
    once: bool
    priority: int
    tag: str | None = None


@dataclass
class DispatchResult:
    """Outcome of a :meth:`HookManager.dispatch` call.

    Attributes:
        fired: Number of hooks that ran.
        blocked: True if any hook raised :class:`HookBlocked`.
        reason: Block reason, if any.
        errors: List of ``(hook_id, exception)`` tuples for hooks that
            raised an unexpected exception. These are logged and
            swallowed so one broken hook does not take down the
            runtime.
    """

    fired: int = 0
    blocked: bool = False
    reason: str = ""
    errors: list[tuple[str, Exception]] = field(default_factory=list)


class HookManager:
    """Event-keyed dispatcher for runtime lifecycle hooks.

    Supports per-event registration with optional ``once`` semantics,
    ordered dispatch by priority, exception isolation (one broken hook
    never blocks the rest), and programmatic deregistration by ID or
    tag.

    Thread / coroutine safety: all mutation and dispatch are guarded by
    an asyncio lock. It is safe to register hooks from inside a hook
    callback.
    """

    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[_Registration]] = {
            event: [] for event in HookEvent
        }
        self._lock = asyncio.Lock()

    # -- Registration --

    def on(
        self,
        event: HookEvent,
        *,
        once: bool = False,
        priority: int = 0,
        tag: str | None = None,
    ) -> Callable[[HookCallable], HookCallable]:
        """Decorator form of :meth:`register`.

        Example::

            @hooks.on(HookEvent.USER_PROMPT_SUBMIT)
            async def audit(ctx):
                log(ctx.data["prompt"])

            @hooks.on(HookEvent.SESSION_START, once=True)
            async def greet(ctx):
                print("hello!")
        """

        def decorator(fn: HookCallable) -> HookCallable:
            self.register(event, fn, once=once, priority=priority, tag=tag)
            return fn

        return decorator

    def register(
        self,
        event: HookEvent,
        callback: HookCallable,
        *,
        once: bool = False,
        priority: int = 0,
        tag: str | None = None,
    ) -> str:
        """Register a hook callback for an event.

        Args:
            event: Which :class:`HookEvent` to subscribe to.
            callback: Async function taking a single
                :class:`HookContext` argument.
            once: If True, the hook auto-deregisters after its first
                successful or failed invocation. Useful for one-shot
                reminders, first-run setup, "next time" patterns.
            priority: Higher priority hooks run first within an event.
                Default 0. Use negative priorities to run late.
            tag: Optional string tag for grouped deregistration with
                :meth:`remove_by_tag`.

        Returns:
            A hook ID suitable for passing to :meth:`remove`.
        """
        if not asyncio.iscoroutinefunction(callback):
            raise TypeError(
                f"hook callback for {event.value!r} must be an async function, "
                f"got {type(callback).__name__}"
            )

        hook_id = f"hook-{uuid.uuid4().hex[:12]}"
        registration = _Registration(
            hook_id=hook_id,
            event=event,
            callback=callback,
            once=once,
            priority=priority,
            tag=tag,
        )
        bucket = self._hooks[event]
        bucket.append(registration)
        # Stable sort by negative priority so ties preserve insertion order
        bucket.sort(key=lambda r: -r.priority)
        return hook_id

    async def remove(self, hook_id: str) -> bool:
        """Deregister a single hook by its ID.

        Returns:
            True if the hook was found and removed, False otherwise.
        """
        async with self._lock:
            for bucket in self._hooks.values():
                for i, reg in enumerate(bucket):
                    if reg.hook_id == hook_id:
                        bucket.pop(i)
                        return True
        return False

    async def remove_by_tag(self, tag: str) -> int:
        """Deregister every hook with a matching ``tag``.

        Returns:
            Number of hooks removed.
        """
        removed = 0
        async with self._lock:
            for bucket in self._hooks.values():
                keep = [r for r in bucket if r.tag != tag]
                removed += len(bucket) - len(keep)
                bucket[:] = keep
        return removed

    def clear(self, event: HookEvent | None = None) -> None:
        """Remove all hooks.

        Args:
            event: If provided, clear only this event's hooks;
                otherwise clear everything.
        """
        if event is None:
            for bucket in self._hooks.values():
                bucket.clear()
        else:
            self._hooks[event].clear()

    def registered(self, event: HookEvent | None = None) -> list[str]:
        """Return the hook IDs currently registered.

        Args:
            event: If provided, filter to this event.
        """
        if event is None:
            return [r.hook_id for bucket in self._hooks.values() for r in bucket]
        return [r.hook_id for r in self._hooks[event]]

    # -- Dispatch --

    async def dispatch(
        self,
        event: HookEvent,
        *,
        process_id: str = "",
        data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DispatchResult:
        """Fire all hooks registered for ``event``.

        Hooks run in priority order. Exceptions other than
        :class:`HookBlocked` are caught, logged, and recorded in the
        returned :class:`DispatchResult` — they never propagate to the
        caller. ``HookBlocked`` short-circuits remaining hooks and
        returns a blocked result.

        After dispatch, any hook with ``once=True`` is removed from the
        registry regardless of success or failure.

        Args:
            event: The event to fire.
            process_id: Identifier of the dispatching process.
            data: Event payload. Hooks may mutate.
            metadata: Per-dispatch metadata.

        Returns:
            A :class:`DispatchResult` summarizing what happened.
        """
        ctx = HookContext(
            event=event,
            process_id=process_id,
            data=dict(data or {}),
            metadata=dict(metadata or {}),
        )
        result = DispatchResult()

        # Snapshot under the lock so re-entrant registrations can't mess
        # up ordering mid-dispatch.
        async with self._lock:
            snapshot = list(self._hooks[event])

        for reg in snapshot:
            try:
                await reg.callback(ctx)
                result.fired += 1
            except HookBlocked as exc:
                result.blocked = True
                result.reason = exc.reason
                logger.info(
                    "hook %s blocked event %s: %s", reg.hook_id, event.value, exc.reason
                )
                # mark the one-shot for removal even when blocking
                if reg.once:
                    await self.remove(reg.hook_id)
                return result
            except Exception as exc:  # noqa: BLE001 — hooks are untrusted
                logger.exception(
                    "hook %s raised on event %s", reg.hook_id, event.value
                )
                result.errors.append((reg.hook_id, exc))
            finally:
                if reg.once:
                    await self.remove(reg.hook_id)

        return result

    def __repr__(self) -> str:
        total = sum(len(b) for b in self._hooks.values())
        return f"HookManager(registered={total})"


__all__ = [
    "DispatchResult",
    "HookBlocked",
    "HookCallable",
    "HookContext",
    "HookEvent",
    "HookManager",
]
