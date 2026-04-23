"""Webhook and event notification system for Promptise agents.

Emits structured notifications when significant things happen during
agent execution — invocation complete, tool failure, guardrail block,
budget exceeded, process failed.  Events are delivered to configurable
sinks (webhooks, callbacks, logs) via a fire-and-forget async queue.

Example::

    from promptise import build_agent, EventNotifier, WebhookSink, CallbackSink

    notifier = EventNotifier(sinks=[
        WebhookSink(
            url="https://hooks.slack.com/services/...",
            events=["invocation.error", "budget.exceeded"],
        ),
        CallbackSink(lambda event: print(event.event_type)),
    ])

    agent = await build_agent(..., events=notifier)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac_mod
import json
import logging
import re as _re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("promptise.events")

__all__ = [
    "AgentEvent",
    "EventSink",
    "EventNotifier",
    "WebhookSink",
    "CallbackSink",
    "LogSink",
    "EventBusSink",
    "default_pii_sanitizer",
]


# ---------------------------------------------------------------------------
# PII / credential redaction (shared by all sinks + observability)
# ---------------------------------------------------------------------------

_PII_PATTERNS: list[tuple[_re.Pattern[str], str]] = [
    (_re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), "[CARD]"),
    (_re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (_re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "[EMAIL]"),
    (_re.compile(r"\b(sk-[a-zA-Z0-9]{20,})\b"), "[API_KEY]"),
    (_re.compile(r"\b(AKIA[A-Z0-9]{16})\b"), "[AWS_KEY]"),
    (_re.compile(r"\b(ghp_[a-zA-Z0-9]{36})\b"), "[GITHUB_TOKEN]"),
    (_re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"), "Bearer [REDACTED]"),
    (_re.compile(r"://[^:]+:[^@]+@"), "://[REDACTED]@"),
]


def default_pii_sanitizer(data: dict[str, Any]) -> dict[str, Any]:
    """Redact PII and credentials from a data dictionary.

    Serialises the dict to JSON, applies regex patterns, and
    deserialises back.  Safe to call on any dict — returns the
    original on serialization failure.

    Args:
        data: Arbitrary dict (event payload, observability metadata, etc.).

    Returns:
        A new dict with sensitive values replaced by placeholders.
    """
    try:
        text = json.dumps(data, default=str)
        for pattern, replacement in _PII_PATTERNS:
            text = pattern.sub(replacement, text)
        return json.loads(text)
    except Exception:
        return data


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class AgentEvent:
    """A structured notification event from the Promptise framework.

    Attributes:
        event_type: Dotted event name (e.g. ``"invocation.complete"``).
        severity: One of ``"info"``, ``"warning"``, ``"error"``, ``"critical"``.
        timestamp: When the event occurred (``time.time()``).
        agent_id: Agent or process identifier.
        user_id: User who triggered the action (from CallerContext).
        session_id: Conversation session ID if applicable.
        data: Event-specific payload (tool name, error message, etc.).
        metadata: Agent configuration, model ID, etc.
    """

    event_type: str
    severity: str = "info"
    timestamp: float = field(default_factory=time.time)
    agent_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "event_type": self.event_type,
            "severity": self.severity,
            "timestamp": self.timestamp,
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "data": self.data,
            "metadata": self.metadata,
        }

    def compute_hmac(self, secret: str) -> str:
        """Compute HMAC-SHA256 signature for webhook verification."""
        payload = json.dumps(self.to_dict(), sort_keys=True, default=str)
        return _hmac_mod.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Severity levels
# ---------------------------------------------------------------------------

SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2, "critical": 3}


# ---------------------------------------------------------------------------
# Sink protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EventSink(Protocol):
    """Protocol for event notification sinks.

    Sinks receive events from the :class:`EventNotifier` and deliver
    them to external systems (webhooks, logs, callbacks, etc.).
    """

    async def emit(self, event: AgentEvent) -> None:
        """Deliver a single event."""
        ...


# ---------------------------------------------------------------------------
# Built-in sinks
# ---------------------------------------------------------------------------


class WebhookSink:
    """Deliver events via HTTP POST to a webhook URL.

    Features: HMAC-SHA256 signing, retry with exponential backoff,
    SSRF protection, per-event filtering, payload redaction.

    Args:
        url: Webhook URL to POST events to.
        events: Event types to subscribe to (``None`` = all events).
        headers: Custom HTTP headers (e.g. auth tokens).
        secret: HMAC secret for signing payloads.  If not provided,
            a random secret is generated.
        max_retries: Maximum retry attempts on failure.
        retry_delay: Initial retry delay in seconds (doubles each retry).
        redact_sensitive: Scan payloads for PII/credentials before sending.
        min_severity: Minimum severity level to emit.
    """

    def __init__(
        self,
        url: str,
        *,
        events: list[str] | None = None,
        headers: dict[str, str] | None = None,
        secret: str | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        redact_sensitive: bool = True,
        min_severity: str | None = None,
        transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        # SSRF protection
        try:
            from promptise.mcp.server._openapi import _validate_url_not_private

            _validate_url_not_private(url)
        except (ImportError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise

        self._url = url
        self._events = set(events) if events else None
        self._headers = headers or {}
        self._secret = secret or secrets.token_hex(32)
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._redact_sensitive = redact_sensitive
        self._min_severity = min_severity
        self._transform = transform
        self._client: Any = None  # Lazy httpx.AsyncClient

    def _should_emit(self, event: AgentEvent) -> bool:
        """Check if this sink should process the event."""
        if self._events and event.event_type not in self._events:
            return False
        if self._min_severity:
            event_level = SEVERITY_ORDER.get(event.severity, 0)
            min_level = SEVERITY_ORDER.get(self._min_severity, 0)
            if event_level < min_level:
                return False
        return True

    def _redact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Redact sensitive data from the payload before sending.

        Delegates to :func:`default_pii_sanitizer` for the ``data`` field.
        """
        if not self._redact_sensitive:
            return payload
        payload = dict(payload)
        if "data" in payload:
            payload["data"] = default_pii_sanitizer(
                payload["data"] if isinstance(payload["data"], dict) else {"_raw": payload["data"]}
            )
        return payload

    async def emit(self, event: AgentEvent) -> None:
        """POST the event to the webhook URL with retries."""
        if not self._should_emit(event):
            return

        try:
            import httpx
        except ImportError:
            logger.warning("httpx not installed — WebhookSink cannot deliver events")
            return

        payload = event.to_dict()
        payload = self._redact_payload(payload)

        # Apply custom transform (e.g., PagerDuty/Slack format)
        if self._transform is not None:
            try:
                payload = self._transform(payload)
            except Exception as exc:
                logger.warning("WebhookSink: transform failed: %s", exc)
                return

        # HMAC is computed on the FINAL payload (after redaction + transform)
        # so webhook receivers can verify integrity of the actual body received.
        payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode()
        signature = _hmac_mod.new(self._secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Promptise-Signature": signature,
            "X-Promptise-Event": event.event_type,
            **self._headers,
        }

        delay = self._retry_delay
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10)
        client = self._client
        for attempt in range(self._max_retries + 1):
            try:
                resp = await client.post(
                    self._url,
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                return  # Success
            except Exception as exc:
                if attempt < self._max_retries:
                    logger.debug(
                        "WebhookSink: attempt %d failed for %s: %s, retrying in %.1fs",
                        attempt + 1,
                        event.event_type,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay *= 2  # Exponential backoff
                else:
                    logger.warning(
                        "WebhookSink: failed to deliver %s after %d attempts: %s",
                        event.event_type,
                        self._max_retries + 1,
                        exc,
                    )

    async def close(self) -> None:
        """Release the persistent HTTP connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class CallbackSink:
    """Deliver events to a Python callable.

    Args:
        callback: Async or sync callable that receives an :class:`AgentEvent`.
        events: Event types to subscribe to (``None`` = all events).
        min_severity: Minimum severity level to emit.
    """

    def __init__(
        self,
        callback: Callable[..., Any],
        *,
        events: list[str] | None = None,
        min_severity: str | None = None,
    ) -> None:
        self._callback = callback
        self._events = set(events) if events else None
        self._min_severity = min_severity

    async def emit(self, event: AgentEvent) -> None:
        """Call the callback with the event."""
        if self._events and event.event_type not in self._events:
            return
        if self._min_severity:
            if SEVERITY_ORDER.get(event.severity, 0) < SEVERITY_ORDER.get(self._min_severity, 0):
                return

        try:
            result = self._callback(event)
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                await result
        except Exception as exc:
            logger.warning("CallbackSink: handler error: %s", exc)


class LogSink:
    """Deliver events to Python's logging system.

    Args:
        events: Event types to subscribe to (``None`` = all events).
        logger_name: Logger name (default: ``"promptise.events"``).
        min_severity: Minimum severity level to emit.
    """

    _SEVERITY_TO_LEVEL = {
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }

    def __init__(
        self,
        *,
        events: list[str] | None = None,
        logger_name: str = "promptise.events",
        min_severity: str | None = None,
    ) -> None:
        self._events = set(events) if events else None
        self._logger = logging.getLogger(logger_name)
        self._min_severity = min_severity

    async def emit(self, event: AgentEvent) -> None:
        """Log the event as a structured JSON line."""
        if self._events and event.event_type not in self._events:
            return
        if self._min_severity:
            if SEVERITY_ORDER.get(event.severity, 0) < SEVERITY_ORDER.get(self._min_severity, 0):
                return

        level = self._SEVERITY_TO_LEVEL.get(event.severity, logging.INFO)
        self._logger.log(
            level,
            "%s [%s] %s",
            event.event_type,
            event.severity,
            json.dumps(event.data, default=str),
        )


class EventBusSink:
    """Bridge events to the runtime's EventBus for inter-process notifications.

    Args:
        event_bus: Any object with an ``emit(event_type, data)`` method.
        events: Event types to subscribe to (``None`` = all events).
    """

    def __init__(
        self,
        event_bus: Any,
        *,
        events: list[str] | None = None,
    ) -> None:
        self._bus = event_bus
        self._events = set(events) if events else None

    async def emit(self, event: AgentEvent) -> None:
        """Publish the event to the EventBus."""
        if self._events and event.event_type not in self._events:
            return

        try:
            emit_fn = getattr(self._bus, "emit", None)
            if emit_fn is None:
                return
            result = emit_fn(event.event_type, event.to_dict())
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                await result
        except Exception as exc:
            logger.warning("EventBusSink: delivery error: %s", exc)


# ---------------------------------------------------------------------------
# EventNotifier — the central coordinator
# ---------------------------------------------------------------------------


class EventNotifier:
    """Central event coordinator that routes events to configured sinks.

    Events are placed on an async queue and delivered by a background
    task.  The agent never blocks waiting for event delivery.

    Args:
        sinks: List of :class:`EventSink` implementations.
        max_queue_size: Maximum events in the delivery queue.
            When full, new events are dropped with a warning.

    Example::

        notifier = EventNotifier(sinks=[
            WebhookSink("https://hooks.slack.com/...", events=["invocation.error"]),
            CallbackSink(my_handler),
        ])
        await notifier.start()
        notifier.emit_sync(AgentEvent(event_type="invocation.start", severity="info"))
        await notifier.stop()
    """

    def __init__(
        self,
        sinks: list[EventSink],
        *,
        max_queue_size: int = 1000,
    ) -> None:
        if not sinks:
            raise ValueError("EventNotifier requires at least one sink")
        self._sinks = list(sinks)
        self._queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue(maxsize=max_queue_size)
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._max_queue_size = max_queue_size

    async def start(self) -> None:
        """Start the background event delivery task."""
        if self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._drain_loop())
        logger.info("EventNotifier started with %d sink(s)", len(self._sinks))

    async def stop(self) -> None:
        """Drain remaining events and stop the background task."""
        if not self._started:
            return
        self._started = False
        # Signal the drain loop to stop
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        logger.info("EventNotifier stopped")

    async def emit(self, event: AgentEvent) -> None:
        """Queue an event for delivery (non-blocking).

        If the queue is full, the event is dropped with a warning log.
        """
        if not self._started:
            # Auto-start if not started yet
            await self.start()
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "EventNotifier: queue full (%d), dropping %s event",
                self._max_queue_size,
                event.event_type,
            )

    def emit_sync(self, event: AgentEvent) -> None:
        """Queue an event from a synchronous context.

        Used by the LangChain callback handler (which is synchronous).
        If the queue is full, the event is silently dropped.
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Track dropped events for observability
            self._dropped_count = getattr(self, "_dropped_count", 0) + 1
            if self._dropped_count % 100 == 1:
                logger.warning(
                    "EventNotifier: queue full, %d event(s) dropped", self._dropped_count
                )
        except Exception:
            pass  # Never block or raise in sync context

    async def _drain_loop(self) -> None:
        """Background task that drains the queue and delivers to sinks."""
        while True:
            try:
                event = await self._queue.get()
                if event is None:
                    # Drain remaining events before stopping
                    while not self._queue.empty():
                        remaining = self._queue.get_nowait()
                        if remaining is not None:
                            await self._deliver(remaining)
                    break
                await self._deliver(event)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("EventNotifier drain error: %s", exc)

    async def _deliver(self, event: AgentEvent) -> None:
        """Deliver an event to all sinks (sink failures are isolated)."""
        for sink in self._sinks:
            try:
                await sink.emit(event)
            except Exception as exc:
                logger.warning(
                    "EventNotifier: sink %s failed for %s: %s",
                    type(sink).__name__,
                    event.event_type,
                    exc,
                )


# ---------------------------------------------------------------------------
# Helper for emission points
# ---------------------------------------------------------------------------


def emit_event(
    notifier: EventNotifier | None,
    event_type: str,
    severity: str = "info",
    data: dict[str, Any] | None = None,
    *,
    agent_id: str | None = None,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit an event from any code path (null-safe, sync-safe).

    Reads ``user_id`` from the current :class:`CallerContext` if available.
    Does nothing if ``notifier`` is None.

    Args:
        notifier: The :class:`EventNotifier` instance (or None to no-op).
        event_type: Dotted event name.
        severity: Event severity level.
        data: Event-specific payload.
        agent_id: Agent or process identifier.
        session_id: Conversation session ID.
        metadata: Additional context metadata.
    """
    if notifier is None:
        return

    user_id: str | None = None
    try:
        from .agent import get_current_caller

        caller = get_current_caller()
        if caller is not None:
            user_id = getattr(caller, "user_id", None)
    except (ImportError, Exception):
        pass

    event = AgentEvent(
        event_type=event_type,
        severity=severity,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        data=data or {},
        metadata=metadata or {},
    )
    notifier.emit_sync(event)
