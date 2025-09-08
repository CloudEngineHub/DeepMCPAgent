"""Agent builders that use the FastMCP client and MCP-only tools."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.prebuilt import create_react_agent

from .clients import FastMCPMulti
from .config import ServerSpec
from .prompt import DEFAULT_SYSTEM_PROMPT
from .tools import MCPClientError, MCPToolLoader

# Model can be a provider string (handled by LangChain), a chat model instance, or a Runnable.
ModelLike = str | BaseChatModel | Runnable[Any, Any]


def _normalize_model(model: ModelLike) -> Runnable[Any, Any]:
    """Normalize the supplied model into a Runnable."""
    if isinstance(model, str):
        # This supports many providers via lc init strings, not just OpenAI.
        return cast(Runnable[Any, Any], init_chat_model(model))
    # Already BaseChatModel or Runnable
    return cast(Runnable[Any, Any], model)


async def build_deep_agent(
    *,
    servers: Mapping[str, ServerSpec],
    model: ModelLike,
    instructions: str | None = None,
    trace_tools: bool = False,
) -> tuple[Runnable[Any, Any], MCPToolLoader]:
    """Build an MCP-only agent graph.

    This function discovers tools from the configured MCP servers, converts them into
    LangChain tools, and then builds an agent. If the optional `deepagents` package is
    installed, a Deep Agent loop is created. Otherwise, a LangGraph ReAct agent is used.

    Args:
        servers: Mapping of server name to spec (HTTP/SSE recommended for FastMCP).
        model: REQUIRED. Either a LangChain chat model instance, a provider id string
            accepted by `init_chat_model`, or a Runnable.
        instructions: Optional system prompt. If not provided, uses DEFAULT_SYSTEM_PROMPT.
        trace_tools: If True, print each tool invocation and result from inside the tool
            wrapper (works for both DeepAgents and LangGraph prebuilt).

    Returns:
        Tuple of `(graph, loader)` where:
            - `graph` is a LangGraph or DeepAgents runnable with `.ainvoke`.
            - `loader` can be used to introspect tools.
    """
    if model is None:  # Defensive check; CLI/code must always pass a model now.
        raise ValueError("A model is required. Provide a model instance or a provider id string.")

    # Simple printing callbacks for tracing (kept dependency-free)
    def _before(name: str, kwargs: dict[str, Any]) -> None:
        if trace_tools:
            print(f"→ Invoking tool: {name} with {kwargs}")

    def _after(name: str, res: Any) -> None:
        if not trace_tools:
            return
        pretty = res
        for attr in ("data", "text", "content", "result"):
            if hasattr(res, attr):
                try:
                    pretty = getattr(res, attr)
                    break
                except Exception:
                    pass
        print(f"✔ Tool result from {name}: {pretty}")

    def _error(name: str, exc: Exception) -> None:
        if trace_tools:
            print(f"✖ {name} error: {exc}")

    multi = FastMCPMulti(servers)
    loader = MCPToolLoader(
        multi,
        on_before=_before if trace_tools else None,
        on_after=_after if trace_tools else None,
        on_error=_error if trace_tools else None,
    )

    try:
        discovered = await loader.get_all_tools()
        tools: list[BaseTool] = list(discovered) if discovered else []
    except MCPClientError as exc:
        raise RuntimeError(
            f"Failed to initialize agent because tool discovery failed. Details: {exc}"
        ) from exc

    if not tools:
        print("[deepmcpagent] No tools discovered from MCP servers; agent will run without tools.")

    chat: Runnable[Any, Any] = _normalize_model(model)
    sys_prompt = instructions or DEFAULT_SYSTEM_PROMPT

    try:
        # Optional deep agent loop if the extra is installed.
        from deepagents import create_deep_agent  # type: ignore

        graph = cast(
            Runnable[Any, Any],
            create_deep_agent(tools=tools, instructions=sys_prompt, model=chat),
        )
    except ImportError:
        # Solid fallback with LangGraph's ReAct agent.
        graph = cast(
            Runnable[Any, Any],
            create_react_agent(model=chat, tools=tools, state_modifier=sys_prompt),
        )

    return graph, loader
