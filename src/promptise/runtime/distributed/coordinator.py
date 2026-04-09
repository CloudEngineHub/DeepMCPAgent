"""Cluster coordinator for distributed runtime.

The :class:`RuntimeCoordinator` tracks runtime nodes, aggregates status
across the cluster, monitors node health, and can redistribute processes
when nodes fail.

Example::

    from promptise.runtime.distributed.coordinator import RuntimeCoordinator

    coordinator = RuntimeCoordinator()
    coordinator.register_node("node-1", "http://host1:9100")
    coordinator.register_node("node-2", "http://host2:9100")

    # Check cluster health
    health = await coordinator.check_health()
    print(health)  # {"node-1": {"status": "healthy"}, ...}

    # Get aggregated status
    status = await coordinator.cluster_status()
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Try to import aiohttp for HTTP-based health checks
try:
    from aiohttp import ClientSession, ClientTimeout

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


@dataclass
class NodeInfo:
    """Information about a runtime node in the cluster.

    Attributes:
        node_id: Unique node identifier.
        url: Base URL for the node's transport API.
        last_heartbeat: Timestamp of last successful health check.
        status: Current node status.
        process_count: Number of processes on this node.
        metadata: Additional node metadata.
    """

    node_id: str
    url: str
    last_heartbeat: float = field(default_factory=time.monotonic)
    status: str = "unknown"
    process_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "node_id": self.node_id,
            "url": self.url,
            "last_heartbeat": self.last_heartbeat,
            "status": self.status,
            "process_count": self.process_count,
            "metadata": self.metadata,
        }

    @property
    def is_healthy(self) -> bool:
        """Check if node has had a recent heartbeat."""
        return self.status == "healthy"


class RuntimeCoordinator:
    """Cluster coordinator for distributed runtime management.

    Tracks nodes, monitors health, and aggregates cluster state.

    Args:
        health_check_interval: Seconds between health checks.
        node_timeout: Seconds before a node is considered unhealthy.
    """

    def __init__(
        self,
        *,
        health_check_interval: float = 15.0,
        node_timeout: float = 45.0,
    ) -> None:
        self._health_check_interval = health_check_interval
        self._node_timeout = node_timeout
        self._nodes: dict[str, NodeInfo] = {}
        self._health_task: asyncio.Task[None] | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Node management
    # ------------------------------------------------------------------

    def register_node(
        self,
        node_id: str,
        url: str,
        metadata: dict[str, Any] | None = None,
    ) -> NodeInfo:
        """Register a runtime node with the coordinator.

        Args:
            node_id: Unique node identifier.
            url: Base URL for the node's transport API.
            metadata: Optional additional metadata.

        Returns:
            The registered :class:`NodeInfo`.
        """
        node = NodeInfo(
            node_id=node_id,
            url=url.rstrip("/"),
            metadata=metadata or {},
        )
        self._nodes[node_id] = node
        logger.info("Coordinator: registered node %s at %s", node_id, url)
        return node

    def unregister_node(self, node_id: str) -> None:
        """Remove a node from the cluster.

        Args:
            node_id: Node to remove.

        Raises:
            KeyError: If node doesn't exist.
        """
        if node_id not in self._nodes:
            raise KeyError(f"Node {node_id!r} not registered")
        del self._nodes[node_id]
        logger.info("Coordinator: unregistered node %s", node_id)

    def get_node(self, node_id: str) -> NodeInfo:
        """Get information about a registered node.

        Args:
            node_id: Node identifier.

        Returns:
            :class:`NodeInfo` for the node.

        Raises:
            KeyError: If node doesn't exist.
        """
        if node_id not in self._nodes:
            raise KeyError(f"Node {node_id!r} not registered")
        return self._nodes[node_id]

    @property
    def nodes(self) -> dict[str, NodeInfo]:
        """Read-only view of registered nodes."""
        return dict(self._nodes)

    @property
    def healthy_nodes(self) -> list[NodeInfo]:
        """List of currently healthy nodes."""
        return [n for n in self._nodes.values() if n.is_healthy]

    # ------------------------------------------------------------------
    # Health monitoring
    # ------------------------------------------------------------------

    async def start_health_monitor(self) -> None:
        """Start the background health monitoring loop."""
        self._running = True
        self._health_task = asyncio.create_task(
            self._health_loop(),
            name="coordinator-health-monitor",
        )
        logger.info("Coordinator: health monitor started")

    async def stop_health_monitor(self) -> None:
        """Stop the health monitoring loop."""
        self._running = False
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
        logger.info("Coordinator: health monitor stopped")

    async def _health_loop(self) -> None:
        """Background health check loop."""
        try:
            while self._running:
                await self.check_health()
                await asyncio.sleep(self._health_check_interval)
        except asyncio.CancelledError:
            return

    async def check_health(self) -> dict[str, dict[str, Any]]:
        """Check health of all registered nodes.

        Makes HTTP requests to each node's ``/health`` endpoint.

        Returns:
            Dict mapping node_id to health status.
        """
        results: dict[str, dict[str, Any]] = {}

        if not HAS_AIOHTTP:
            # Without aiohttp, mark all nodes as unknown
            for node_id, node in self._nodes.items():
                node.status = "unknown"
                results[node_id] = {"status": "unknown", "reason": "aiohttp not installed"}
            return results

        timeout = ClientTimeout(total=10)
        async with ClientSession(timeout=timeout) as session:
            tasks = []
            for node_id, node in self._nodes.items():
                tasks.append(self._check_node_health(session, node))

            responses = await asyncio.gather(*tasks, return_exceptions=True)

            for (node_id, node), response in zip(self._nodes.items(), responses, strict=False):
                if isinstance(response, Exception):
                    node.status = "unhealthy"
                    results[node_id] = {
                        "status": "unhealthy",
                        "error": str(response),
                    }
                else:
                    node.status = "healthy"
                    node.last_heartbeat = time.monotonic()
                    node.process_count = response.get("process_count", 0)
                    results[node_id] = response

        return results

    async def _check_node_health(self, session: Any, node: NodeInfo) -> dict[str, Any]:
        """Check health of a single node."""
        url = f"{node.url}/health"
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Health check failed: {resp.status}")
            return await resp.json()

    # ------------------------------------------------------------------
    # Cluster status
    # ------------------------------------------------------------------

    async def cluster_status(self) -> dict[str, Any]:
        """Aggregate status across all nodes.

        Returns:
            Dict with cluster-wide summary and per-node details.
        """
        health = await self.check_health()

        total_processes = sum(n.process_count for n in self._nodes.values())
        healthy_count = len(self.healthy_nodes)

        return {
            "total_nodes": len(self._nodes),
            "healthy_nodes": healthy_count,
            "unhealthy_nodes": len(self._nodes) - healthy_count,
            "total_processes": total_processes,
            "nodes": {
                node_id: {
                    **node.to_dict(),
                    "health": health.get(node_id, {}),
                }
                for node_id, node in self._nodes.items()
            },
        }

    async def get_node_status(self, node_id: str) -> dict[str, Any]:
        """Get detailed status from a specific node via HTTP.

        Args:
            node_id: Node to query.

        Returns:
            Full status from the node.

        Raises:
            KeyError: If node doesn't exist.
            RuntimeError: If aiohttp is not available.
        """
        node = self.get_node(node_id)

        if not HAS_AIOHTTP:
            raise RuntimeError("aiohttp required for remote status")

        timeout = ClientTimeout(total=10)
        async with ClientSession(timeout=timeout) as session:
            async with session.get(f"{node.url}/status") as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Status request failed: {resp.status}")
                return await resp.json()

    # ------------------------------------------------------------------
    # Remote operations
    # ------------------------------------------------------------------

    async def start_process_on_node(self, node_id: str, process_name: str) -> dict[str, Any]:
        """Start a process on a specific node.

        Args:
            node_id: Target node.
            process_name: Process to start.

        Returns:
            Response from the node.
        """
        node = self.get_node(node_id)

        if not HAS_AIOHTTP:
            raise RuntimeError("aiohttp required for remote operations")

        timeout = ClientTimeout(total=30)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(f"{node.url}/processes/{process_name}/start") as resp:
                return await resp.json()

    async def stop_process_on_node(self, node_id: str, process_name: str) -> dict[str, Any]:
        """Stop a process on a specific node.

        Args:
            node_id: Target node.
            process_name: Process to stop.

        Returns:
            Response from the node.
        """
        node = self.get_node(node_id)

        if not HAS_AIOHTTP:
            raise RuntimeError("aiohttp required for remote operations")

        timeout = ClientTimeout(total=30)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(f"{node.url}/processes/{process_name}/stop") as resp:
                return await resp.json()

    async def inject_event_on_node(
        self,
        node_id: str,
        process_name: str,
        payload: Any,
        trigger_type: str = "remote",
    ) -> dict[str, Any]:
        """Inject a trigger event on a remote node.

        Args:
            node_id: Target node.
            process_name: Target process.
            payload: Event payload.
            trigger_type: Type of trigger event.

        Returns:
            Response from the node.
        """
        node = self.get_node(node_id)

        if not HAS_AIOHTTP:
            raise RuntimeError("aiohttp required for remote operations")

        timeout = ClientTimeout(total=10)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{node.url}/processes/{process_name}/event",
                json={
                    "trigger_type": trigger_type,
                    "payload": payload,
                },
            ) as resp:
                return await resp.json()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> RuntimeCoordinator:
        await self.start_health_monitor()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop_health_monitor()

    def __repr__(self) -> str:
        healthy = len(self.healthy_nodes)
        total = len(self._nodes)
        return f"RuntimeCoordinator(nodes={total}, healthy={healthy})"
