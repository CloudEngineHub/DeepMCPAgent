"""Process lifecycle state machine.

Defines the valid states an :class:`AgentProcess` can be in and the
legal transitions between them.  The :class:`ProcessLifecycle` class
provides an auditable state machine, safe for concurrent use from
asyncio tasks of one event loop (it is not thread-safe), that records
every transition with timestamps and reasons.

Transitions (as :class:`~promptise.runtime.process.AgentProcess` uses
them)::

    CREATED   ──► STARTING, STOPPED (stop() before start())
    STARTING  ──► RUNNING, FAILED, STOPPING
    RUNNING   ──► SUSPENDED, STOPPING, FAILED
    SUSPENDED ──► RUNNING, STOPPING, FAILED
    STOPPING  ──► STOPPED, FAILED
    STOPPED   ──► STARTING
    FAILED    ──► STARTING (start() / automatic restart), STOPPING (stop())

* ``SUSPENDED``: processing paused.  Entered by
  :meth:`~AgentProcess.suspend` (an operator, the API or the dashboard),
  a budget or health action, or ``idle_timeout``.  Trigger events keep
  queueing (in order) and run after :meth:`~AgentProcess.resume`; a
  process suspended for ``idle_timeout`` resumes by itself when the next
  trigger event arrives.
* ``FAILED``: too many consecutive failures, or a failed start.
  :meth:`~AgentProcess.start` (or an automatic restart) goes back to
  ``STARTING``; :meth:`~AgentProcess.stop` goes to ``STOPPING`` →
  ``STOPPED``.
* ``AWAITING``: reserved.  The value and its transitions are kept for
  backward compatibility, but :class:`AgentProcess` never enters it: an
  idle process is ``RUNNING`` (or ``SUSPENDED`` after ``idle_timeout``).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .exceptions import ProcessStateError

logger = logging.getLogger(__name__)

#: Callback invoked after every successful transition (sync or async).
TransitionListener = Callable[["ProcessTransition"], "Awaitable[None] | None"]


class ProcessState(str, Enum):
    """Agent process lifecycle states.

    ``AWAITING`` is reserved: :class:`~promptise.runtime.process.AgentProcess`
    never enters it (see the module docstring).
    """

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    SUSPENDED = "suspended"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    AWAITING = "awaiting"


#: Valid transitions: current state → set of allowed target states.
VALID_TRANSITIONS: dict[ProcessState, frozenset[ProcessState]] = {
    ProcessState.CREATED: frozenset({ProcessState.STARTING, ProcessState.STOPPED}),
    ProcessState.STARTING: frozenset(
        {ProcessState.RUNNING, ProcessState.FAILED, ProcessState.STOPPING}
    ),
    ProcessState.RUNNING: frozenset(
        {
            ProcessState.SUSPENDED,
            ProcessState.STOPPING,
            ProcessState.FAILED,
            ProcessState.AWAITING,
        }
    ),
    ProcessState.SUSPENDED: frozenset(
        {ProcessState.RUNNING, ProcessState.STOPPING, ProcessState.FAILED}
    ),
    ProcessState.AWAITING: frozenset(
        {ProcessState.RUNNING, ProcessState.STOPPING, ProcessState.FAILED}
    ),
    ProcessState.STOPPING: frozenset({ProcessState.STOPPED, ProcessState.FAILED}),
    ProcessState.STOPPED: frozenset({ProcessState.STARTING}),
    ProcessState.FAILED: frozenset({ProcessState.STARTING, ProcessState.STOPPING}),
}


class StateError(ProcessStateError, ValueError):
    """Raised when an invalid state transition is attempted.

    One exception type for invalid transitions: it is a
    :class:`~promptise.runtime.exceptions.ProcessStateError` (so
    ``except RuntimeBaseError`` catches it) and, as before, a
    :class:`ValueError`.

    Attributes:
        current: The state the transition started from.
        target: The rejected target state.
        process_id: The owning process (its name), when known.
        current_state: ``current.value`` (as on ``ProcessStateError``).
        attempted_state: ``target.value``.
    """

    def __init__(
        self,
        current: ProcessState,
        target: ProcessState,
        *,
        process_id: str | None = None,
    ) -> None:
        self.current = current
        self.target = target
        allowed = sorted(s.value for s in VALID_TRANSITIONS.get(current, set()))
        owner = f"Process {process_id!r}: " if process_id else ""
        super().__init__(
            process_id,
            current.value,
            target.value,
            message=(
                f"{owner}Cannot transition from {current.value!r} to {target.value!r}. "
                f"Allowed targets: {allowed}"
            ),
        )


@dataclass
class ProcessTransition:
    """Record of a single state transition.

    Attributes:
        from_state: Previous state.
        to_state: New state.
        timestamp: When the transition occurred (timezone.utc).
        reason: Human-readable reason for the transition.
        metadata: Additional context (e.g. error details).
    """

    from_state: ProcessState
    to_state: ProcessState
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "timestamp": self.timestamp.isoformat(),
            "reason": self.reason,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProcessTransition:
        """Deserialize from a dict."""
        return cls(
            from_state=ProcessState(data["from_state"]),
            to_state=ProcessState(data["to_state"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            reason=data.get("reason", ""),
            metadata=data.get("metadata", {}),
        )


class ProcessLifecycle:
    """State machine for process lifecycle.

    Tracks the current state, validates transitions against
    :data:`VALID_TRANSITIONS`, and records an audit trail of all
    transitions.  Transitions are serialised with an :class:`asyncio.Lock`,
    so concurrent tasks of one event loop can use it; it is not
    thread-safe.

    Args:
        initial: Starting state (default :attr:`ProcessState.CREATED`).
        process_id: Owner name shown in :class:`StateError` messages.

    Example::

        lc = ProcessLifecycle()
        assert lc.state == ProcessState.CREATED

        await lc.transition(ProcessState.STARTING, reason="user requested")
        await lc.transition(ProcessState.RUNNING)

        assert len(lc.history) == 2
    """

    def __init__(
        self,
        initial: ProcessState = ProcessState.CREATED,
        *,
        process_id: str | None = None,
    ) -> None:
        self._state = initial
        self._process_id = process_id
        self._history: list[ProcessTransition] = []
        self._lock = asyncio.Lock()
        self._listeners: list[TransitionListener] = []

    def add_listener(self, listener: TransitionListener) -> None:
        """Register a callback invoked after every successful transition.

        Listeners run in registration order, outside the state lock, so
        they may inspect :attr:`state` (and even trigger further
        transitions).  A listener that raises is logged and skipped; it
        never undoes the transition.

        Args:
            listener: Sync or async callable receiving the
                :class:`ProcessTransition`.
        """
        self._listeners.append(listener)

    @property
    def state(self) -> ProcessState:
        """Current process state."""
        return self._state

    @property
    def history(self) -> list[ProcessTransition]:
        """Chronological list of all transitions."""
        return list(self._history)

    def can_transition(self, target: ProcessState) -> bool:
        """Check whether transitioning to *target* is valid."""
        return target in VALID_TRANSITIONS.get(self._state, frozenset())

    async def transition(
        self,
        target: ProcessState,
        *,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ProcessTransition:
        """Attempt a state transition.

        Args:
            target: Desired next state.
            reason: Human-readable explanation.
            metadata: Extra context to store with the transition.

        Returns:
            The recorded :class:`ProcessTransition`.

        Raises:
            StateError: If the transition is not valid from the current state.
        """
        async with self._lock:
            if not self.can_transition(target):
                raise StateError(self._state, target, process_id=self._process_id)

            transition = ProcessTransition(
                from_state=self._state,
                to_state=target,
                reason=reason,
                metadata=metadata or {},
            )
            self._state = target
            self._history.append(transition)

        for listener in list(self._listeners):
            try:
                result = listener(transition)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception(
                    "Lifecycle listener failed for %s -> %s",
                    transition.from_state.value,
                    transition.to_state.value,
                )
        return transition

    def snapshot(self) -> dict[str, Any]:
        """Serializable snapshot of current state and full history."""
        return {
            "state": self._state.value,
            "history": [t.to_dict() for t in self._history],
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> ProcessLifecycle:
        """Reconstruct a :class:`ProcessLifecycle` from a snapshot dict."""
        lc = cls(initial=ProcessState(data["state"]))
        lc._history = [ProcessTransition.from_dict(t) for t in data.get("history", [])]
        return lc

    def __repr__(self) -> str:
        return f"ProcessLifecycle(state={self._state.value!r}, transitions={len(self._history)})"
