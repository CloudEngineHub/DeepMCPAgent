"""MCP tool discovery and conversion to LangChain tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Type

from pydantic import BaseModel, PrivateAttr, create_model
from langchain_core.tools import BaseTool

from .clients import FastMCPMulti


@dataclass(frozen=True)
class ToolInfo:
    """Human-friendly metadata for a discovered MCP tool."""
    server_guess: str
    name: str
    description: str
    input_schema: Dict[str, Any]


def _jsonschema_to_pydantic(schema: Dict[str, Any]) -> Type[BaseModel]:
    """Convert a basic JSON Schema dict to a Pydantic model for tool args."""
    props = (schema or {}).get("properties", {}) or {}
    required = set((schema or {}).get("required", []) or [])

    def f(n: str, p: Dict[str, Any]) -> Tuple[Any, Any]:
        t = p.get("type")
        req = n in required
        if t == "string":
            return (str, ...) if req else (str, None)
        if t == "integer":
            return (int, ...) if req else (int, None)
        if t == "number":
            return (float, ...) if req else (float, None)
        if t == "boolean":
            return (bool, ...) if req else (bool, None)
        if t == "array":
            return (list, ...) if req else (list, None)
        if t == "object":
            return (dict, ...) if req else (dict, None)
        return (Any, ...) if req else (Any, None)

    fields = {n: f(n, spec or {}) for n, spec in props.items()} or {"payload": (dict, None)}
    return create_model("Args", **fields)  # type: ignore[arg-type]


class _FastMCPTool(BaseTool):
    """LangChain `BaseTool` wrapper that invokes a FastMCP tool by name."""

    name: str
    description: str
    args_schema: Type[BaseModel]

    _tool_name: str = PrivateAttr()
    _client: Any = PrivateAttr()

    def __init__(
        self,
        *,
        name: str,
        description: str,
        args_schema: Type[BaseModel],
        tool_name: str,
        client: Any,
    ) -> None:
        super().__init__(name=name, description=description, args_schema=args_schema)
        self._tool_name = tool_name
        self._client = client

    async def _arun(self, **kwargs: Any) -> Any:  # type: ignore[override]
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

    async def get_all_tools(self) -> List[BaseTool]:
        """Return all available tools as LangChain `BaseTool` instances."""
        c = self._multi.client
        async with c:
            tools = await c.list_tools()
            out: List[BaseTool] = []
            for t in tools:
                name = t.name
                desc = getattr(t, "description", "") or ""
                schema = getattr(t, "inputSchema", None) or {}
                model = _jsonschema_to_pydantic(schema)
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

    async def list_tool_info(self) -> List[ToolInfo]:
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
