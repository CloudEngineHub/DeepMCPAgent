# Examples

## Sample MCP server (HTTP)
`examples/servers/math_server.py`:
```python
from fastmcp import FastMCP

mcp = FastMCP("Math")

@mcp.tool()
def add(a: int, b: int) -> int:
    return a + b

@mcp.tool()
def multiply(a: int, b: int) -> int:
    return a * b

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8000, path="/mcp")
```
Run:
```bash
python examples/servers/math_server.py
```

## Fancy console trace
`examples/use_agent.py` prints:
- Discovered tools (table)
- Each tool call (name + args)
- Each tool result
- Final LLM answer (panel)

Run:
```bash
python examples/use_agent.py
```
