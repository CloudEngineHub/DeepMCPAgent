---
title: "Deploying AI Agents to Kubernetes with Promptise"
description: "A concrete, copy-paste path from local server to a K8s Deployment: liveness/readiness/startup probes wired to real health checks, a /metrics endpoint for…"
keywords: "deploying AI agents to Kubernetes, MCP server Kubernetes deployment, liveness readiness startup probes, promptise serve http, Prometheus metrics for agents, K8s agent health checks"
date: 2026-07-16
slug: deploying-ai-agents-to-kubernetes
categories:
  - Production
---

# Deploying AI Agents to Kubernetes with Promptise

Deploying AI agents to Kubernetes is where most agent projects stall: the tool server runs fine on your laptop, but the cluster wants HTTP endpoints, real health probes, and metrics it can scale on — none of which a bare `python server.py` gives you. Promptise closes that gap with pieces you can wire in a few lines: a `HealthCheck` for dependency-aware readiness, `PrometheusMiddleware` for metrics, and `promptise serve --transport http` to run the server without boilerplate. By the end of this post you'll have a copy-paste path from a local MCP server to a running Kubernetes `Deployment` with liveness, readiness, and startup probes and a scrapeable `/metrics` endpoint.

<!-- more -->

This guide is about the servers your agents call — the MCP tool servers that hold your business logic. That is the piece you containerize and scale. The agent that consumes them connects over HTTP, and we cover that at the end.

## From local server to an MCP server Kubernetes deployment

Locally, you run a Promptise MCP server over stdio for a desktop client or over HTTP for everything else. For a cluster you want HTTP (Streamable HTTP), because Kubernetes probes, Services, and horizontal scaling all speak HTTP. The [Deployment guide](../../mcp/server/deployment.md) walks through transport selection in detail; the short version is: pick `http`, bind `0.0.0.0`, and expose a port.

The CLI removes the `if __name__ == "__main__"` boilerplate entirely:

```bash
# promptise serve http — run any server object over Streamable HTTP
promptise serve server:server --transport http --host 0.0.0.0 --port 8080
```

The target is `module:attribute` — Promptise imports `server.py` and serves the `server` object it finds. That single command is what your container's `CMD` runs in production.

## Building a cluster-ready server module

Here is a complete, runnable `server.py` that turns three Promptise primitives into a Kubernetes-ready server. It records Prometheus metrics on every tool call, reports readiness based on a real dependency check, and exposes `/metrics` on a sidecar port:

```python
# server.py — an MCP tool server ready for Kubernetes
# pip install promptise prometheus-client
from prometheus_client import start_http_server
from promptise.mcp.server import MCPServer, HealthCheck, PrometheusMiddleware

server = MCPServer(name="k8s-agent-tools")


@server.tool()
async def search_orders(customer_id: str) -> list[dict]:
    """Look up recent orders for a customer."""
    return [{"id": "A-1001", "customer_id": customer_id, "total": 42.0}]


# 1) Record Prometheus metrics into the default registry, on every tool call.
server.add_middleware(PrometheusMiddleware(namespace="mcp"))

# 2) Dependency-aware readiness, surfaced as the health://readiness resource.
health = HealthCheck()


async def database_ready() -> bool:
    # Replace with a real ping to your datastore.
    return True


health.add_check("database", database_ready, required_for_ready=True)
health.register_resources(server)  # registers health://liveness + health://readiness

# 3) Expose /metrics on a sidecar port so Prometheus (and the HPA) can scrape it.
start_http_server(9090)
```

Run it in the cluster with the exact `promptise serve` command from above. Because `start_http_server(9090)` runs at import time, it also binds when the CLI imports your module — so `/metrics` is live whether you launch via the CLI or `python server.py`.

## Wiring liveness, readiness, and startup probes to real health checks

`HealthCheck` is the feature that makes K8s agent health checks meaningful. You register named checks with `add_check(...)`; a check marked `required_for_ready=True` flips readiness to `not_ready` when it fails. Calling `register_resources(server)` publishes two MCP resources:

- `health://liveness` — process is up, with uptime.
- `health://readiness` — aggregated status of every required dependency check.

These are protocol-level resources any MCP client can read. Kubernetes probes, however, target HTTP. As the deployment guide notes, MCP health lives as resources, so for kubelet probes you point at the transport's `/mcp` endpoint. Liveness, readiness, and startup probes all target the same HTTP endpoint, while `HealthCheck` gives your agents and dashboards dependency-aware readiness at the protocol level:

```yaml title="Deployment — liveness, readiness, startup probes"
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-tools
spec:
  replicas: 3
  selector:
    matchLabels: { app: agent-tools }
  template:
    metadata:
      labels: { app: agent-tools }
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "9090"
        prometheus.io/path: "/metrics"
    spec:
      containers:
        - name: agent-tools
          image: registry.example.com/agent-tools:1.0.0
          command: ["promptise", "serve", "server:server",
                    "--transport", "http", "--host", "0.0.0.0", "--port", "8080"]
          ports:
            - { name: mcp, containerPort: 8080 }
            - { name: metrics, containerPort: 9090 }
          env:
            - name: OPENAI_API_KEY
              valueFrom: { secretKeyRef: { name: agent-secrets, key: openai } }
          # Startup: give slow cold starts room before liveness kicks in.
          startupProbe:
            httpGet: { path: /mcp, port: 8080 }
            failureThreshold: 30
            periodSeconds: 2
          # Liveness: restart the pod if the process wedges.
          livenessProbe:
            httpGet: { path: /mcp, port: 8080 }
            periodSeconds: 10
          # Readiness: pull the pod out of the Service until it can serve.
          readinessProbe:
            httpGet: { path: /mcp, port: 8080 }
            periodSeconds: 5
```

A startup probe with a generous `failureThreshold` prevents Kubernetes from killing a pod that is still loading models or opening pooled connections. Once it passes, liveness and readiness take over on tight intervals. For a deeper treatment of failure isolation — circuit breakers alongside these checks — see [Resilience patterns](../../mcp/server/resilience-patterns.md).

## Exposing Prometheus metrics for agents (and the HPA)

`PrometheusMiddleware` records three standard series on every tool call, so Prometheus metrics for agents come for free once the middleware is added:

- `mcp_tool_calls_total` — counter, labelled by `tool` and `status`.
- `mcp_tool_duration_seconds` — histogram, labelled by `tool`.
- `mcp_tool_in_flight` — gauge, labelled by `tool`.

Because the middleware registers into the default Prometheus registry, the standard `prometheus_client.start_http_server(9090)` in your module exposes them at `/metrics` with no extra route wiring. If you prefer to serve metrics through the MCP protocol instead of a sidecar port, `PrometheusMiddleware.get_metrics_text()` returns the same text exposition format — the [Production features](../../mcp/server/production-features.md) page shows how to serve it from a resource handler.

The `mcp_tool_in_flight` gauge is the natural signal for autoscaling. Scrape it with Prometheus, expose it through the prometheus-adapter as a custom metric, and drive a `HorizontalPodAutoscaler`:

```yaml title="Scale on in-flight tool calls"
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: agent-tools
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: agent-tools }
  minReplicas: 3
  maxReplicas: 20
  metrics:
    - type: Pods
      pods:
        metric: { name: mcp_tool_in_flight }
        target: { type: AverageValue, averageValue: "10" }
```

Promptise does not estimate LLM provider costs, so scale on load signals you control — in-flight calls and latency — rather than a synthetic cost metric.

## Connecting an agent to the deployed server

Once the server is running behind a Service, an agent reaches it over HTTP with a typed server spec. This is the other half of deploying AI agents to Kubernetes: the reasoning agent and the tool server are separate, independently scalable processes.

```python
import asyncio
from promptise import build_agent
from promptise.config import HTTPServerSpec


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            "orders": HTTPServerSpec(
                url="http://agent-tools.default.svc.cluster.local:8080/mcp",
                bearer_token="...",
            ),
        },
        instructions="You help support staff answer order questions.",
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What did customer c_42 order?"}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

The agent auto-discovers `search_orders` from the cluster Service and starts calling it. Before you go live, walk the [Production AI agent checklist](production-ai-agent-checklist.md) — auth, rate limits, and audit logging belong in the same manifest.

## When a managed agent platform is the better fit

Kubernetes is the right home when you already run one — you have a platform team, a Prometheus stack, and GitOps. If you don't, be honest about the overhead: probes, HPAs, secrets, and a metrics pipeline are real operational surface. A managed serverless container runtime (Cloud Run, ECS, Fly, or a hosted MCP platform) can run the same `promptise serve --transport http` command with far less to maintain, and Promptise servers deploy there unchanged. Reach for Kubernetes when you need fine-grained scaling, multi-tenant isolation, or co-location with services already in the cluster — not because it is the default. If multi-tenancy is your driver, see [Multi-tenant AI agents](multi-tenant-ai-agent.md).

## Frequently asked questions

### Does Promptise expose Kubernetes-native health endpoints automatically?

`HealthCheck` publishes `health://liveness` and `health://readiness` as MCP resources, which carry your dependency-aware readiness logic. Kubernetes probes speak HTTP, so the deployment guide points kubelet probes at the transport's `/mcp` endpoint. You get real readiness aggregation at the protocol level plus a working HTTP probe target for the cluster.

### How do I scrape Prometheus metrics from a Promptise MCP server?

Add `PrometheusMiddleware()` and call `prometheus_client.start_http_server(9090)` in your server module. Because the middleware records into the default registry, metrics appear at `:9090/metrics` in standard exposition format, ready for a Prometheus scrape annotation. You can also serve the same text through an MCP resource via `get_metrics_text()`.

### Can I use `promptise serve` as my container entrypoint?

Yes. Set the container `command` to `promptise serve module:server --transport http --host 0.0.0.0 --port 8080`. It handles argument parsing and transport startup, so you don't ship a `__main__` block just to run in production.

## Next steps

Follow the [Deployment guide](../../mcp/server/deployment.md) and ship your server to a cluster with `promptise serve --transport http`. New to the framework? Start with the [Quick Start](../../getting-started/quickstart.md), then add health checks and metrics before you write the manifest.
