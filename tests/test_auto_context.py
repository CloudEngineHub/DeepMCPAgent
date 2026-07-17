"""Automatic context handling: ``context_scope="auto"`` on PromptNode.

Auto stays "full" while a tool loop is short (no change to simple tasks) and
switches to the bounded "ledger" once enough tool results accumulate — so deep
tool loops stay token-efficient without choosing a different pattern. The
default ReAct pattern (and thus ``build_agent``) uses it.
"""

from __future__ import annotations

from promptise.engine import PromptGraph, PromptNode
from promptise.engine.state import GraphState


def _state_with_observations(n: int) -> GraphState:
    state = GraphState(messages=[])
    for i in range(n):
        state.add_observation(tool_name="lookup", result=f"r{i}", args={"i": i}, success=True)
    return state


def test_auto_stays_full_for_short_loops_then_switches_to_ledger():
    node = PromptNode("reason", context_scope="auto", auto_ledger_after=3)
    # Short loop → behaves like "full" (zero change to simple tasks).
    assert node._effective_context_scope(_state_with_observations(0)) == "full"
    assert node._effective_context_scope(_state_with_observations(2)) == "full"
    # At/over the threshold → bounded ledger kicks in automatically.
    assert node._effective_context_scope(_state_with_observations(3)) == "ledger"
    assert node._effective_context_scope(_state_with_observations(10)) == "ledger"


def test_explicit_scopes_are_not_affected_by_auto_logic():
    full = PromptNode("a", context_scope="full")
    ledger = PromptNode("b", context_scope="ledger")
    scoped = PromptNode("c", context_scope="scoped")
    deep = _state_with_observations(50)
    assert full._effective_context_scope(deep) == "full"
    assert ledger._effective_context_scope(_state_with_observations(0)) == "ledger"
    assert scoped._effective_context_scope(deep) == "scoped"


def test_threshold_is_configurable():
    eager = PromptNode("reason", context_scope="auto", auto_ledger_after=1)
    assert eager._effective_context_scope(_state_with_observations(1)) == "ledger"
    lazy = PromptNode("reason", context_scope="auto", auto_ledger_after=100)
    assert lazy._effective_context_scope(_state_with_observations(20)) == "full"


def test_react_default_uses_auto_context():
    # The default pattern (and therefore build_agent's default) is smart by
    # default: automatic context handling with no pattern to choose.
    graph = PromptGraph.react(tools=[], system_prompt="hi")
    assert graph.get_node("reason").context_scope == "auto"
