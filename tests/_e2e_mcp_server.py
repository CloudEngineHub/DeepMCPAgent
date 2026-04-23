"""Tiny MCP server for E2E runtime tests. Run via stdio."""

import json

from promptise.mcp.server import MCPServer

server = MCPServer("e2e-tools")


@server.tool()
async def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return json.dumps({"city": city, "temp": 22, "condition": "sunny", "humidity": 65})


@server.tool()
async def get_time(timezone: str = "UTC") -> str:
    """Get the current time in a timezone."""
    return json.dumps({"time": "14:30", "timezone": timezone, "date": "2026-04-06"})


@server.tool()
async def search_news(query: str) -> str:
    """Search recent news articles."""
    return json.dumps(
        {
            "results": [
                {"title": f"Breaking: {query} trends rising", "source": "Reuters"},
                {"title": f"{query}: what experts say", "source": "BBC"},
            ]
        }
    )


@server.tool()
async def calculate(expression: str) -> str:
    """Evaluate a math expression."""
    try:
        result = eval(expression, {"__builtins__": {}})
        return json.dumps({"expression": expression, "result": result})
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    server.run(transport="stdio")
