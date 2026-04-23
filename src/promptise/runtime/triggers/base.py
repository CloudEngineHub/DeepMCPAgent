"""Base trigger protocol and TriggerEvent dataclass.

All trigger implementations must satisfy the :class:`BaseTrigger` protocol:

* ``start()`` / ``stop()`` — lifecycle management
* ``wait_for_next()`` — async generator that yields :class:`TriggerEvent`
  objects whenever the trigger fires

The :class:`TriggerEvent` carries metadata about what caused the firing
(cron time, webhook body, file path, etc.) and is placed on the
:class:`AgentProcess` trigger queue for processing.
"""

from __future__ import annotations

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


@runtime_checkable
class BaseTrigger(Protocol):
    """Protocol for all trigger implementations.

    Triggers produce :class:`TriggerEvent` objects that cause the agent
    process to invoke the agent.  They run as background tasks and must
    be safely cancellable.
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
