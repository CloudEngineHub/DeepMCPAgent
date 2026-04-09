# Config & Server Specs

Server specifications tell Promptise *how* to connect to each MCP server your
agent needs.  Two transports are supported out of the box: **stdio** (launch a
local process) and **HTTP** (connect to a remote endpoint with optional token
authentication).

**Source:** `src/promptise/config.py`

## Quick example

```python
import asyncio
from promptise import build_agent, StdioServerSpec, HTTPServerSpec

async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            # Local server launched as a subprocess
            "math": StdioServerSpec(
                command="python",
                args=["-m", "my_math_server"],
            ),
            # Remote server with Bearer token auth
            "search": HTTPServerSpec(
                url="https://search.example.com/mcp",
                bearer_token="eyJhbGciOiJIUzI1NiIs...",
            ),
        },
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Search for recent AI papers"}]}
    )
    print(result["messages"][-1].content)

asyncio.run(main())
```

## Concepts

### StdioServerSpec

Use `StdioServerSpec` when the MCP server runs as a local subprocess.  Promptise
launches it via its `command` and communicates over stdin/stdout using the MCP
stdio transport.

```python
from promptise import StdioServerSpec

spec = StdioServerSpec(
    command="python",
    args=["-m", "my_server"],
    env={"LOG_LEVEL": "debug"},
    cwd="/opt/servers",
    keep_alive=True,
)
```

### HTTPServerSpec

Use `HTTPServerSpec` when the MCP server is reachable over the network.  Three
transport variants are supported: `"http"` (Streamable HTTP, the default),
`"streamable-http"`, and `"sse"` (Server-Sent Events).

```python
from promptise import HTTPServerSpec

# Unauthenticated
spec = HTTPServerSpec(url="http://localhost:8080/mcp")

# Bearer token (from your IdP)
spec = HTTPServerSpec(
    url="http://localhost:8080/mcp",
    bearer_token="eyJhbGciOiJIUzI1NiIs...",
)

# API key (simple pre-shared secret)
spec = HTTPServerSpec(
    url="http://localhost:8080/mcp",
    api_key="my-secret-key",
)

# Manual header
spec = HTTPServerSpec(
    url="http://localhost:8080/mcp",
    headers={"authorization": "Bearer <token>"},
)
```

### ServerSpec union

`ServerSpec` is a type alias for `StdioServerSpec | HTTPServerSpec`.  Use it
in type annotations when a function accepts either transport type.

```python
from promptise import ServerSpec

def print_spec(name: str, spec: ServerSpec) -> None:
    if isinstance(spec, StdioServerSpec):
        print(f"{name}: stdio -> {spec.command}")
    else:
        print(f"{name}: http -> {spec.url}")
```

### servers_to_mcp_config

This helper converts a mapping of server specs into the dictionary format
expected by the MCP client library.  You rarely call it directly --
`build_agent` handles this internally -- but it is useful if you are
building custom pipelines.

```python
from promptise import StdioServerSpec, HTTPServerSpec, servers_to_mcp_config

servers = {
    "math": StdioServerSpec(command="python", args=["-m", "math_server"]),
    "search": HTTPServerSpec(url="http://localhost:9090/mcp"),
}

config = servers_to_mcp_config(servers)
# Returns:
# {
#     "math": {
#         "transport": "stdio",
#         "command": "python",
#         "args": ["-m", "math_server"],
#         "env": {},
#         "cwd": None,
#         "keep_alive": True,
#     },
#     "search": {
#         "transport": "http",
#         "url": "http://localhost:9090/mcp",
#     },
# }
```

## API summary

### StdioServerSpec

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `command` | `str` | *required* | Executable to launch (e.g. `"python"`) |
| `args` | `list[str]` | `[]` | Positional arguments for the process |
| `env` | `dict[str, str]` | `{}` | Environment variables for the process |
| `cwd` | `str \| None` | `None` | Working directory for the process |
| `keep_alive` | `bool` | `True` | Keep a persistent session with the server |

### HTTPServerSpec

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | *required* | Full endpoint URL (e.g. `"http://localhost:8080/mcp"`) |
| `transport` | `Literal["http", "streamable-http", "sse"]` | `"http"` | HTTP transport variant |
| `headers` | `dict[str, str]` | `{}` | Extra HTTP headers sent on every request |
| `auth` | `str \| None` | `None` | Legacy auth hint (kept for backward compatibility) |
| `bearer_token` | `str \| None` | `None` | Pre-issued Bearer token -- injected as `Authorization: Bearer <token>` |
| `api_key` | `str \| None` | `None` | Pre-shared API key -- injected as `x-api-key: <key>` |

### servers_to_mcp_config

| Parameter | Type | Description |
|-----------|------|-------------|
| `servers` | `Mapping[str, ServerSpec]` | Mapping of server name to specification |
| **Returns** | `dict[str, dict[str, object]]` | Dict suitable for MCP client configuration |

## Tips and gotchas

!!! tip
    Use environment variables in your server URLs and tokens with the
    [Environment Resolver](env-resolver.md) to keep secrets out of source code:

    ```python
    HTTPServerSpec(
        url="${MCP_SERVER_URL}",
        bearer_token="${MCP_TOKEN}",
    )
    ```

!!! warning
    Both `StdioServerSpec` and `HTTPServerSpec` use `extra="forbid"` validation.
    Any unrecognised field name will raise a Pydantic `ValidationError` at
    construction time.  Double-check your field names if you see unexpected
    errors.

!!! tip
    For Bearer token auth, obtain tokens from your Identity Provider (Auth0,
    Keycloak, Okta) or from the MCP server's built-in token endpoint for
    development.  The agent **never** generates tokens itself.

## What's next

- [Types & ModelLike](types.md) -- the `ModelLike` union and other type aliases
- [Environment Resolver](env-resolver.md) -- resolve `${VAR}` placeholders in config
- [Building Agents](agents/building-agents.md) -- use server specs with `build_agent`
