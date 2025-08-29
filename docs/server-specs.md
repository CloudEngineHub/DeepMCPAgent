# Server Specs & Auth

DeepMCPAgent describes servers programmatically with typed specs.

## HTTPServerSpec (recommended)
```python
from deepmcpagent import HTTPServerSpec

srv = HTTPServerSpec(
    url="https://api.example.com/mcp",     # include the path, e.g., /mcp
    transport="http",                      # "http", "streamable-http", or "sse"
    headers={"Authorization": "Bearer X"}, # optional
    auth=None                              # optional hint for FastMCP deployments
)
```

!!! note
    FastMCP’s Python client is designed for remote servers (HTTP/SSE).  
    If you need a local stdio server, run it behind an HTTP shim or use a different adapter.

## Multiple servers
```python
servers = {
    "math": HTTPServerSpec(url="http://127.0.0.1:8000/mcp", transport="http"),
    "search": HTTPServerSpec(url="https://search.example.com/mcp", transport="sse"),
}
```

## Headers & authentication
Attach custom headers (e.g., `Authorization`, `X-Org`) using `headers={...}`.  
If your deployment supports special `auth` keys, set `auth="..."`.

## Streamable HTTP vs SSE
- `http`: regular HTTP requests
- `streamable-http`: same endpoint, but optimized for streaming payloads
- `sse`: Server-Sent Events (event-stream)
