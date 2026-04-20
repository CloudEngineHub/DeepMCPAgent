"""Webhook trigger: HTTP endpoint that fires trigger events.

An aiohttp server listens on a configurable port and path. When a POST
request arrives, a :class:`TriggerEvent` is produced with the request
body as payload.

Uses ``aiohttp`` (ships with the base ``pip install promptise``).

Example::

    from promptise.runtime.triggers.webhook import WebhookTrigger

    trigger = WebhookTrigger(path="/webhook", port=9090)
    await trigger.start()

    # In another task:
    event = await trigger.wait_for_next()
    print(event.payload)  # {"key": "value"} from the POST body

    await trigger.stop()
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import json
import logging

from aiohttp import web

from .base import TriggerEvent

logger = logging.getLogger(__name__)


class WebhookTrigger:
    """HTTP webhook trigger.

    Starts an aiohttp server that listens for incoming POST requests
    and converts them to :class:`TriggerEvent` objects.

    Args:
        path: URL path to listen on (e.g. ``"/webhook"``).
        port: TCP port to bind to.
        host: Host/IP to bind to.  Defaults to ``"127.0.0.1"``
            (loopback only).  Set to ``"0.0.0.0"`` for multi-node
            deployments where external services or other nodes need
            to reach this webhook.
        hmac_secret: Optional HMAC secret for signature verification.
            When set, incoming requests must include a valid
            ``X-Webhook-Signature`` header containing
            ``sha256=<hex-digest>`` computed over the raw request body.
            Verification uses timing-safe comparison.  **Strongly
            recommended** when ``host`` is ``"0.0.0.0"``.
    """

    def __init__(
        self,
        path: str = "/webhook",
        port: int = 9090,
        host: str = "127.0.0.1",
        hmac_secret: str | None = None,
    ) -> None:
        self._path = path
        self._port = port
        self._host = host
        self._hmac_secret = hmac_secret.encode() if hmac_secret else None
        if self._hmac_secret is None:
            logger.warning(
                "WebhookTrigger on port %d has no HMAC secret — "
                "any HTTP client can trigger this webhook. "
                "Set hmac_secret for production use.",
                port,
            )

        self.trigger_id: str = f"webhook-{port}{path}"
        self._queue: asyncio.Queue[TriggerEvent] = asyncio.Queue(maxsize=1000)
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._stopped = False

    async def start(self) -> None:
        """Start the webhook HTTP server."""
        self._stopped = False
        self._app = web.Application()
        self._app.router.add_post(self._path, self._handle_request)

        # Also add a health check endpoint
        self._app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info(
            "WebhookTrigger started on %s:%d%s",
            self._host,
            self._port,
            self._path,
        )

    async def stop(self) -> None:
        """Stop the webhook HTTP server."""
        self._stopped = True

        # Unblock any waiters
        sentinel = TriggerEvent(
            trigger_id=self.trigger_id,
            trigger_type="webhook",
            payload=None,
            metadata={"_stop": True},
        )
        try:
            self._queue.put_nowait(sentinel)
        except asyncio.QueueFull:
            pass

        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._site = None
        self._app = None
        logger.info("WebhookTrigger stopped")

    async def wait_for_next(self) -> TriggerEvent:
        """Wait for the next webhook event.

        Returns:
            A :class:`TriggerEvent` with the request body as payload.

        Raises:
            asyncio.CancelledError: If the wait is cancelled.
        """
        while True:
            event = await self._queue.get()
            # Skip stop sentinels
            if event.metadata and event.metadata.get("_stop"):
                if self._stopped:
                    raise asyncio.CancelledError("Webhook trigger stopped")
                continue
            return event

    async def _handle_request(self, request: web.Request) -> web.Response:
        """Handle incoming POST request."""
        try:
            # -- HMAC signature verification (if configured) --
            raw_body = await request.read()
            if self._hmac_secret is not None:
                sig_header = request.headers.get("X-Webhook-Signature", "")
                if not sig_header.startswith("sha256="):
                    return web.json_response(
                        {"status": "error", "message": "Missing or invalid signature"},
                        status=401,
                    )
                expected = hmac_mod.new(self._hmac_secret, raw_body, hashlib.sha256).hexdigest()
                actual = sig_header[7:]  # strip "sha256="
                if not hmac_mod.compare_digest(expected, actual):
                    logger.warning(
                        "WebhookTrigger: HMAC signature mismatch from %s", request.remote
                    )
                    return web.json_response(
                        {"status": "error", "message": "Invalid signature"},
                        status=401,
                    )

            # Try to parse JSON body
            try:
                body = json.loads(raw_body)
            except (json.JSONDecodeError, Exception):
                body = raw_body.decode("utf-8", errors="replace")

            # Extract headers for metadata
            headers = dict(request.headers)

            event = TriggerEvent(
                trigger_id=self.trigger_id,
                trigger_type="webhook",
                payload=body,
                metadata={
                    "method": request.method,
                    "path": str(request.path),
                    "query": dict(request.query),
                    "headers": {
                        k: v
                        for k, v in headers.items()
                        if k.lower() not in ("authorization", "cookie", "set-cookie")
                    },
                    "remote": request.remote,
                },
            )

            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("WebhookTrigger: queue full, dropping event")
                return web.json_response(
                    {"status": "error", "message": "queue full"},
                    status=503,
                )

            return web.json_response(
                {"status": "accepted", "event_id": event.event_id},
                status=202,
            )

        except Exception as exc:
            logger.exception("WebhookTrigger: error handling request")
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=500,
            )

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response(
            {
                "status": "healthy",
                "trigger_id": self.trigger_id,
                "queue_size": self._queue.qsize(),
            }
        )

    def __repr__(self) -> str:
        return (
            f"WebhookTrigger(path={self._path!r}, "
            f"port={self._port}, "
            f"queue_size={self._queue.qsize()})"
        )
