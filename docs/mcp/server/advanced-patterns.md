---
seo_title: "MCP Tool Versioning, Transforms, Composition"
description: "Advanced MCP server patterns: tool versioning, transforms, server composition, an OpenAPI bridge, batch calls, streaming results, elicitation and sampling."
---

# Advanced MCP Server Patterns

Patterns for evolving, composing, and extending MCP servers: tool versioning for backwards compatibility, controlling which tools each caller sees, server composition for gateways, OpenAPI bridging, batch execution, streaming results, elicitation, and sampling.

## Tool Versioning

### When you need it

You ship `search` to production and agents depend on its arguments. Now you want to add a `filters` parameter without breaking the agents that use the old shape.

### What works in 1.6.0

`MCPServer` serves one tool per name: `@server.tool()` has no `version` parameter. Ship a new version under its own name and keep the old one until its callers move:

```python
from promptise.mcp.server import MCPServer

server = MCPServer(name="search-api")

DOCS = [{"id": 1, "title": "Refund policy", "score": 0.9}, {"id": 2, "title": "Shipping", "score": 0.4}]

@server.tool(name="search_v1", description="Full-text search (v1, kept for older agents).")
async def search_v1(query: str) -> list[dict]:
    return [d for d in DOCS if query.lower() in d["title"].lower()]

@server.tool(description="Full-text search with an optional minimum score.")
async def search(query: str, min_score: float = 0.0) -> list[dict]:
    return [d for d in DOCS if query.lower() in d["title"].lower() and d["score"] >= min_score]
```

New optional parameters with defaults (like `min_score` above) are backwards compatible on their own: an agent that doesn't send them gets the old behaviour.

### `VersionedToolRegistry`

`VersionedToolRegistry` is a standalone helper for code that keeps several `ToolDef` versions under one base name, for example a gateway that builds its own tool list. The server does not consult it: registering a `ToolDef` here does not serve it.

```python
from promptise.mcp.server import VersionedToolRegistry, ToolDef

async def handler(query: str) -> list:
    return []

v1 = ToolDef(name="search@1.0", description="v1", handler=handler, input_schema={"type": "object"})
v2 = ToolDef(name="search@2.0", description="v2", handler=handler, input_schema={"type": "object"})

vr = VersionedToolRegistry()
vr.register("search", "1.0", v1)
vr.register("search", "2.0", v2)

print(vr.get("search").description)      # "v2": the highest version
print(vr.get("search@1.0").description)  # "v1"
print(vr.list_versions("search"))        # ["1.0", "2.0"]
print(vr.has("search"))                  # True
print(len(vr.list_all()))                # 2: every registered version
```

Versions are compared numerically per dot-separated part: `"2.0"` > `"1.0"`, `"1.10"` > `"1.9"` (a non-numeric part counts as 0). Registering the same version twice raises `ValueError`; `get()` returns `None` for an unknown name or version.

---

## Tool Transforms

### When you need it

You want different agents to discover different tools: prefixed names, hidden admin tools, only public tools.

### What works in 1.6.0

`MCPServer` has no transform hook (there is no `server.add_transform()`). Use these server features instead:

| Goal | Use |
|------|-----|
| Prefix tool names | `MCPRouter(prefix="analytics")` or `mount(gateway, child, prefix="analytics")` |
| Hide tools a caller can't use | Guards or `roles=` on the tools, plus `MCPServer(hide_unauthorized_tools=True)`: `tools/list` is filtered per caller and the guards still block calls |
| Expose only a subset to one audience | A separate `MCPServer` (or gateway) that mounts or includes only those routers |

```python
from promptise.mcp.server import MCPServer, MCPRouter, AuthMiddleware, APIKeyAuth

server = MCPServer(name="admin-api", hide_unauthorized_tools=True)
server.add_middleware(AuthMiddleware(APIKeyAuth({
    "sk-admin": {"client_id": "ops", "roles": ["admin"]},
    "sk-view": {"client_id": "viewer", "roles": ["viewer"]},
})))

users = MCPRouter(prefix="users")

@users.tool(auth=True)
async def list_all() -> list[str]:
    """List all users."""
    return ["alice", "bob"]

@users.tool(roles=["admin"], destructive_hint=True)
async def delete(user_id: str) -> dict:
    """Delete a user permanently."""
    return {"deleted": user_id}

server.include_router(users)
# The viewer key lists users_list_all only; the admin key also sees users_delete.
```

### Transform classes

`NamespaceTransform(prefix)`, `VisibilityTransform(hidden=None)` and `TagFilterTransform(required_tags)` implement the `ToolTransform` protocol, `apply(tools: list[ToolDef], ctx: RequestContext | None = None) -> list[ToolDef]`. They are standalone helpers for code that works with `ToolDef` lists (a custom gateway, a manifest, tests); the server does not apply them.

```python
from promptise.mcp.server import ToolDef, NamespaceTransform, VisibilityTransform, TagFilterTransform

async def noop() -> str:
    return "ok"

tools = [
    ToolDef(name="query", description="Run a query", handler=noop, input_schema={}, tags=["public"]),
    ToolDef(name="reset", description="Reset data", handler=noop, input_schema={}, tags=["admin"]),
]

public = TagFilterTransform(required_tags={"public"}).apply(tools)          # tools with any of the tags
visible = VisibilityTransform(hidden={"reset": lambda ctx: True}).apply(tools)  # drop when predicate is true
named = NamespaceTransform(prefix="analytics").apply(public)                 # "analytics_query"
print([t.name for t in named], [t.name for t in visible])
```

---

## Server Composition

### When you need it

Your company has separate teams building separate MCP servers: `payments`, `users`, `analytics`. You want to expose them all through a single gateway server so agents only connect to one endpoint.

### `mount()`

```python
from promptise.mcp.server import MCPServer, mount

# Team 1: Payment tools
payments = MCPServer(name="payments")

@payments.tool()
async def charge(customer_id: str, amount_cents: int) -> dict:
    """Charge a customer's payment method."""
    return await stripe.charge(customer_id, amount_cents)

@payments.tool()
async def refund(charge_id: str) -> dict:
    """Refund a charge."""
    return await stripe.refund(charge_id)

# Team 2: User tools
users = MCPServer(name="users")

@users.tool()
async def get_user(user_id: str) -> dict:
    """Get user profile."""
    return await db.get_user(user_id)

@users.tool()
async def update_user(user_id: str, name: str | None = None) -> dict:
    """Update user profile."""
    return await db.update_user(user_id, name=name)

# Gateway: compose into one server
gateway = MCPServer(name="api-gateway", version="1.0.0")
mount(gateway, payments, prefix="pay")
mount(gateway, users, prefix="usr")

# Agents see: pay_charge, pay_refund, usr_get_user, usr_update_user
if __name__ == "__main__":
    gateway.run(transport="http", port=8080)
```

### What gets mounted

`mount()` copies everything from the child into the parent:

| Registry | Behavior |
|----------|----------|
| **Tools** | Names prefixed with `{prefix}_` |
| **Resources** | Copied as-is (no prefix) |
| **Prompts** | Copied as-is |
| **Exception handlers** | Merged into parent |
| **Input models** | Re-mapped to prefixed tool names |
| **`docs://manifest`** | Skipped (the parent serves its own) |

`mount(parent, child, *, prefix="", tags=None)` returns the number of tools mounted. It copies the child's registrations at the time you call it; later registrations on the child are not seen. Each copy keeps its `auth`, roles, guards and router middleware.

!!! warning "The child's server middleware is not mounted"
    Middleware added with `child.add_middleware(...)` (authentication, rate limits, audit) does not come along: mounted tools run through the **parent's** middleware chain. Add the middleware to the gateway. Name clashes (two tools with the same final name) raise `ValueError`.

### Adding tags during mount

```python
# Tag all payment tools for filtering
mount(gateway, payments, prefix="pay", tags=["payment", "billing"])
mount(gateway, users, prefix="usr", tags=["user-management"])
```

---

## OpenAPI Bridge

### When you need it

You have an existing REST API with an OpenAPI (or Swagger 2) spec and want agents to call it through MCP.

!!! warning "Use MCPcast for OpenAPI in 1.6.0"
    `OpenAPIProvider` registers one tool per operation, but in 1.6.0 the generated tools are not usable: their input schema is a single required `kwargs` field instead of the operation's parameters, so a call with the real arguments fails with `VALIDATION_ERROR`, and the method-based annotations are not attached. Use [MCPcast](mcpcast.md) (`promptise mcpcast ./openapi.yaml`), which generates a typed, curated server from the same spec.

`OpenAPIProvider(spec, *, base_url=None, prefix="", include=None, exclude=None, auth_header=None, tags=None)` and `provider.register(server) -> int` (the number of tools) remain available:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `spec` | (required) | URL (fetched with `httpx`), file path (JSON, or YAML with `.yaml`/`.yml`), JSON string, or dict |
| `base_url` | From spec | Override the API base URL (`servers[0].url`, or Swagger `schemes`/`host`/`basePath`) |
| `prefix` | `""` | Prepended to tool names as given (include your own `_`) |
| `include` | All | Only these operation IDs |
| `exclude` | None | Skip these operation IDs |
| `auth_header` | None | `(header_name, value)` sent on every request |
| `tags` | `[]` | Tags for all generated tools |

Tool names are the `operationId` (or `<method>_<path>`) with `-` and `/` replaced by `_`. A spec URL or base URL that resolves to a private or loopback address is refused.

---

## Batch Tool Calls

### When you need it

An agent needs to fetch 20 user profiles. Without batching, that's 20 sequential MCP round-trips. With a batch tool, it's one request that executes all 20 in parallel.

### `register_batch_tool()`

```python
from promptise.mcp.server import MCPServer, register_batch_tool

server = MCPServer(name="user-api")

@server.tool()
async def get_user(user_id: str) -> dict:
    """Get a user by ID."""
    return await db.get_user(user_id)

@server.tool()
async def get_team(team_id: str) -> dict:
    """Get a team by ID."""
    return await db.get_team(team_id)

# Register the batch meta-tool
register_batch_tool(server, name="batch_call", max_parallel=10)
```

Agents call it like this:

```json
{
  "tool": "batch_call",
  "arguments": {
    "calls": [
      {"tool": "get_user", "args": {"user_id": "u-001"}},
      {"tool": "get_user", "args": {"user_id": "u-002"}},
      {"tool": "get_team", "args": {"team_id": "t-100"}}
    ]
  }
}
```

Response:

```json
[
  {"tool": "get_user", "status": "ok", "result": ["..."]},
  {"tool": "get_user", "status": "ok", "result": ["..."]},
  {"tool": "get_team", "status": "ok", "result": ["..."]}
]
```

### Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `name` | `"batch_call"` | Name of the batch tool |
| `description` | `"Execute multiple tool calls in parallel."` | Description |
| `max_parallel` | `10` | Maximum concurrent executions |

`register_batch_tool(server, *, name="batch_call", description="Execute multiple tool calls in parallel.", max_parallel=10)`. A batch larger than `max_parallel * 2` calls is refused with a single `{"status": "error", "error": "Batch size ... exceeds max ..."}` entry. Each sub-call runs through the server's full pipeline (middleware, guards, validation) with the caller's request headers, so authentication and permissions apply per sub-call. Failed individual calls return `{"tool": ..., "status": "error", "error": "..."}` without failing the batch; `result` holds the text of each content item.

---

## Streaming Results

### When you need it

Your tool processes a large dataset or streams search results. Instead of accumulating everything in memory and returning a flat dict, you build up results incrementally and return them with metadata.

### `StreamingResult`

```python
from promptise.mcp.server import MCPServer, StreamingResult

server = MCPServer(name="search-api")

@server.tool()
async def search_products(
    query: str,
    max_results: int = 50,
) -> StreamingResult:
    """Search the product catalog.

    Returns results as they're found, with search metadata.
    """
    result = StreamingResult()
    result.set_metadata("query", query)

    async for hit in catalog.stream_search(query):
        result.add({
            "id": hit.id,
            "name": hit.name,
            "price": hit.price,
            "score": hit.relevance_score,
        })
        if len(result) >= max_results:
            break

    result.set_metadata("total_available", await catalog.count(query))
    return result
```

The framework serializes `StreamingResult` as JSON:

```json
{
  "items": [
    {"id": "p-001", "name": "Widget", "price": 999, "score": 0.95},
    {"id": "p-002", "name": "Gadget", "price": 1499, "score": 0.87}
  ],
  "count": 2,
  "metadata": {
    "query": "electronics",
    "total_available": 1250
  }
}
```

### Batch adding

```python
result = StreamingResult()

# Add one at a time
result.add({"id": 1, "name": "Alice"})

# Add many at once
result.add_many([
    {"id": 2, "name": "Bob"},
    {"id": 3, "name": "Charlie"},
])

# Attach metadata
result.set_metadata("source", "users_table")
result.set_metadata("cached", False)

len(result)         # → 3
result.items        # → [{"id": 1, ...}, {"id": 2, ...}, {"id": 3, ...}]
result.metadata     # → {"source": "users_table", "cached": False}
```

---

## Elicitation

### When you need it

Your tool is about to deploy to production or delete data. You want to ask the user for confirmation **during** tool execution, not before.

### `Elicitor`

```python
from promptise.mcp.server import MCPServer, Elicitor, Depends

server = MCPServer(name="deploy-api")

@server.tool()
async def deploy_to_production(
    service: str,
    version: str,
    elicit: Elicitor = Depends(Elicitor),
) -> dict:
    """Deploy a service version to production.

    Asks the user for confirmation before proceeding.
    """
    # Show what's about to happen and ask for confirmation
    answer = await elicit.ask(
        message=(
            f"About to deploy {service} v{version} to production.\n"
            f"Current production version: {await get_current_version(service)}\n"
            f"Confirm deployment?"
        ),
        schema={
            "type": "object",
            "properties": {
                "confirm": {
                    "type": "boolean",
                    "description": "Set to true to proceed with deployment",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional: reason for deployment",
                },
            },
            "required": ["confirm"],
        },
    )

    if not answer or not answer.get("confirm"):
        return {"status": "cancelled", "reason": (answer or {}).get("reason", "User declined")}

    deploy_id = await deploy(service, version)
    return {"status": "deployed", "deploy_id": deploy_id, "version": version}
```

### Graceful degradation

`ask()` sends the request through the SDK's `ServerSession.elicit`, waits up to its `timeout` (default from the `Elicitor`), and returns the response `content` only when the user **accepts**. If the client does not support elicitation (not all MCP clients do), declines, cancels, or the request times out, `ask()` returns `None` and logs the reason at `WARNING`. Design your tools to handle this:

```python
@server.tool()
async def delete_project(project_id: str, elicit: Elicitor = Depends(Elicitor)) -> dict:
    """Delete a project after the user confirms."""
    answer = await elicit.ask(
        f"Delete project {project_id}?",
        schema={"type": "object", "properties": {"confirm": {"type": "boolean"}}, "required": ["confirm"]},
    )
    if answer is None:
        # The client can't elicit, or the user declined: don't act
        return {"error": "This action requires confirmation. Please confirm and retry."}
    if not answer.get("confirm"):
        return {"status": "cancelled"}
    return {"status": "deleted", "project_id": project_id}
```

`ask(message, schema=None, *, timeout=None)`; without a schema the request is a bare confirmation (an empty object form). Elicitation needs a session that can send requests to the client, so it doesn't work with `asgi_app(stateless=True)`.

### Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `timeout` | `60.0` | Seconds to wait for user response |

---

## Sampling

### When you need it

Your tool needs to call an LLM to process data (summarize text, classify content, extract entities) but the **client** controls which LLM to use. MCP sampling lets the server request completions through the client's model.

### `Sampler`

```python
from promptise.mcp.server import MCPServer, Sampler, Depends

server = MCPServer(name="content-api")

@server.tool()
async def summarize_document(
    document_url: str,
    sampler: Sampler = Depends(Sampler),
) -> dict:
    """Summarize a document using the client's LLM.

    The server fetches the document; the client provides the model.
    """
    content = await fetch_document(document_url)

    summary = await sampler.create_message(
        messages=[
            {"role": "user", "content": f"Summarize this document in 3 bullet points:\n\n{content[:5000]}"},
        ],
        max_tokens=500,
        system="You are a professional document summarizer. Be concise and factual.",
        temperature=0.3,
    )

    if summary is None:
        return {"error": "Sampling not supported by client"}

    return {
        "url": document_url,
        "summary": summary,
        "model": "client-provided",
    }
```

### Parameters

```python
result = await sampler.create_message(
    messages=[{"role": "user", "content": "..."}],
    max_tokens=1024,          # Maximum tokens to generate
    model="claude-sonnet-4-20250514",      # Model hint (client may ignore)
    system="You are...",      # System prompt
    temperature=0.7,          # Sampling temperature
    stop_sequences=["\n\n"],  # Stop sequences
)
```

The client controls which model actually runs. The `model` parameter is sent as a model preference hint; the client can override it based on cost, availability, or policy. Message roles other than `"assistant"` are sent as `"user"` (MCP sampling has no system role; use `system=`). `create_message()` returns the generated text, or `None` when the client doesn't support sampling or the request fails (logged at `WARNING`). Like elicitation, sampling needs a session that can send requests to the client.

---

## Session State

### When you need it

A client makes several calls in one conversation and a tool needs to remember something between them: a cart, a selected project, a cursor.

### `SessionState`

```python
from promptise.mcp.server import MCPServer, SessionState, Depends

server = MCPServer(name="cart-api")

@server.tool()
async def add_item(item: str, session: SessionState = Depends(SessionState)) -> dict:
    """Add an item to this session's cart."""
    items = session.get("items", [])
    items.append(item)
    session.set("items", items)
    return {"count": len(items)}

@server.tool()
async def cart(session: SessionState = Depends(SessionState)) -> list[str]:
    """List the items in this session's cart."""
    return session.get("items", [])
```

`SessionState` is a small key-value store: `get(key, default=None)`, `set(key, value)`, `delete(key)`, `clear()`, `keys()`, `to_dict()` and `key in session`. The server keeps one store per MCP connection (in a `SessionManager`), in memory.

!!! warning "Limits in 1.6.0"
    - State lives in the server process: it is lost on restart and not shared between replicas or workers.
    - Stores are not removed when a session ends, so a long-running HTTP server keeps one per connection it has served. Don't put large values in it.
    - The store is keyed by the connection object's Python `id()`. After a connection is closed and garbage-collected, a later connection can receive the same id and see the old store. Don't keep secrets or per-user data that must not leak between sessions in `SessionState`; key such data by `ctx.client.client_id` in your own store instead.
    - Without a session (`asgi_app(stateless=True)`, or `TestClient`) each call gets a fresh store.

---

## Server Manifest

### When you need it

You want agents to introspect your server (discover all tools, their schemas, tags, auth requirements, and rate limits) without calling each tool individually.

### `register_manifest()`

```python
from promptise.mcp.server import MCPServer, register_manifest

server = MCPServer(name="my-api", version="2.0.0", auto_manifest=False)

@server.tool(tags=["math"], roles=["user"])
async def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

@server.tool(tags=["math"], auth=True)
async def divide(a: float, b: float) -> float:
    """Divide two numbers."""
    return a / b

register_manifest(server)

# MCPServer(auto_manifest=True), the default, registers docs://manifest when the
# server is built. Call register_manifest() yourself only with auto_manifest=False.
```

Agents read the manifest via MCP's resource protocol:

```json
{
  "server": {"name": "my-api", "version": "2.0.0", "instructions": null},
  "tools": [
    {
      "name": "add",
      "description": "Add two numbers.",
      "input_schema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}},
      "tags": ["math"],
      "auth_required": true,
      "roles": ["user"],
      "guards": ["HasRole"]
    },
    {
      "name": "divide",
      "description": "Divide two numbers.",
      "input_schema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}},
      "tags": ["math"],
      "auth_required": true
    }
  ],
  "resources": [],
  "resource_templates": [],
  "prompts": []
}
```

Each entry lists only the fields that apply: `tags`, `auth_required`, `roles`, `guards` (class names), `rate_limit`, `timeout`. With `MCPServer(hide_unauthorized_tools=True)` the manifest is filtered per caller like the lists.

### Programmatic access

```python
from promptise.mcp.server import build_manifest

manifest = build_manifest(server)
print(manifest["tools"])  # All registered tools with metadata
```

`build_manifest(server, tools=None, *, resources=None, resource_templates=None, prompts=None)`: pass any of the lists to describe a subset instead of every registration.

---

## Hot Reload

### When you need it

You're developing an MCP server and want to see changes immediately without restarting manually. Hot reload watches your Python files and restarts the server when you save.

### `hot_reload()`

```python
from promptise.mcp.server import MCPServer, hot_reload

server = MCPServer(name="dev-server")

@server.tool()
async def greet(name: str) -> str:
    """Greet someone."""
    return f"Hello, {name}!"

if __name__ == "__main__":
    hot_reload(
        server,
        transport="http",
        port=8080,
        watch_dirs=["src/"],       # Directories to watch
        poll_interval=1.0,          # Check every second
    )
```

```
--- Starting server (pid will follow) ---
    Watching: src/
    Poll interval: 1.0s

MCP server running on http://127.0.0.1:8080/mcp

--- Detected changes in 1 file(s) ---
    src/tools.py
--- Restarting server ---
```

### How it works

1. The parent process watches `*.py` files for modification time changes
2. When a change is detected, the child server process is terminated
3. A new child process is started with the updated code
4. If the server crashes on startup (syntax error), it waits for the next file change

### Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `transport` | `"http"` | Transport for the child server |
| `host` | `"127.0.0.1"` | Bind host |
| `port` | `8080` | Bind port |
| `watch_dirs` | `["."]` | Directories to watch |
| `poll_interval` | `1.0` | Seconds between checks |
| `dashboard` | `False` | Enable dashboard in child |
| `allowed_hosts` | `None` | `Host` allow-list for the child (see [Deployment](deployment.md)) |
| `allowed_origins` | `None` | `Origin` allow-list for the child |

`hot_reload()` restarts **the script you ran** (`sys.argv`) in a child process, so call it from the `if __name__ == "__main__":` block of the file that defines the server. A child that exits with an error waits for the next file change before restarting. Changes are detected by modification time of `*.py` files under `watch_dirs`.

!!! warning "Development only"
    Hot reload is for development. In production, use a process manager like systemd, supervisord, or Kubernetes.

---

## CLI Serve

### When you need it

You've built an MCP server in a Python module but don't want to add `if __name__ == "__main__"` boilerplate. The CLI `serve` command runs any server from the command line.

### Usage

```bash
# Run with stdio transport (default)
promptise serve myapp.server:server

# Run with HTTP transport
promptise serve myapp.server:server --transport http --port 8080

# With hot reload (development)
promptise serve myapp.server:server --transport http --reload

# With live dashboard
promptise serve myapp.server:server --transport http --dashboard
```

### Target format

The target uses `module:attribute` format:

```bash
# module.path:attribute_name
promptise serve myapp.server:server     # myapp/server.py → server variable
promptise serve myapp.api:app           # myapp/api.py → app variable
promptise serve tools:tool_server       # tools.py → tool_server variable
```

### CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--transport`, `-t` | `stdio` | Transport: `stdio`, `http`, or `sse` |
| `--host` | `127.0.0.1` | Bind host |
| `--port`, `-p` | `8080` | Bind port |
| `--dashboard` | off | Enable live dashboard (HTTP/SSE only; ignored with a warning on stdio) |
| `--reload` | off | Enable hot reload |
| `--allowed-host HOST` | none | `Host` header value to accept (repeatable); see [Deployment](deployment.md) |
| `--allowed-origin ORIGIN` | none | `Origin` header value to accept for browser clients (repeatable; needs `--allowed-host` on a non-loopback bind) |

The current directory is put on `sys.path`, so `promptise serve server:server` finds `./server.py`. A target that is not `module:attribute`, can't be imported, or is not an `MCPServer` exits with code 1 and a message on stderr (stdout stays clean for the stdio protocol).

---

## API Summary

| Symbol | Type | Description |
|--------|------|-------------|
| `VersionedToolRegistry` | Class | Standalone helper: several `ToolDef` versions per name (not used by the server) |
| `ToolTransform` | Protocol | `apply(tools, ctx=None) -> list[ToolDef]` (not applied by the server) |
| `NamespaceTransform(prefix)` | Class | Prefix tool names in a `ToolDef` list |
| `VisibilityTransform(hidden)` | Class | Drop tools whose predicate returns true |
| `TagFilterTransform(required_tags)` | Class | Keep tools with at least one of the tags |
| `ToolDef` | Dataclass | A registered tool: `name`, `description`, `handler`, `input_schema`, `tags`, `auth`, `rate_limit`, `timeout`, `guards`, `roles`, `router_middleware`, `annotations`, `max_concurrent`, `requires_approval`, `cache` |
| `mount(parent, child, prefix, tags)` | Function | Compose servers |
| `OpenAPIProvider(spec, ...)` | Class | Generate tools from OpenAPI (see the warning above; prefer MCPcast) |
| `register_batch_tool(server, ...)` | Function | Parallel batch execution |
| `StreamingResult` | Class | Incremental result collection |
| `Elicitor` | Class | Ask user for input mid-execution (via DI) |
| `Sampler` | Class | Request LLM completions from client (via DI) |
| `SessionState` | Class | Per-session key-value store (via DI) |
| `SessionManager` | Class | Holds the `SessionState` stores of one server (created by the server) |
| `build_manifest(server)` | Function | Build server manifest dict |
| `register_manifest(server)` | Function | Register `docs://manifest` resource |
| `hot_reload(server, ...)` | Function | File-watching server restarter |
| `build_serve_parser(...)` | Function | CLI argument parser |
| `resolve_server(target)` | Function | Import server from `module:attr` |
| `run_serve(args, server=None)` | Function | Execute CLI serve command |

## What's Next

- [Deployment](deployment.md): HTTP transport, CORS, containers, reverse proxy
- [Caching & Performance](caching-performance.md): Cache, rate limit, concurrency control
- [Observability & Monitoring](observability.md): Metrics, tracing, Prometheus, logging
- [Resilience Patterns](resilience-patterns.md): Circuit breakers, health checks, webhooks
