# Deployment

Run your MCP server in production — choose the right transport, configure CORS for browser clients, deploy behind a reverse proxy, containerize with Docker, and use the CLI for zero-boilerplate startup.

## Transport Selection

Promptise supports three transports. Choose based on your deployment target:

```mermaid
graph TD
    A[Where does the MCP client run?] --> B{Same machine?}
    B -->|Yes| C[stdio]
    B -->|No| D{Needs SSE compatibility?}
    D -->|Yes| E[sse]
    D -->|No| F[http — Streamable HTTP]
```

| Transport | Protocol | Use case |
|-----------|----------|----------|
| `stdio` | stdin/stdout | Local integration: Claude Desktop, CLI tools, IDEs |
| `http` | Streamable HTTP | Remote agents, web apps, microservices, production |
| `sse` | Server-Sent Events | Legacy clients, environments that don't support Streamable HTTP |

### `stdio` — Local connections

```python
server = MCPServer(name="local-tools")
server.run(transport="stdio")
```

Best for Claude Desktop integration. The MCP client spawns your server as a subprocess and communicates via stdin/stdout. No network configuration needed.

```json title="Claude Desktop config"
{
  "mcpServers": {
    "my-tools": {
      "command": "python",
      "args": ["-m", "myapp.server"]
    }
  }
}
```

### `http` — Streamable HTTP (recommended for remote)

```python
server = MCPServer(name="remote-api")
server.run(
    transport="http",
    host="0.0.0.0",
    port=8080,
)
```

The server exposes a single endpoint at `/mcp` that handles all MCP protocol messages. Supports session tracking, bidirectional streaming, and concurrent requests.

### `sse` — Server-Sent Events (legacy)

```python
server = MCPServer(name="legacy-api")
server.run(
    transport="sse",
    host="0.0.0.0",
    port=8080,
)
```

Exposes `/sse` for the event stream and `/messages/` for client-to-server messages. Use this only when your client doesn't support Streamable HTTP.

---

## CORS Configuration

### When you need it

Your MCP server runs on `api.example.com:8080`. A web-based agent frontend on `app.example.com` needs to connect to it. Without CORS headers, the browser blocks the requests.

### `CORSConfig`

```python
from promptise.mcp.server import MCPServer, CORSConfig

server = MCPServer(name="web-api")

server.run(
    transport="http",
    port=8080,
    cors=CORSConfig(
        allow_origins=["https://app.example.com"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "x-api-key", "Content-Type"],
        allow_credentials=True,
        max_age=3600,
    ),
)
```

### Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `allow_origins` | `["*"]` | Allowed origin URLs |
| `allow_methods` | `["GET", "POST", "DELETE", "OPTIONS"]` | Allowed HTTP methods |
| `allow_headers` | `["*"]` | Allowed request headers |
| `allow_credentials` | `False` | Allow cookies and auth headers |
| `max_age` | `600` | Preflight cache duration (seconds) |

### Development vs production

```python
import os

if os.getenv("ENV") == "production":
    cors = CORSConfig(
        allow_origins=["https://app.yourcompany.com"],
        allow_credentials=True,
    )
else:
    cors = CORSConfig(
        allow_origins=["*"],  # Allow everything in dev
    )

server.run(transport="http", port=8080, cors=cors)
```

---

## Authentication at the Transport Level

### When you need it

You want to reject unauthenticated HTTP requests **before** they reach MCP protocol handling. This prevents unauthenticated sessions from being created.

### Transport-level auth gate

```python
from promptise.mcp.server import MCPServer, JWTAuth

server = MCPServer(name="secure-api")

jwt = JWTAuth(secret="your-secret-key")

server.run(
    transport="http",
    port=8080,
    require_auth=True,   # Enables transport-level auth gate
)
```

When `require_auth=True`, the server checks every HTTP request for:

1. **Bearer token**: `Authorization: Bearer <jwt-token>` — verified with the configured auth provider
2. **API key**: `x-api-key: <key>` — verified with the API key provider

Unauthenticated requests receive a `401` JSON response:

```json
{
  "error": "Authentication required",
  "message": "Pass a Bearer token via the Authorization header or an API key via the x-api-key header."
}
```

### Built-in token endpoint

For development and testing, you can enable a built-in token endpoint that issues JWTs:

```python
from promptise.mcp.server import MCPServer, JWTAuth, TokenEndpointConfig

server = MCPServer(name="secure-api")
jwt = JWTAuth(secret="dev-secret")

server.run(
    transport="http",
    port=8080,
    require_auth=True,
    token_endpoint=TokenEndpointConfig(
        path="/token",
        auth_provider=jwt,
    ),
)
```

```bash
# Get a token
curl -X POST http://localhost:8080/token \
  -H "Content-Type: application/json" \
  -d '{"client_id": "my-agent"}'

# Use the token
curl http://localhost:8080/mcp \
  -H "Authorization: Bearer <token>"
```

The token endpoint is automatically excluded from the auth gate (it issues tokens, so it can't require one).

---

## Reverse Proxy

### Nginx

```nginx
upstream mcp_backend {
    server 127.0.0.1:8080;
}

server {
    listen 443 ssl;
    server_name api.example.com;

    ssl_certificate     /etc/ssl/certs/api.example.com.pem;
    ssl_certificate_key /etc/ssl/private/api.example.com.key;

    location /mcp {
        proxy_pass http://mcp_backend;
        proxy_http_version 1.1;

        # Required for Streamable HTTP
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE requires long-lived connections
        proxy_read_timeout 86400s;
        proxy_buffering off;
    }
}
```

### Key proxy settings

| Setting | Why |
|---------|-----|
| `proxy_buffering off` | SSE and streaming responses must not be buffered |
| `proxy_read_timeout 86400s` | MCP sessions are long-lived |
| `proxy_http_version 1.1` | Required for keep-alive and upgrade |
| `Connection "upgrade"` | Required for WebSocket-like transports |

---

## Docker

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application code
COPY src/ src/

# Expose port
EXPOSE 8080

# Run the MCP server
CMD ["python", "-m", "myapp.server"]
```

### docker-compose.yml

```yaml
services:
  mcp-server:
    build: .
    ports:
      - "8080:8080"
    environment:
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - DATABASE_URL=postgresql://db:5432/myapp
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/mcp"]
      interval: 30s
      timeout: 10s
      retries: 3
    depends_on:
      - db

  db:
    image: postgres:16
    environment:
      POSTGRES_DB: myapp
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

### Health checks with Kubernetes

```python
from promptise.mcp.server import MCPServer, HealthCheck

server = MCPServer(name="k8s-api")
health = HealthCheck()

async def check_db() -> bool:
    try:
        await db.execute("SELECT 1")
        return True
    except Exception:
        return False

health.add_check("database", check_db, required_for_ready=True)
health.register_resources(server)
```

```yaml title="Kubernetes deployment"
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: mcp-server
          image: myapp:latest
          ports:
            - containerPort: 8080
          # MCP health checks are exposed as resources,
          # but for k8s probes you need HTTP endpoints.
          # Use the /mcp endpoint as a basic liveness probe.
          livenessProbe:
            httpGet:
              path: /mcp
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 10
```

---

## CLI Serve

### When you need it

You want to run your MCP server without writing `if __name__ == "__main__"` boilerplate. The CLI handles argument parsing, transport selection, and hot reload.

### Usage

```bash
# Default: stdio transport
promptise serve myapp.server:server

# HTTP with specific port
promptise serve myapp.server:server -t http -p 9090

# With hot reload for development
promptise serve myapp.server:server -t http --reload

# With live dashboard
promptise serve myapp.server:server -t http --dashboard
```

### Target format

```
module.path:attribute_name
```

The CLI imports the module and gets the named attribute, which must be an `MCPServer` instance:

```python title="myapp/server.py"
from promptise.mcp.server import MCPServer

# This is what the CLI imports
server = MCPServer(name="my-tools")

@server.tool()
async def greet(name: str) -> str:
    return f"Hello, {name}!"
```

```bash
promptise serve myapp.server:server
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--transport`, `-t` | `stdio` | `stdio`, `http`, or `sse` |
| `--host` | `127.0.0.1` | Bind host |
| `--port`, `-p` | `8080` | Bind port |
| `--dashboard` | off | Live terminal dashboard |
| `--reload` | off | Hot reload on file changes |

---

## Hot Reload

For development, hot reload watches your Python files and restarts the server when changes are detected:

```python
from promptise.mcp.server import MCPServer, hot_reload

server = MCPServer(name="dev")

@server.tool()
async def hello(name: str) -> str:
    return f"Hello, {name}!"

if __name__ == "__main__":
    hot_reload(
        server,
        transport="http",
        port=8080,
        watch_dirs=["src/"],
        poll_interval=1.0,
    )
```

Or via the CLI:

```bash
promptise serve myapp.server:server -t http --reload
```

See [Advanced Patterns — Hot Reload](advanced-patterns.md#hot-reload) for details.

---

## Production Checklist

Before deploying to production:

- [ ] **Transport**: Use `http` (Streamable HTTP) for remote access, `stdio` for local
- [ ] **Authentication**: Enable `require_auth=True` with JWT or API key validation
- [ ] **CORS**: Restrict `allow_origins` to your actual frontend domains
- [ ] **TLS**: Terminate TLS at the reverse proxy (Nginx, Caddy, cloud LB)
- [ ] **Health checks**: Register `HealthCheck` with required dependency checks
- [ ] **Observability**: Add `MetricsMiddleware` or `OTelMiddleware` for monitoring
- [ ] **Rate limiting**: Add `RateLimitMiddleware` to prevent abuse
- [ ] **Circuit breakers**: Protect against flaky downstream dependencies
- [ ] **Audit logging**: Add `AuditMiddleware` for compliance
- [ ] **Process management**: Use systemd, supervisord, or Kubernetes — not `hot_reload`

## API Summary

| Symbol | Type | Description |
|--------|------|-------------|
| `CORSConfig(...)` | Dataclass | CORS settings for HTTP/SSE transports |
| `TransportType` | Enum | `STDIO`, `HTTP`, `SSE` |
| `hot_reload(server, ...)` | Function | File-watching dev server |
| `build_serve_parser(...)` | Function | CLI argument parser builder |
| `resolve_server(target)` | Function | Import server from `module:attr` |
| `run_serve(args)` | Function | Run server from CLI args |
| `TokenEndpointConfig(...)` | Dataclass | Built-in token endpoint config |

## What's Next

- [Authentication & Security](auth-security.md) — JWT, API keys, guards, roles
- [Caching & Performance](caching-performance.md) — Cache, rate limit, concurrency
- [Observability & Monitoring](observability.md) — Metrics, tracing, logging
- [Resilience Patterns](resilience-patterns.md) — Circuit breakers, health checks
