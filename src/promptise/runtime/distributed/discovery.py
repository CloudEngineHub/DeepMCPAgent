"""Process discovery across runtime nodes.

Provides mechanisms for runtime nodes to find each other:

* **Registry-based**: Nodes register with a central coordinator URL.
* **Static**: Explicit list of known node URLs (for simple deployments).

Example::

    from promptise.runtime.distributed.discovery import (
        ProcessDiscovery,
        StaticDiscovery,
        RegistryDiscovery,
    )

    # Static discovery — known list of nodes
    discovery = StaticDiscovery(
        nodes={
            "node-1": "http://host1:9100",
            "node-2": "http://host2:9100",
        }
    )
    nodes = await discovery.discover()

    # Registry-based — nodes register at startup
    registry = RegistryDiscovery()
    registry.register("node-1", "http://host1:9100")
    nodes = await registry.discover()
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredNode:
    """A discovered runtime node.

    Attributes:
        node_id: Unique node identifier.
        url: Base URL for the node's transport API.
        discovered_at: Timestamp when the node was discovered.
        metadata: Additional node metadata.
    """

    node_id: str
    url: str
    discovered_at: float = field(default_factory=time.monotonic)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "node_id": self.node_id,
            "url": self.url,
            "discovered_at": self.discovered_at,
            "metadata": self.metadata,
        }


@runtime_checkable
class ProcessDiscovery(Protocol):
    """Protocol for node discovery mechanisms."""

    async def discover(self) -> list[DiscoveredNode]:
        """Discover available runtime nodes.

        Returns:
            List of discovered nodes.
        """
        ...

    async def register(
        self,
        node_id: str,
        url: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register this node for discovery by others.

        Args:
            node_id: Unique node identifier.
            url: Base URL for the node's transport API.
            metadata: Optional additional metadata.
        """
        ...

    async def unregister(self, node_id: str) -> None:
        """Remove this node from discovery.

        Args:
            node_id: Node to remove.
        """
        ...


class StaticDiscovery:
    """Static discovery from a known list of node URLs.

    Suitable for fixed-topology deployments where all node
    addresses are known at configuration time.

    Args:
        nodes: Mapping of node_id → URL.
    """

    def __init__(self, nodes: dict[str, str] | None = None) -> None:
        self._nodes: dict[str, DiscoveredNode] = {}
        if nodes:
            for node_id, url in nodes.items():
                self._nodes[node_id] = DiscoveredNode(
                    node_id=node_id,
                    url=url.rstrip("/"),
                )

    async def discover(self) -> list[DiscoveredNode]:
        """Return all statically configured nodes."""
        return list(self._nodes.values())

    async def register(
        self,
        node_id: str,
        url: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add a node to the static list."""
        self._nodes[node_id] = DiscoveredNode(
            node_id=node_id,
            url=url.rstrip("/"),
            metadata=metadata or {},
        )

    async def unregister(self, node_id: str) -> None:
        """Remove a node from the static list."""
        self._nodes.pop(node_id, None)

    def __repr__(self) -> str:
        return f"StaticDiscovery(nodes={len(self._nodes)})"


class RegistryDiscovery:
    """In-process registry for node discovery.

    Nodes register themselves with the registry, and other nodes can
    discover them.  This is suitable for single-process testing or
    when a shared registry object is accessible to all nodes.

    For production deployments across machines, use with a central
    coordinator (the coordinator can hold the RegistryDiscovery
    instance and expose it via its HTTP API).

    Args:
        ttl: Time-to-live for registrations in seconds.
            After this period without re-registration, nodes are
            considered stale and removed from discovery.
    """

    def __init__(self, ttl: float = 60.0) -> None:
        self._ttl = ttl
        self._nodes: dict[str, DiscoveredNode] = {}
        self._lock = asyncio.Lock()

    async def discover(self) -> list[DiscoveredNode]:
        """Return all registered nodes, pruning stale entries."""
        async with self._lock:
            self._prune_stale()
            return list(self._nodes.values())

    async def register(
        self,
        node_id: str,
        url: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a node for discovery."""
        async with self._lock:
            self._nodes[node_id] = DiscoveredNode(
                node_id=node_id,
                url=url.rstrip("/"),
                metadata=metadata or {},
            )
            logger.info(
                "RegistryDiscovery: registered %s at %s",
                node_id,
                url,
            )

    async def unregister(self, node_id: str) -> None:
        """Remove a node from the registry."""
        async with self._lock:
            self._nodes.pop(node_id, None)
            logger.info("RegistryDiscovery: unregistered %s", node_id)

    async def heartbeat(self, node_id: str) -> None:
        """Update a node's registration timestamp.

        Args:
            node_id: Node to refresh.
        """
        async with self._lock:
            if node_id in self._nodes:
                self._nodes[node_id].discovered_at = time.monotonic()

    def _prune_stale(self) -> None:
        """Remove nodes that haven't been refreshed within TTL."""
        cutoff = time.monotonic() - self._ttl
        stale = [nid for nid, node in self._nodes.items() if node.discovered_at < cutoff]
        for nid in stale:
            del self._nodes[nid]
            logger.info("RegistryDiscovery: pruned stale node %s", nid)

    def __repr__(self) -> str:
        return f"RegistryDiscovery(nodes={len(self._nodes)}, ttl={self._ttl}s)"
