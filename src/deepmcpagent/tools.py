"""MCP tool discovery and conversion to LangChain tools."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr, create_model

from .clients import FastMCPMulti


@dataclass(frozen=True)
class ToolInfo:
    """Human-friendly metadata for a discovered MCP tool."""

    server_guess: str
    name: str
    description: str
    input_schema: dict[str, Any]


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

    def __init__(
        self,
        *,
        name: str,
        description: str,
        args_schema: type[BaseModel],
        tool_name: str,
        client: Any,
    ) -> None:
        super().__init__(name=name, description=description, args_schema=args_schema)
        self._tool_name = tool_name
        self._client = client

    async def _arun(self, **kwargs: Any) -> Any:
        """Asynchronously execute the MCP tool via the FastMCP client."""
        # Open a session context for each call to be safe across runners.
        async with self._client:
            res = await self._client.call_tool(self._tool_name, kwargs)
        for attr in ("data", "text", "content", "result"):
            if hasattr(res, attr):
                return getattr(res, attr)
        return res

    def _run(self, **kwargs: Any) -> Any:  # pragma: no cover
        """Synchronous execution path (rarely used)."""
        import anyio

        return anyio.run(self._arun, **kwargs)


class MCPToolLoader:
    """Discover MCP tools via FastMCP and convert them to LangChain tools."""

    def __init__(self, multi: FastMCPMulti) -> None:
        self._multi = multi

    async def get_all_tools(self) -> list[BaseTool]:
        """Return all available tools as LangChain `BaseTool` instances."""
        c = self._multi.client
        async with c:
            tools = await c.list_tools()
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
                        client=c,
                    )
                )
            return out

    async def list_tool_info(self) -> list[ToolInfo]:
        """Return human-readable tool metadata for introspection or debugging."""
        c = self._multi.client
        async with c:
            tools = await c.list_tools()
            return [
                ToolInfo(
                    server_guess="",
                    name=t.name,
                    description=getattr(t, "description", "") or "",
                    input_schema=getattr(t, "inputSchema", None) or {},
                )
                for t in tools
            ]
