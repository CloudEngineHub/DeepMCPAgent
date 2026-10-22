"""Every setting a standard skill picks can be overridden by keyword."""

from __future__ import annotations

import pytest

from promptise.engine import RouterNode
from promptise.engine.skills import (
    code_reviewer,
    data_analyst,
    decision_router,
    fact_checker,
    formatter,
    planner,
    summarizer,
    web_researcher,
)


@pytest.mark.parametrize(
    "factory",
    [web_researcher, code_reviewer, data_analyst, summarizer, planner],
)
def test_prompt_skills_take_instructions(factory):
    node = factory(instructions="Use the house style.")
    assert node.instructions == "Use the house style."


def test_summarizer_default_next_and_tools_are_overridable():
    node = summarizer(default_next="review", tools=[])
    assert node.default_next == "review"


def test_web_researcher_tool_choice_is_overridable():
    node = web_researcher(tool_choice="required")
    assert node.tool_choice == "required"


def test_formatter_default_next_is_overridable():
    node = formatter(default_next="publish", instructions="Format as a table.")
    assert node.default_next == "publish"
    assert node.instructions == "Format as a table."


def test_router_and_guard_skills_take_instructions():
    router = decision_router(routes={"a": "__end__"}, instructions="Pick fast.")
    guard = fact_checker(instructions="Check numbers only.")
    assert isinstance(router, RouterNode)
    assert router.instructions == "Pick fast."
    assert guard.instructions == "Check numbers only."


def test_defaults_are_kept_when_not_overridden():
    node = summarizer()
    assert node.default_next == "__end__"
    assert "Summarize all gathered information" in node.instructions
