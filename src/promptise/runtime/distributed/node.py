"""ClusterNode — what :class:`AgentRuntime` runs for ``distributed.enabled``.

Wires a :class:`~promptise.runtime.config.DistributedConfig` into the
building blocks of this package:

* a :class:`~promptise.runtime.distributed.transport.RuntimeTransport` on
  ``transport_host:transport_port`` (the node's management API, bearer
  authenticated with the cluster token);
* with ``coordinator=True``: a
  :class:`~promptise.runtime.distributed.coordinator.RuntimeCoordinator`
  (health monitoring) and its discovery, a
  :class:`~promptise.runtime.distributed.discovery.RegistryDiscovery`
  (nodes register at ``POST /cluster/nodes``; ones that stop
  re-registering for ``node_timeout`` are dropped) or a
  :class:`~promptise.runtime.distributed.discovery.StaticDiscovery`
  (``static_nodes``);
* with ``coordinator_url``: the registration loop — register at start,
  re-register every ``heartbeat_interval`` (a restarted coordinator finds
  the node again), unregister at stop.

You normally don't use it directly: :meth:`AgentRuntime.start_all` starts
it and :meth:`AgentRuntime.shutdown` stops it
(:attr:`AgentRuntime.cluster` exposes it).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config import DistributedConfig

logger = logging.getLogger(__name__)

#: Seconds a registration request may take.
_REGISTER_TIMEOUT = 10.0


def _resolve_token(config: DistributedConfig) -> str | None:
    """The cluster token, with an ``${ENV_VAR}`` reference resolved."""
    if config.auth_token is None:
        return None
    raw = config.auth_token.get_secret_value()
    from promptise.env_resolver import resolve_env_var

    try:
        value = resolve_env_var(raw, context="distributed.auth_token")
    except Exception as exc:
        raise ValueError(f"distributed.auth_token: {exc}") from None
    if not value.strip():
        raise ValueError("distributed.auth_token resolved to an empty string")
    return value


class ClusterNode:
    """This runtime's membership in a cluster (see the module docstring).

    Args:
        runtime: The :class:`AgentRuntime` the transport manages.
        config: Its ``distributed`` settings (``enabled`` is not checked
            here).
    """

    def __init__(self, runtime: Any, config: DistributedConfig) -> None:
        self._runtime = runtime
        self._config = config
        self.transport: Any | None = None
        self.coordinator: Any | None = None
        self.discovery: Any | None = None
        self.node_id: str = config.node_id or ""
        self.url: str = ""
        self._token: str | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self.registered = False
        self.last_registration_error: str | None = None
        self._started = False

    @property
    def role(self) -> str:
        """``"coordinator"``, ``"node"`` (registers with one) or ``"standalone"``."""
        if self._config.coordinator:
            return "coordinator"
        return "node" if self._config.coordinator_url else "standalone"

    async def start(self) -> None:
        """Start the transport, the coordinator and/or the registration.

        Raises:
            ValueError: The token's ``${ENV_VAR}`` is not set.
            OSError: The transport port can't be bound.
        """
        if self._started:
            return
        from .coordinator import RuntimeCoordinator
        from .discovery import RegistryDiscovery, StaticDiscovery
        from .transport import RuntimeTransport

        cfg = self._config
        self._token = _resolve_token(cfg)
        timeout = cfg.effective_node_timeout()
        if cfg.coordinator:
            self.coordinator = RuntimeCoordinator(
                health_check_interval=cfg.heartbeat_interval,
                node_timeout=timeout,
                auth_token=self._token,
            )
            self.discovery = (
                StaticDiscovery(dict(cfg.static_nodes))
                if cfg.discovery_method == "static"
                else RegistryDiscovery(ttl=timeout)
            )
        self.transport = RuntimeTransport(
            self._runtime,
            host=cfg.transport_host,
            port=cfg.transport_port,
            node_id=self.node_id or "pending",
            auth_token=self._token,
            allow_unauthenticated=cfg.allow_unauthenticated,
            coordinator=self.coordinator,
            discovery=self.discovery,
            allowed_node_hosts=list(cfg.allowed_node_hosts),
        )
        if self._token is None:
            logger.warning(
                "Distributed runtime: the transport on %s:%s has no auth_token; any local "
                "process can manage this runtime. Set distributed.auth_token.",
                cfg.transport_host,
                cfg.transport_port,
            )
        await self.transport.start()
        try:
            port = self.transport.port
            if not self.node_id:
                self.node_id = f"{socket.gethostname()}-{port}"
            self.transport.node_id = self.node_id
            self.url = cfg.advertise_url or _url_for(cfg.transport_host, port)

            if self.coordinator is not None:
                for node_id, url in cfg.static_nodes.items():
                    self.coordinator.register_node(node_id, url)
                self.coordinator.register_node(self.node_id, self.url, {"role": "coordinator"})
                await self.coordinator.start_health_monitor()
                if cfg.discovery_method == "registry":
                    self._tasks.append(
                        asyncio.create_task(self._prune_loop(), name="cluster-prune")
                    )
            if cfg.coordinator_url:
                await self._register()
                self._tasks.append(
                    asyncio.create_task(self._registration_loop(), name="cluster-register")
                )
        except BaseException:
            await self.stop()
            raise
        self._started = True
        logger.info(
            "Distributed runtime: node %s (%s) at %s",
            self.node_id,
            self.role,
            self.url,
        )

    async def stop(self) -> None:
        """Unregister (best effort), stop the coordinator and the transport."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self._config.coordinator_url and self.registered:
            with contextlib.suppress(Exception):
                await self._request("DELETE", f"/cluster/nodes/{self.node_id}")
            self.registered = False
        if self.coordinator is not None:
            with contextlib.suppress(Exception):
                await self.coordinator.stop_health_monitor()
        if self.transport is not None:
            with contextlib.suppress(Exception):
                await self.transport.stop()
        self._started = False

    def status(self) -> dict[str, Any]:
        """JSON-serialisable summary (part of :meth:`AgentRuntime.status`)."""
        info: dict[str, Any] = {
            "node_id": self.node_id,
            "role": self.role,
            "url": self.url,
            "discovery_method": self._config.discovery_method,
            "authenticated": self._token is not None,
        }
        if self._config.coordinator_url:
            info["coordinator_url"] = self._config.coordinator_url
            info["registered"] = self.registered
            if self.last_registration_error:
                info["last_registration_error"] = self.last_registration_error
        if self.coordinator is not None:
            info["nodes"] = len(self.coordinator.nodes)
            info["healthy_nodes"] = len(self.coordinator.healthy_nodes)
        return info

    # ------------------------------------------------------------------
    # Registration with a remote coordinator
    # ------------------------------------------------------------------

    async def _request(self, method: str, path: str, body: Any = None) -> None:
        from aiohttp import ClientSession, ClientTimeout

        assert self._config.coordinator_url is not None
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        async with (
            ClientSession(timeout=ClientTimeout(total=_REGISTER_TIMEOUT)) as session,
            session.request(
                method, f"{self._config.coordinator_url}{path}", json=body, headers=headers
            ) as resp,
        ):
            if resp.status >= 400:
                text = (await resp.text())[:200]
                raise RuntimeError(f"{method} {path} -> {resp.status} {text}".rstrip())

    async def _register(self) -> bool:
        """Register with the coordinator; log (don't raise) a failure."""
        try:
            await self._request(
                "POST",
                "/cluster/nodes",
                {
                    "node_id": self.node_id,
                    "url": self.url,
                    "metadata": {"processes": len(self._runtime.processes)},
                },
            )
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            if message != self.last_registration_error:
                logger.warning(
                    "Distributed runtime: node %s could not register with %s: %s "
                    "(retrying every %gs)",
                    self.node_id,
                    self._config.coordinator_url,
                    message,
                    self._config.heartbeat_interval,
                )
            self.last_registration_error = message
            self.registered = False
            return False
        if not self.registered:
            logger.info(
                "Distributed runtime: node %s registered with %s",
                self.node_id,
                self._config.coordinator_url,
            )
        self.registered = True
        self.last_registration_error = None
        return True

    async def _registration_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.heartbeat_interval)
            await self._register()

    # ------------------------------------------------------------------
    # Coordinator: drop nodes the registry expired
    # ------------------------------------------------------------------

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.heartbeat_interval)
            with contextlib.suppress(Exception):
                await self.prune()

    async def prune(self) -> list[str]:
        """Unregister nodes whose registration expired; returns their IDs."""
        if self.coordinator is None or self.discovery is None:
            return []
        live = {n.node_id for n in await self.discovery.discover()}
        dropped = [
            node_id
            for node_id in self.coordinator.nodes
            if node_id != self.node_id
            and node_id not in live
            and node_id not in self._config.static_nodes
        ]
        for node_id in dropped:
            with contextlib.suppress(KeyError):
                self.coordinator.unregister_node(node_id)
            logger.info("Distributed runtime: node %s expired (no re-registration)", node_id)
        return dropped


def _url_for(host: str, port: int) -> str:
    name = host.strip("[]")
    if ":" in name:
        name = f"[{name}]"
    return f"http://{name}:{port}"


__all__ = ["ClusterNode"]
