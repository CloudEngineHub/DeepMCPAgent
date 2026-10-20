"""Distributed runtime coordination.

Enables agent processes to run across multiple machines with:

* :class:`RuntimeTransport` — HTTP management API per node
* :class:`RuntimeCoordinator` — cluster coordination and health monitoring
* :class:`ProcessDiscovery` — node discovery protocol
* :class:`StaticDiscovery` — fixed-topology discovery
* :class:`RegistryDiscovery` — dynamic registry-based discovery
* :class:`ClusterNode` — what :class:`AgentRuntime` runs for
  ``DistributedConfig(enabled=True)``: the transport plus the coordinator
  or the registration with one
"""

from __future__ import annotations

from .coordinator import NodeInfo, NodeUnreachableError, RuntimeCoordinator
from .discovery import (
    DiscoveredNode,
    ProcessDiscovery,
    RegistryDiscovery,
    StaticDiscovery,
)
from .node import ClusterNode

# Transport requires aiohttp — lazy import
try:
    from .transport import RuntimeTransport

    __all__ = [
        "ClusterNode",
        "RuntimeTransport",
        "RuntimeCoordinator",
        "NodeInfo",
        "NodeUnreachableError",
        "ProcessDiscovery",
        "DiscoveredNode",
        "StaticDiscovery",
        "RegistryDiscovery",
    ]
except ImportError:
    __all__ = [
        "ClusterNode",
        "RuntimeCoordinator",
        "NodeInfo",
        "NodeUnreachableError",
        "ProcessDiscovery",
        "DiscoveredNode",
        "StaticDiscovery",
        "RegistryDiscovery",
    ]
