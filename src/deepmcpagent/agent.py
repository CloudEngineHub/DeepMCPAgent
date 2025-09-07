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
from .tools import MCPToolLoader

# Model can be a provider string (handled by LangChain), a chat model instance, or a Runnable.
ModelLike = str | BaseChatModel | Runnable[Any, Any]


def _normalize_model(model: ModelLike) -> Runnable[Any, Any]:
    """Normalize the supplied model into a Runnable.

    Args:
        model: Either a model instance, a provider id string, or a Runnable.

    Returns:
        Runnable compatible with LangGraph agents.
    """
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

    Returns:
        Tuple of `(graph, loader)` where:
            - `graph` is a LangGraph or DeepAgents runnable with `.ainvoke`.
            - `loader` can be used to introspect tools.
    """
    if model is None:  # Defensive check; CLI/code must always pass a model now.
        raise ValueError("A model is required. Provide a model instance or a provider id string.")

    multi = FastMCPMulti(servers)
    loader = MCPToolLoader(multi)
    tools: list[BaseTool] = await loader.get_all_tools()
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
