"""Base trigger protocol and TriggerEvent dataclass.

All trigger implementations must satisfy the :class:`BaseTrigger` protocol:

* ``start()`` / ``stop()`` — lifecycle management
* ``wait_for_next()`` — async generator that yields :class:`TriggerEvent`
  objects whenever the trigger fires

:func:`config_trigger_ids` gives a process's config triggers their stable
IDs (used by :class:`~promptise.runtime.process.AgentProcess` and the
orchestration API).

The :class:`TriggerEvent` carries metadata about what caused the firing
(cron time, webhook body, file path, etc.) and is placed on the
:class:`AgentProcess` trigger queue for processing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4


@dataclass
class TriggerEvent:
    """Event produced by a trigger.

    Attributes:
        trigger_id: Which trigger produced this event.
        trigger_type: Type of the trigger (``cron``, ``webhook``, etc.).
        event_id: Unique event identifier.
        timestamp: When the event was produced (timezone.utc).
        payload: Trigger-specific data (cron time, webhook body,
            file path, event data, message content).
        metadata: Additional context.
    """

    trigger_id: str
    trigger_type: str = ""
    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "trigger_id": self.trigger_id,
            "trigger_type": self.trigger_type,
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "payload": self.payload,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TriggerEvent:
        """Deserialize from a dict."""
        return cls(
            trigger_id=data["trigger_id"],
            trigger_type=data.get("trigger_type", ""),
            event_id=data.get("event_id", str(uuid4())),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            payload=data.get("payload", {}),
            metadata=data.get("metadata", {}),
        )


def drain_queue(queue: asyncio.Queue[TriggerEvent]) -> list[TriggerEvent]:
    """Take every event waiting in *queue*, without the stop sentinels.

    For a trigger's ``drain_pending()``: what a stopped trigger still held,
    so the process can dead-letter (or, on a restart, requeue) it instead
    of losing it.
    """
    events: list[TriggerEvent] = []
    while True:
        try:
            event = queue.get_nowait()
        except asyncio.QueueEmpty:
            return events
        if not (event.metadata and event.metadata.get("_stop")):
            events.append(event)


@runtime_checkable
class BaseTrigger(Protocol):
    """Protocol for all trigger implementations.

    Triggers produce :class:`TriggerEvent` objects that cause the agent
    process to invoke the agent.  They run as background tasks and must
    be safely cancellable.

    A trigger that buffers events may also implement
    ``drain_pending() -> list[TriggerEvent]``: after :meth:`stop`, the
    process takes those events and dead-letters them (on a restart: queues
    them for the restarted process) instead of losing them.
    """

    trigger_id: str

    async def start(self) -> None:
        """Start listening for trigger conditions."""
        ...

    async def stop(self) -> None:
        """Stop the trigger and release resources."""
        ...

    async def wait_for_next(self) -> TriggerEvent:
        """Block until the next trigger event occurs.

        Returns:
            The next :class:`TriggerEvent`.

        Raises:
            asyncio.CancelledError: If the trigger is stopped.
            TriggerError: If the trigger encounters a fatal error.
        """
        ...


# ----------------------------------------------------------------------
# Stable trigger IDs
# ----------------------------------------------------------------------


#: ``custom_config`` keys whose values are masked (in trigger IDs and in
#: ``GET .../triggers``).
CREDENTIAL_KEY = re.compile(
    r"secret|token|passw|pwd|api[-_]?key|auth|credential|private[-_]?key|signature|cookie",
    re.I,
)

#: The user-info part of a URL (``scheme://user:password@``).
_URL_USERINFO = re.compile(r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s]+@")

MASK = "**********"


def mask_secret_values(value: Any, *, by_key: bool = True) -> Any:
    """*value* with credential-looking keys and URL user info masked.

    The value of a key that looks like a credential (``token``,
    ``secret``, ``password``, ``api_key``, ``auth``...) becomes
    ``**********`` (unless *by_key* is false), and so does the user info
    of a URL in any string (``postgres://app:pw@db/x`` becomes
    ``postgres://**********@db/x``).
    """
    if isinstance(value, Mapping):
        return {
            k: MASK
            if by_key and isinstance(k, str) and CREDENTIAL_KEY.search(k) and v not in (None, "")
            else mask_secret_values(v, by_key=by_key)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [mask_secret_values(v, by_key=by_key) for v in value]
    if isinstance(value, str) and "@" in value:
        return _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{MASK}@", value)
    return value


def mask_trigger_config(data: Mapping[str, Any]) -> dict[str, Any]:
    """A dumped :class:`TriggerConfig` with its secrets masked.

    ``custom_config`` is masked by key name and URL user info; the
    trigger's own fields by URL user info only (``hmac_secret`` is a
    ``SecretStr``, already masked when dumped).
    """
    masked = {k: mask_secret_values(v, by_key=False) for k, v in data.items()}
    if data.get("custom_config"):
        masked["custom_config"] = mask_secret_values(data["custom_config"])
    return masked


def _callable_name(func: Any) -> str:
    """``module.qualname`` of a filter callable: the same in every program run.

    ``repr()`` of a function includes its memory address, which changes
    every time the program starts.
    """
    target = func if hasattr(func, "__qualname__") else type(func)
    module = getattr(target, "__module__", None) or ""
    name = getattr(target, "__qualname__", None) or getattr(target, "__name__", None)
    return f"{module}.{name}" if name else type(func).__name__


def _trigger_fingerprint(trigger_config: Any) -> str:
    """A digest of a trigger config's content (secrets masked)."""
    try:
        data = trigger_config.model_dump(mode="json", exclude={"filter_expression"})
        expr = getattr(trigger_config, "filter_expression", None)
        data["filter_expression"] = (
            expr if isinstance(expr, str) or expr is None else f"callable:{_callable_name(expr)}"
        )
        text = json.dumps(mask_trigger_config(data), sort_keys=True, default=str)
    except Exception:
        text = repr(trigger_config)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def config_trigger_ids(triggers: Iterable[Any]) -> list[str]:
    """Stable IDs for a process's config triggers, in list order.

    An ID is ``<type>-<digest of the trigger's config>`` (secret values are
    masked before hashing), so it does not depend on the trigger's
    position: removing one trigger leaves the others' IDs unchanged, and
    the IDs are the same after a restart.  Identical configs get ``-2``,
    ``-3``... suffixes.

    :class:`~promptise.runtime.process.AgentProcess` gives each running
    config trigger this ID (``trigger.trigger_id`` and the ``trigger_id``
    of its events), and the orchestration API uses it in its trigger
    routes.
    """
    ids: list[str] = []
    seen: dict[str, int] = {}
    for tc in triggers:
        base = f"{getattr(tc, 'type', 'trigger')}-{_trigger_fingerprint(tc)}"
        seen[base] = seen.get(base, 0) + 1
        ids.append(base if seen[base] == 1 else f"{base}-{seen[base]}")
    return ids
