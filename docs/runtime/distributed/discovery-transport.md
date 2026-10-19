# Service Discovery and Transport

The distributed runtime uses two complementary systems: **service discovery** for nodes to find each other, and **transport** for remote management via HTTP. Together, they enable multi-node agent deployments where processes can be started, stopped, monitored, and triggered across machines.

With [distributed mode](index.md) (`DistributedConfig(enabled=True)`), every runtime builds its own `RuntimeTransport`, and the coordinator its discovery, from the config. This page documents the pieces themselves: the HTTP routes every node serves, and the discovery classes, for when you call them directly or assemble a cluster by hand.

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
    # HTTP API now available at http://127.0.0.1:9100/ (this machine only)
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

A dynamic registry where nodes register themselves and discover each other. Stale registrations are pruned after a TTL (default 60 seconds). The object lives in one Python process; a coordinator started with distributed mode holds one (with `ttl=node_timeout`) and exposes it over HTTP as `POST /cluster/nodes`, so nodes on other machines register by calling that route.

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
- The coordinator of a cluster (`discovery_method="registry"`, the default)

Over HTTP, a node refreshes its registration by posting it again (a `ClusterNode` does that every `heartbeat_interval`); `heartbeat()` is the in-process equivalent.

---

## RuntimeTransport

The `RuntimeTransport` exposes an `AgentRuntime` as an HTTP API for remote management. It runs an `aiohttp` server that handles process control, status queries, and event injection.

### Creating a transport

```python
import os

from promptise.runtime import AgentRuntime
from promptise.runtime.distributed.transport import RuntimeTransport

runtime = AgentRuntime()
transport = RuntimeTransport(
    runtime,
    host="0.0.0.0",                     # reachable from other machines
    port=9100,
    node_id="node-1",
    auth_token=os.environ["PROMPTISE_NODE_TOKEN"],  # required off loopback
)

await transport.start()
# HTTP API available on port 9100 of every interface
await transport.stop()
```

The default `host` is `127.0.0.1` (this machine only). Pass `port=0` to let the OS pick a free port; `transport.port` reports it after `start()`.

| Argument | Default | Description |
|---|---|---|
| `runtime` | required | The `AgentRuntime` to manage |
| `host` | `"127.0.0.1"` | Interface to bind |
| `port` | `9100` | Port (`0`: a free one) |
| `node_id` | `"node-1"` | This node's ID (in `/health` and `/status`) |
| `auth_token` | `None` | Bearer token for every route except `GET /health`; required off loopback |
| `allow_unauthenticated` | `False` | Allow a non-loopback bind without a token |
| `coordinator` | `None` | A `RuntimeCoordinator`: adds the `/cluster` routes (this node hosts the coordinator) |
| `discovery` | `None` | The coordinator's discovery (`RegistryDiscovery` or `StaticDiscovery`) |
| `allowed_node_hosts` | `None` | Hosts a node registering at `POST /cluster/nodes` may name in its URL (`*.example.com` matches subdomains); others get `403`. `None` or empty: any host |

### Security

The transport can start, stop and drive your agents, so it is locked down by default:

| Rule | Behaviour |
|---|---|
| Bearer token | With `auth_token` set, every endpoint except `GET /health` requires `Authorization: Bearer <token>` (compared in constant time). Missing or wrong token, including a header with non-ASCII bytes: `401`. |
| Public bind needs a token | `RuntimeTransport(host="0.0.0.0")` (or any non-loopback address) without `auth_token` raises `ValueError`. If an authenticating proxy or a private network you control already protects the port, opt out explicitly with `allow_unauthenticated=True` (a warning is logged at start). |
| Loopback bind blocks browsers | On `127.0.0.1` / `localhost` / `::1`, a request whose `Host` header is not a loopback name gets `421` (DNS rebinding), and a request with an `Origin` header from a non-loopback origin gets `403`. A web page you visit cannot drive the local runtime. Non-browser clients (the coordinator, `curl`, scripts) send no `Origin` and are unaffected. |
| Empty token | `auth_token=""` raises `ValueError`. |
| Node URLs | `POST /cluster/nodes` takes `http://` or `https://` URLs with a host only (`422` otherwise), and only hosts in `allowed_node_hosts` when it is set (`403`). |

!!! warning "Registered URLs receive the cluster token"
    The coordinator sends health checks and management calls, with the cluster token, to every URL registered at `POST /cluster/nodes`. Anyone holding the token can register a URL, and so make the coordinator send requests (and the token) to any host it can reach, internal services included. Set `allowed_node_hosts` (`DistributedConfig.allowed_node_hosts`) to your nodes' host names or a domain such as `*.cluster.internal`, or use static discovery, where only your config lists nodes.

Use TLS (a reverse proxy) when the token crosses an untrusted network: the transport itself speaks plain HTTP.

!!! warning "Changed in 1.3.0"
    A non-loopback bind without `auth_token` used to log a warning and serve every endpoint unauthenticated. It now raises `ValueError` unless you pass `allow_unauthenticated=True`.

### Context manager

```python
async with RuntimeTransport(runtime, port=9100, auth_token="node-token") as transport:
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

Request body (a JSON object; `payload` and `metadata` must be objects when given and default to `{}`):

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

### Cluster routes (coordinator only)

A transport created with `coordinator=` also serves `GET /cluster/nodes`, `POST /cluster/nodes` (register or refresh `{"node_id", "url", "metadata"}`), `DELETE /cluster/nodes/{node_id}` and `GET /cluster/status`. See [Coordinator](coordinator.md#hosted-by-a-runtime) for the request and response details.

### Error responses

All endpoints return appropriate HTTP error codes:

| Code | Meaning |
|---|---|
| 200 | Success |
| 202 | Accepted (async operations like event injection) |
| 400 | Bad request (invalid JSON, body or `payload`/`metadata` not an object) |
| 401 | Missing or wrong bearer token |
| 403 | Cross-origin browser request (loopback bind) |
| 404 | Process (or, on `/cluster`, node) not found |
| 409 | Cluster routes: registering with static discovery, or the coordinator's own ID |
| 421 | `Host` header is not a loopback name (loopback bind) |
| 422 | Cluster routes: a bad `node_id`, `url` or `metadata` |
| 500 | Internal server error |

---

## Putting It Together

For a real cluster, let the config wire everything: see the two-node setup in [Distributed Mode](index.md). By hand, the same pieces look like this (a node with a transport, and a coordinator that knows it):

```python
import asyncio
import os

from promptise.runtime import AgentRuntime, ProcessConfig
from promptise.runtime.distributed.coordinator import RuntimeCoordinator
from promptise.runtime.distributed.transport import RuntimeTransport

TOKEN = os.environ["CLUSTER_TOKEN"]

async def run_node() -> None:
    runtime = AgentRuntime()
    await runtime.add_process("watcher", ProcessConfig(
        model="openai:gpt-5-mini", instructions="Monitor data.",
    ))
    await runtime.start_all()
    async with RuntimeTransport(runtime, port=9101, node_id="node-1", auth_token=TOKEN):
        await asyncio.Event().wait()   # serve until cancelled
    await runtime.shutdown()

async def run_coordinator() -> None:
    async with RuntimeCoordinator(auth_token=TOKEN) as coordinator:
        coordinator.register_node("node-1", "http://127.0.0.1:9101")
        while True:
            status = await coordinator.cluster_status()
            print(status["healthy_nodes"], "of", status["total_nodes"], "nodes healthy")
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
| `RuntimeTransport(runtime, host, port, node_id, auth_token, allow_unauthenticated, coordinator, discovery)` | Create a transport server |
| `node_id` | This node's unique identifier |
| `port` | The port being listened on (the OS-assigned one after `start()` with `port=0`) |
| `await start()` | Start the HTTP server |
| `await stop()` | Stop the HTTP server |

---

## Tips and Gotchas

!!! tip "Let distributed mode host the registry"
    `DistributedConfig(coordinator=True)` gives the coordinator a `RegistryDiscovery` and serves it as `POST /cluster/nodes`; nodes with `coordinator_url` register at start and re-register every `heartbeat_interval`. Stale nodes are pruned.

!!! tip "Event injection for cross-node coordination"
    Use the `POST /processes/{name}/event` endpoint to trigger processes on remote nodes. This enables cross-node agent coordination without shared message brokers.

!!! info "aiohttp shipped with base install"
    The `RuntimeTransport` uses `aiohttp`, which is included in the base `pip install promptise`.

!!! warning "One token per node, plain HTTP"
    `auth_token` is a single shared secret with full control of the node, and the transport does not terminate TLS. In production, put nodes behind a TLS reverse proxy or on a private network, and give each node its own token (`RuntimeCoordinator.register_node(..., auth_token=...)`).

!!! warning "RegistryDiscovery lives in one Python process"
    The registry is in memory. Across machines, nodes reach it through the coordinator's transport (`/cluster/nodes`); a restarted coordinator starts with an empty registry and fills it as nodes re-register.

!!! warning "Trailing slashes are stripped"
    Both `StaticDiscovery` and `RegistryDiscovery` strip trailing slashes from URLs to ensure consistent URL construction.

---

## What's Next

- [Distributed Mode](index.md) -- `DistributedConfig`, `ClusterNode` and a two-node setup
- [Coordinator](coordinator.md) -- cluster coordination and health monitoring
- [Configuration](../configuration.md) -- `DistributedConfig` reference
- [Runtime Manager](../runtime-manager.md) -- the `AgentRuntime` that runs on each node
- [Triggers Overview](../triggers/index.md) -- trigger events that can be injected remotely

!!! tip "Learn the concept"

    - [Run Multiple AI Agents: One Runtime, Many Processes](https://www.promptise.com/guides/view/run-multiple-ai-agents) -- Many processes in one runtime, and when to spread them across nodes.
    - [AI Agent Control API: Manage Running Agents Over HTTP](https://www.promptise.com/guides/view/ai-agent-control-api) -- Manage running agents over HTTP with a bearer token.
