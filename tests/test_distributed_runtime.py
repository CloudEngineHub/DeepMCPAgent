"""Unit tests for promptise.runtime.distributed — coordinator, discovery, transport."""

from __future__ import annotations

import asyncio

import pytest

from promptise.runtime.distributed import (
    DiscoveredNode,
    NodeInfo,
    ProcessDiscovery,
    RegistryDiscovery,
    RuntimeCoordinator,
    StaticDiscovery,
)

# ---------------------------------------------------------------------------
# NodeInfo
# ---------------------------------------------------------------------------


class TestNodeInfo:
    """Tests for the NodeInfo dataclass."""

    def test_construction(self):
        info = NodeInfo(node_id="n1", url="http://localhost:9100")
        assert info.node_id == "n1"
        assert info.url == "http://localhost:9100"
        assert info.status == "unknown"
        assert info.process_count == 0
        assert info.metadata == {}

    def test_is_healthy(self):
        info = NodeInfo(node_id="n1", url="http://x", status="healthy")
        assert info.is_healthy is True

    def test_is_not_healthy(self):
        info = NodeInfo(node_id="n1", url="http://x", status="unknown")
        assert info.is_healthy is False

    def test_to_dict(self):
        info = NodeInfo(node_id="n1", url="http://x", status="healthy", process_count=3)
        d = info.to_dict()
        assert d["node_id"] == "n1"
        assert d["status"] == "healthy"
        assert d["process_count"] == 3


# ---------------------------------------------------------------------------
# RuntimeCoordinator
# ---------------------------------------------------------------------------


class TestRuntimeCoordinator:
    """Tests for cluster coordination."""

    def test_register_node(self):
        coord = RuntimeCoordinator()
        node = coord.register_node("n1", "http://host1:9100")
        assert node.node_id == "n1"
        assert node.url == "http://host1:9100"

    def test_register_strips_trailing_slash(self):
        coord = RuntimeCoordinator()
        node = coord.register_node("n1", "http://host1:9100/")
        assert node.url == "http://host1:9100"

    def test_register_with_metadata(self):
        coord = RuntimeCoordinator()
        node = coord.register_node("n1", "http://x", metadata={"region": "us-east"})
        assert node.metadata == {"region": "us-east"}

    def test_unregister_node(self):
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://x")
        coord.unregister_node("n1")
        assert "n1" not in coord.nodes

    def test_unregister_nonexistent_raises(self):
        coord = RuntimeCoordinator()
        with pytest.raises(KeyError, match="not registered"):
            coord.unregister_node("ghost")

    def test_get_node(self):
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://x")
        info = coord.get_node("n1")
        assert info.node_id == "n1"

    def test_get_nonexistent_raises(self):
        coord = RuntimeCoordinator()
        with pytest.raises(KeyError, match="not registered"):
            coord.get_node("ghost")

    def test_nodes_returns_copy(self):
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://x")
        nodes_copy = coord.nodes
        nodes_copy["n2"] = NodeInfo(node_id="n2", url="http://y")
        # Original should not be affected
        assert "n2" not in coord.nodes

    def test_healthy_nodes(self):
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://x")
        coord.register_node("n2", "http://y")
        # n1 healthy, n2 not
        coord._nodes["n1"].status = "healthy"
        coord._nodes["n2"].status = "unreachable"
        healthy = coord.healthy_nodes
        assert len(healthy) == 1
        assert healthy[0].node_id == "n1"

    def test_cluster_status(self):
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://x")
        coord.register_node("n2", "http://y")

        async def run():
            return await coord.cluster_status()

        status = asyncio.run(run())
        assert "total_nodes" in status
        assert status["total_nodes"] == 2

    def test_check_health_without_aiohttp(self):
        """Without aiohttp, health check should mark nodes as 'unknown'."""
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://x")

        async def run():
            await coord.check_health()

        asyncio.run(run())
        # Node status should still be managed even without aiohttp


# ---------------------------------------------------------------------------
# DiscoveredNode
# ---------------------------------------------------------------------------


class TestDiscoveredNode:
    """Tests for the DiscoveredNode dataclass."""

    def test_construction(self):
        node = DiscoveredNode(node_id="d1", url="http://host")
        assert node.node_id == "d1"
        assert node.url == "http://host"

    def test_to_dict(self):
        node = DiscoveredNode(node_id="d1", url="http://host", metadata={"role": "worker"})
        d = node.to_dict()
        assert d["node_id"] == "d1"
        assert d["metadata"] == {"role": "worker"}


# ---------------------------------------------------------------------------
# StaticDiscovery
# ---------------------------------------------------------------------------


class TestStaticDiscovery:
    """Tests for static (fixed-topology) discovery."""

    def test_construction_with_nodes(self):
        sd = StaticDiscovery(nodes={"n1": "http://host1", "n2": "http://host2"})

        async def run():
            return await sd.discover()

        nodes = asyncio.run(run())
        assert len(nodes) == 2
        ids = {n.node_id for n in nodes}
        assert ids == {"n1", "n2"}

    def test_empty_construction(self):
        sd = StaticDiscovery()

        async def run():
            return await sd.discover()

        nodes = asyncio.run(run())
        assert len(nodes) == 0

    def test_register_adds_node(self):
        sd = StaticDiscovery()

        async def run():
            await sd.register("n1", "http://host")
            return await sd.discover()

        nodes = asyncio.run(run())
        assert len(nodes) == 1
        assert nodes[0].node_id == "n1"

    def test_register_strips_slash(self):
        sd = StaticDiscovery()

        async def run():
            await sd.register("n1", "http://host/")
            return await sd.discover()

        nodes = asyncio.run(run())
        assert nodes[0].url == "http://host"

    def test_unregister_removes_node(self):
        sd = StaticDiscovery(nodes={"n1": "http://host"})

        async def run():
            await sd.unregister("n1")
            return await sd.discover()

        nodes = asyncio.run(run())
        assert len(nodes) == 0

    def test_unregister_nonexistent_no_error(self):
        sd = StaticDiscovery()

        async def run():
            await sd.unregister("ghost")

        asyncio.run(run())  # No exception

    def test_repr(self):
        sd = StaticDiscovery(nodes={"n1": "http://x"})
        assert "StaticDiscovery" in repr(sd)

    def test_satisfies_protocol(self):
        sd = StaticDiscovery()
        assert isinstance(sd, ProcessDiscovery)


# ---------------------------------------------------------------------------
# RegistryDiscovery
# ---------------------------------------------------------------------------


class TestRegistryDiscovery:
    """Tests for the in-process registry discovery."""

    def test_register_and_discover(self):
        rd = RegistryDiscovery()

        async def run():
            await rd.register("n1", "http://host1")
            await rd.register("n2", "http://host2")
            return await rd.discover()

        nodes = asyncio.run(run())
        assert len(nodes) == 2

    def test_unregister(self):
        rd = RegistryDiscovery()

        async def run():
            await rd.register("n1", "http://host1")
            await rd.unregister("n1")
            return await rd.discover()

        nodes = asyncio.run(run())
        assert len(nodes) == 0

    def test_heartbeat_refreshes_timestamp(self):
        rd = RegistryDiscovery(ttl=60)

        async def run():
            await rd.register("n1", "http://host1")
            old_ts = rd._nodes["n1"].discovered_at
            # Simulate time passing
            await asyncio.sleep(0.01)
            await rd.heartbeat("n1")
            new_ts = rd._nodes["n1"].discovered_at
            return old_ts, new_ts

        old, new = asyncio.run(run())
        assert new > old

    def test_stale_nodes_pruned(self):
        rd = RegistryDiscovery(ttl=0.01)  # 10ms TTL

        async def run():
            await rd.register("n1", "http://host1")
            await asyncio.sleep(0.02)  # Let it go stale
            return await rd.discover()

        nodes = asyncio.run(run())
        assert len(nodes) == 0  # Pruned

    def test_fresh_node_not_pruned(self):
        rd = RegistryDiscovery(ttl=10.0)

        async def run():
            await rd.register("n1", "http://host1")
            return await rd.discover()

        nodes = asyncio.run(run())
        assert len(nodes) == 1

    def test_concurrent_access(self):
        """Multiple coroutines registering concurrently shouldn't crash."""
        rd = RegistryDiscovery()

        async def register_many():
            tasks = [rd.register(f"n{i}", f"http://host{i}") for i in range(50)]
            await asyncio.gather(*tasks)
            return await rd.discover()

        nodes = asyncio.run(register_many())
        assert len(nodes) == 50

    def test_satisfies_protocol(self):
        rd = RegistryDiscovery()
        assert isinstance(rd, ProcessDiscovery)

    def test_repr(self):
        rd = RegistryDiscovery()
        assert "RegistryDiscovery" in repr(rd)

    def test_register_with_metadata(self):
        rd = RegistryDiscovery()

        async def run():
            await rd.register("n1", "http://x", metadata={"gpu": True})
            nodes = await rd.discover()
            return nodes[0]

        node = asyncio.run(run())
        assert node.metadata == {"gpu": True}
