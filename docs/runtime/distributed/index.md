---
seo_title: "Distributed Agent Runtime Across Machines"
description: "Run several agent runtimes on different machines as one cluster: what each node runs, registry or static discovery, DistributedConfig, auth and ClusterNode."
---

# Distributed Mode

Distributed mode lets several `AgentRuntime`s on different machines (or ports) work as one cluster. Each runtime runs a small management transport (an HTTP API for its processes); one runtime also hosts the coordinator, which knows every node and checks its health. You turn it on with `RuntimeConfig.distributed`; `start_all()` and `run_forever()` start it, `shutdown()` stops it. Use it when one machine is not enough, or when agents must run close to the systems they work on.

```python
# coordinator.py -- hosts the cluster coordinator and one process
import asyncio

from promptise.runtime import AgentRuntime, DistributedConfig, ProcessConfig, RuntimeConfig

config = RuntimeConfig(
    processes={
        "planner": ProcessConfig(model="openai:gpt-5-mini", instructions="Plan the work."),
    },
    distributed=DistributedConfig(
        enabled=True,
        coordinator=True,               # this runtime hosts the coordinator
        node_id="coordinator",
        transport_port=9100,
        auth_token="${CLUSTER_TOKEN}",  # resolved when the transport starts
        heartbeat_interval=5,
    ),
)

asyncio.run(AgentRuntime(config).run_forever())
```

```python
# node.py -- a worker node that registers with the coordinator
import asyncio

from promptise.runtime import AgentRuntime, DistributedConfig, ProcessConfig, RuntimeConfig

config = RuntimeConfig(
    processes={
        "worker": ProcessConfig(model="openai:gpt-5-mini", instructions="Do the work."),
    },
    distributed=DistributedConfig(
        enabled=True,
        node_id="worker-1",
        transport_port=9101,
        coordinator_url="http://127.0.0.1:9100",
        auth_token="${CLUSTER_TOKEN}",
        heartbeat_interval=5,
    ),
)

asyncio.run(AgentRuntime(config).run_forever())
```

Run both with the same token, then ask the coordinator:

```bash
export CLUSTER_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
python coordinator.py &
python node.py &

curl -s http://127.0.0.1:9100/health
# {"status": "healthy", "node_id": "coordinator", "process_count": 1}

curl -s -H "Authorization: Bearer $CLUSTER_TOKEN" http://127.0.0.1:9100/cluster/nodes
# {"node_id": "coordinator", "nodes": [
#    {"node_id": "coordinator", "url": "http://127.0.0.1:9100", "status": "healthy",
#     "process_count": 1, "metadata": {"role": "coordinator"}, ...},
#    {"node_id": "worker-1", "url": "http://127.0.0.1:9101", "status": "healthy",
#     "process_count": 1, "metadata": {"processes": 1}, ...}], "count": 2}

# Send the worker a trigger event through its transport
curl -s -X POST -H "Authorization: Bearer $CLUSTER_TOKEN" -H 'Content-Type: application/json' \
     -d '{"payload": {"task": "hello"}}' http://127.0.0.1:9101/processes/worker/event
# {"status": "injected", "event_id": "5650beaa-...", "process": "worker"}
```

Without the token, `/cluster/nodes` answers `401`. When `node.py` stops (Ctrl+C or SIGTERM), it unregisters, and the coordinator lists only itself again.

---

## What a node runs

With `distributed.enabled=True`, `AgentRuntime.start_all()` (and `run_forever()`) first starts a `ClusterNode`, then the processes. A failure to start it (a port in use, an unset token variable) raises; the processes don't start. `AgentRuntime.shutdown()` stops the processes, then the node. `runtime.cluster` is the running `ClusterNode` (or `None`), and `runtime.status()["cluster"]` summarizes it.

| Role | Config | Runs |
|---|---|---|
| Coordinator | `coordinator=True` | The [transport](discovery-transport.md#runtimetransport) with the `/cluster` routes, a [`RuntimeCoordinator`](coordinator.md) that health-checks every node each `heartbeat_interval`, and its discovery (registry or static). The coordinator lists itself |
| Node | `coordinator_url=...` | The transport, plus registration: register at start, re-register every `heartbeat_interval` (so a restarted coordinator finds the node again), unregister at stop |
| Stand-alone | neither | The transport only (a static coordinator elsewhere can list it) |

A node that can't reach the coordinator still starts; it logs the failure and keeps trying every `heartbeat_interval` (`runtime.status()["cluster"]["last_registration_error"]` shows why).

### Discovery: registry or static

| `discovery_method` | How the coordinator learns about nodes |
|---|---|
| `"registry"` (default) | Nodes register themselves at `POST /cluster/nodes` on the coordinator's transport. A node that hasn't re-registered for `node_timeout` seconds is dropped |
| `"static"` | The coordinator lists the nodes in `static_nodes` (`node_id -> URL`). Nodes don't register (`POST /cluster/nodes` answers `409`) |
| `"multicast"` | Rejected: `discovery_method='multicast' is not implemented. Use 'registry' (nodes register with the coordinator at coordinator_url) or 'static' (list the nodes in static_nodes on the coordinator).` |

A static coordinator:

```python
DistributedConfig(
    enabled=True,
    coordinator=True,
    discovery_method="static",
    static_nodes={
        "worker-1": "http://10.0.0.11:9100",
        "worker-2": "http://10.0.0.12:9100",
    },
    transport_host="0.0.0.0",
    advertise_url="http://10.0.0.10:9100",
    auth_token="${CLUSTER_TOKEN}",
)
```

The workers then run stand-alone (`enabled=True`, no `coordinator_url`) with the same token.

---

## DistributedConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Start the transport (and the coordinator or registration) with the runtime |
| `coordinator` | `bool` | `False` | This runtime hosts the coordinator |
| `coordinator_url` | `str \| None` | `None` | Base URL of the coordinator's transport; this node registers there |
| `node_id` | `str \| None` | `None` | This node's ID. Default `<hostname>-<port>` |
| `transport_host` | `str` | `"127.0.0.1"` | Interface the transport binds |
| `transport_port` | `int` | `9100` | Port of the transport; `0` picks a free port. Ports 1-1024 are rejected |
| `advertise_url` | `str \| None` | `None` | URL other nodes reach this one at. Default `http://<transport_host>:<port>`. Required with a wildcard `transport_host` on a coordinator or a registering node |
| `auth_token` | `SecretStr \| None` | `None` | The cluster bearer token, or a `${ENV_VAR}` reference resolved when the transport starts |
| `allow_unauthenticated` | `bool` | `False` | Allow a non-loopback transport or coordinator without `auth_token` |
| `discovery_method` | `"registry" \| "static" \| "multicast"` | `"registry"` | How the coordinator finds nodes (`"multicast"` is rejected) |
| `static_nodes` | `dict[str, str]` | `{}` | `node_id -> URL`, for a coordinator with `discovery_method="static"` |
| `allowed_node_hosts` | `list[str]` | `[]` | For a registry coordinator: the hosts a registering node's URL may name (`*.example.com` matches subdomains); others get `403`. Empty: any host |
| `heartbeat_interval` | `float` | `15.0` | Seconds between the coordinator's health checks and between a node's re-registrations |
| `node_timeout` | `float \| None` | `None` | Seconds without a successful health check (or re-registration) before a node counts as unhealthy (or is dropped from the registry). Default `3 * heartbeat_interval` |

URLs must be `http://` or `https://` with a host; a trailing slash is removed. Unknown keys are ignored with a `FutureWarning`. In a JSON dump (`RuntimeConfig.to_dict()`), a literal `auth_token` is written as `"**********"` and a `${VAR}` reference as is; loading the masked placeholder back is an error.

Inconsistent combinations fail when the config is built, each with a message that says what to change:

- `coordinator=True` together with `coordinator_url`;
- `static_nodes` without `coordinator=True` and `discovery_method="static"`;
- `discovery_method="static"` with `coordinator_url` (static nodes don't register);
- a non-loopback `transport_host`, or a `coordinator_url` on another host, without `auth_token` (unless `allow_unauthenticated=True`);
- a wildcard `transport_host` (`0.0.0.0`, `::`) without `advertise_url` on a coordinator or registering node;
- an empty `auth_token`.

`DEFAULT_DEVELOPMENT_CONFIG` and `DEFAULT_PRODUCTION_CONFIG` both leave distributed mode off: it needs your cluster's addresses and token, which no preset can know.

---

## Authentication

One cluster token authenticates everything:

- requests to every node's transport (all routes except `GET /health` need `Authorization: Bearer <token>`; compared in constant time);
- a node's registration with the coordinator;
- the coordinator's health checks and requests to the nodes.

Generate it once (`secrets.token_urlsafe(32)`), give it to every node through an environment variable, and reference it as `auth_token="${CLUSTER_TOKEN}"` so it never sits in code or config files. A transport without a token logs a warning at start: any local process could manage that runtime. A node started with the wrong token is refused by the coordinator (`401`) and keeps retrying, logging the failure.

The transport speaks plain HTTP. Across an untrusted network, put the nodes behind a TLS-terminating reverse proxy and use `https://` URLs for `coordinator_url`, `advertise_url` and `static_nodes`.

The coordinator calls every registered node URL with the cluster token, so a token holder could register an internal address and have the coordinator send requests there. On a registry coordinator, set `allowed_node_hosts` to your nodes' hosts (see [Security](discovery-transport.md#security)).

---

## ClusterNode

`ClusterNode(runtime, config)` is what `AgentRuntime` creates; you rarely build one yourself.

| Member | Description |
|---|---|
| `await start()` | Start the transport, then the coordinator (and its prune loop for registry discovery) or the registration. Raises `ValueError` when the token's `${ENV_VAR}` is not set, `OSError` when the port can't be bound. Calling it twice does nothing |
| `await stop()` | Unregister (best effort), stop the coordinator's health monitor and the transport |
| `role` | `"coordinator"`, `"node"` or `"standalone"` |
| `node_id`, `url` | This node's ID and advertised URL (known after `start()`) |
| `transport`, `coordinator`, `discovery` | The `RuntimeTransport`, and on a coordinator the `RuntimeCoordinator` and its discovery |
| `registered`, `last_registration_error` | Registration state of a node |
| `status()` | `node_id`, `role`, `url`, `discovery_method`, `authenticated`; plus `coordinator_url`, `registered` (and `last_registration_error`) on a node, `nodes` and `healthy_nodes` on a coordinator |
| `await prune()` | On a registry coordinator: unregister nodes whose registration expired; returns their IDs |

`AgentRuntime.start_distributed()` and `stop_distributed()` start and stop the node on their own, without the processes.

---

## Common pitfalls

- **Port 9100 twice.** `DistributedConfig.transport_port` and `OrchestrationAPI(port=...)` both default to `9100`. On one host, give each node and each control API its own port.
- **`0.0.0.0` without `advertise_url`.** Other nodes can't connect to a wildcard address; the config refuses it on a coordinator or registering node.
- **An unset token variable.** `auth_token="${CLUSTER_TOKEN}"` is resolved at start; when the variable is missing, `start_all()` raises `ValueError: distributed.auth_token: Environment variable 'CLUSTER_TOKEN' not found` and no process starts.
- **A short `node_timeout`.** A registry node that misses re-registrations for `node_timeout` seconds is dropped. Keep it at least two or three `heartbeat_interval`s (the default is three).
- **Expecting failover.** The coordinator monitors health; it does not move processes. When a node fails, your code picks a healthy node (`coordinator.healthy_nodes`) and starts the process there (`start_process_on_node`).
- **Expecting `promptise runtime start` to join a cluster.** The CLI runs a runtime without a `RuntimeConfig`, so distributed mode is configured in Python, as above.
- **The coordinator as a single point.** It is not replicated. If it goes away, nodes keep running and keep trying to register; a restarted coordinator finds them again within one `heartbeat_interval`.

---

## What's Next

- [Coordinator](coordinator.md) -- health monitoring, cluster status and remote operations
- [Discovery and Transport](discovery-transport.md) -- the HTTP routes of every node and the discovery classes
- [Runtime Manager](../runtime-manager.md) -- `AgentRuntime`, `start_all()`, `run_forever()` and `shutdown()`
- [Configuration](../configuration.md) -- `RuntimeConfig` and the other options

!!! tip "Learn the concept"

    - [Run Multiple AI Agents: One Runtime, Many Processes](https://www.promptise.com/guides/view/run-multiple-ai-agents) -- When one runtime with many processes is enough, and when to spread out.
    - [Autonomous AI Agent Runtime: Budgets, Crash Recovery, API](https://www.promptise.com/guides/view/autonomous-ai-agent-runtime) -- How a runtime supervises long-running agents.
