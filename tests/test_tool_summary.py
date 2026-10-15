"""The "Available tools" block in the system prompt is opt-in (1.3.4).

The model receives the tool definitions through ``bind_tools``; the summary
repeats them, so it is off unless ``tool_summary`` asks for it.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from promptise import build_agent
from promptise.engine import PromptGraph, PromptGraphEngine, PromptNode


class Recorder(BaseChatModel):
    systems: list[str] = []
    bound: list[list[str]] = []

    @property
    def _llm_type(self) -> str:
        return "recorder"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Recorder:  # type: ignore[override]
        self.bound.append([t.name for t in tools])
        return self

    def _generate(self, messages: list[BaseMessage], stop: Any = None, **kw: Any) -> ChatResult:
        raise NotImplementedError("async only")

    async def _agenerate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> ChatResult:
        self.systems.append(
            "\n".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        )
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


@tool
def list_pages(cursor: str = "", limit: int = 10) -> str:
    """List pages starting at a cursor."""
    return "[]"


@tool
def get_page(page_id: str) -> str:
    """Get one page by id."""
    return "{}"


TOOLS = [list_pages, get_page]
QUESTION = {"messages": [HumanMessage(content="hi")]}


async def _agent_system(**kwargs: Any) -> tuple[str, Recorder]:
    model = Recorder(systems=[], bound=[])
    agent = await build_agent(servers={}, model=model, extra_tools=TOOLS, **kwargs)
    try:
        await agent.ainvoke(QUESTION)
    finally:
        await agent.shutdown()
    return model.systems[0], model


@pytest.mark.asyncio
async def test_build_agent_default_has_no_summary_but_binds_the_tools() -> None:
    system, model = await _agent_system()
    assert "Available tools" not in system
    assert "list_pages" not in system
    assert sorted(model.bound[0]) == ["get_page", "list_pages"]


@pytest.mark.asyncio
async def test_build_agent_names_summary() -> None:
    system, _ = await _agent_system(tool_summary="names")
    assert "Available tools: list_pages, get_page" in system
    assert "List pages starting" not in system


@pytest.mark.asyncio
async def test_build_agent_full_summary_quotes_string_defaults() -> None:
    system, _ = await _agent_system(tool_summary="full")
    assert "Available tools:\n" in system
    assert '  - list_pages(cursor: string="", limit: integer=10): List pages' in system
    assert "  - get_page(page_id: string*): Get one page by id." in system


@pytest.mark.asyncio
async def test_node_setting_overrides_the_engine() -> None:
    model = Recorder(systems=[], bound=[])
    graph = PromptGraph("g", mode="static")
    graph.add_node(PromptNode("a", tools=TOOLS, tool_summary="names", default_next="b"))
    graph.add_node(PromptNode("b", tools=TOOLS, default_next="__end__"))
    graph.set_entry("a")
    engine = PromptGraphEngine(graph=graph, model=model, tool_summary="full")
    await engine.ainvoke(QUESTION)
    assert "Available tools: list_pages, get_page" in model.systems[0]
    assert "  - get_page(page_id: string*)" in model.systems[1]


def test_invalid_mode_raises() -> None:
    with pytest.raises(ValueError, match="tool_summary"):
        PromptNode("a", tool_summary="yes")
    with pytest.raises(ValueError, match="tool_summary"):
        PromptGraphEngine(graph=PromptGraph("g"), model=Recorder(), tool_summary="all")


def test_tool_summary_survives_serialization() -> None:
    from promptise.engine.serialization import node_from_config, node_to_config

    config = node_to_config(PromptNode("a", tool_summary="names"))
    assert config["tool_summary"] == "names"
    assert node_from_config(config).tool_summary == "names"  # type: ignore[attr-defined]
    assert "tool_summary" not in node_to_config(PromptNode("b"))
