"""Promptise on both MCP SDK majors (1.5.1).

1.5.0 required ``mcp<2``: with the 2.x SDK installed ``import promptise``
failed (``McpError`` became ``MCPError``) and the server's handler
registration, the client's HTTP plumbing and the protocol-field names had
all changed.  These tests run on whichever SDK is installed; CI runs them
with 2.x (the main matrix) and 1.x (the ``test-mcp-v1`` job).
"""

from __future__ import annotations

import base64
import importlib.metadata
from pathlib import Path

import pytest
from _mcp_memory import connected_session
from mcp.types import Tool

from promptise.mcp._compat import (
    MCP_V2,
    McpError,
    field,
    mcp_error,
    mcp_error_code,
    mcp_error_message,
)
from promptise.mcp.server import MCPServer, ToolError

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


def test_the_requirement_allows_both_sdk_majors() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip("not running from a source checkout")
    deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    [mcp] = [d for d in deps if d.replace(" ", "").startswith("mcp>")]
    assert mcp.replace(" ", "") == "mcp>=1.9.0,<3"


def test_detects_the_installed_sdk() -> None:
    major = int(importlib.metadata.version("mcp").split(".")[0])
    assert MCP_V2 is (major == 2)


def test_mcp_error_round_trip() -> None:
    exc = mcp_error(-32002, "Resource not found", {"code": "RESOURCE_NOT_FOUND"})
    assert isinstance(exc, McpError)
    assert mcp_error_code(exc) == -32002
    assert mcp_error_message(exc) == "Resource not found"
    assert mcp_error_code(ValueError("x")) is None


def test_field_reads_either_spelling() -> None:
    tool = Tool(name="t", inputSchema={"type": "object", "properties": {}})
    assert field(tool, "input_schema") == {"type": "object", "properties": {}}
    assert field(object(), "input_schema", "default") == "default"


def _server() -> MCPServer:
    server = MCPServer("compat")

    @server.tool()
    async def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @server.tool()
    async def refuse() -> str:
        """Always fails."""
        raise ToolError("no stock", code="OUT_OF_STOCK")

    @server.resource("blob://logo", mime_type="image/png")
    async def logo() -> bytes:
        return b"\x89PNG"

    @server.resource("text://hello")
    async def hello() -> str:
        return "hello"

    @server.prompt()
    async def greet(name: str) -> str:
        """Greet someone."""
        return f"Hello {name}"

    return server


async def test_server_handlers_work_on_the_installed_sdk() -> None:
    async with connected_session(_server()._build_lowlevel_server()) as session:
        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} >= {"add", "refuse"}
        add = next(t for t in tools if t.name == "add")
        assert set(field(add, "input_schema")["properties"]) == {"a", "b"}

        ok = await session.call_tool("add", {"a": 2, "b": 3})
        assert field(ok, "is_error") is False and ok.content[0].text == "5"
        failed = await session.call_tool("refuse", {})
        assert field(failed, "is_error") is True
        assert "OUT_OF_STOCK" in failed.content[0].text

        blob = (await session.read_resource("blob://logo")).contents[0]
        assert field(blob, "mime_type") == "image/png"
        assert base64.b64decode(blob.blob) == b"\x89PNG"
        text = (await session.read_resource("text://hello")).contents[0]
        assert text.text == "hello"
        with pytest.raises(McpError) as info:
            await session.read_resource("text://nope")
        assert mcp_error_code(info.value) == -32002

        prompt = await session.get_prompt("greet", {"name": "Ada"})
        assert prompt.messages[0].content.text == "Hello Ada"
