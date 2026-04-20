# Service Discovery and Transport

The distributed runtime uses two complementary systems: **service discovery** for nodes to find each other, and **transport** for remote management via HTTP. Together, they enable multi-node agent deployments where processes can be started, stopped, monitored, and triggered across machines.

```python
from promptise.runtime.distributed.discovery import StaticDiscovery, RegistryDiscovery
from promptise.runtime.distributed.transport import RuntimeTransport
from promptise.runtime import AgentRuntime

# Discovery: nodes find each other
discovery = StaticDiscovery(nodes={
    "node-1": "http://host1:9100",
    "node-2": "http://host2:9100",
})
nodes = await discovery.discover()

# Transport: expose a node for remote management
runtime = AgentRuntime()
async with RuntimeTransport(runtime, port=9100) as transport:
    # HTTP API now available at http://host:9100/
    ...
```

---

## Service Discovery

Service discovery provides mechanisms for runtime nodes to find each other. Two implementations are provided:

### ProcessDiscovery Protocol

All discovery implementations satisfy this protocol:

```python
from promptise.runtime.distributed.discovery import ProcessDiscovery

class ProcessDiscovery(Protocol):
    async def discover(self) -> list[DiscoveredNode]:
        """Discover available runtime nodes."""
        ...

    async def register(self, node_id: str, url: str, metadata: dict | None = None) -> None:
        """Register this node for discovery by others."""
        ...

    async def unregister(self, node_id: str) -> None:
        """Remove this node from discovery."""
        ...
```

### DiscoveredNode

Each discovered node is represented as a `DiscoveredNode` dataclass:

| Field | Type | Description |
|---|---|---|
| `node_id` | `str` | Unique node identifier |
| `url` | `str` | Base URL for the node's transport API |
| `discovered_at` | `float` | Timestamp when the node was discovered |
| `metadata` | `dict[str, Any]` | Additional node metadata |

---

## StaticDiscovery

A simple discovery mechanism for fixed-topology deployments where all node addresses are known at configuration time.

```python
from promptise.runtime.distributed.discovery import StaticDiscovery

discovery = StaticDiscovery(nodes={
    "node-1": "http://host1:9100",
    "node-2": "http://host2:9100",
    "node-3": "http://host3:9100",
})

# Discover all nodes
nodes = await discovery.discover()
for node in nodes:
    print(f"{node.node_id}: {node.url}")

# Add a node dynamically
await discovery.register("node-4", "http://host4:9100")

# Remove a node
await discovery.unregister("node-2")
```

Best for:

- Development and testing environments
- Small, fixed-size clusters
- Deployments where node addresses are known at startup

---

## RegistryDiscovery

A dynamic, in-process registry where nodes register themselves and discover each other. Stale registrations are automatically pruned after a configurable TTL.

```python
from promptise.runtime.distributed.discovery import RegistryDiscovery

registry = RegistryDiscovery(ttl=60.0)  # 60-second TTL

# Nodes register themselves at startup
await registry.register("node-1", "http://host1:9100", metadata={"region": "us-east"})
await registry.register("node-2", "http://host2:9100", metadata={"region": "eu-west"})

# Discover all active nodes
nodes = await registry.discover()  # Prunes stale entries first

# Nodes send periodic heartbeats to stay registered
await registry.heartbeat("node-1")

# Unregister on shutdown
await registry.unregister("node-1")
```

### TTL and stale pruning

Nodes that do not refresh their registration within the TTL are automatically removed:

```python
registry = RegistryDiscovery(ttl=30.0)

await registry.register("node-1", "http://host1:9100")
# ... 30+ seconds pass without heartbeat ...
nodes = await registry.discover()  # node-1 is pruned
```

### Thread safety

`RegistryDiscovery` uses an `asyncio.Lock` for all operations, making it safe for concurrent use.

Best for:

- Dynamic clusters where nodes come and go
- Single-process testing with multiple logical nodes
- Coordinator-hosted registry exposed via HTTP API

---

## RuntimeTransport

The `RuntimeTransport` exposes an `AgentRuntime` as an HTTP API for remote management. It runs an `aiohttp` server that handles process control, status queries, and event injection.

### Creating a transport

```python
from promptise.runtime import AgentRuntime
from promptise.runtime.distributed.transport import RuntimeTransport

runtime = AgentRuntime()
transport = RuntimeTransport(
    runtime,
    host="0.0.0.0",
    port=9100,
    node_id="node-1",
)

await transport.start()
# HTTP API available at http://0.0.0.0:9100/
await transport.stop()
```

### Context manager

```python
async with RuntimeTransport(runtime, port=9100) as transport:
    # Server running
    ...
# Server stopped automatically
```

---

## HTTP API Endpoints

The transport exposes the following REST endpoints:

### Health check

```
GET /health
```

Response:

```json
{
    "status": "healthy",
    "node_id": "node-1",
    "process_count": 3
}
```

### Runtime status

```
GET /status
```

Returns the full `runtime.status()` dict including per-process status, plus the `node_id`.

### List processes

```
GET /processes
```

Response:

```json
{
    "node_id": "node-1",
    "processes": [
        {"name": "watcher", "state": "running", "process_id": "abc123"},
        {"name": "handler", "state": "stopped", "process_id": "def456"}
    ]
}
```

### Process status

```
GET /processes/{name}/status
```

Returns the status dict for a single process. Returns 404 if the process does not exist.

### Start a process

```
POST /processes/{name}/start
```

Response (200):

```json
{"status": "started", "name": "watcher"}
```

### Stop a process

```
POST /processes/{name}/stop
```

Response (200):

```json
{"status": "stopped", "name": "watcher"}
```

### Restart a process

```
POST /processes/{name}/restart
```

Response (200):

```json
{"status": "restarted", "name": "watcher"}
```

### Inject event

```
POST /processes/{name}/event
```

Request body:

```json
{
    "trigger_id": "remote",
    "trigger_type": "remote",
    "payload": {"alert": "high_error_rate"},
    "metadata": {}
}
```

Response (202):

```json
{
    "status": "injected",
    "event_id": "...",
    "process": "watcher"
}
```

### Error responses

All endpoints return appropriate HTTP error codes:

| Code | Meaning |
|---|---|
| 200 | Success |
| 202 | Accepted (async operations like event injection) |
| 400 | Bad request (invalid JSON) |
| 404 | Process not found |
| 500 | Internal server error |

---

## Putting It Together

A complete distributed deployment:

```python
import asyncio
from promptise.runtime import AgentRuntime, ProcessConfig, TriggerConfig
from promptise.runtime.distributed.transport import RuntimeTransport
from promptise.runtime.distributed.coordinator import RuntimeCoordinator
from promptise.runtime.distributed.discovery import RegistryDiscovery

async def run_node(node_id: str, port: int):
    """Run a single runtime node."""
    runtime = AgentRuntime()
    await runtime.add_process("watcher", ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Monitor data.",
        triggers=[TriggerConfig(type="cron", cron_expression="*/5 * * * *")],
    ))
    await runtime.start_all()

    async with RuntimeTransport(runtime, port=port, node_id=node_id):
        # Node is now discoverable and remotely manageable
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    await runtime.stop_all()

async def run_coordinator():
    """Run the cluster coordinator."""
    async with RuntimeCoordinator() as coordinator:
        coordinator.register_node("node-1", "http://localhost:9100")
        coordinator.register_node("node-2", "http://localhost:9101")

        # Monitor cluster health
        while True:
            status = await coordinator.cluster_status()
            print(f"Nodes: {status['total_nodes']}, "
                  f"Healthy: {status['healthy_nodes']}, "
                  f"Processes: {status['total_processes']}")
            await asyncio.sleep(15)
```

---

## API Summary

### Discovery

| Class | Description |
|---|---|
| `ProcessDiscovery` | Protocol for discovery implementations |
| `DiscoveredNode` | Dataclass for discovered nodes |
| `StaticDiscovery(nodes)` | Fixed-topology discovery |
| `RegistryDiscovery(ttl)` | Dynamic registry with TTL |

### Transport

| Method / Property | Description |
|---|---|
| `RuntimeTransport(runtime, host, port, node_id)` | Create a transport server |
| `node_id` | This node's unique identifier |
| `port` | The port being listened on |
| `await start()` | Start the HTTP server |
| `await stop()` | Stop the HTTP server |

---

## Tips and Gotchas

!!! tip "Use RegistryDiscovery with the coordinator"
    The coordinator can host a `RegistryDiscovery` instance and expose it via its HTTP API. Nodes register at startup and send periodic heartbeats. Stale nodes are automatically pruned.

!!! tip "Event injection for cross-node coordination"
    Use the `POST /processes/{name}/event` endpoint to trigger processes on remote nodes. This enables cross-node agent coordination without shared message brokers.

!!! info "aiohttp shipped with base install"
    The `RuntimeTransport` uses `aiohttp`, which is included in the base `pip install promptise`.

!!! warning "No authentication on transport endpoints"
    The HTTP API does not include authentication or authorization. In production, always deploy behind a reverse proxy with TLS, or within a private network with network-level access controls.

!!! warning "RegistryDiscovery is in-process only"
    The `RegistryDiscovery` lives in memory within a single Python process. For multi-machine discovery, host it within the coordinator and expose registration/discovery via the coordinator's HTTP API.

!!! warning "Trailing slashes are stripped"
    Both `StaticDiscovery` and `RegistryDiscovery` strip trailing slashes from URLs to ensure consistent URL construction.

---

## What's Next

- [Coordinator](coordinator.md) -- cluster coordination and health monitoring
- [Configuration](../configuration.md) -- `DistributedConfig` reference
- [Runtime Manager](../runtime-manager.md) -- the `AgentRuntime` that runs on each node
- [Triggers Overview](../triggers/index.md) -- trigger events that can be injected remotely
