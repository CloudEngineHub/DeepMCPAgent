"""Distributed runtime coordination.

Enables agent processes to run across multiple machines with:

* :class:`RuntimeTransport` — HTTP management API per node
* :class:`RuntimeCoordinator` — cluster coordination and health monitoring
* :class:`ProcessDiscovery` — node discovery protocol
* :class:`StaticDiscovery` — fixed-topology discovery
* :class:`RegistryDiscovery` — dynamic registry-based discovery
"""

from __future__ import annotations

from .coordinator import NodeInfo, RuntimeCoordinator
from .discovery import (
    DiscoveredNode,
    ProcessDiscovery,
    RegistryDiscovery,
    StaticDiscovery,
)

# Transport requires aiohttp — lazy import
try:
    from .transport import RuntimeTransport

    __all__ = [
        "RuntimeTransport",
        "RuntimeCoordinator",
        "NodeInfo",
        "ProcessDiscovery",
        "DiscoveredNode",
        "StaticDiscovery",
        "RegistryDiscovery",
    ]
except ImportError:
    __all__ = [
        "RuntimeCoordinator",
        "NodeInfo",
        "ProcessDiscovery",
        "DiscoveredNode",
        "StaticDiscovery",
        "RegistryDiscovery",
    ]
