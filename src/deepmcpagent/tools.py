"""MCP tool discovery and conversion to LangChain tools."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr, create_model

from .clients import FastMCPMulti

# Callback types for tracing tool calls
OnBefore = Callable[[str, dict[str, Any]], None]
OnAfter = Callable[[str, Any], None]
OnError = Callable[[str, Exception], None]


@dataclass(frozen=True)
class ToolInfo:
    """Human-friendly metadata for a discovered MCP tool."""

    server_guess: str
    name: str
    description: str
    input_schema: dict[str, Any]


class MCPClientError(RuntimeError):
    """Raised when communicating with the MCP client fails."""


def _jsonschema_to_pydantic(schema: dict[str, Any], *, model_name: str = "Args") -> type[BaseModel]:
    props = (schema or {}).get("properties", {}) or {}
    required = set((schema or {}).get("required", []) or [])

    # Each value is (annotation, default)
    def f(n: str, p: dict[str, Any]) -> tuple[type[Any], Any]:
        t = p.get("type")
        desc = p.get("description")
        default = p.get("default")
        req = n in required

        def default_val() -> Any:
            return ... if req else default

        if t == "string":
            return (str, Field(default_val(), description=desc))
        if t == "integer":
            return (int, Field(default_val(), description=desc))
        if t == "number":
            return (float, Field(default_val(), description=desc))
        if t == "boolean":
            return (bool, Field(default_val(), description=desc))
        if t == "array":
            return (list, Field(default_val(), description=desc))
        if t == "object":
            return (dict, Field(default_val(), description=desc))
        return (Any, Field(default_val(), description=desc))

    fields: dict[str, tuple[type[Any], Any]] = {
        n: f(n, spec or {}) for n, spec in props.items()
    } or {"payload": (dict, Field(None, description="Raw payload"))}

    safe_name = re.sub(r"[^0-9a-zA-Z_]", "_", model_name) or "Args"

    # Hand the kwargs to pydantic as Any to satisfy the stubbed overloads
    model = create_model(safe_name, **cast(dict[str, Any], fields))
    return cast(type[BaseModel], model)


class _FastMCPTool(BaseTool):
    """LangChain `BaseTool` wrapper that invokes a FastMCP tool by name."""

    name: str
    description: str
    args_schema: type[BaseModel]

    _tool_name: str = PrivateAttr()
    _client: Any = PrivateAttr()
    _on_before: OnBefore | None = PrivateAttr(default=None)
    _on_after: OnAfter | None = PrivateAttr(default=None)
    _on_error: OnError | None = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        name: str,
        description: str,
        args_schema: type[BaseModel],
        tool_name: str,
        client: Any,
        on_before: OnBefore | None = None,
        on_after: OnAfter | None = None,
        on_error: OnError | None = None,
    ) -> None:
        super().__init__(name=name, description=description, args_schema=args_schema)
        self._tool_name = tool_name
        self._client = client
        self._on_before = on_before
        self._on_after = on_after
        self._on_error = on_error

    async def _arun(self, **kwargs: Any) -> Any:
        """Asynchronously execute the MCP tool via the FastMCP client."""
        if self._on_before:
            with contextlib.suppress(Exception):
                self._on_before(self.name, kwargs)

        try:
            async with self._client:
                res = await self._client.call_tool(self._tool_name, kwargs)
        except Exception as exc:  # surface transport/protocol issues
            if self._on_error:
                with contextlib.suppress(Exception):
                    self._on_error(self.name, exc)
            raise MCPClientError(f"Failed to call MCP tool '{self._tool_name}': {exc}") from exc

        if self._on_after:
            with contextlib.suppress(Exception):
                self._on_after(self.name, res)

        return res

    def _run(self, **kwargs: Any) -> Any:  # pragma: no cover
        """Synchronous execution path (rarely used)."""
        import anyio

        return anyio.run(lambda: self._arun(**kwargs))


class MCPToolLoader:
    """Discover MCP tools via FastMCP and convert them to LangChain tools."""

    def __init__(
        self,
        multi: FastMCPMulti,
        *,
        on_before: OnBefore | None = None,
        on_after: OnAfter | None = None,
        on_error: OnError | None = None,
    ) -> None:
        self._multi = multi
        self._on_before = on_before
        self._on_after = on_after
        self._on_error = on_error

    async def _list_tools_raw(self) -> tuple[Any, list[Any]]:
        """Fetch raw tool descriptors from all configured MCP servers."""
        c = self._multi.client
        try:
            async with c:
                tools = await c.list_tools()
        except Exception as exc:
            raise MCPClientError(
                f"Failed to list tools from MCP servers: {exc}. "
                "Check server URLs, network connectivity, and authentication headers."
            ) from exc
        return c, list(tools or [])

    async def get_all_tools(self) -> list[BaseTool]:
        """Return all available tools as LangChain `BaseTool` instances."""
        client, tools = await self._list_tools_raw()

        out: list[BaseTool] = []
        for t in tools:
            name = t.name
            desc = getattr(t, "description", "") or ""
            schema = getattr(t, "inputSchema", None) or {}
            model = _jsonschema_to_pydantic(schema, model_name=f"Args_{name}")
            out.append(
                _FastMCPTool(
                    name=name,
                    description=desc,
                    args_schema=model,
                    tool_name=name,
                    client=client,
                    on_before=self._on_before,
                    on_after=self._on_after,
                    on_error=self._on_error,
                )
            )
        return out

    async def list_tool_info(self) -> list[ToolInfo]:
        """Return human-readable tool metadata for introspection or debugging."""
        _, tools = await self._list_tools_raw()
        return [
            ToolInfo(
                server_guess=(getattr(t, "server", None) or getattr(t, "serverName", None) or ""),
                name=t.name,
                description=getattr(t, "description", "") or "",
                input_schema=getattr(t, "inputSchema", None) or {},
            )
            for t in tools
        ]
