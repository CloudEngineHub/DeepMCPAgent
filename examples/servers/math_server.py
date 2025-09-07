from __future__ import annotations

from fastmcp import FastMCP

mcp = FastMCP("Math")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def multiply(a: int, b: int) -> int:
    """Multiply two integers."""
    return a * b


if __name__ == "__main__":
    # Serve over HTTP at /mcp
    mcp.run(transport="http", host="127.0.0.1", port=8000, path="/mcp")
