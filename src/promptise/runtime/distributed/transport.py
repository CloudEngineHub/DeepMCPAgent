"""HTTP transport for inter-node runtime management.

Each runtime node exposes an HTTP API that allows remote management:
starting/stopping processes, querying status, injecting events, and
health checks.

Uses ``aiohttp`` (ships with the base ``pip install promptise``).

Example::

    from promptise.runtime.distributed.transport import RuntimeTransport
    from promptise.runtime.runtime import AgentRuntime

    runtime = AgentRuntime()
    transport = RuntimeTransport(runtime, port=9100)

    await transport.start()
    # HTTP API now available at http://host:9100/
    await transport.stop()
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from ..runtime import AgentRuntime

logger = logging.getLogger(__name__)


class RuntimeTransport:
    """HTTP management API for an :class:`AgentRuntime`.

    Exposes REST endpoints for remote process management:

    * ``GET /health`` — health check
    * ``GET /status`` — full runtime status
    * ``GET /processes`` — list all processes
    * ``GET /processes/{name}/status`` — single process status
    * ``POST /processes/{name}/start`` — start a process
    * ``POST /processes/{name}/stop`` — stop a process
    * ``POST /processes/{name}/restart`` — restart a process
    * ``POST /processes/{name}/event`` — inject a trigger event

    Args:
        runtime: The :class:`AgentRuntime` to manage.
        host: Host to bind to (default ``127.0.0.1`` — localhost only).
        port: Port to bind to.
        node_id: Unique identifier for this node.
        auth_token: Bearer token required on all management requests.
            When set, every request must include
            ``Authorization: Bearer <token>``. **Strongly recommended
            for any non-localhost deployment.**
    """

    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        host: str = "127.0.0.1",
        port: int = 9100,
        node_id: str = "node-1",
        auth_token: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._host = host
        self._port = port
        self._node_id = node_id
        self._auth_token = auth_token

        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def node_id(self) -> str:
        """This node's unique identifier."""
        return self._node_id

    @property
    def port(self) -> int:
        """The port this transport is listening on."""
        return self._port

    def _check_auth(self, request: web.Request) -> bool:
        """Verify Bearer token if auth is configured."""
        if self._auth_token is None:
            return True
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        import hmac as _hmac

        return _hmac.compare_digest(auth_header[7:], self._auth_token)

    async def start(self) -> None:
        """Start the HTTP transport server."""
        if self._host != "127.0.0.1" and self._auth_token is None:
            logger.warning(
                "RuntimeTransport %s: binding to %s WITHOUT auth_token. "
                "Any network client can manage this runtime. Set auth_token "
                "for production deployments.",
                self._node_id,
                self._host,
            )
        self._app = web.Application()
        self._setup_routes()

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info(
            "RuntimeTransport %s started on %s:%d",
            self._node_id,
            self._host,
            self._port,
        )

    async def stop(self) -> None:
        """Stop the HTTP transport server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._site = None
        self._app = None
        logger.info("RuntimeTransport %s stopped", self._node_id)

    def _setup_routes(self) -> None:
        """Register all HTTP routes."""
        assert self._app is not None
        r = self._app.router
        r.add_get("/health", self._handle_health)
        r.add_get("/status", self._handle_status)
        r.add_get("/processes", self._handle_list_processes)
        r.add_get("/processes/{name}/status", self._handle_process_status)
        r.add_post("/processes/{name}/start", self._handle_start)
        r.add_post("/processes/{name}/stop", self._handle_stop)
        r.add_post("/processes/{name}/restart", self._handle_restart)
        r.add_post("/processes/{name}/event", self._handle_inject_event)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response(
            {
                "status": "healthy",
                "node_id": self._node_id,
                "process_count": len(self._runtime.processes),
            }
        )

    async def _handle_status(self, request: web.Request) -> web.Response:
        """Full runtime status (requires auth)."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        status = self._runtime.status()
        status["node_id"] = self._node_id
        return web.json_response(status, dumps=_json_dumps)

    async def _handle_list_processes(self, request: web.Request) -> web.Response:
        """List all processes (requires auth)."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        processes = self._runtime.list_processes()
        return web.json_response({"node_id": self._node_id, "processes": processes})

    async def _handle_process_status(self, request: web.Request) -> web.Response:
        """Single process status (requires auth)."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        name = request.match_info["name"]
        try:
            status = self._runtime.process_status(name)
            return web.json_response(status, dumps=_json_dumps)
        except KeyError:
            return web.json_response({"error": f"Process {name!r} not found"}, status=404)

    async def _handle_start(self, request: web.Request) -> web.Response:
        """Start a process (requires auth)."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        name = request.match_info["name"]
        try:
            await self._runtime.start_process(name)
            return web.json_response({"status": "started", "name": name}, status=200)
        except KeyError:
            return web.json_response({"error": f"Process {name!r} not found"}, status=404)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_stop(self, request: web.Request) -> web.Response:
        """Stop a process (requires auth)."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        name = request.match_info["name"]
        try:
            await self._runtime.stop_process(name)
            return web.json_response({"status": "stopped", "name": name}, status=200)
        except KeyError:
            return web.json_response({"error": f"Process {name!r} not found"}, status=404)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_restart(self, request: web.Request) -> web.Response:
        """Restart a process (requires auth)."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        name = request.match_info["name"]
        try:
            await self._runtime.restart_process(name)
            return web.json_response({"status": "restarted", "name": name}, status=200)
        except KeyError:
            return web.json_response({"error": f"Process {name!r} not found"}, status=404)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_inject_event(self, request: web.Request) -> web.Response:
        """Inject a trigger event into a process (requires auth)."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        from ..triggers.base import TriggerEvent

        name = request.match_info["name"]
        try:
            process = self._runtime.get_process(name)
        except KeyError:
            return web.json_response({"error": f"Process {name!r} not found"}, status=404)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        event = TriggerEvent(
            trigger_id=body.get("trigger_id", "remote"),
            trigger_type=body.get("trigger_type", "remote"),
            payload=body.get("payload"),
            metadata=body.get("metadata", {}),
        )
        await process.inject(event)

        return web.json_response(
            {
                "status": "injected",
                "event_id": event.event_id,
                "process": name,
            },
            status=202,
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> RuntimeTransport:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def __repr__(self) -> str:
        return f"RuntimeTransport(node={self._node_id!r}, port={self._port})"


def _json_dumps(obj: Any) -> str:
    """JSON serializer that handles non-standard types."""
    return json.dumps(obj, default=str, ensure_ascii=False)
