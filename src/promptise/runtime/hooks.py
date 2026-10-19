"""Runtime lifecycle hooks.

Runtime hooks let you react to high-level events in an agent process
without modifying the agent itself.  They fire at *process-level*
moments: a session starting or ending, a prompt about to be sent to the
model, an approval request, an open-mode subagent spawn, conversation
compaction, a watched file changing, a configuration change, and every
trigger event entering and leaving the queue.

**Not the engine hooks.**  :mod:`promptise.engine.hooks` (``Hook``,
``LoggingHook``, ``TimingHook``, ...) are *reasoning-graph* hooks: objects
with ``pre_node`` / ``post_node`` / ``pre_tool`` / ``post_tool`` methods,
called by the graph engine for every node and tool call of a single
agent run.  Runtime hooks are async callables keyed by a
:class:`HookEvent`, dispatched by :class:`~promptise.runtime.AgentProcess`
around runs.  Use the engine hooks to inspect or rewrite what happens
*inside* a run; use runtime hooks for what happens *to the process*.

Wiring: every :class:`~promptise.runtime.AgentProcess` owns a
:class:`HookManager` (:attr:`AgentProcess.hooks`; pass ``hooks=`` to
share one) and dispatches every :class:`HookEvent`.  Hooks are declared
in code (``process.hooks.on(...)``, or ``ProcessConfig(hooks=[HookConfig
(event=..., callback=fn)])``) or, as external commands run by
:class:`~promptise.runtime.ShellHook`, in a manifest's ``hooks:`` section.

Typical uses:

* Send a Slack message when a session starts or a permission is denied.
* Audit-log every prompt before the agent sees it.
* Mutate a prompt to inject standing context.
* Refuse a trigger event, an agent's self-modification or a subagent
  spawn by raising :class:`HookBlocked`.

Hooks run in priority order (highest first).  A hook can be
``once=True`` (fires a single time, then is removed, even when several
dispatches race), have a ``timeout`` (a hook that takes longer is
cancelled and recorded as an error) and be ``fail_closed`` (its error or
timeout blocks the action instead of being ignored).  An exception other
than :class:`HookBlocked` never propagates: it is logged and recorded in
:attr:`DispatchResult.errors`, so a broken hook can't take the runtime
down.

Example::

    from promptise.runtime.hooks import HookManager, HookEvent

    hooks = HookManager()

    @hooks.on(HookEvent.USER_PROMPT_SUBMIT)
    async def audit(ctx):
        await audit_log.write(ctx.data["prompt"])

    @hooks.on(HookEvent.SESSION_START, once=True)
    async def welcome(ctx):
        print("First session of the run!")

    # AgentProcess dispatches for you; manual dispatch works too:
    result = await hooks.dispatch(HookEvent.SESSION_START, data={"session_id": "s1"})
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class HookEvent(str, Enum):
    """Lifecycle events that a :class:`HookManager` can dispatch.

    There are 13 events.  :class:`~promptise.runtime.AgentProcess`
    dispatches each of them at the moment described below; ``ctx.data``
    holds the listed keys (secret values redacted).  Events in
    :data:`BLOCKABLE_EVENTS` can be refused by raising
    :class:`HookBlocked`; for the others a block is logged and ignored.
    They are deliberately coarse: for per-node / per-tool introspection
    of a single run, use :mod:`promptise.engine.hooks` instead.
    """

    #: The process entered RUNNING (every :meth:`start`, including
    #: automatic restarts).  Data: ``restart`` (bool), ``recovered``
    #: (what journal recovery restored, or None).  Not blockable.
    SESSION_START = "session_start"

    #: The process is about to stop (before teardown): an explicit
    #: :meth:`stop`, or a restart.  Data: ``reason`` (``"stop"`` or
    #: ``"restart"``), ``invocation_count``.  Not blockable (a stop
    #: always proceeds).
    SESSION_END = "session_end"

    #: A trigger event's prompt is about to be sent to the model.  Data:
    #: ``prompt`` (the user message; a hook may replace it), ``event_id``,
    #: ``trigger_id``, ``trigger_type``.  Blockable: the run is skipped
    #: and the event dead-lettered (``"blocked by hook"``).
    USER_PROMPT_SUBMIT = "user_prompt_submit"

    #: A tool that requires approval (``ProcessConfig.approval``) is
    #: about to be sent to the approval handler.  Data: ``tool_name``,
    #: ``arguments`` (the reviewer's redacted copy), ``request_id``.
    #: Blockable: the call is denied without asking the handler.
    PERMISSION_REQUEST = "permission_request"

    #: An approval request was denied (by the handler, a classifier rule,
    #: the gate or a hook).  Data: ``tool_name``, ``request_id``,
    #: ``reason``, ``decided_by``.  Not blockable.
    PERMISSION_DENIED = "permission_denied"

    #: An open-mode agent is about to spawn a process (``spawn_process``
    #: meta-tool).  Data: ``process_name``, ``execution_mode``,
    #: ``trigger_count``.  Blockable: the spawn is refused.
    SUBAGENT_START = "subagent_start"

    #: A process this one spawned stopped or failed.  Data:
    #: ``process_name``, ``state``.  Not blockable.
    SUBAGENT_STOP = "subagent_stop"

    #: The conversation buffer is about to drop its oldest messages
    #: (``context.conversation_max_messages``).  Data: ``buffer_size``,
    #: ``max_messages``, ``dropping`` (the messages that will be dropped,
    #: e.g. to archive them).  Not blockable (the buffer is bounded).
    PRE_COMPACT = "pre_compact"

    #: After that compaction.  Data: ``buffer_size``, ``dropped``.  Not
    #: blockable.
    POST_COMPACT = "post_compact"

    #: A file-watch trigger reported a change.  Data: the event payload
    #: (``path``, ``event_type``, ...), ``trigger_id``, ``event_id``.
    #: Blockable: the event is dropped (counted as filtered).
    FILE_CHANGED = "file_changed"

    #: The process's effective configuration changed.  Data: ``source``
    #: (``"agent"`` for an open-mode self-modification, ``"operator"``
    #: otherwise) and ``change`` (``"instructions"``, ``"tool"``,
    #: ``"mcp_server"``, ``"trigger_added"``, ``"trigger_removed"``,
    #: ``"budget"``, ``"health"``, ``"mission"``, ``"rollback"``,
    #: ``"rewind"``) plus details.  Blockable only for ``source="agent"``:
    #: the agent's change is not applied.  Operator changes always apply.
    CONFIG_CHANGE = "config_change"

    #: A trigger event is about to be queued (from a trigger or
    #: :meth:`inject`).  Data: ``event_id``, ``trigger_id``,
    #: ``trigger_type``.  Blockable: the event is dead-lettered
    #: (``"blocked by hook"``) instead of queued.
    TASK_CREATED = "task_created"

    #: A queued event finished.  Data: ``event_id``, ``trigger_type``,
    #: ``outcome`` (``"success"``, ``"retrying"``, ``"failed"``,
    #: ``"blocked"`` or ``"dead_lettered"``), ``duration_ms``, ``error``.
    #: Not blockable.
    TASK_COMPLETED = "task_completed"


#: Events whose action a hook can refuse by raising :class:`HookBlocked`
#: (see each :class:`HookEvent` for what "refuse" means).
BLOCKABLE_EVENTS: frozenset[HookEvent] = frozenset(
    {
        HookEvent.USER_PROMPT_SUBMIT,
        HookEvent.PERMISSION_REQUEST,
        HookEvent.SUBAGENT_START,
        HookEvent.FILE_CHANGED,
        HookEvent.CONFIG_CHANGE,
        HookEvent.TASK_CREATED,
    }
)


@dataclass
class HookContext:
    """Context passed to every hook callback.

    Attributes:
        event: The event that is firing.
        process_id: Identifier of the :class:`AgentProcess` dispatching
            the hook (its name). May be empty for top-level dispatch.
        timestamp: timezone.utc timestamp of dispatch.
        data: Free-form event payload. Hook implementations may read
            and (for some events, e.g. USER_PROMPT_SUBMIT) mutate this
            dict to alter the subsequent runtime behavior; the caller
            reads it back from :attr:`DispatchResult.data`.
        metadata: Additional per-dispatch metadata (``process_name``,
            ``process_id``), not typically mutated by hooks.
    """

    event: HookEvent
    process_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
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
    operation (e.g. cancel a tool call, refuse a user prompt).  Only the
    events in :data:`BLOCKABLE_EVENTS` can be blocked.
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
    timeout: float | None = None
    fail_closed: bool = False


@dataclass
class DispatchResult:
    """Outcome of a :meth:`HookManager.dispatch` call.

    Attributes:
        fired: Number of hooks that ran.
        blocked: True if any hook raised :class:`HookBlocked` (or a
            ``fail_closed`` hook failed).
        reason: Block reason, if any.
        errors: List of ``(hook_id, exception)`` tuples for hooks that
            raised an unexpected exception or timed out. These are logged
            and swallowed so one broken hook does not take down the
            runtime.
        data: The event payload after the hooks ran (hooks may mutate
            it, e.g. rewrite ``data["prompt"]``).
    """

    fired: int = 0
    blocked: bool = False
    reason: str = ""
    errors: list[tuple[str, Exception]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


def _is_async_callable(callback: Any) -> bool:
    if inspect.iscoroutinefunction(callback):
        return True
    call = getattr(callback, "__call__", None)  # noqa: B004 - async __call__ objects
    return call is not None and inspect.iscoroutinefunction(call)


class HookManager:
    """Event-keyed dispatcher for runtime lifecycle hooks.

    Supports per-event registration with optional ``once`` semantics,
    ordered dispatch by priority, per-hook timeouts, exception isolation
    (one broken hook never blocks the rest), and programmatic
    deregistration by ID or tag.

    Coroutine safety: all mutation and dispatch are guarded by an asyncio
    lock. It is safe to register hooks from inside a hook callback.  A
    hook must not await a lifecycle method of the process that
    dispatched it (``stop()``, ``start()``): schedule it with
    ``asyncio.create_task`` instead.

    Args:
        default_timeout: Seconds a hook may run when it was registered
            without a ``timeout`` (``None``: no limit, the default for a
            standalone manager; the manager an :class:`AgentProcess`
            creates uses 30 seconds).
    """

    def __init__(self, *, default_timeout: float | None = None) -> None:
        if default_timeout is not None and default_timeout <= 0:
            raise ValueError("default_timeout must be positive (or None for no limit)")
        self._hooks: dict[HookEvent, list[_Registration]] = {event: [] for event in HookEvent}
        self._lock = asyncio.Lock()
        self.default_timeout = default_timeout

    # -- Registration --

    def on(
        self,
        event: HookEvent,
        *,
        once: bool = False,
        priority: int = 0,
        tag: str | None = None,
        timeout: float | None = None,
        fail_closed: bool = False,
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
            self.register(
                event,
                fn,
                once=once,
                priority=priority,
                tag=tag,
                timeout=timeout,
                fail_closed=fail_closed,
            )
            return fn

        return decorator

    def register(
        self,
        event: HookEvent | str,
        callback: HookCallable,
        *,
        once: bool = False,
        priority: int = 0,
        tag: str | None = None,
        timeout: float | None = None,
        fail_closed: bool = False,
    ) -> str:
        """Register a hook callback for an event.

        Args:
            event: Which :class:`HookEvent` to subscribe to (or its
                value, e.g. ``"session_start"``).
            callback: Async function (or object with an async
                ``__call__``) taking a single :class:`HookContext`.
            once: If True, the hook auto-deregisters after its first
                invocation (successful or not).  It runs at most once,
                even when several dispatches of the event overlap.
            priority: Higher priority hooks run first within an event.
                Default 0. Use negative priorities to run late.
            tag: Optional string tag for grouped deregistration with
                :meth:`remove_by_tag`.
            timeout: Seconds the hook may run (default: the manager's
                ``default_timeout``).  A hook that takes longer is
                cancelled and recorded as an error.
            fail_closed: If True, an error or timeout of this hook blocks
                the action (for a blockable event) instead of being
                ignored.  Use it for hooks that enforce a policy.

        Returns:
            A hook ID suitable for passing to :meth:`remove`.

        Raises:
            TypeError: *callback* is not async.
            ValueError: *event* is not a :class:`HookEvent` value, or
                *timeout* is not positive.
        """
        event = _coerce_event(event)
        if not _is_async_callable(callback):
            raise TypeError(
                f"hook callback for {event.value!r} must be an async function, "
                f"got {type(callback).__name__}"
            )
        if timeout is not None and timeout <= 0:
            raise ValueError(f"hook timeout must be positive, got {timeout!r}")

        hook_id = f"hook-{uuid.uuid4().hex[:12]}"
        registration = _Registration(
            hook_id=hook_id,
            event=event,
            callback=callback,
            once=once,
            priority=priority,
            tag=tag,
            timeout=timeout,
            fail_closed=fail_closed,
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
            return self._remove_unlocked(hook_id)

    def _remove_unlocked(self, hook_id: str) -> bool:
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

    def has_hooks(self, event: HookEvent) -> bool:
        """Whether any hook is registered for *event* (cheap; no lock)."""
        return bool(self._hooks[event])

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
        caller (a ``fail_closed`` hook's error also blocks).  A hook that
        exceeds its timeout is cancelled and recorded as an error.
        ``HookBlocked`` short-circuits remaining hooks and returns a
        blocked result.

        Hooks with ``once=True`` are removed from the registry before they
        run, so each runs at most once, whatever happens.

        Args:
            event: The event to fire.
            process_id: Identifier of the dispatching process.
            data: Event payload. Hooks may mutate the copy they receive;
                the result is :attr:`DispatchResult.data`.
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
        result = DispatchResult(data=ctx.data)

        # Snapshot under the lock so re-entrant registrations can't mess
        # up ordering mid-dispatch; claim the one-shot hooks at the same
        # time so a concurrent dispatch can't run them a second time.
        async with self._lock:
            snapshot = list(self._hooks[event])
            for reg in snapshot:
                if reg.once:
                    self._remove_unlocked(reg.hook_id)

        for reg in snapshot:
            timeout = reg.timeout if reg.timeout is not None else self.default_timeout
            try:
                if timeout is None:
                    await reg.callback(ctx)
                else:
                    await asyncio.wait_for(reg.callback(ctx), timeout=timeout)
                result.fired += 1
            except HookBlocked as exc:
                result.fired += 1
                result.blocked = True
                result.reason = exc.reason
                logger.info("hook %s blocked event %s: %s", reg.hook_id, event.value, exc.reason)
                break
            except asyncio.TimeoutError:
                error = TimeoutError(f"hook {reg.hook_id} exceeded its timeout of {timeout}s")
                logger.warning("hook %s timed out on event %s", reg.hook_id, event.value)
                result.errors.append((reg.hook_id, error))
                if reg.fail_closed:
                    result.blocked = True
                    result.reason = f"hook failed (fail_closed): {error}"
                    break
            except Exception as exc:  # noqa: BLE001 — hooks are untrusted
                logger.exception("hook %s raised on event %s", reg.hook_id, event.value)
                result.errors.append((reg.hook_id, exc))
                if reg.fail_closed:
                    result.blocked = True
                    result.reason = f"hook failed (fail_closed): {type(exc).__name__}: {exc}"
                    break

        # The hooks may have replaced ctx.data (ShellHook does it in place).
        result.data = ctx.data
        return result

    def __repr__(self) -> str:
        total = sum(len(b) for b in self._hooks.values())
        return f"HookManager(registered={total})"


def _coerce_event(event: HookEvent | str) -> HookEvent:
    if isinstance(event, HookEvent):
        return event
    try:
        return HookEvent(event)
    except ValueError:
        valid = ", ".join(e.value for e in HookEvent)
        raise ValueError(f"Unknown hook event {event!r}; valid events are: {valid}") from None


__all__ = [
    "BLOCKABLE_EVENTS",
    "DispatchResult",
    "HookBlocked",
    "HookCallable",
    "HookContext",
    "HookEvent",
    "HookManager",
]
