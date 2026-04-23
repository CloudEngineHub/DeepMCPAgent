"""Phase 5 tests: Distributed coordination (transport, coordinator, discovery).

Tests for:
- RuntimeTransport (HTTP management API)
- RuntimeCoordinator (node management, health monitoring)
- ProcessDiscovery (StaticDiscovery, RegistryDiscovery)
- Integration: transport + coordinator working together
"""

from __future__ import annotations

import asyncio
import time

import pytest

from promptise.runtime.config import ProcessConfig
from promptise.runtime.distributed.coordinator import (
    NodeInfo,
    RuntimeCoordinator,
)
from promptise.runtime.distributed.discovery import (
    DiscoveredNode,
    ProcessDiscovery,
    RegistryDiscovery,
    StaticDiscovery,
)
from promptise.runtime.runtime import AgentRuntime

# ======================================================================
# NodeInfo
# ======================================================================


class TestNodeInfo:
    """Test NodeInfo dataclass."""

    def test_creation(self):
        node = NodeInfo(node_id="n1", url="http://host:9100")
        assert node.node_id == "n1"
        assert node.url == "http://host:9100"
        assert node.status == "unknown"
        assert node.process_count == 0

    def test_to_dict(self):
        node = NodeInfo(node_id="n1", url="http://host:9100")
        d = node.to_dict()
        assert d["node_id"] == "n1"
        assert d["url"] == "http://host:9100"
        assert "last_heartbeat" in d

    def test_is_healthy(self):
        node = NodeInfo(node_id="n1", url="http://host:9100")
        assert not node.is_healthy  # status is "unknown"

        node.status = "healthy"
        assert node.is_healthy

        node.status = "unhealthy"
        assert not node.is_healthy


# ======================================================================
# DiscoveredNode
# ======================================================================


class TestDiscoveredNode:
    """Test DiscoveredNode dataclass."""

    def test_creation(self):
        node = DiscoveredNode(node_id="n1", url="http://host:9100")
        assert node.node_id == "n1"
        assert node.url == "http://host:9100"
        assert isinstance(node.discovered_at, float)

    def test_to_dict(self):
        node = DiscoveredNode(
            node_id="n1",
            url="http://host:9100",
            metadata={"region": "us-east"},
        )
        d = node.to_dict()
        assert d["node_id"] == "n1"
        assert d["metadata"]["region"] == "us-east"


# ======================================================================
# RuntimeCoordinator
# ======================================================================


class TestRuntimeCoordinator:
    """Test cluster coordinator."""

    def test_register_node(self):
        coord = RuntimeCoordinator()
        node = coord.register_node("n1", "http://host:9100")
        assert node.node_id == "n1"
        assert "n1" in coord.nodes

    def test_register_node_with_metadata(self):
        coord = RuntimeCoordinator()
        node = coord.register_node("n1", "http://host:9100", metadata={"region": "us-east"})
        assert node.metadata["region"] == "us-east"

    def test_unregister_node(self):
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://host:9100")
        coord.unregister_node("n1")
        assert "n1" not in coord.nodes

    def test_unregister_nonexistent_raises(self):
        coord = RuntimeCoordinator()
        with pytest.raises(KeyError):
            coord.unregister_node("nope")

    def test_get_node(self):
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://host:9100")
        node = coord.get_node("n1")
        assert node.node_id == "n1"

    def test_get_nonexistent_raises(self):
        coord = RuntimeCoordinator()
        with pytest.raises(KeyError):
            coord.get_node("nope")

    def test_healthy_nodes_empty_initially(self):
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://host:9100")
        assert len(coord.healthy_nodes) == 0  # status is "unknown"

    def test_healthy_nodes_after_marking(self):
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://host:9100")
        coord.get_node("n1").status = "healthy"
        assert len(coord.healthy_nodes) == 1

    def test_nodes_property(self):
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://host1:9100")
        coord.register_node("n2", "http://host2:9100")
        nodes = coord.nodes
        assert len(nodes) == 2
        assert "n1" in nodes
        assert "n2" in nodes

    @pytest.mark.asyncio
    async def test_check_health_no_aiohttp(self):
        """Without aiohttp connectivity, nodes should be marked unknown."""
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://host:9100")

        # Mock aiohttp as unavailable
        import promptise.runtime.distributed.coordinator as coord_module

        original = coord_module.HAS_AIOHTTP
        coord_module.HAS_AIOHTTP = False
        try:
            results = await coord.check_health()
            assert "n1" in results
            assert results["n1"]["status"] == "unknown"
        finally:
            coord_module.HAS_AIOHTTP = original

    @pytest.mark.asyncio
    async def test_cluster_status(self):
        """Cluster status should aggregate node info."""
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://host1:9100")
        coord.register_node("n2", "http://host2:9100")

        import promptise.runtime.distributed.coordinator as coord_module

        original = coord_module.HAS_AIOHTTP
        coord_module.HAS_AIOHTTP = False
        try:
            status = await coord.cluster_status()
            assert status["total_nodes"] == 2
            assert "nodes" in status
            assert "n1" in status["nodes"]
            assert "n2" in status["nodes"]
        finally:
            coord_module.HAS_AIOHTTP = original

    def test_repr(self):
        coord = RuntimeCoordinator()
        coord.register_node("n1", "http://host:9100")
        r = repr(coord)
        assert "RuntimeCoordinator" in r
        assert "nodes=1" in r


# ======================================================================
# StaticDiscovery
# ======================================================================


class TestStaticDiscovery:
    """Test static node discovery."""

    @pytest.mark.asyncio
    async def test_discover_from_constructor(self):
        discovery = StaticDiscovery(
            nodes={
                "n1": "http://host1:9100",
                "n2": "http://host2:9100",
            }
        )
        nodes = await discovery.discover()
        assert len(nodes) == 2
        node_ids = {n.node_id for n in nodes}
        assert node_ids == {"n1", "n2"}

    @pytest.mark.asyncio
    async def test_discover_empty(self):
        discovery = StaticDiscovery()
        nodes = await discovery.discover()
        assert len(nodes) == 0

    @pytest.mark.asyncio
    async def test_register(self):
        discovery = StaticDiscovery()
        await discovery.register("n1", "http://host1:9100")
        nodes = await discovery.discover()
        assert len(nodes) == 1
        assert nodes[0].node_id == "n1"

    @pytest.mark.asyncio
    async def test_unregister(self):
        discovery = StaticDiscovery(nodes={"n1": "http://host1:9100"})
        await discovery.unregister("n1")
        nodes = await discovery.discover()
        assert len(nodes) == 0

    @pytest.mark.asyncio
    async def test_url_trailing_slash_stripped(self):
        discovery = StaticDiscovery(nodes={"n1": "http://host1:9100/"})
        nodes = await discovery.discover()
        assert nodes[0].url == "http://host1:9100"

    def test_repr(self):
        discovery = StaticDiscovery(nodes={"n1": "http://host1:9100"})
        assert "StaticDiscovery" in repr(discovery)
        assert "nodes=1" in repr(discovery)

    def test_protocol_compliance(self):
        """StaticDiscovery should be a ProcessDiscovery."""
        assert isinstance(StaticDiscovery(), ProcessDiscovery)


# ======================================================================
# RegistryDiscovery
# ======================================================================


class TestRegistryDiscovery:
    """Test registry-based node discovery."""

    @pytest.mark.asyncio
    async def test_register_and_discover(self):
        registry = RegistryDiscovery()
        await registry.register("n1", "http://host1:9100")
        await registry.register("n2", "http://host2:9100")

        nodes = await registry.discover()
        assert len(nodes) == 2

    @pytest.mark.asyncio
    async def test_unregister(self):
        registry = RegistryDiscovery()
        await registry.register("n1", "http://host1:9100")
        await registry.unregister("n1")

        nodes = await registry.discover()
        assert len(nodes) == 0

    @pytest.mark.asyncio
    async def test_heartbeat_refreshes(self):
        registry = RegistryDiscovery(ttl=1.0)
        await registry.register("n1", "http://host1:9100")

        original_time = registry._nodes["n1"].discovered_at
        await asyncio.sleep(0.05)
        await registry.heartbeat("n1")

        assert registry._nodes["n1"].discovered_at > original_time

    @pytest.mark.asyncio
    async def test_stale_nodes_pruned(self):
        """Nodes that exceed TTL should be pruned on discover()."""
        registry = RegistryDiscovery(ttl=0.1)
        await registry.register("n1", "http://host1:9100")

        # Manually set discovered_at to the past
        registry._nodes["n1"].discovered_at = time.monotonic() - 1.0

        nodes = await registry.discover()
        assert len(nodes) == 0

    @pytest.mark.asyncio
    async def test_register_with_metadata(self):
        registry = RegistryDiscovery()
        await registry.register("n1", "http://host1:9100", metadata={"region": "eu-west"})
        nodes = await registry.discover()
        assert nodes[0].metadata["region"] == "eu-west"

    def test_repr(self):
        registry = RegistryDiscovery(ttl=60)
        assert "RegistryDiscovery" in repr(registry)
        assert "ttl=60" in repr(registry)

    def test_protocol_compliance(self):
        """RegistryDiscovery should be a ProcessDiscovery."""
        assert isinstance(RegistryDiscovery(), ProcessDiscovery)


# ======================================================================
# RuntimeTransport (requires aiohttp)
# ======================================================================


class TestRuntimeTransport:
    """Test HTTP transport endpoints."""

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        """GET /health should return healthy status."""
        try:
            from aiohttp import ClientSession
        except ImportError:
            pytest.skip("aiohttp not installed")

        from promptise.runtime.distributed.transport import RuntimeTransport

        runtime = AgentRuntime()
        transport = RuntimeTransport(runtime, host="127.0.0.1", port=19901, node_id="test-node")

        await transport.start()
        try:
            async with ClientSession() as session:
                async with session.get("http://127.0.0.1:19901/health") as resp:
                    assert resp.status == 200
                    body = await resp.json()
                    assert body["status"] == "healthy"
                    assert body["node_id"] == "test-node"
                    assert body["process_count"] == 0
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_status_endpoint(self):
        """GET /status should return runtime status."""
        try:
            from aiohttp import ClientSession
        except ImportError:
            pytest.skip("aiohttp not installed")

        from promptise.runtime.distributed.transport import RuntimeTransport

        runtime = AgentRuntime()
        config = ProcessConfig(model="openai:gpt-5-mini")
        await runtime.add_process("test-proc", config)

        transport = RuntimeTransport(runtime, host="127.0.0.1", port=19902, node_id="test-node")

        await transport.start()
        try:
            async with ClientSession() as session:
                async with session.get("http://127.0.0.1:19902/status") as resp:
                    assert resp.status == 200
                    body = await resp.json()
                    assert body["process_count"] == 1
                    assert body["node_id"] == "test-node"
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_list_processes_endpoint(self):
        """GET /processes should list all processes."""
        try:
            from aiohttp import ClientSession
        except ImportError:
            pytest.skip("aiohttp not installed")

        from promptise.runtime.distributed.transport import RuntimeTransport

        runtime = AgentRuntime()
        config = ProcessConfig(model="openai:gpt-5-mini")
        await runtime.add_process("proc-a", config)
        await runtime.add_process("proc-b", config)

        transport = RuntimeTransport(runtime, host="127.0.0.1", port=19903)

        await transport.start()
        try:
            async with ClientSession() as session:
                async with session.get("http://127.0.0.1:19903/processes") as resp:
                    assert resp.status == 200
                    body = await resp.json()
                    names = {p["name"] for p in body["processes"]}
                    assert names == {"proc-a", "proc-b"}
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_process_status_endpoint(self):
        """GET /processes/{name}/status should return process status."""
        try:
            from aiohttp import ClientSession
        except ImportError:
            pytest.skip("aiohttp not installed")

        from promptise.runtime.distributed.transport import RuntimeTransport

        runtime = AgentRuntime()
        config = ProcessConfig(model="openai:gpt-5-mini")
        await runtime.add_process("my-proc", config)

        transport = RuntimeTransport(runtime, host="127.0.0.1", port=19904)

        await transport.start()
        try:
            async with ClientSession() as session:
                async with session.get("http://127.0.0.1:19904/processes/my-proc/status") as resp:
                    assert resp.status == 200
                    body = await resp.json()
                    assert body["name"] == "my-proc"
                    assert body["state"] == "created"
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_process_not_found(self):
        """GET /processes/{name}/status for unknown process returns 404."""
        try:
            from aiohttp import ClientSession
        except ImportError:
            pytest.skip("aiohttp not installed")

        from promptise.runtime.distributed.transport import RuntimeTransport

        runtime = AgentRuntime()
        transport = RuntimeTransport(runtime, host="127.0.0.1", port=19905)

        await transport.start()
        try:
            async with ClientSession() as session:
                async with session.get(
                    "http://127.0.0.1:19905/processes/nonexistent/status"
                ) as resp:
                    assert resp.status == 404
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_inject_event_endpoint(self):
        """POST /processes/{name}/event should inject a trigger event."""
        try:
            from aiohttp import ClientSession
        except ImportError:
            pytest.skip("aiohttp not installed")

        from promptise.runtime.distributed.transport import RuntimeTransport

        runtime = AgentRuntime()
        config = ProcessConfig(model="openai:gpt-5-mini")
        await runtime.add_process("my-proc", config)

        transport = RuntimeTransport(runtime, host="127.0.0.1", port=19906)

        await transport.start()
        try:
            async with ClientSession() as session:
                async with session.post(
                    "http://127.0.0.1:19906/processes/my-proc/event",
                    json={
                        "trigger_type": "remote",
                        "payload": {"key": "value"},
                    },
                ) as resp:
                    assert resp.status == 202
                    body = await resp.json()
                    assert body["status"] == "injected"
                    assert body["process"] == "my-proc"

            # Check the event is in the queue
            process = runtime.get_process("my-proc")
            assert process._trigger_queue.qsize() == 1
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Transport should work as context manager."""
        try:
            from aiohttp import ClientSession
        except ImportError:
            pytest.skip("aiohttp not installed")

        from promptise.runtime.distributed.transport import RuntimeTransport

        runtime = AgentRuntime()
        async with RuntimeTransport(runtime, host="127.0.0.1", port=19907):
            async with ClientSession() as session:
                async with session.get("http://127.0.0.1:19907/health") as resp:
                    assert resp.status == 200

    def test_repr(self):
        pytest.importorskip("aiohttp")

        from promptise.runtime.distributed.transport import RuntimeTransport

        runtime = AgentRuntime()
        transport = RuntimeTransport(runtime, port=9100, node_id="my-node")
        r = repr(transport)
        assert "RuntimeTransport" in r
        assert "my-node" in r
        assert "9100" in r


# ======================================================================
# Integration: Transport + Coordinator
# ======================================================================


class TestTransportCoordinatorIntegration:
    """Test transport and coordinator working together."""

    @pytest.mark.asyncio
    async def test_coordinator_health_check_via_transport(self):
        """Coordinator should check health via transport HTTP API."""
        pytest.importorskip("aiohttp")

        from promptise.runtime.distributed.transport import RuntimeTransport

        # Set up runtime + transport
        runtime = AgentRuntime()
        config = ProcessConfig(model="openai:gpt-5-mini")
        await runtime.add_process("test-proc", config)

        transport = RuntimeTransport(runtime, host="127.0.0.1", port=19908, node_id="node-a")
        await transport.start()

        try:
            # Set up coordinator pointing to the transport
            coordinator = RuntimeCoordinator()
            coordinator.register_node("node-a", "http://127.0.0.1:19908")

            # Check health
            health = await coordinator.check_health()
            assert "node-a" in health
            assert health["node-a"]["status"] == "healthy"
            assert coordinator.get_node("node-a").status == "healthy"
            assert coordinator.get_node("node-a").process_count == 1
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_coordinator_remote_status(self):
        """Coordinator should fetch remote node status."""
        pytest.importorskip("aiohttp")

        from promptise.runtime.distributed.transport import RuntimeTransport

        runtime = AgentRuntime()
        config = ProcessConfig(model="openai:gpt-5-mini")
        await runtime.add_process("proc-1", config)

        transport = RuntimeTransport(runtime, host="127.0.0.1", port=19909)
        await transport.start()

        try:
            coordinator = RuntimeCoordinator()
            coordinator.register_node("node-1", "http://127.0.0.1:19909")

            status = await coordinator.get_node_status("node-1")
            assert status["process_count"] == 1
            assert "proc-1" in status["processes"]
        finally:
            await transport.stop()
