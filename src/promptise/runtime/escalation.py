"""Shared escalation infrastructure for the agent runtime.

Provides a common :func:`escalate` function used by the mission, budget,
and health subsystems to notify external systems (webhooks, event bus)
when attention is required.

Requires ``httpx`` for webhook delivery::

    pip install httpx

Example::

    from promptise.runtime.escalation import escalate
    from promptise.runtime.config import EscalationTarget

    target = EscalationTarget(webhook_url="https://hooks.slack.com/...")
    await escalate(target, {"reason": "budget exceeded"})
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import EscalationTarget

logger = logging.getLogger("promptise.runtime.escalation")


async def escalate(
    target: EscalationTarget,
    payload: dict[str, Any],
    *,
    event_bus: Any | None = None,
) -> None:
    """Fire an escalation notification.

    Sends the payload to all configured targets.  Errors are logged as
    warnings but never raised — escalation is fire-and-forget so it
    doesn't disrupt agent execution.  A webhook that answers with an
    HTTP error status counts as a failed delivery.  A target with nothing
    to deliver to (no ``webhook_url``, and no ``event_type`` or no event
    bus) is logged as a warning, since the notification goes nowhere.

    Args:
        target: Where to send the notification.
        payload: JSON-serialisable payload describing the escalation.
        event_bus: Optional EventBus for event-based escalation.
    """
    delivered_somewhere = False
    if target.webhook_url:
        delivered_somewhere = True
        try:
            await _fire_webhook(target.webhook_url, payload)
        except Exception as exc:
            logger.warning("Escalation webhook failed: %s", exc)

    if target.event_type:
        if event_bus is None:
            logger.warning(
                "Escalation event %r not emitted: the process has no event bus. Create "
                "the runtime with AgentRuntime(event_bus=...) (promptise.runtime."
                "InProcessEventBus for processes in this Python process; 'promptise "
                "runtime start' has one), or set EscalationTarget.webhook_url",
                target.event_type,
            )
        else:
            delivered_somewhere = True
            try:
                delivered = await event_bus.emit(target.event_type, payload)
            except Exception as exc:
                logger.warning("Escalation event emission failed: %s", exc, exc_info=True)
            else:
                if type(delivered) is int and delivered == 0:
                    logger.warning(
                        "Escalation event %r reached no subscriber: nothing on the event "
                        "bus listens for it (for example a process with an 'event' trigger "
                        "for %r)",
                        target.event_type,
                        target.event_type,
                    )

    if not delivered_somewhere:
        logger.warning(
            "Escalation (%s) has no target: set EscalationTarget.webhook_url or event_type",
            payload.get("type", "unknown"),
        )


async def _fire_webhook(url: str, payload: dict[str, Any]) -> None:
    """POST a JSON payload to a webhook URL.

    SSRF protection: the host is resolved on every call, the request is
    refused if any address is private or internal, and it is sent to the
    address that was checked (``Host`` header and TLS server name kept), so
    the name cannot be re-resolved to an internal address in between (DNS
    rebinding).  Redirects are not followed.  Requires ``httpx``.
    """
    try:
        import httpx
    except ImportError:
        logger.warning("httpx not installed — cannot send escalation webhook")
        return

    from promptise._outbound import BlockedTarget, pin_target

    try:
        target = await pin_target(url)
    except BlockedTarget as exc:
        logger.warning("Escalation SSRF blocked: %s", exc)
        return
    except OSError as exc:
        logger.warning("Escalation webhook host did not resolve: %s", exc)
        return

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.post(
                target.url,
                json=payload,
                headers={"Content-Type": "application/json", **target.headers},
                extensions=target.extensions,
                follow_redirects=False,
            )
    except Exception as exc:
        logger.warning("Escalation webhook delivery failed: %s", exc, exc_info=True)
        return
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and status >= 300:
        logger.warning("Escalation webhook answered HTTP %d; notification not delivered", status)
