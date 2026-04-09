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

    Sends the payload to all configured targets.  Errors are logged
    but never raised — escalation is fire-and-forget so it doesn't
    disrupt agent execution.

    Args:
        target: Where to send the notification.
        payload: JSON-serialisable payload describing the escalation.
        event_bus: Optional EventBus for event-based escalation.
    """
    if target.webhook_url:
        try:
            await _fire_webhook(target.webhook_url, payload)
        except Exception as exc:
            logger.warning("Escalation webhook failed: %s", exc)

    if target.event_type and event_bus is not None:
        try:
            await event_bus.emit(target.event_type, payload)
        except Exception as exc:
            logger.debug("Escalation event emission failed: %s", exc, exc_info=True)


async def _fire_webhook(url: str, payload: dict[str, Any]) -> None:
    """POST a JSON payload to a webhook URL.

    Validates the URL against private IP ranges (SSRF protection)
    and requires ``httpx``.
    """
    # SSRF protection
    try:
        from promptise.mcp.server._openapi import _validate_url_not_private

        _validate_url_not_private(url)
    except (ImportError, ValueError) as exc:
        if isinstance(exc, ValueError):
            logger.warning("Escalation SSRF blocked: %s", exc)
            return

    try:
        import httpx
    except ImportError:
        logger.warning("httpx not installed — cannot send escalation webhook")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except Exception as exc:
        logger.debug("Webhook delivery failed: %s", exc, exc_info=True)
