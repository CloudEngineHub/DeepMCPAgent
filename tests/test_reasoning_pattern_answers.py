"""Every built-in reasoning pattern ends with a user-facing answer (1.3.3).

Offline, against a scripted chat model:

- each ``agent_pattern`` name's run ends with an answer message — never
  routing JSON, a verdict or a reflection — in ``ainvoke`` and in
  ``astream_with_tools``, and offers the tools where the pattern says so
- ``debate``: the critic's severity and the judge's verdict route the
  proposer back; the proposer has tools; ``answer`` writes the reply
- ``deliberate``: reflect's ``continue`` / ``replan`` / ``answer`` route
- ``research``: verify is a model judgement that loops back to search
- ``autonomous``: a tool step makes several calls, a tool name runs the step
  that has the tool, the routing exchange never enters the conversation
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import PrivateAttr

from promptise.engine import PromptGraph, PromptGraphEngine, PromptNode
from promptise.engine.nodes import AutonomousNode
from promptise.engine.prebuilts import (
    DebateCritique,
    DebateVerdict,
    PeoatrPlan,
    PeoatrReflection,
    PeoatrThought,
    ResearchVerdict,
)

FINAL = "FINAL ANSWER: result for q0"
FINDINGS = "FINDINGS: result for q0"

Reply = Callable[[list[BaseMessage], list[str], Any], Any]


class ScriptedChat(BaseChatModel):
    """A chat model whose answers come from ``reply(messages, tool_names, schema)``.

    Records every call; ``with_structured_output`` returns the parsed reply
    in LangChain's ``include_raw`` envelope.
    """

    _reply: Any = PrivateAttr()
    _bound: list = PrivateAttr(default_factory=list)
    _calls: list = PrivateAttr(default_factory=list)

    def __init__(self, reply: Reply, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._reply = reply

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self._calls

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChat:  # type: ignore[override]
        clone = ScriptedChat(self._reply)
        clone._calls = self._calls
        clone._bound = [t.name for t in tools]
        return clone

    def with_structured_output(self, schema: Any, *, include_raw: bool = False, **kw: Any) -> Any:  # type: ignore[override]
        outer = self

        class _Structured:
            async def ainvoke(self, messages: list[BaseMessage], config: Any = None) -> Any:
                outer._calls.append({"messages": messages, "tools": [], "schema": schema})
                parsed = outer._reply(messages, [], schema)
                raw = AIMessage(content="")
                if include_raw:
                    return {"raw": raw, "parsed": parsed, "parsing_error": None}
                return parsed

        return _Structured()

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        self._calls.append({"messages": messages, "tools": list(self._bound), "schema": None})
        msg = self._reply(messages, list(self._bound), None)
        if not isinstance(msg, AIMessage):
            msg = AIMessage(content=str(msg))
        return ChatResult(generations=[ChatGeneration(message=msg)])


@tool
def lookup(query: str) -> str:
    """Look something up."""
    return f"result for {query}"


def _system_text(messages: list[BaseMessage]) -> str:
    return "\n".join(str(m.content) for m in messages if isinstance(m, SystemMessage))


def _tool_results(messages: list[BaseMessage]) -> int:
    return sum(isinstance(m, ToolMessage) for m in messages)


def _seq(values: list[Any]) -> Iterator[Any]:
    """Yield *values*, then repeat the last one."""
    yield from values
    while True:
        yield values[-1]


def scripted(
    *,
    tool_rounds: int = 1,
    severities: list[float] | None = None,
    judge: list[bool] | None = None,
    verify: list[bool] | None = None,
    reflect: list[str] | None = None,
    routes: list[str] | None = None,
) -> Reply:
    """One scripted model that plays every stage of every pattern.

    Tool stages call ``lookup`` *tool_rounds* times (one call per round),
    then summarize; answer stages write ``FINAL``; other text stages write
    notes.  Structured stages and the autonomous router follow the lists.
    """
    sev, jud, ver = _seq(severities or [0.1]), _seq(judge or [True]), _seq(verify or [True])
    ref, rts = _seq(reflect or ["answer"]), _seq(routes or ["__done__"])

    def reply(messages: list[BaseMessage], tools: list[str], schema: Any) -> Any:
        if schema is PeoatrPlan:
            return PeoatrPlan(subgoals=["look it up"], active_subgoal="look it up", quality_score=4)
        if schema is PeoatrThought:
            return PeoatrThought(analysis="found it", subgoal_complete=True, route="reflect")
        if schema is PeoatrReflection:
            return PeoatrReflection(
                progress="p", mistake="", correction="", confidence=4, route=next(ref)
            )
        if schema is DebateCritique:
            return DebateCritique(weaknesses=["w"], improvements=["i"], severity=next(sev))
        if schema is DebateVerdict:
            passes = next(jud)
            return DebateVerdict(passes=passes, issues=[] if passes else ["judge issue"])
        if schema is ResearchVerdict:
            passes = next(ver)
            return ResearchVerdict(passes=passes, issues=[] if passes else ["missing fact"])
        assert schema is None, f"unexpected schema {schema!r}"

        system = _system_text(messages)
        if "Available reasoning steps:" in system:
            return AIMessage(content=json.dumps({"next_node": next(rts), "reason": "r"}))
        done = _tool_results(messages)
        if tools and done < tool_rounds:
            return AIMessage(
                content="",
                tool_calls=[{"name": "lookup", "args": {"query": f"q{done}"}, "id": f"c{done}"}],
            )
        if "Write the final answer" in system or "Synthesize all gathered" in system:
            return AIMessage(content=FINAL)
        if tools:
            return AIMessage(content=FINDINGS)
        return AIMessage(content="NOTES: thinking")

    return reply


def _assert_answer(messages: list[BaseMessage], expected: str) -> None:
    """The run ended with an answer message, not routing JSON or a verdict."""
    last = messages[-1]
    assert isinstance(last, AIMessage), f"last message is {type(last).__name__}"
    assert not last.tool_calls
    assert last.content == expected
    try:
        parsed = json.loads(str(last.content))
    except ValueError:
        parsed = None
    assert not isinstance(parsed, dict), "the reply is a JSON object"


# ═══════════════════════════════════════════════════════════════════════════════
# Every pattern name, through build_agent
# ═══════════════════════════════════════════════════════════════════════════════

#: What each pattern's reply is: its final-answer stage's text, or for the
#: single-node patterns the tool node's own answer.
EXPECTED = {
    "react": FINDINGS,
    "managed": FINDINGS,
    "verify": FINDINGS,
    "peoatr": FINAL,
    "research": FINAL,
    "autonomous": FINAL,
    "deliberate": FINAL,
    "debate": FINAL,
}


def test_every_pattern_is_covered():
    from promptise.agent import AGENT_PATTERNS

    # code-action writes a program that runs in Docker; covered by its own tests.
    assert set(AGENT_PATTERNS) - {"code-action"} == set(EXPECTED)


async def _agent(name: str, model: ScriptedChat, monkeypatch) -> Any:
    from promptise import build_agent

    monkeypatch.setenv("PROMPTISE_NO_DOTENV", "1")
    return await build_agent(model=model, servers={}, extra_tools=[lookup], agent_pattern=name)


class TestEveryPatternEndsWithAnAnswer:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(EXPECTED))
    async def test_ainvoke_reply_is_an_answer_and_tools_are_offered(self, name, monkeypatch):
        # The autonomous router picks the tool step once, then finishes.
        model = ScriptedChat(scripted(routes=["reason", "__done__"]))
        agent = await _agent(name, model, monkeypatch)
        try:
            result = await agent.ainvoke({"messages": [HumanMessage(content="What is q0?")]})
        finally:
            await agent.shutdown()

        _assert_answer(result["messages"], EXPECTED[name])
        # Every built-in pattern has a stage that is offered the agent's tools...
        assert any("lookup" in c["tools"] for c in model.calls), f"{name}: no tools offered"
        # ...and the scripted model used them.
        assert any(isinstance(m, ToolMessage) for m in result["messages"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(EXPECTED))
    async def test_streamed_reply_is_the_answer(self, name, monkeypatch):
        from promptise.streaming import DoneEvent, ToolEndEvent

        model = ScriptedChat(scripted(routes=["reason", "__done__"]))
        agent = await _agent(name, model, monkeypatch)
        try:
            events = [
                e
                async for e in agent.astream_with_tools(
                    {"messages": [HumanMessage(content="What is q0?")]}
                )
            ]
        finally:
            await agent.shutdown()

        done = [e for e in events if isinstance(e, DoneEvent)]
        assert done and done[-1].full_response == EXPECTED[name]
        assert any(isinstance(e, ToolEndEvent) for e in events)


# ═══════════════════════════════════════════════════════════════════════════════
# debate
# ═══════════════════════════════════════════════════════════════════════════════


async def _run(graph: PromptGraph, model: ScriptedChat) -> tuple[list[BaseMessage], list[str]]:
    engine = PromptGraphEngine(graph=graph, model=model)
    result = await engine.ainvoke({"messages": [HumanMessage(content="What is q0?")]})
    assert engine.last_report is not None
    return result["messages"], engine.last_report.nodes_visited


class TestDebate:
    @pytest.mark.asyncio
    async def test_critic_and_judge_send_the_proposer_back(self):
        model = ScriptedChat(scripted(severities=[0.9, 0.1], judge=[False, True]))
        messages, visited = await _run(PromptGraph.debate(tools=[lookup]), model)

        assert visited == [
            "proposer",  # calls lookup
            "proposer",  # first answer
            "critic",  # severity 0.9 → revise
            "proposer",
            "critic",  # severity 0.1
            "judge",  # fails → proposer
            "proposer",
            "critic",
            "judge",  # passes
            "answer",
        ]
        _assert_answer(messages, FINAL)
        proposer_calls = [c for c in model.calls if c["tools"] == ["lookup"]]
        assert len(proposer_calls) == 4
        # The first proposal sees no feedback; the revisions see the critique
        # and the verdict.
        assert "critique:" not in _system_text(proposer_calls[1]["messages"])
        assert "critique:" in _system_text(proposer_calls[2]["messages"])
        assert "judge issue" in _system_text(proposer_calls[3]["messages"])

    @pytest.mark.asyncio
    async def test_a_critic_that_never_agrees_still_ends_with_an_answer(self):
        model = ScriptedChat(scripted(severities=[0.9], judge=[False]))
        messages, visited = await _run(PromptGraph.debate(tools=[lookup], max_rounds=2), model)

        assert visited[-1] == "answer"
        assert visited.count("critic") == 2
        assert visited.count("judge") <= 2
        _assert_answer(messages, FINAL)


# ═══════════════════════════════════════════════════════════════════════════════
# deliberate
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeliberate:
    @pytest.mark.asyncio
    async def test_reflect_continue_goes_back_to_act(self):
        model = ScriptedChat(scripted(reflect=["continue", "answer"]))
        messages, visited = await _run(PromptGraph.deliberate(tools=[lookup]), model)

        assert visited == [
            "think",
            "plan",
            "act",
            "act",
            "observe",
            "reflect",  # continue
            "act",
            "observe",
            "reflect",  # answer
            "answer",
        ]
        _assert_answer(messages, FINAL)

    @pytest.mark.asyncio
    async def test_reflect_replan_goes_back_to_plan(self):
        model = ScriptedChat(scripted(reflect=["replan", "answer"]))
        messages, visited = await _run(PromptGraph.deliberate(tools=[lookup]), model)

        assert visited.count("plan") == 2
        _assert_answer(messages, FINAL)

    @pytest.mark.asyncio
    async def test_a_reflect_that_never_says_answer_still_ends_with_an_answer(self):
        model = ScriptedChat(scripted(reflect=["continue"]))
        messages, visited = await _run(PromptGraph.deliberate(tools=[lookup]), model)

        assert visited[-1] == "answer"
        assert len(visited) < 30  # bounded by the stage budgets
        _assert_answer(messages, FINAL)


# ═══════════════════════════════════════════════════════════════════════════════
# research
# ═══════════════════════════════════════════════════════════════════════════════


class TestResearch:
    @pytest.mark.asyncio
    async def test_failed_verification_loops_back_to_search(self):
        base = scripted(verify=[False, True])
        fixing = scripted(verify=[False, True], tool_rounds=2)

        def reply(messages: list[BaseMessage], tools: list[str], schema: Any) -> Any:
            # After a failed verification, search looks up one more thing.
            if tools and "missing fact" in _system_text(messages):
                return fixing(messages, tools, schema)
            return base(messages, tools, schema)

        model = ScriptedChat(reply)
        messages, visited = await _run(PromptGraph.research(search_tools=[lookup]), model)

        assert visited == [
            "search",
            "search",  # lookup q0, then findings
            "verify",  # fails: "missing fact"
            "search",
            "search",  # lookup q1, then findings
            "verify",  # passes
            "synthesize",
        ]
        _assert_answer(messages, FINAL)
        # verify is a model judgement with a structured verdict...
        assert sum(c["schema"] is ResearchVerdict for c in model.calls) == 2
        # ...and the second search sees its issues.
        search_calls = [c for c in model.calls if c["tools"] == ["lookup"]]
        assert "missing fact" not in _system_text(search_calls[0]["messages"])
        assert "missing fact" in _system_text(search_calls[2]["messages"])

    @pytest.mark.asyncio
    async def test_verification_loops_are_bounded(self):
        model = ScriptedChat(scripted(verify=[False]))
        messages, visited = await _run(
            PromptGraph.research(search_tools=[lookup], max_verify_rounds=2), model
        )

        assert visited.count("verify") == 2
        assert visited[-1] == "synthesize"
        _assert_answer(messages, FINAL)

    def test_verify_is_not_an_empty_guard(self):
        from promptise.engine.reasoning_nodes import ValidateNode

        verify = PromptGraph.research().nodes["verify"]
        assert isinstance(verify, ValidateNode)
        assert verify.output_schema is ResearchVerdict


# ═══════════════════════════════════════════════════════════════════════════════
# autonomous
# ═══════════════════════════════════════════════════════════════════════════════


def _is_routing(message: BaseMessage) -> bool:
    if isinstance(message, SystemMessage):
        return "Available reasoning steps:" in str(message.content)
    try:
        parsed = json.loads(str(message.content))
    except ValueError:
        return False
    return isinstance(parsed, dict) and "next_node" in parsed


class TestAutonomous:
    @pytest.mark.asyncio
    async def test_tool_step_makes_several_calls_and_the_run_ends_with_an_answer(self):
        model = ScriptedChat(scripted(tool_rounds=3, routes=["reason", "__done__"]))
        engine = PromptGraphEngine(graph=PromptGraph.autonomous(tools=[lookup]), model=model)
        result = await engine.ainvoke({"messages": [HumanMessage(content="What is q0?")]})

        messages = result["messages"]
        assert engine.last_report is not None
        assert engine.last_report.tool_calls == 3  # one reason step, three rounds
        _assert_answer(messages, FINAL)
        assert not any(_is_routing(m) for m in messages)
        # The step's model saw every tool result before summarizing.
        summary = next(m for m in messages if m.content == FINDINGS)
        assert _tool_results(messages[: messages.index(summary)]) == 3

    @pytest.mark.asyncio
    async def test_a_tool_name_runs_the_step_that_has_the_tool(self):
        model = ScriptedChat(scripted(routes=["lookup", "__done__"]))
        engine = PromptGraphEngine(graph=PromptGraph.autonomous(tools=[lookup]), model=model)
        result = await engine.ainvoke({"messages": [HumanMessage(content="What is q0?")]})

        assert engine.last_report is not None
        assert engine.last_report.tool_calls == 1
        _assert_answer(result["messages"], FINAL)

    @pytest.mark.asyncio
    async def test_unknown_step_is_reported_to_the_router_not_the_conversation(self):
        model = ScriptedChat(scripted(routes=["nonsense", "synthesize"]))
        engine = PromptGraphEngine(graph=PromptGraph.autonomous(tools=[lookup]), model=model)
        result = await engine.ainvoke({"messages": [HumanMessage(content="What is q0?")]})

        routing = [
            c for c in model.calls if "Available reasoning steps:" in _system_text(c["messages"])
        ]
        assert len(routing) == 2
        assert "'nonsense' is not a step" in _system_text(routing[1]["messages"])
        # The routing prompts don't pile up: one per call.
        assert _system_text(routing[1]["messages"]).count("Available reasoning steps:") == 1
        assert not any(_is_routing(m) for m in result["messages"])
        _assert_answer(result["messages"], FINAL)

    @pytest.mark.asyncio
    async def test_choosing_the_terminal_step_ends_without_a_second_answer(self):
        model = ScriptedChat(scripted(routes=["synthesize"]))
        engine = PromptGraphEngine(graph=PromptGraph.autonomous(tools=[lookup]), model=model)
        result = await engine.ainvoke({"messages": [HumanMessage(content="What is q0?")]})

        assert sum(m.content == FINAL for m in result["messages"]) == 1
        _assert_answer(result["messages"], FINAL)

    @pytest.mark.asyncio
    async def test_custom_pool_without_a_terminal_node_still_ends_with_an_answer(self):
        graph = PromptGraph("pool", mode="static")
        graph.add_node(
            AutonomousNode(
                "agent",
                node_pool=[PromptNode("reason", tools=[lookup]), PromptNode("analyze")],
                default_next="__end__",
            )
        )
        graph.set_entry("agent")
        model = ScriptedChat(scripted(routes=["reason", "analyze", "__done__"]))
        result = await PromptGraphEngine(graph=graph, model=model).ainvoke(
            {"messages": [HumanMessage(content="What is q0?")]}
        )
        _assert_answer(result["messages"], FINAL)

    @pytest.mark.asyncio
    async def test_streaming_relays_the_steps_events(self):
        model = ScriptedChat(scripted(routes=["reason", "__done__"]))
        engine = PromptGraphEngine(graph=PromptGraph.autonomous(tools=[lookup]), model=model)
        events = [
            e
            async for e in engine.astream_events(
                {"messages": [HumanMessage(content="What is q0?")]}
            )
        ]
        kinds = [e["event"] for e in events]
        assert "on_tool_start" in kinds and "on_tool_end" in kinds
        chunks = [e for e in events if e["event"] == "on_chat_model_stream"]
        last_run = chunks[-1]["data"]["run_id"]
        text = "".join(
            str(e["data"]["chunk"].content) for e in chunks if e["data"]["run_id"] == last_run
        )
        assert text == FINAL
        # Routing replies are never streamed as text.
        assert not any("next_node" in str(e["data"]["chunk"].content) for e in chunks)


# ═══════════════════════════════════════════════════════════════════════════════
# A prebuilt graph passed to build_agent runs as built
# ═══════════════════════════════════════════════════════════════════════════════


class TestPrebuiltGraphInstances:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("build", "entry"),
        [
            (lambda: PromptGraph.peoatr(tools=[lookup]), "plan"),
            (lambda: PromptGraph.research(search_tools=[lookup]), "search"),
            (lambda: PromptGraph.deliberate(tools=[lookup]), "think"),
            (lambda: PromptGraph.debate(tools=[lookup]), "proposer"),
        ],
    )
    async def test_graph_instance_is_not_wrapped_in_a_node_pool(self, build, entry, monkeypatch):
        from promptise import build_agent

        monkeypatch.setenv("PROMPTISE_NO_DOTENV", "1")
        agent = await build_agent(model=ScriptedChat(scripted()), servers={}, agent_pattern=build())
        try:
            result = await agent.ainvoke({"messages": [HumanMessage(content="What is q0?")]})
            assert agent.last_report.nodes_visited[0] == entry
        finally:
            await agent.shutdown()
        _assert_answer(result["messages"], FINAL)
