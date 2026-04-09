# Observability & Monitoring

Track what your MCP server is doing in production with built-in metrics, distributed tracing, Prometheus export, structured logging, audit trails, and a live terminal dashboard.

## Built-in Metrics

### `MetricsCollector`

Track per-tool call counts, error rates, and latency out of the box:

```python
from promptise.mcp.server import (
    MCPServer, MetricsCollector, MetricsMiddleware,
)

server = MCPServer(name="hr-api")
metrics = MetricsCollector()

server.add_middleware(MetricsMiddleware(metrics))

# Expose metrics as an MCP resource (agents can read them)
metrics.register_resource(server)
```

Now every tool call is tracked. Agents can read `metrics://server` to see:

```json
{
  "uptime_seconds": 3600,
  "tools": {
    "search_employees": {
      "calls": 142,
      "errors": 3,
      "avg_latency_ms": 45.2
    },
    "create_employee": {
      "calls": 12,
      "errors": 0,
      "avg_latency_ms": 128.7
    }
  }
}
```

### Live Dashboard

For development and debugging, enable the terminal dashboard:

```python
server.run(transport="http", port=8080, dashboard=True)
```

The dashboard shows 6 tabs (switch with 1-6 keys):

1. **Overview** -- Server info, uptime, key metrics
2. **Tools** -- Registered tools with per-tool call stats
3. **Agents** -- Connected clients and session details
4. **Logs** -- Scrolling request log
5. **Metrics** -- Performance data and error breakdown
6. **Raw Logs** -- Python logger output

---

## OpenTelemetry

### When you need it

Your MCP server is one piece of a larger system -- the agent calls your server, which calls a database, which calls a cache. When something is slow, you need to trace the entire request path across services.

### `OTelMiddleware`

```python
from promptise.mcp.server import MCPServer, OTelMiddleware

server = MCPServer(name="order-api")
server.add_middleware(OTelMiddleware(
    service_name="order-mcp-server",
    endpoint="http://jaeger:4317",  # OTLP collector endpoint
))
```

Each tool call becomes a span with these attributes:

| Attribute | Example |
|-----------|---------|
| `mcp.tool.name` | `"create_order"` |
| `mcp.request.id` | `"a3f2b1"` |
| `mcp.client.id` | `"agent-checkout"` |
| `mcp.tool.status` | `"ok"` or `"error"` |

The middleware also records:

- **Histogram**: `mcp.tool.duration` -- call duration distribution
- **Counter**: `mcp.tool.errors` -- error count by tool

**No-op when not installed**: If `opentelemetry-api` is not installed, the middleware passes through without overhead. Install with `pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp`.

### Real-world setup with Jaeger

```python
from promptise.mcp.server import MCPServer, OTelMiddleware

# In production, configure via env vars:
# OTEL_SERVICE_NAME=order-mcp-server
# OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4317

server = MCPServer(name="order-api")
server.add_middleware(OTelMiddleware(service_name="order-mcp-server"))

@server.tool()
async def create_order(customer_id: str, items: list[dict]) -> dict:
    """Create an order.

    The OTel middleware creates a parent span. Your code can add child spans:
    """
    from opentelemetry import trace
    tracer = trace.get_tracer("order-api")

    with tracer.start_as_current_span("validate_inventory"):
        await check_inventory(items)

    with tracer.start_as_current_span("charge_payment"):
        await charge_customer(customer_id, items)

    return {"order_id": "ord-123", "status": "confirmed"}
```

---

## Prometheus Metrics

### When you need it

Your ops team uses Grafana dashboards and Prometheus alerting. You need standard `/metrics` endpoint that Prometheus can scrape.

### `PrometheusMiddleware`

```python
from promptise.mcp.server import MCPServer, PrometheusMiddleware

server = MCPServer(name="api")
prom = PrometheusMiddleware(namespace="myapp")
server.add_middleware(prom)
```

Records three standard metrics:

| Metric | Type | Labels |
|--------|------|--------|
| `myapp_tool_calls_total` | Counter | `tool`, `status` |
| `myapp_tool_duration_seconds` | Histogram | `tool` |
| `myapp_tool_in_flight` | Gauge | `tool` |

### Exposing `/metrics` endpoint

Use `get_metrics_text()` to serve Prometheus format:

```python
# In your HTTP handler or resource
@server.resource("metrics://prometheus", mime_type="text/plain")
async def prometheus_metrics() -> str:
    return prom.get_metrics_text()
```

Output (Prometheus text exposition format):

```
# HELP myapp_tool_calls_total Total MCP tool calls
# TYPE myapp_tool_calls_total counter
myapp_tool_calls_total{tool="search_employees",status="ok"} 142
myapp_tool_calls_total{tool="search_employees",status="error"} 3
myapp_tool_calls_total{tool="create_employee",status="ok"} 12

# HELP myapp_tool_duration_seconds MCP tool call duration in seconds
# TYPE myapp_tool_duration_seconds histogram
myapp_tool_duration_seconds_bucket{tool="search_employees",le="0.5"} 138
myapp_tool_duration_seconds_bucket{tool="search_employees",le="1.0"} 141
```

**No-op when not installed**: If `prometheus-client` is not installed, the middleware passes through. Install with `pip install prometheus-client`.

### Custom Prometheus registry

For testing or multi-component setups, use a custom registry:

```python
from prometheus_client import CollectorRegistry

registry = CollectorRegistry()
prom = PrometheusMiddleware(namespace="myapp", registry=registry)
```

---

## Structured Logging

### When you need it

Your log aggregator (ELK, Datadog, CloudWatch) expects JSON-formatted log entries. Python's default logging produces unstructured text that's hard to parse and alert on.

### `StructuredLoggingMiddleware`

```python
from promptise.mcp.server import MCPServer, StructuredLoggingMiddleware

server = MCPServer(name="api")
server.add_middleware(StructuredLoggingMiddleware())
```

Every tool call emits a JSON log entry:

```json
{
  "event": "tool_call",
  "tool": "search_employees",
  "request_id": "a3f2b1",
  "duration_ms": 45.2,
  "status": "ok",
  "timestamp": "2026-03-07T10:30:00Z"
}
```

On error:

```json
{
  "event": "tool_call",
  "tool": "search_employees",
  "request_id": "b4c3d2",
  "duration_ms": 12.1,
  "status": "error",
  "error": "ConnectionError: database unreachable",
  "timestamp": "2026-03-07T10:30:05Z"
}
```

---

## Audit Logging

### When you need it

Your compliance team requires a tamper-evident record of every tool call -- who called what, when, with what arguments, and what happened. HIPAA, SOC 2, and GDPR compliance often require audit trails.

### `AuditMiddleware`

```python
from promptise.mcp.server import MCPServer, AuditMiddleware

server = MCPServer(name="medical-records-api")
server.add_middleware(AuditMiddleware(
    log_path="audit.jsonl",    # Write to file
    signed=True,               # HMAC chain for tamper detection
    hmac_secret="your-secret", # Or set PROMPTISE_AUDIT_SECRET env var
    include_args=True,         # Log tool arguments
    include_result=False,      # Don't log results (may contain PHI)
))
```

Each entry in `audit.jsonl`:

```json
{
  "timestamp": 1709812200.0,
  "tool": "view_patient_record",
  "client_id": "dr-smith-agent",
  "request_id": "c5d4e3",
  "status": "ok",
  "duration_s": 0.045,
  "args": {"patient_id": "P-12345"},
  "prev_hash": "0000...0000",
  "hmac": "a1b2c3d4..."
}
```

### HMAC chain integrity

Each entry's `hmac` is computed over the entry + the previous entry's `hmac`, forming a chain. If anyone modifies a past entry, the chain breaks:

```python
# After collecting entries, verify the chain hasn't been tampered with
audit = server._middleware[0]  # Get your AuditMiddleware instance
assert audit.verify_chain()    # Returns False if any entry was modified
```

### Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `log_path` | `None` | File path for JSONL audit log |
| `signed` | `True` | Enable HMAC chain |
| `hmac_secret` | env or default | Secret for HMAC computation |
| `include_args` | `False` | Log tool arguments (may contain PII) |
| `include_result` | `False` | Log tool results |

---

## Server-to-Client Logging

### When you need it

Your tool runs a multi-step process and you want the agent (or human watching) to see progress messages -- not just the final result.

### `ServerLogger`

```python
from promptise.mcp.server import MCPServer, ServerLogger, Depends

server = MCPServer(name="data-pipeline")

@server.tool()
async def import_csv(
    file_url: str,
    logger: ServerLogger = Depends(ServerLogger),
) -> dict:
    """Import a CSV file into the database."""
    await logger.info("Downloading CSV...")
    data = await download(file_url)

    await logger.info(f"Parsing {len(data)} rows...")
    rows = parse_csv(data)

    await logger.warning(f"Skipped {skipped} invalid rows")

    await logger.info("Inserting into database...")
    await db.bulk_insert(rows)

    return {"imported": len(rows), "skipped": skipped}
```

Log messages are sent to the client via MCP's `notifications/message` protocol. The client can display them in real-time.

Available log levels: `debug`, `info`, `notice`, `warning`, `error`, `critical`, `alert`, `emergency`.

---

## Combining Observability Features

A production server typically layers multiple observability tools:

```python
from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JWTAuth,
    MetricsCollector, MetricsMiddleware,
    OTelMiddleware, PrometheusMiddleware,
    StructuredLoggingMiddleware, AuditMiddleware,
)

server = MCPServer(name="production-api")
metrics = MetricsCollector()

# Observability stack (order matters)
server.add_middleware(StructuredLoggingMiddleware())    # JSON logs for every call
server.add_middleware(AuditMiddleware(                  # Compliance audit trail
    log_path="/var/log/mcp-audit.jsonl",
    signed=True,
))
server.add_middleware(OTelMiddleware(                   # Distributed tracing
    service_name="production-api",
))
server.add_middleware(PrometheusMiddleware())           # Prometheus metrics
server.add_middleware(MetricsMiddleware(metrics))       # Built-in metrics
server.add_middleware(AuthMiddleware(JWTAuth(...)))     # Auth (before tools)

metrics.register_resource(server)
```

---

## API Summary

| Symbol | Type | Description |
|--------|------|-------------|
| `MetricsCollector()` | Class | Per-tool call count, latency, error tracking |
| `MetricsMiddleware(collector)` | Class | Record metrics for every tool call |
| `MetricsCollector.register_resource(server)` | Method | Expose `metrics://server` resource |
| `OTelMiddleware(service_name, endpoint)` | Class | OpenTelemetry tracing middleware |
| `PrometheusMiddleware(namespace, registry)` | Class | Prometheus metrics middleware |
| `StructuredLoggingMiddleware()` | Class | JSON structured logging middleware |
| `AuditMiddleware(log_path, signed, ...)` | Class | HMAC-chained audit log middleware |
| `ServerLogger` | Class | Send log messages to MCP client (via DI) |
| `Dashboard` | Class | Live terminal monitoring dashboard |

## What's Next

- [Caching & Performance](caching-performance.md) -- Cache, rate limit, concurrency control
- [Resilience Patterns](resilience-patterns.md) -- Circuit breaker, health checks, webhooks
- [Deployment](deployment.md) -- HTTP deployment, CORS, containers
