"""EventBus and MessageBroker trigger adapters.

:class:`EventTrigger` fires when a subscribed event matches a pattern,
converting the event payload into :class:`TriggerEvent` objects.

:class:`MessageTrigger` does the same for message topic subscriptions.

Both use an internal ``asyncio.Queue`` to decouple event delivery from
trigger consumption.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from .base import TriggerEvent

logger = logging.getLogger(__name__)


class EventTrigger:
    """Trigger that fires when an EventBus event matches.

    Wraps an EventBus subscription and converts matching events into
    :class:`TriggerEvent` objects via an internal queue.

    Args:
        event_bus: An object with ``subscribe(event_type, handler)`` and ``unsubscribe(event_type, handler)`` methods.
        event_type: Event type string to listen for (e.g. ``"task.completed"``).
        source_filter: Optional source filter — only events from this
            source will fire the trigger.
        trigger_id: Unique identifier (auto-generated if not provided).
    """

    def __init__(
        self,
        event_bus: Any,
        event_type: str,
        *,
        source_filter: str | None = None,
        trigger_id: str | None = None,
    ) -> None:
        self.trigger_id = trigger_id or f"event-{uuid4().hex[:8]}"
        self._event_bus = event_bus
        self._event_type = event_type
        self._source_filter = source_filter
        self._queue: asyncio.Queue[TriggerEvent] = asyncio.Queue(maxsize=100)
        self._running = False

    async def _handler(self, event: Any) -> None:
        """EventBus subscriber callback."""
        if not self._running:
            return
        if self._source_filter and getattr(event, "source", "") != self._source_filter:
            return

        trigger_event = TriggerEvent(
            trigger_id=self.trigger_id,
            trigger_type="event",
            payload={
                "event_type": self._event_type,
                "event_id": getattr(event, "event_id", ""),
                "source": getattr(event, "source", ""),
                "data": getattr(event, "data", {}),
            },
        )
        try:
            self._queue.put_nowait(trigger_event)
        except asyncio.QueueFull:
            logger.warning("EventTrigger %s: queue full, dropping event", self.trigger_id)

    async def start(self) -> None:
        """Subscribe to the EventBus."""
        self._running = True
        # EventBus.subscribe accepts EventType enum or string
        await self._event_bus.subscribe(self._event_type, self._handler)
        logger.info(
            "EventTrigger %s started: listening for %s",
            self.trigger_id,
            self._event_type,
        )

    async def stop(self) -> None:
        """Unsubscribe and drain the queue."""
        self._running = False
        try:
            await self._event_bus.unsubscribe(self._event_type, self._handler)
        except Exception:  # noqa: BLE001
            logger.debug(
                "EventTrigger %s: unsubscribe failed (bus may be stopped)", self.trigger_id
            )
        # Unblock any waiting consumers
        try:
            self._queue.put_nowait(
                TriggerEvent(
                    trigger_id=self.trigger_id, trigger_type="event", metadata={"_stop": True}
                )
            )
        except asyncio.QueueFull:
            pass
        logger.info("EventTrigger %s stopped", self.trigger_id)

    async def wait_for_next(self) -> TriggerEvent:
        """Block until a matching event arrives.

        Raises:
            asyncio.CancelledError: If the trigger is stopped.
        """
        while True:
            event = await self._queue.get()
            if event.metadata.get("_stop"):
                raise asyncio.CancelledError("Trigger stopped")
            return event

    def __repr__(self) -> str:
        return (
            f"EventTrigger(id={self.trigger_id!r}, "
            f"event_type={self._event_type!r}, "
            f"running={self._running})"
        )


class MessageTrigger:
    """Trigger that fires when a message arrives on a broker topic.

    Wraps a message broker subscription.

    Args:
        broker: An object with ``subscribe(topic, handler)`` and ``unsubscribe(topic, subscription_id)`` methods.
        topic: Topic to subscribe to (supports wildcards like ``reports.*``).
        trigger_id: Unique identifier (auto-generated if not provided).
    """

    def __init__(
        self,
        broker: Any,
        topic: str,
        *,
        trigger_id: str | None = None,
    ) -> None:
        self.trigger_id = trigger_id or f"message-{uuid4().hex[:8]}"
        self._broker = broker
        self._topic = topic
        self._queue: asyncio.Queue[TriggerEvent] = asyncio.Queue(maxsize=100)
        self._running = False
        self._subscription_id: str | None = None

    async def _handler(self, message: Any) -> None:
        """MessageBroker subscriber callback."""
        if not self._running:
            return

        trigger_event = TriggerEvent(
            trigger_id=self.trigger_id,
            trigger_type="message",
            payload={
                "topic": self._topic,
                "message_id": getattr(message, "message_id", ""),
                "sender": getattr(message, "sender", ""),
                "content": getattr(message, "content", str(message)),
            },
        )
        try:
            self._queue.put_nowait(trigger_event)
        except asyncio.QueueFull:
            logger.warning(
                "MessageTrigger %s: queue full, dropping message",
                self.trigger_id,
            )

    async def start(self) -> None:
        """Subscribe to the MessageBroker topic."""
        self._running = True
        self._subscription_id = await self._broker.subscribe(self._topic, self._handler)
        logger.info(
            "MessageTrigger %s started: topic=%s",
            self.trigger_id,
            self._topic,
        )

    async def stop(self) -> None:
        """Unsubscribe from the topic."""
        self._running = False
        if self._subscription_id:
            try:
                await self._broker.unsubscribe(self._subscription_id)
            except Exception:  # noqa: BLE001
                logger.debug("MessageTrigger %s: unsubscribe failed", self.trigger_id)
        # Unblock waiters
        try:
            self._queue.put_nowait(
                TriggerEvent(
                    trigger_id=self.trigger_id, trigger_type="message", metadata={"_stop": True}
                )
            )
        except asyncio.QueueFull:
            pass
        logger.info("MessageTrigger %s stopped", self.trigger_id)

    async def wait_for_next(self) -> TriggerEvent:
        """Block until a message arrives on the topic.

        Raises:
            asyncio.CancelledError: If the trigger is stopped.
        """
        while True:
            event = await self._queue.get()
            if event.metadata.get("_stop"):
                raise asyncio.CancelledError("Trigger stopped")
            return event

    def __repr__(self) -> str:
        return (
            f"MessageTrigger(id={self.trigger_id!r}, "
            f"topic={self._topic!r}, "
            f"running={self._running})"
        )
