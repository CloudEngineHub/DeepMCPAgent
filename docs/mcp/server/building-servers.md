---
seo_title: "Build an MCP Server: Tools, Resources, Prompts"
description: "Build an MCP server in Python: register tools with decorators and Pydantic validation, add resources, templates and prompts, lifecycle hooks, then run it."
---

# MCP Server Fundamentals

Full reference for building MCP servers with decorator-based tool registration, Pydantic validation, and lifecycle hooks.

!!! tip "New to MCP servers?"
    Start with the [Step-by-Step Guide](../../guides/production-mcp-servers.md) for a hands-on walkthrough. This page is the deep reference.

## Quick Start

```python
from promptise.mcp.server import MCPServer

server = MCPServer(name="my-tools", version="1.0.0")

@server.tool()
async def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

@server.resource("config://app", mime_type="application/json")
async def app_config() -> str:
    """Return application configuration."""
    return '{"version": "1.0.0"}'

if __name__ == "__main__":
    server.run()  # stdio transport by default
```

Run this file and any MCP-compatible client can discover and call the `add` tool. Parameters are validated automatically from the function signature and type hints: a call with a wrong or missing argument gets a validation error, and the handler does not run.

## Concepts

**MCPServer** is the central object. It manages tool, resource, and prompt registries, a middleware chain, lifecycle hooks, and the transport layer. You register handlers with decorators and call `server.run()` to start serving.

**Tools** are the primary capability. Each tool is a Python function exposed to MCP clients. The framework automatically generates a JSON Schema from the function signature, including Pydantic model parameters.

**Resources** expose read-only data at static URIs (e.g. `config://app`). **Resource templates** use `{param}` placeholders for dynamic URIs.

**Prompts** expose reusable prompt templates that clients can discover and render with arguments.

## Registering Tools

### Basic tools

Decorate any sync or async function with `@server.tool()`. The function name becomes the tool name, and the docstring's summary paragraph becomes the description.

```python
@server.tool()
async def search(query: str, limit: int = 10) -> list[dict]:
    """Search records by keyword."""
    return await db.search(query, limit)
```

### Describing parameters

The model only sees what is in the tool's `inputSchema`, so describe every parameter whose meaning is not obvious from its name. There are two ways to do it.

**`Annotated` with `Field`.** Put the description and any constraints on the type. They go into the schema and are enforced when the tool is called:

```python
from typing import Annotated

from pydantic import Field

@server.tool()
async def get_order_status(
    order_id: Annotated[str, Field(description='The order ID, for example "A-1001".')],
    limit: Annotated[int, Field(ge=1, le=50, description="Max shipping events.")] = 10,
) -> dict:
    """Get the status of an order and its shipping events."""
    ...
```

```json
{
  "type": "object",
  "properties": {
    "order_id": {"type": "string", "title": "Order Id", "description": "The order ID, for example \"A-1001\"."},
    "limit": {"type": "integer", "title": "Limit", "default": 10, "minimum": 1, "maximum": 50, "description": "Max shipping events."}
  },
  "required": ["order_id"]
}
```

A `Field(...)` as the default works the same way: `order_id: str = Field(description="...")` is a required parameter, and `limit: int = Field(default=10, ge=1)` is an optional one.

**The docstring `Args:` section.** Any parameter without a `Field` description takes its description from the docstring. Google style (`name: text` or `name (type): text` under `Args:`, `Arguments:` or `Parameters:`) and Sphinx style (`:param name: text`) are both read. A description that wraps onto indented continuation lines is joined into one line:

```python
@server.tool()
async def get_order_status(order_id: str, include_items: bool = False) -> dict:
    """Get the status of an order.

    Args:
        order_id: The order ID, for example "A-1001". Case
            sensitive.
        include_items: Also return the line items.
    """
    ...
```

If both are given, the `Field` description wins. Constraints from `Field` are kept when the description comes from the docstring. Injected parameters (`RequestContext`, `Depends(...)`) never appear in the schema, even if the docstring documents them. NumPy-style `Parameters` / `----------` blocks are not parsed. Use `Field` for those.

### How the description is chosen

The tool description is the first of these that applies:

1. `description=` passed to `@server.tool()`.
2. The docstring's **summary paragraph**: every line up to the first blank line, section header (`Args:`, `Returns:`, `Raises:`, `Example:`, ...) or Sphinx field (`:param ...:`). The lines are joined with single spaces, so a summary that wraps over two lines comes through as one sentence.
3. The function name.

Later paragraphs are **not** sent. In Python they are usually notes for maintainers (implementation details, caveats, examples), and every client gets the description with each `tools/list`. The `Args:` section goes into the parameter schema instead. To send more guidance to the model, such as when to use the tool or how it relates to others, put it in the summary paragraph or pass `description=`. Resources and prompts use the same rule, and prompt arguments read the `Args:` section the same way.

```python
@server.tool()
async def get_order_status(order_id: str) -> dict:
    """Get the current status of an order, including the carrier and
    the expected delivery date.

    Reads from the replica, so it can lag the primary by a few seconds.
    """
    ...

# description: "Get the current status of an order, including the carrier and the expected delivery date."
```

### Custom name and description

```python
@server.tool(name="find_users", description="Find users by name or email.")
async def user_search(query: str) -> list[dict]:
    return await db.find_users(query)
```

### Pydantic model parameters

For complex inputs, use Pydantic models. The framework generates a nested JSON Schema that MCP clients can use to construct valid requests.

```python
from pydantic import BaseModel, Field

class Address(BaseModel):
    street: str = Field(description="Street name and number")
    city: str = Field(description="City name")
    zip_code: str = Field(pattern=r"^\d{5}(-\d{4})?$")

class CreateUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str
    address: Address
    tags: list[str] = Field(default_factory=list)

@server.tool(tags=["users", "write"])
async def create_user(user: CreateUserRequest) -> dict:
    """Create a new user with validated address."""
    return {"id": "u-123", "name": user.name}
```

The `tags` parameter is metadata for categorisation and does not affect behaviour.

Nested models would normally appear in the schema as `$ref` pointers into `$defs`. Many MCP clients can't resolve those, so the server inlines every reference before it lists the tool: the `inputSchema` clients receive has no `$ref` or `$defs`. The same helper is public as `dereference_schema(schema) -> dict`, for schemas you build yourself:

```python
from promptise.mcp.server import dereference_schema

schema = {
    "$defs": {"Addr": {"type": "object", "properties": {"city": {"type": "string"}}}},
    "properties": {"address": {"$ref": "#/$defs/Addr"}},
    "type": "object",
}
print(dereference_schema(schema))
# {'properties': {'address': {'type': 'object', 'properties': {'city': {'type': 'string'}}}}, 'type': 'object'}
```

### Tool-level options

The `@server.tool()` decorator accepts several options:

```python
@server.tool(
    name="delete_user",          # Override tool name
    description="Remove a user", # Override description
    tags=["users", "admin"],     # Categorisation tags
    auth=True,                   # Require authentication
    roles=["admin"],             # Require specific roles (shorthand for HasRole guard)
    guards=[RequireAuth()],      # Access control guards
    rate_limit="100/min",        # Per-tool rate limit string
    timeout=10.0,                # Per-call timeout in seconds
    max_concurrent=5,            # Per-tool concurrency limit
    # MCP spec annotations (hints for clients):
    title="Delete User",         # Human-readable title
    read_only_hint=False,        # Tool modifies state
    destructive_hint=True,       # Tool performs destructive action
    idempotent_hint=True,        # Safe to retry
    open_world_hint=False,       # No external system interaction
)
async def delete_user(user_id: str) -> dict:
    """Delete a user by ID."""
    return {"deleted": user_id}
```

Two more options carry server-wide invariants: `requires_approval=True`
requires a human decision before every call, and the server **refuses to build**
unless an `ApprovalGateMiddleware` is installed (see
[Approval Gates](approval-gates.md)). Tenant access is enforced with the
tenant guards or `MCPServer(require_tenant=True)` (see
[Multi-Tenancy](multi-tenancy.md)).

#### `@server.tool()` parameters

| Parameter | Default | Description |
|---|---|---|
| `name` | function name | Tool name sent to clients |
| `description` | docstring summary | See [How the description is chosen](#how-the-description-is-chosen) |
| `tags` | `None` | Categorisation tags (used by routers, `TagFilterTransform`, the manifest) |
| `auth` | `False` | Require an authenticated caller (needs an `AuthMiddleware`) |
| `rate_limit` | `None` | `"<count>/<unit>"` with unit `s`/`sec`/`second`, `m`/`min`/`minute` or `h`/`hr`/`hour` (for example `"100/min"`). One bucket per authenticated client (and tenant), one shared bucket for anonymous callers; enforced with no extra middleware; a malformed string raises `ValueError` at registration |
| `timeout` | `None` | Per-call timeout in seconds |
| `guards` | `None` | Guards checked before the handler; a built-in identity guard implies `auth=True` |
| `roles` | `None` | Shorthand for `guards=[HasRole(*roles)]`; implies `auth=True` |
| `title` | `None` | MCP annotation: human-readable title |
| `read_only_hint` | `None` | MCP annotation: the tool doesn't change state |
| `destructive_hint` | `None` | MCP annotation: the tool may destroy data (such tools are never cached) |
| `idempotent_hint` | `None` | MCP annotation: repeating a call has no further effect |
| `open_world_hint` | `None` | MCP annotation: the tool talks to external systems |
| `max_concurrent` | `None` | Per-tool concurrency cap; extra calls get a retryable error |
| `requires_approval` | `False` | Human approval before every call (needs `ApprovalGateMiddleware`) |
| `cache` | `True` | `False` keeps a server-wide `CacheMiddleware` from caching this tool |

The five annotation parameters are sent to clients as the tool's MCP `annotations` (`title`, `readOnlyHint`, ...) only when at least one is set; they are hints, not enforcement. The registered definition is a `ToolDef` (with an `annotations` field of type `ToolAnnotations`), which you see in transforms and the manifest.

### Structured tool outputs

By default, tool return values are serialised to JSON text. For richer responses, return MCP content types directly:

```python
from promptise.mcp.server import MCPServer, ImageContent

server = MCPServer(name="charts")

@server.tool()
async def generate_chart(data: list[float]) -> ImageContent:
    """Generate a bar chart as PNG."""
    png_bytes = render_chart(data)
    return ImageContent(data=png_bytes, mime_type="image/png")
```

You can also return mixed content lists:

```python
from mcp.types import TextContent

@server.tool()
async def analyze(query: str) -> list:
    """Return analysis text with a chart."""
    chart_png = await render_chart(query)
    return [
        TextContent(type="text", text="Analysis complete:"),
        ImageContent(data=chart_png, mime_type="image/png"),
    ]
```

Supported return types:

| Return type | Behaviour |
|---|---|
| `str`, `int`, `float`, etc. | `str(value)` in one `TextContent` |
| `dict`, `list` (plain data) | JSON-serialised into `TextContent` (non-JSON values with `str()`) |
| `None` | `TextContent` with the text `OK` |
| `ImageContent(data, mime_type="image/png")` | Converted to MCP `ImageContent` (base64-encoded) |
| `TextContent(type="text", text=...)` | Passed through as-is |
| `EmbeddedResource(...)` | Passed through as-is |
| Mixed `list` of the above | Each item serialised individually |
| `ToolResponse(content, metadata={})` | `content` serialised as above; `metadata` stored in `ctx.state["response_metadata"]` for middleware and audit |

!!! warning "Return Pydantic models as dicts"
    A tool that returns a Pydantic model is serialised with `str()`, so the client receives `title='Dune' year=1965`, not JSON (a `list` of models becomes a JSON list of those strings). Return `model.model_dump(mode="json")` (or a list of them) to send JSON. Resources do serialise models as JSON.

## Registering Resources

Resources expose read-only data at a fixed URI. Signature: `@server.resource(uri, *, name=None, description=None, mime_type=None, tags=None, auth=False, roles=None, guards=None, rate_limit=None, timeout=None)`; `@server.resource_template(uri_template, ...)` takes the same options.

```python
@server.resource("config://app")
async def app_config() -> dict:
    """Server configuration."""
    return {"version": "1.0.0", "environment": "production"}
```

What the handler returns decides what the client receives:

| Handler returns | Client receives |
|---|---|
| `str` | Text, with the declared `mime_type` |
| `dict`, `list` or a Pydantic model | JSON text |
| `bytes` | Binary content (`BlobResourceContents`, base64-encoded) |
| `ReadResourceContents` (or a list of them) | Passed through, for several contents or per-item MIME types |

Without `mime_type=`, the MIME type comes from the return annotation: `-> dict`, `-> list` or a Pydantic model gives `application/json`, `-> bytes` gives `application/octet-stream`, anything else `text/plain`. A declared `mime_type` is always sent as declared:

```python
@server.resource("img://logo", mime_type="image/png")
async def logo() -> bytes:
    return Path("logo.png").read_bytes()
```

### Resource templates

For dynamic URIs, use `@server.resource_template()` with `{param}` placeholders. Each placeholder must name a handler parameter, and its value is converted to the parameter's type hint (`"2"` becomes `2` for an `int`):

```python
@server.resource_template("users://{user_id}/orders/{page}")
async def user_orders(user_id: str, page: int) -> dict:
    """One page of a user's orders."""
    return {"user_id": user_id, "page": page, "orders": await db.orders(user_id, page)}
```

`{param}` matches a single path segment (no `/`). For hierarchical ids, use `{param*}` (or `{+param}`), which matches the rest of the URI, slashes included:

```python
@server.resource_template("docs://pages/{path*}", mime_type="text/markdown")
async def page(path: str) -> str:
    # docs://pages/guides/setup → path == "guides/setup"
    return load_page(path)
```

A template without a catch-all is tried before one with a catch-all, so `docs://pages/{slug}/history` wins over `docs://pages/{path*}` whatever the registration order. Parameter values are percent-decoded (`caf%C3%A9` → `café`).

## Registering Prompts

Prompts are reusable templates that clients discover and render with arguments. Signature: `@server.prompt(name=None, *, description=None, tags=None, auth=False, roles=None, guards=None, rate_limit=None, timeout=None)`. To serve prompts built with the [Promptise prompt framework](../../prompting/index.md), use `server.include_prompts(my_prompt, MySuite(), registry)`: each becomes an MCP prompt whose arguments come from the function signature (descriptions from `Annotated[..., Field(description=...)]`, the `.prompt` file's `arguments`, or the placeholder name), rendered with `render_async()` (blocks, context providers, strategy and constraints applied).

```python
@server.prompt()
async def summarize(text: str, max_words: int = 100) -> str:
    """Summarize the given text."""
    return f"Summarize the following text in at most {max_words} words:\n\n{text}"
```

MCP sends every prompt argument as a string. They are converted to the handler's type hints, so `max_words` above arrives as an `int`; a value that does not fit is refused with a validation error.

A prompt handler can return:

- a `str` (one user message)
- a `PromptMessage`
- a `{"role": ..., "content": ...}` dict (string content becomes text)
- a list mixing any of the above, for a multi-turn prompt
- a full `GetPromptResult`

```python
@server.prompt()
async def code_review(code: str) -> list:
    """Review a piece of code."""
    return [
        f"Review this code:\n\n{code}",
        {"role": "assistant", "content": "I'll check correctness first, then style."},
    ]
```

## Securing Resources and Prompts

Resource reads and prompt requests run through the same middleware chain as tool calls: logging, audit, rate limits, timeouts, caching and authentication all apply. `@server.resource`, `@server.resource_template` and `@server.prompt` take the same access-control options as `@server.tool`:

```python
server.add_middleware(AuthMiddleware(JWTAuth(secret=os.environ["JWT_SECRET"])))

@server.resource("hr://payroll", roles=["hr"], rate_limit="30/min")
async def payroll() -> dict:
    return await db.payroll()

@server.prompt(auth=True, guards=[HasTenant("acme")])
async def onboarding(name: str) -> str:
    return f"Write an onboarding plan for {name}."
```

| Option | Effect |
|---|---|
| `auth=True` | The caller must be authenticated |
| `roles=[...]` | Adds a `HasRole` guard and implies `auth=True` |
| `guards=[...]` | Guards checked before the handler runs |
| `rate_limit="100/min"` | Per-client rate limit, enforced with no extra wiring |
| `timeout=5.0` | Per-request timeout (with `TimeoutMiddleware`) |
| `tags=[...]` | Tags for categorisation |

A denied read or prompt request returns an MCP error to the client; the handler never runs. `MCPServer(require_auth=True)` and `require_tenant=True` cover resources and prompts too, as do a router's `auth`, `guards` and `middleware`. A middleware that only makes sense for one kind of request can check `ctx.request_type` (`"tool"`, `"resource"` or `"prompt"`); for a resource read, `ctx.state["resource_uri"]` holds the URI.

!!! note "Listing"
    By default `resources/list`, `resources/templates/list` and `prompts/list` show every registration to every client, as `tools/list` does; guards apply when a resource is read or a prompt is requested. With `MCPServer(hide_unauthorized_tools=True)` all four lists (and `docs://manifest`) are filtered per caller: each definition's guards are evaluated against the caller's credentials, and what the caller may not use is not listed.

## Notifying Clients of Changes

The server advertises `listChanged` for tools, resources and prompts. Registering a tool, resource or prompt while the server is running sends `notifications/tools/list_changed` (or the resources or prompts equivalent) to every connected client, so they can list again. After changing what a listing returns some other way, send it yourself:

```python
await server.notify_resources_changed()   # also notify_tools_changed(), notify_prompts_changed()
```

## Lifecycle Hooks

Run setup and teardown logic with `@server.on_startup` and `@server.on_shutdown`:

```python
@server.on_startup
async def startup():
    """Initialize database pool on server start."""
    await db.connect()
    print("Database connected")

@server.on_shutdown
async def shutdown():
    """Clean up resources on server stop."""
    await db.disconnect()
    print("Database disconnected")
```

Startup hooks run before the server begins accepting requests. Shutdown hooks run during graceful shutdown (configurable via `shutdown_timeout`). Over stdio, when the client closes stdin, the server first answers the requests still running, waiting at most `shutdown_timeout` seconds for them.

## Server Configuration

### Constructor options

```python
server = MCPServer(
    name="hr-platform",           # Server name advertised to clients
    version="2.1.0",              # Version string
    instructions="Use the HR tools to manage employees.",  # Sent to clients on init
    auto_manifest=True,           # Register a manifest resource (default: True)
    shutdown_timeout=30.0,        # Graceful shutdown timeout in seconds
    require_auth=False,           # Force auth=True on every tool, resource and prompt
)
```

| Parameter | Default | Description |
|---|---|---|
| `name` | `"promptise-server"` | Server name advertised to clients |
| `version` | `"0.1.0"` | Server version |
| `instructions` | `None` | Instructions sent to clients in the `initialize` result |
| `auto_manifest` | `True` | Register the `docs://manifest` resource |
| `shutdown_timeout` | `30.0` | Seconds to wait for in-flight requests and shutdown hooks; `None` waits without limit |
| `require_auth` | `False` | Force `auth=True` on every tool, resource and prompt, from every registration path |
| `require_tenant` | `False` | Also add a `RequireTenant` guard everywhere (implies `require_auth`); see [Multi-Tenancy](multi-tenancy.md) |
| `hide_unauthorized_tools` | `False` | Filter `tools/list`, `resources/list`, `resources/templates/list`, `prompts/list` and the manifest per caller |
| `cancel_grace_period` | `5.0` | When a client cancels a call to a tool that takes a `CancellationToken`, seconds the handler gets to stop before its task is cancelled |

### ServerSettings for environment variables

Subclass `ServerSettings` to load configuration from environment variables using Pydantic's `BaseSettings`:

```python
from promptise.mcp.server import ServerSettings, Depends

class AppSettings(ServerSettings):
    database_url: str = "sqlite:///local.db"
    max_results: int = 100

    model_config = {"env_prefix": "MY_APP_", "extra": "ignore"}

@server.tool()
async def info(settings: AppSettings = Depends(AppSettings)) -> dict:
    """Return current configuration."""
    return {"db": settings.database_url, "max": settings.max_results}
```

Set `MY_APP_DATABASE_URL` and `MY_APP_MAX_RESULTS` in the environment to override defaults.

## Running the Server

`server.run()` is a blocking call that starts the transport and event loop:

```python
# stdio (default) -- for Claude Desktop, Cursor, etc.
server.run()

# HTTP (Streamable HTTP) -- for remote agents and web clients
server.run(transport="http", host="127.0.0.1", port=8080)

# SSE (Server-Sent Events, legacy)
server.run(transport="sse", port=9090)

# HTTP with live terminal dashboard
server.run(transport="http", host="127.0.0.1", port=8080, dashboard=True)

# Every interface (containers, other machines) -- name the public host
server.run(transport="http", host="0.0.0.0", port=8080, allowed_hosts=["mcp.example.com"])
```

HTTP and SSE bind `127.0.0.1` unless you pass `host`, and also serve `GET /health` and `GET /health/ready` for probes. To run under your own ASGI server (uvicorn flags, gunicorn workers), use `app = server.asgi_app(...)` instead of `run()`. See [Deployment](deployment.md) for binds, `Host`/`Origin` validation, CORS, probes and replicas.

For async code, use `await server.run_async(...)` instead.

A Promptise server runs on the MCP SDK you have installed, 1.x (`mcp>=1.9`) or 2.x (see [MCP SDK versions](../../getting-started/installation-extras.md#mcp-sdk-versions)), and serves clients of both SDK generations. On the 2.x SDK it answers `server/discover`, so a 2.x client (current Pydantic AI and OpenAI Agents SDK releases, for example) uses the 2026-07-28 protocol revision, and 1.x clients use the `initialize` handshake as before. On the 1.x SDK, which doesn't know `server/discover`, the server answers it with `-32601 Method not found` and a 2.x client falls back to the `initialize` handshake. On either, a request for a method the server does not implement gets `-32601` without a log warning, and a notification of an unknown method is ignored.

On a 2026-07-28 connection the server cannot send requests to the client, so features that ask the client mid-call (elicitation, including the [approval gate](../../core/approval.md)'s elicitation approver, and sampling) fail closed there; a client that needs them connects with the handshake (the 2.x SDK's `Client(..., mode="legacy")`; Promptise's `MCPClient` always does).

## Complete Example

A full server with Pydantic validation, lifecycle hooks, and HTTP transport:

```python
import json
from pydantic import BaseModel, Field
from promptise.mcp.server import MCPServer

server = MCPServer(name="bookstore", version="1.0.0")

_books: dict[str, dict] = {}

class Book(BaseModel):
    title: str = Field(min_length=1, description="Book title")
    author: str = Field(description="Author name")
    year: int = Field(ge=1450, description="Publication year")

@server.on_startup
async def seed():
    _books["b-1"] = {"id": "b-1", "title": "Dune", "author": "Frank Herbert", "year": 1965}

@server.tool(tags=["books", "read"])
async def list_books() -> list[dict]:
    """List all books in the store."""
    return list(_books.values())

@server.tool(tags=["books", "write"])
async def add_book(book: Book) -> dict:
    """Add a book to the store."""
    book_id = f"b-{len(_books) + 1}"
    record = {"id": book_id, **book.model_dump()}
    _books[book_id] = record
    return record

@server.resource("catalog://stats", mime_type="application/json")
async def catalog_stats() -> str:
    return json.dumps({"total_books": len(_books)})

if __name__ == "__main__":
    server.run(transport="http", host="127.0.0.1", port=8080)
```

## API Summary

| Symbol | Type | Description |
|---|---|---|
| `MCPServer(name, version, ...)` | Class | Main server class |
| `@server.tool(...)` | Decorator | Register a tool handler |
| `@server.resource(uri, ...)` | Decorator | Register a static resource |
| `@server.resource_template(uri, ...)` | Decorator | Register a resource template |
| `@server.prompt(...)` | Decorator | Register a prompt |
| `@server.on_startup` | Decorator | Register a startup hook |
| `@server.on_shutdown` | Decorator | Register a shutdown hook |
| `server.add_middleware(mw)` | Method | Add middleware to the chain |
| `server.include_router(router)` | Method | Merge a router into the server |
| `@server.middleware` | Decorator | Register a function middleware `async (ctx, call_next)` |
| `@server.exception_handler(exc_type)` | Decorator | Map an exception type to an `MCPError` |
| `server.include_prompts(*sources)` | Method | Serve Promptise `Prompt`s, suites or registries as MCP prompts |
| `server.enable_token_endpoint(jwt_auth, clients, ...)` | Method | Dev/test OAuth2 token endpoint ([Auth](auth-security.md)) |
| `server.notify_tools_changed()` (and resources, prompts) | Async method | Send a `list_changed` notification |
| `server.run(transport, host, port)` | Method | Start the server (blocking) |
| `server.asgi_app(transport, allowed_hosts, ...)` | Method | The server as an ASGI app for uvicorn / gunicorn |
| `server.run_async(...)` | Method | Start the server (async) |
| `ServerSettings` | Class | Base class for env-var settings |

!!! tip "Sync and async handlers"
    Both sync and async handler functions are supported. The framework automatically awaits coroutines. Use async handlers for I/O-bound work and sync handlers for simple computations.

!!! warning "Pydantic v2 required"
    The server uses Pydantic v2 for input validation and schema generation. Pydantic v1 models are not supported.

!!! tip "Auto-generated manifest"
    By default, `MCPServer` registers a `docs://manifest` resource containing a JSON summary of all tools, resources, and prompts. Disable with `auto_manifest=False`.

## What's Next?

- [Routers & Middleware](routers-middleware.md): Organize tools into modules and add cross-cutting concerns
- [Authentication & Security](auth-security.md): Secure your server with JWT, API keys, and guards
- [Production Features](production-features.md): Add caching, rate limiting, health checks, and metrics
- [Testing](testing.md): Test servers in-process with `TestClient`
- [Step-by-Step Guide](../../guides/production-mcp-servers.md): Build a complete server incrementally

!!! tip "Learn the concept"

    - [Build a Production MCP Server in Python (Step by Step)](https://www.promptise.com/guides/view/build-production-mcp-server-python): Build a server that real agents can use, step by step.
    - [MCP Tools vs Resources vs Prompts (With Python Examples)](https://www.promptise.com/guides/view/mcp-tools-vs-resources-vs-prompts): When to expose a tool, a resource or a prompt.
