---
title: Build a Production MCP Server in Python — 12-step guide
description: Build a production-grade MCP (Model Context Protocol) server in Python from scratch with Promptise Foundry in 12 incremental steps. Covers tools, resources, prompts, JWT authentication, capability-based authorization, middleware, caching, rate limiting, audit logging, and deployment.
keywords: build MCP server, MCP server tutorial, production MCP server, MCP server Python, Model Context Protocol server, JWT MCP server, MCP authentication
---

# Building Production MCP Servers

Build a production-grade MCP server from scratch in 12 incremental steps. Each step adds one concept so you understand the full picture.

!!! tip "This is the recommended starting point for building MCP servers"
    This guide walks you through building a complete server step by step. For deep reference on individual features, see the [Server Fundamentals](../mcp/server/building-servers.md), [Routers & Middleware](../mcp/server/routers-middleware.md), [Auth & Security](../mcp/server/auth-security.md), and [Production Features](../mcp/server/production-features.md) pages.

## What You'll Build

A task management API with CRUD operations, JWT authentication with structured client context, route organization, middleware, caching, scope-based authorization, request tracing, response metadata, and background job queues -- the same patterns used in real-world deployments. By step 12, you'll have a server that agents can connect to and use immediately.

## Concepts

The **Model Context Protocol (MCP)** standardizes how AI agents discover and invoke tools. Instead of hardcoding tool definitions, agents connect to MCP servers and dynamically discover what's available. This decouples agent logic from tool implementation -- you can update tools without changing agents, and multiple agents can share the same tool server.

An **MCPServer** is the central object. You register tools, resources, and prompts using decorators, add middleware for cross-cutting concerns, and call `server.run()` to start serving. The framework automatically generates JSON Schemas from your function signatures and Pydantic models, so clients always have accurate parameter descriptions.

Production servers layer additional capabilities: **authentication** to control who can call tools, **routers** to organize tools by domain, **middleware** for logging and rate limiting, **guards** for fine-grained access control based on roles and OAuth2 scopes, **caching** for expensive operations, **request tracing** for distributed debugging, and **response metadata** for audit trails.

---

## Step 1: Basic Server

Start with a minimal server that exposes tools:

```python
from promptise.mcp.server import MCPServer

server = MCPServer(name="task-api", version="1.0.0")

@server.tool()
async def list_tasks(status: str = "all") -> list[dict]:
    """List tasks, optionally filtered by status."""
    tasks = [
        {"id": "1", "title": "Review PR", "status": "open"},
        {"id": "2", "title": "Deploy v2", "status": "done"},
    ]
    if status != "all":
        tasks = [t for t in tasks if t["status"] == status]
    return tasks

@server.tool()
async def create_task(title: str, assignee: str = "") -> dict:
    """Create a new task."""
    return {"id": "3", "title": title, "assignee": assignee, "status": "open"}

server.run(transport="http", port=8080)
```

Run this and any MCP client can discover and call your tools. Parameters are validated automatically from the function signature.

---

## Step 2: Pydantic Validation

For complex inputs, use Pydantic models. The framework generates nested JSON Schemas that clients use for validation:

```python
from pydantic import BaseModel, Field

class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200, description="Task title")
    description: str = Field(default="", description="Detailed description")
    priority: int = Field(default=3, ge=1, le=5, description="Priority 1-5")
    tags: list[str] = Field(default_factory=list, description="Tags")

class TaskUpdate(BaseModel):
    title: str | None = None
    status: str | None = Field(None, pattern="^(open|in_progress|done)$")
    priority: int | None = Field(None, ge=1, le=5)

@server.tool(tags=["tasks", "write"])
async def create_task(task: TaskCreate) -> dict:
    """Create a task with validated fields."""
    return {"id": "new-1", **task.model_dump()}

@server.tool(tags=["tasks", "write"])
async def update_task(task_id: str, updates: TaskUpdate) -> dict:
    """Update task fields."""
    changes = updates.model_dump(exclude_none=True)
    return {"id": task_id, "updated": changes}
```

---

## Step 3: Resources and Prompts

**Resources** expose read-only data at static URIs. **Resource templates** use parameters for dynamic data. **Prompts** expose reusable prompt templates:

```python
@server.resource("config://app", mime_type="application/json")
async def app_config() -> str:
    """Application configuration."""
    return '{"version": "1.0.0", "environment": "production"}'

@server.resource_template("tasks://{task_id}", mime_type="application/json")
async def task_detail(task_id: str) -> str:
    """Get task details by ID."""
    return f'{{"id": "{task_id}", "title": "Task {task_id}"}}'

@server.prompt()
async def task_summary(project: str, status: str = "all") -> str:
    """Generate a prompt to summarize tasks for a project."""
    return f"Summarize all {status} tasks for project {project}."
```

---

## Step 4: Authentication and Client Context

Add JWT authentication to protect sensitive tools. After authentication, every tool receives a structured `ClientContext` with the caller's identity, roles, scopes, and JWT claims:

```python
from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JWTAuth, RequestContext,
)

jwt_auth = JWTAuth(secret="your-secret-key-min-32-chars-long!!")
server = MCPServer(name="task-api", version="1.0.0")
server.add_middleware(AuthMiddleware(jwt_auth))

# Public tool -- no auth required
@server.tool()
async def health_check() -> dict:
    """Check server health."""
    return {"status": "healthy"}

# Protected tool -- requires valid JWT
@server.tool(auth=True)
async def create_task(ctx: RequestContext, title: str) -> dict:
    """Create a task (authenticated users only)."""
    return {
        "id": "1",
        "title": title,
        "created_by": ctx.client.client_id,  # Structured client identity
    }

# Role-restricted tool -- requires 'admin' role in JWT claims
@server.tool(auth=True, roles=["admin"])
async def delete_task(task_id: str) -> dict:
    """Delete a task (admin only)."""
    return {"deleted": task_id}
```

The `ctx.client` object gives you typed access to everything about the caller:

```python
@server.tool(auth=True)
async def whoami(ctx: RequestContext) -> dict:
    """Return full client identity."""
    return {
        "client_id": ctx.client.client_id,     # "agent-prod"
        "roles": sorted(ctx.client.roles),      # ["admin", "write"]
        "scopes": sorted(ctx.client.scopes),    # ["read", "write"]
        "issuer": ctx.client.issuer,            # JWT "iss" claim
        "subject": ctx.client.subject,          # JWT "sub" claim
        "ip_address": ctx.client.ip_address,    # Client IP
        "user_agent": ctx.client.user_agent,    # Client user-agent header
    }
```

For development, you can enable a built-in token endpoint:

```python
server.enable_token_endpoint(
    jwt_auth=jwt_auth,
    clients={
        "agent-prod":  {"secret": "agent-secret",  "roles": ["admin", "write"]},
        "agent-viewer": {"secret": "viewer-secret", "roles": ["read"]},
    },
)
# POST /auth/token with {"client_id": "agent-prod", "client_secret": "agent-secret"}
```

For production, use a real identity provider (Auth0, Keycloak, Okta).

---

## Step 5: Guards -- Roles and Scopes

Guards enforce fine-grained permissions after authentication. Use role-based guards for API key auth, and scope-based guards for OAuth2/JWT:

```python
from promptise.mcp.server import HasRole, HasAllRoles, HasScope, HasAllScopes, RequireClientId

# Any ONE of these roles grants access
@server.tool(auth=True, guards=[HasRole("admin", "manager")])
async def approve_task(task_id: str) -> dict:
    """Approve a task (admin or manager)."""
    return {"approved": task_id}

# ALL roles required
@server.tool(auth=True, guards=[HasAllRoles("admin", "billing")])
async def refund_payment(payment_id: str) -> dict:
    """Refund a payment (requires both admin AND billing roles)."""
    return {"refunded": payment_id}

# OAuth2 scope-based access (reads from JWT "scope" claim)
@server.tool(auth=True, guards=[HasScope("tasks:write")])
async def bulk_update(task_ids: list[str]) -> dict:
    """Bulk update tasks (requires tasks:write scope)."""
    return {"updated": len(task_ids)}

# All scopes required
@server.tool(auth=True, guards=[HasAllScopes("tasks:read", "tasks:write")])
async def export_and_archive(project: str) -> dict:
    """Export and archive (requires both read AND write scopes)."""
    return {"archived": project}

# Restrict to specific client identifiers
@server.tool(auth=True, guards=[RequireClientId("agent-prod", "agent-staging")])
async def deploy(version: str) -> dict:
    """Deploy (only specific agents allowed)."""
    return {"deployed": version}
```

When a guard denies access, the error message explains exactly what's wrong:

```
Requires any of roles [admin, manager], but client has [viewer]
Requires all scopes [tasks:read, tasks:write], client has [tasks:read], missing [tasks:write]
```

---

## Step 6: Client Enrichment Hook

Use `on_authenticate` to add custom data to the client context after authentication -- look up user profiles, load permissions from a database, or inject organization metadata:

```python
from promptise.mcp.server import AuthMiddleware, JWTAuth, RequestContext

async def enrich_client(client, ctx):
    """Called after every successful authentication."""
    # Look up additional info from your database
    org = await db.get_org(client.client_id)
    client.extra["org_name"] = org.name
    client.extra["plan"] = org.plan
    client.extra["feature_flags"] = org.feature_flags

jwt_auth = JWTAuth(secret="your-secret-key-min-32-chars-long!!")
server.add_middleware(AuthMiddleware(jwt_auth, on_authenticate=enrich_client))

@server.tool(auth=True)
async def premium_feature(ctx: RequestContext) -> dict:
    """A feature only available on premium plans."""
    if ctx.client.extra.get("plan") != "premium":
        raise ToolError("This feature requires a premium plan")
    return {"result": "premium data"}
```

---

## Step 7: Routers

Organize tools by domain with `MCPRouter`. Each router adds a prefix and can apply shared auth, tags, and guards:

```python
from promptise.mcp.server import MCPRouter

# Task management tools
task_router = MCPRouter(prefix="tasks", tags=["tasks"], auth=True)

@task_router.tool()
async def list(status: str = "all") -> list[dict]:
    """List all tasks."""  # Tool name becomes "tasks_list"
    return []

@task_router.tool()
async def create(title: str) -> dict:
    """Create a task."""  # Tool name becomes "tasks_create"
    return {"id": "1", "title": title}

# User management tools
user_router = MCPRouter(prefix="users", tags=["users"], auth=True)

@user_router.tool()
async def list_users() -> list[dict]:
    """List all users."""  # Tool name becomes "users_list_users"
    return []

# Register routers with the server
server.include_router(task_router)
server.include_router(user_router)
```

---

## Step 8: Middleware

Add cross-cutting concerns with the middleware chain. Middleware runs in registration order (first added = outermost):

```python
from promptise.mcp.server import LoggingMiddleware, TimeoutMiddleware, RateLimitMiddleware

# Logging -- logs every tool call with timing
server.add_middleware(LoggingMiddleware())

# Timeouts -- kill slow tool calls
server.add_middleware(TimeoutMiddleware(default_timeout=30.0))

# Rate limiting -- prevent abuse
server.add_middleware(RateLimitMiddleware(rate_per_minute=100, per_tool=True))
```

Write custom middleware with the decorator:

```python
@server.middleware
async def metrics_middleware(ctx, call_next):
    """Track tool call metrics."""
    import time
    start = time.monotonic()
    try:
        result = await call_next(ctx)
        duration = time.monotonic() - start
        print(f"[METRIC] {ctx.tool_name}: {duration:.3f}s (success)")
        return result
    except Exception as exc:
        duration = time.monotonic() - start
        print(f"[METRIC] {ctx.tool_name}: {duration:.3f}s (error: {exc})")
        raise
```

---

## Step 9: Caching and Dependencies

**Cache** expensive operations:

```python
from promptise.mcp.server import cached, InMemoryCache

cache = InMemoryCache(max_size=1000, cleanup_interval=60.0)

@server.tool()
@cached(ttl=300, backend=cache)
async def search_tasks(query: str) -> list[dict]:
    """Search tasks (cached for 5 minutes)."""
    # Expensive database query
    return await db.search(query)
```

**Inject dependencies** with `Depends` for resource cleanup:

```python
from promptise.mcp.server import Depends

async def get_db():
    db = await Database.connect()
    try:
        yield db  # Injected into the handler
    finally:
        await db.close()  # Cleanup after handler returns

@server.tool()
async def query_tasks(sql: str, db = Depends(get_db)) -> list:
    """Run a database query with automatic connection cleanup."""
    return await db.execute(sql)
```

**Fire-and-forget** tasks with `BackgroundTasks`:

```python
from promptise.mcp.server import BackgroundTasks

@server.tool()
async def process_data(data: str, bg: BackgroundTasks = Depends(BackgroundTasks)) -> str:
    """Process data and send notifications in the background."""
    bg.add(send_notification, "Data processed", data)
    bg.add(log_analytics, "process_data", data)
    return "Processing started"
```

---

## Step 10: Request Tracing and Response Metadata

**Request tracing** propagates a unique ID through the entire request lifecycle. Clients send `X-Request-ID` headers (or one is generated automatically), and you can use it for distributed debugging:

```python
@server.tool(auth=True)
async def traced_operation(ctx: RequestContext, data: str) -> dict:
    """An operation with full tracing context."""
    # Request ID available on every context
    request_id = ctx.request_id  # Client-provided or auto-generated

    # Pass it to downstream services
    await downstream_api.call(data, trace_id=request_id)

    return {"processed": data, "trace_id": request_id}
```

**ToolResponse** wraps your return value with metadata for audit and observability -- the metadata is captured automatically without changing what the client sees:

```python
from promptise.mcp.server import ToolResponse

@server.tool(auth=True)
async def create_order(ctx: RequestContext, product_id: str, qty: int) -> ToolResponse:
    """Create an order with audit metadata."""
    order = await db.create_order(product_id, qty)

    return ToolResponse(
        content={"order_id": order.id, "status": "created"},
        metadata={
            "audit_action": "order.create",
            "actor": ctx.client.client_id,
            "ip": ctx.client.ip_address,
            "product_id": product_id,
        },
    )
    # Client sees: {"order_id": "...", "status": "created"}
    # Metadata stored on ctx.state["response_metadata"] for middleware/logging
```

---

## Step 11: Job Queues for Long-Running Work

MCP tool calls are synchronous -- the agent blocks until the response. This breaks down for long-running work like report generation, data pipelines, or batch processing. `MCPQueue` turns any long operation into a non-blocking submit-and-poll workflow:

```python
import asyncio
from promptise.mcp.server import MCPServer, MCPQueue

server = MCPServer(name="analytics-api", version="1.0.0")
queue = MCPQueue(server, max_workers=4)

@queue.job(name="generate_report", timeout=120)
async def generate_report(department: str, quarter: str = "Q4") -> dict:
    """Generate a quarterly analytics report (takes ~30 seconds)."""
    await asyncio.sleep(30)  # Simulate long-running work
    return {"department": department, "quarter": quarter, "rows": 1250, "status": "ready"}

@queue.job(name="train_model", timeout=600, max_retries=2, backoff_base=2.0)
async def train_model(dataset: str, epochs: int = 10) -> dict:
    """Train a model on the given dataset."""
    for i in range(epochs):
        await asyncio.sleep(1)
    return {"accuracy": 0.95, "model_id": "model-abc123"}

server.run(transport="http", port=8080)
```

`MCPQueue` auto-registers 5 tools that agents use to interact with the queue:

| Auto-registered tool | Purpose |
|---------------------|---------|
| `queue_submit` | Submit a job -- returns a `job_id` immediately |
| `queue_status` | Check job status and progress |
| `queue_result` | Retrieve a completed job's return value |
| `queue_cancel` | Cancel a pending or running job |
| `queue_list` | List jobs (filterable by status) |

**Typical agent workflow:**

```
Agent: queue_submit(job_type="generate_report", args={"department": "Engineering"})
  -> {"job_id": "a1b2c3d4", "status": "pending"}

Agent: queue_status(job_id="a1b2c3d4")
  -> {"status": "running", "progress": 0.3, "progress_message": "Processing Q3 data"}

Agent: queue_result(job_id="a1b2c3d4")
  -> {"status": "completed", "result": {"department": "Engineering", "rows": 1250}}
```

Jobs support priority levels (`low`, `normal`, `high`, `critical`), automatic retries with exponential backoff, per-job timeouts, and cancellation. The agent submits work and polls for completion -- no long-lived connections or blocking calls.

---

## Step 12: Connect Agents

Once your server is running, connect an agent:

```python
from promptise import build_agent, HTTPServerSpec

agent = await build_agent(
    model="openai:gpt-5-mini",
    servers={
        "tasks": HTTPServerSpec(
            url="http://localhost:8080/mcp",
            bearer_token="your-jwt-token",
        ),
    },
)

result = await agent.ainvoke(
    {"messages": [{"role": "user", "content": "Create a task to review the PR"}]}
)
```

The agent automatically discovers all tools from your server and can call them as needed.

---

## Complete Example

```python
import asyncio
from promptise.mcp.server import (
    MCPServer, MCPRouter, AuthMiddleware, JWTAuth,
    LoggingMiddleware, TimeoutMiddleware, RateLimitMiddleware,
    Depends, BackgroundTasks, cached, InMemoryCache,
    HasScope, RequestContext, ToolResponse,
)

# Server setup
server = MCPServer(name="task-api", version="1.0.0")
jwt_auth = JWTAuth(secret="your-production-secret-key-here!!")
cache = InMemoryCache(max_size=500)

# Client enrichment
async def enrich(client, ctx):
    client.extra["tier"] = "enterprise"

# Middleware stack
server.add_middleware(LoggingMiddleware())
server.add_middleware(AuthMiddleware(jwt_auth, on_authenticate=enrich))
server.add_middleware(TimeoutMiddleware(default_timeout=30.0))
server.add_middleware(RateLimitMiddleware(rate_per_minute=100))

# Task router
tasks = MCPRouter(prefix="tasks", tags=["tasks"])

@tasks.tool()
@cached(ttl=60, backend=cache)
async def list_tasks(status: str = "all") -> list[dict]:
    """List tasks with optional status filter."""
    return [{"id": "1", "title": "Example", "status": "open"}]

@tasks.tool(auth=True, guards=[HasScope("tasks:write")])
async def create_task(ctx: RequestContext, title: str, priority: int = 3) -> ToolResponse:
    """Create a new task with audit trail."""
    return ToolResponse(
        content={"id": "new", "title": title, "priority": priority},
        metadata={"actor": ctx.client.client_id, "action": "task.create"},
    )

@tasks.tool(auth=True, roles=["admin"])
async def delete_task(task_id: str) -> dict:
    """Delete a task (admin only)."""
    return {"deleted": task_id}

server.include_router(tasks)

# Dev token endpoint
server.enable_token_endpoint(
    jwt_auth=jwt_auth,
    clients={"agent": {"secret": "dev-secret", "roles": ["admin"]}},
)

# Lifecycle hooks
@server.on_startup
async def startup():
    print("Task API server ready")

# Run
server.run(transport="http", port=8080)
```

---

## What's Next

**Go deeper on each feature:**

| Feature used in this guide | Deep reference |
|---|---|
| `@server.tool()`, resources, prompts | [Server Fundamentals](../mcp/server/building-servers.md) |
| `MCPRouter`, `@server.middleware` | [Routers & Middleware](../mcp/server/routers-middleware.md) |
| `JWTAuth`, `AuthMiddleware`, `ClientContext`, guards, scopes | [Auth & Security](../mcp/server/auth-security.md) |
| `@cached`, rate limiting, health checks, metrics | [Production Features](../mcp/server/production-features.md) |
| `TestClient` for testing | [Testing](../mcp/server/testing.md) |

**Connect agents to your server:**

- [Client Guide](../mcp/client/index.md) -- `MCPClient`, `MCPMultiClient`, and authentication
- [Tool Adapter](../mcp/client/tool-adapter.md) -- Convert MCP tools to LangChain tools

**Other guides:**

- [Building Agentic Runtime Systems](agentic-runtime.md) -- Autonomous agents that consume your MCP servers
- [Building AI Agents](building-agents.md) -- The core agent that connects to your tools
- [Prompt Engineering](prompt-engineering.md) -- Build reliable, testable system prompts
