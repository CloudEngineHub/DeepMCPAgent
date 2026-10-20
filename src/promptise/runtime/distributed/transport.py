"""HTTP transport for inter-node runtime management.

Each runtime node exposes an HTTP API that allows remote management:
starting/stopping processes, querying status, injecting events, and
health checks.

Uses ``aiohttp`` (ships with the base ``pip install promptise``).

Security model:

* Every endpoint except ``GET /health`` requires
  ``Authorization: Bearer <auth_token>`` when ``auth_token`` is set.
* Binding to a non-loopback address (e.g. ``0.0.0.0``) without an
  ``auth_token`` is refused, unless ``allow_unauthenticated=True`` says
  that something in front of the node (a proxy, a private network)
  authenticates callers.
* On a loopback bind, requests whose ``Host`` header is not a loopback
  name are refused (``421``, DNS rebinding) and so are requests from a
  browser page on another origin (``403``), so a web page cannot drive
  a local runtime.
* ``POST /cluster/nodes`` takes ``http``/``https`` URLs only.  The
  coordinator calls every registered URL with the cluster token, so a
  token holder can make it send requests (and the token) to any host it
  can reach; ``allowed_node_hosts`` limits the hosts a node may register.

Example::

    from promptise.runtime.distributed.transport import RuntimeTransport
    from promptise.runtime.runtime import AgentRuntime

    runtime = AgentRuntime()
    transport = RuntimeTransport(runtime, port=9100, auth_token="secret")

    await transport.start()
    # HTTP API now available at http://127.0.0.1:9100/
    await transport.stop()
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from ..._logsafe import log_safe
from .._http_guard import LOOPBACK_ORIGIN as _LOOPBACK_ORIGIN
from .._http_guard import bearer_token_matches
from .._http_guard import host_name as _host_name
from .._http_guard import is_loopback as _is_loopback

logger = logging.getLogger(__name__)

#: Endpoints that answer without a bearer token (liveness probes).
_PUBLIC_PATHS = frozenset({"/health"})

#: Node IDs a node may register with.
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _is_http_url(value: str) -> bool:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def _host_allowed(url: str, allowed: list[str]) -> bool:
    """Whether *url*'s host is one of *allowed* (``*.example.com`` matches
    subdomains).  An empty list allows any host."""
    if not allowed:
        return True
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower().rstrip(".")
    for entry in allowed:
        pattern = entry.strip().lower().strip("[]").rstrip(".")
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


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

    With a ``coordinator`` (the node hosting the cluster coordinator):

    * ``GET /cluster/nodes`` — the registered nodes and their health
    * ``POST /cluster/nodes`` — register (or re-register) a node:
      ``{"node_id": ..., "url": ..., "metadata": {...}}``; answers ``409``
      with static discovery (the coordinator lists the nodes itself)
    * ``DELETE /cluster/nodes/{node_id}`` — unregister a node
    * ``GET /cluster/status`` — :meth:`RuntimeCoordinator.cluster_status`

    Args:
        runtime: The :class:`AgentRuntime` to manage.
        host: Host to bind to (default ``127.0.0.1`` — localhost only).
        port: Port to bind to.
        node_id: Unique identifier for this node.
        auth_token: Bearer token required on every endpoint except
            ``GET /health``: requests must include
            ``Authorization: Bearer <token>``.  **Required** for a
            non-loopback ``host``.
        allow_unauthenticated: Allow a non-loopback ``host`` without
            ``auth_token``.  Only for nodes behind something that
            authenticates callers itself (an authenticating proxy, a
            private network you control); anyone who can reach the port
            can start, stop and drive your agents.
        coordinator: A :class:`RuntimeCoordinator` to expose the
            ``/cluster`` routes for (this node hosts the coordinator).
        discovery: The coordinator's discovery: a
            :class:`RegistryDiscovery` (nodes register through
            ``POST /cluster/nodes``) or a :class:`StaticDiscovery`.
        allowed_node_hosts: Hosts a registering node's URL may name
            (``*.example.com`` for subdomains); others get ``403``.  Empty
            or ``None``: any host.

    Raises:
        ValueError: ``auth_token`` is empty, or ``host`` is not a
            loopback address and there is no ``auth_token`` (and
            ``allow_unauthenticated`` is not set).
    """

    def __init__(
        self,
        runtime: Any,  # an AgentRuntime; not imported, to keep runtime -> node -> transport acyclic
        *,
        host: str = "127.0.0.1",
        port: int = 9100,
        node_id: str = "node-1",
        auth_token: str | None = None,
        allow_unauthenticated: bool = False,
        coordinator: Any | None = None,
        discovery: Any | None = None,
        allowed_node_hosts: list[str] | None = None,
    ) -> None:
        if auth_token is not None and not auth_token.strip():
            raise ValueError("auth_token must be a non-empty string (or None)")
        self._loopback = _is_loopback(host)
        if not self._loopback and auth_token is None and not allow_unauthenticated:
            raise ValueError(
                f"RuntimeTransport {node_id}: refusing to bind {host!r} without "
                "auth_token — anyone who can reach the port could start, stop and "
                "drive this runtime's agents. Pass auth_token=..., bind 127.0.0.1, "
                "or set allow_unauthenticated=True if an authenticating proxy or "
                "private network protects the port."
            )
        self._runtime = runtime
        self._host = host
        self._port = port
        self._node_id = node_id
        self._auth_token = auth_token
        self._coordinator = coordinator
        self._discovery = discovery
        self._allowed_node_hosts = list(allowed_node_hosts or [])

        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def node_id(self) -> str:
        """This node's unique identifier."""
        return self._node_id

    @node_id.setter
    def node_id(self, value: str) -> None:
        self._node_id = value

    @property
    def port(self) -> int:
        """The port this transport is listening on.

        With ``port=0`` the OS picks a free port; after :meth:`start` this
        returns the port actually bound.
        """
        if self._port == 0 and self._runner is not None:
            for address in self._runner.addresses:
                if isinstance(address, tuple) and len(address) >= 2:
                    return int(address[1])
        return self._port

    def _check_auth(self, request: web.Request) -> bool:
        """Verify Bearer token if auth is configured (timing-safe; a header
        with non-ASCII characters is a mismatch, not an error)."""
        if self._auth_token is None:
            return True
        return bearer_token_matches(request.headers.get("Authorization", ""), self._auth_token)

    @web.middleware
    async def _guard(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        """Host/Origin checks (loopback binds) and bearer auth for every route."""
        if self._loopback:
            if not _is_loopback(_host_name(request.headers.get("Host", ""))):
                return web.json_response({"error": "Misdirected request"}, status=421)
            origin = request.headers.get("Origin")
            if origin is not None and not _LOOPBACK_ORIGIN.match(origin):
                return web.json_response({"error": "Cross-origin request refused"}, status=403)
        if request.path not in _PUBLIC_PATHS and not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return await handler(request)

    async def start(self) -> None:
        """Start the HTTP transport server."""
        if not self._loopback and self._auth_token is None:
            logger.warning(
                "RuntimeTransport %s: binding to %s WITHOUT auth_token "
                "(allow_unauthenticated=True). Anyone who can reach the port can "
                "manage this runtime.",
                self._node_id,
                self._host,
            )
        self._app = web.Application(middlewares=[self._guard])
        self._setup_routes()

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info(
            "RuntimeTransport %s started on %s:%d",
            self._node_id,
            self._host,
            self.port,
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
        if self._coordinator is not None:
            r.add_get("/cluster/nodes", self._handle_list_nodes)
            r.add_post("/cluster/nodes", self._handle_register_node)
            r.add_delete("/cluster/nodes/{node_id}", self._handle_unregister_node)
            r.add_get("/cluster/status", self._handle_cluster_status)

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
        status = self._runtime.status()
        status["node_id"] = self._node_id
        return web.json_response(status, dumps=_json_dumps)

    async def _handle_list_processes(self, request: web.Request) -> web.Response:
        """List all processes (requires auth)."""
        processes = self._runtime.list_processes()
        return web.json_response({"node_id": self._node_id, "processes": processes})

    async def _handle_process_status(self, request: web.Request) -> web.Response:
        """Single process status (requires auth)."""
        name = request.match_info["name"]
        try:
            status = self._runtime.process_status(name)
            return web.json_response(status, dumps=_json_dumps)
        except KeyError:
            return web.json_response({"error": f"Process {name!r} not found"}, status=404)

    async def _handle_start(self, request: web.Request) -> web.Response:
        """Start a process (requires auth)."""
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
        if not isinstance(body, dict):
            return web.json_response({"error": "Body must be a JSON object"}, status=400)
        payload = body.get("payload")
        metadata = body.get("metadata")
        if payload is not None and not isinstance(payload, dict):
            return web.json_response({"error": "payload must be a JSON object"}, status=400)
        if metadata is not None and not isinstance(metadata, dict):
            return web.json_response({"error": "metadata must be a JSON object"}, status=400)

        event = TriggerEvent(
            trigger_id=str(body.get("trigger_id") or "remote"),
            trigger_type=str(body.get("trigger_type") or "remote"),
            payload=payload or {},
            metadata=metadata or {},
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
    # Cluster (coordinator) handlers
    # ------------------------------------------------------------------

    async def _handle_list_nodes(self, request: web.Request) -> web.Response:
        """The registered nodes (requires auth)."""
        assert self._coordinator is not None
        if self._discovery is not None and hasattr(self._discovery, "discover"):
            await self._discovery.discover()  # a registry prunes stale nodes
        nodes = [n.to_dict() for n in self._coordinator.nodes.values()]
        return web.json_response(
            {"node_id": self._node_id, "nodes": nodes, "count": len(nodes)}, dumps=_json_dumps
        )

    async def _handle_register_node(self, request: web.Request) -> web.Response:
        """Register or refresh a node (requires auth)."""
        from .discovery import StaticDiscovery

        assert self._coordinator is not None
        if isinstance(self._discovery, StaticDiscovery):
            return web.json_response(
                {
                    "error": "This coordinator uses static discovery: nodes are listed in "
                    "its static_nodes, they don't register"
                },
                status=409,
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "Body must be a JSON object"}, status=400)
        node_id, url, metadata = body.get("node_id"), body.get("url"), body.get("metadata")
        if not isinstance(node_id, str) or not _NODE_ID_RE.match(node_id):
            return web.json_response(
                {"error": "node_id must match ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"}, status=422
            )
        if not isinstance(url, str) or not _is_http_url(url):
            return web.json_response({"error": "url must be an http(s) URL"}, status=422)
        if not _host_allowed(url, self._allowed_node_hosts):
            logger.warning(
                "RuntimeTransport %s: refused to register node %r at %s (host not in "
                "allowed_node_hosts)",
                log_safe(self._node_id),
                log_safe(node_id),
                log_safe(url),
            )
            return web.json_response(
                {"error": "The node URL's host is not in the coordinator's allowed_node_hosts"},
                status=403,
            )
        if metadata is not None and not isinstance(metadata, dict):
            return web.json_response({"error": "metadata must be a JSON object"}, status=422)
        if node_id == self._node_id:
            return web.json_response(
                {"error": f"node_id {node_id!r} is the coordinator's own ID"}, status=409
            )
        known = self._coordinator.nodes.get(node_id)
        if known is None or known.url != url.rstrip("/"):
            self._coordinator.register_node(node_id, url, metadata or {})
        elif metadata is not None:
            known.metadata = dict(metadata)
        if self._discovery is not None:
            await self._discovery.register(node_id, url, metadata or {})
        return web.json_response({"status": "registered", "node_id": node_id})

    async def _handle_unregister_node(self, request: web.Request) -> web.Response:
        """Unregister a node (requires auth)."""
        assert self._coordinator is not None
        node_id = request.match_info["node_id"]
        if node_id == self._node_id:
            return web.json_response(
                {"error": "The coordinator can't unregister itself"}, status=409
            )
        try:
            self._coordinator.unregister_node(node_id)
        except KeyError:
            return web.json_response({"error": f"Node {node_id!r} not registered"}, status=404)
        if self._discovery is not None:
            await self._discovery.unregister(node_id)
        return web.json_response({"status": "unregistered", "node_id": node_id})

    async def _handle_cluster_status(self, request: web.Request) -> web.Response:
        """Aggregated cluster status (requires auth)."""
        assert self._coordinator is not None
        status = await self._coordinator.cluster_status()
        status["coordinator"] = self._node_id
        return web.json_response(status, dumps=_json_dumps)

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
