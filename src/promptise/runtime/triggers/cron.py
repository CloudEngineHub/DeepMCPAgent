"""Cron-based trigger.

Fires at scheduled intervals defined by a standard cron expression.
Uses ``croniter`` if available, otherwise falls back to a simple
interval parser for basic expressions like ``*/N * * * *``.

Example::

    trigger = CronTrigger("*/5 * * * *")
    await trigger.start()
    event = await trigger.wait_for_next()  # blocks up to 5 minutes
    print(event.payload)  # {"scheduled_time": "2026-03-01T10:05:00+00:00"}
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ..exceptions import TriggerError
from .base import TriggerEvent

logger = logging.getLogger(__name__)

try:
    from croniter import croniter  # type: ignore[import-untyped]

    CRONITER_AVAILABLE = True
except ImportError:
    CRONITER_AVAILABLE = False


class CronTrigger:
    """Fires at scheduled intervals defined by a cron expression.

    Args:
        cron_expression: Standard cron expression (e.g. ``*/5 * * * *``).
        trigger_id: Unique identifier (auto-generated if not provided).
    """

    def __init__(
        self,
        cron_expression: str,
        *,
        trigger_id: str | None = None,
    ) -> None:
        self.trigger_id = trigger_id or f"cron-{uuid4().hex[:8]}"
        self._cron_expression = cron_expression
        self._running = False
        self._event: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        """Mark the trigger as active."""
        self._running = True
        logger.info(
            "CronTrigger %s started: %s",
            self.trigger_id,
            self._cron_expression,
        )

    async def stop(self) -> None:
        """Mark the trigger as inactive and unblock waiters."""
        self._running = False
        self._event.set()
        logger.info("CronTrigger %s stopped", self.trigger_id)

    async def wait_for_next(self) -> TriggerEvent:
        """Block until the next scheduled time.

        Uses ``self._event`` to allow :meth:`stop` to unblock the wait
        immediately instead of sleeping for the full delay.

        Returns:
            A :class:`TriggerEvent` with the scheduled time in the payload.

        Raises:
            asyncio.CancelledError: If stopped while waiting.
            TriggerError: If the cron expression is invalid.
        """
        self._event.clear()
        next_fire = self._compute_next_fire()
        now = datetime.now(UTC)
        delay = max(0, (next_fire - now).total_seconds())

        if delay > 0:
            # Wait for either the delay or a stop signal
            try:
                await asyncio.wait_for(self._event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass  # Delay elapsed — fire the trigger
            except asyncio.CancelledError:
                raise

        if not self._running:
            raise asyncio.CancelledError("Trigger stopped")

        return TriggerEvent(
            trigger_id=self.trigger_id,
            trigger_type="cron",
            payload={
                "scheduled_time": next_fire.isoformat(),
                "cron_expression": self._cron_expression,
            },
        )

    def _compute_next_fire(self) -> datetime:
        """Calculate the next fire time from now."""
        now = datetime.now(UTC)

        if CRONITER_AVAILABLE:
            try:
                cron = croniter(self._cron_expression, now)
                next_dt = cron.get_next(datetime)
                if next_dt.tzinfo is None:
                    next_dt = next_dt.replace(tzinfo=UTC)
                return next_dt
            except (ValueError, KeyError) as exc:
                raise TriggerError(f"Invalid cron expression: {self._cron_expression!r}") from exc

        # Fallback: parse simple interval expressions like "*/N * * * *"
        return self._simple_next_fire(now)

    def _simple_next_fire(self, now: datetime) -> datetime:
        """Parse simple ``*/N * * * *`` expressions without croniter."""
        parts = self._cron_expression.strip().split()
        if len(parts) < 5:
            raise TriggerError(
                f"Invalid cron expression (need 5 fields): {self._cron_expression!r}"
            )

        minute_field = parts[0]
        match = re.match(r"^\*/(\d+)$", minute_field)
        if match:
            interval = int(match.group(1))
            return now + timedelta(minutes=interval)

        # Every minute
        if minute_field == "*":
            return now + timedelta(minutes=1)

        # Specific minute
        try:
            target_minute = int(minute_field)
            result = now.replace(second=0, microsecond=0)
            if result.minute >= target_minute:
                result += timedelta(hours=1)
            result = result.replace(minute=target_minute)
            return result
        except ValueError:
            pass

        raise TriggerError(
            f"Cannot parse cron expression without croniter: "
            f"{self._cron_expression!r}. Install croniter for full support."
        )

    def __repr__(self) -> str:
        return (
            f"CronTrigger(id={self.trigger_id!r}, "
            f"cron={self._cron_expression!r}, "
            f"running={self._running})"
        )
