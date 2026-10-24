"""``ToolNode(deduplicate=True)`` deduplicates within one model turn of one run.

Regression (v1.3.1): the node kept its record of past calls on the node
object, which every run of the graph shares.  A call made in one run was a
"duplicate" in every later run on the same agent (and in a concurrent run),
and so was a status check the model repeated in a later turn of the same run.
No ``ToolMessage`` was written for such a call, so the model got no result
for its ``tool_call_id`` (which providers that need one result per call
reject).

Now the record belongs to the run: identical calls in one model turn run the
tool once, and each repeat gets its own ``ToolMessage`` with that result,
reported as reused.  A call in a later turn, or in another run, runs the tool.

The agents run on a scripted chat model (no LLM API).
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import PrivateAttr

from promptise import build_agent
from promptise.engine import (
    GraphState,
    NodeResult,
    PromptGraph,
    PromptGraphEngine,
    ToolNode,
    node,
)
from promptise.engine.nodes import REUSED_TOOL_METADATA_KEY
from promptise.observability import ObservabilityCollector

Call = tuple[str, dict[str, Any]]
POLL: Call = ("job_status", {"job_id": "j1"})


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

_ids = itertools.count()


class _Conversations(BaseChatModel):
    """Plays each conversation's own script, so concurrent runs on one model
    stay deterministic.

    The *n*-th model turn of the conversation that asks *question* makes the
    calls ``scripts[question][n]`` (each with a fresh ``tool_call_id``); past
    the end of the script it answers.
    """

    _scripts: dict[str, list[list[Call]]] = PrivateAttr()

    def __init__(self, scripts: dict[str, list[list[Call]]]) -> None:
        super().__init__()
        self._scripts = scripts

    @property
    def _llm_type(self) -> str:
        return "scripted-conversations"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _Conversations:  # type: ignore[override]
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any) -> Any:
        raise NotImplementedError

    async def _agenerate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> ChatResult:
        question = next(str(m.content) for m in messages if isinstance(m, HumanMessage))
        turn = sum(isinstance(m, AIMessage) for m in messages)
        script = self._scripts[question]
        calls = script[turn] if turn < len(script) else []
        if calls:
            message = AIMessage(
                content="",
                tool_calls=[{"name": n, "args": a, "id": f"call-{next(_ids)}"} for n, a in calls],
            )
        else:
            message = AIMessage(content=f"Answer to: {question}")
        return ChatResult(generations=[ChatGeneration(message=message)])


def _agent_graph(tool_node: ToolNode) -> PromptGraph:
    """The classic loop: the model's turn, then a ToolNode runs its calls."""

    @node("agent")
    async def agent(state: GraphState, config: dict[str, Any]) -> NodeResult:
        reply = await config["_engine_model"].ainvoke(state.messages)
        state.messages.append(reply)
        return NodeResult(
            node_name="agent",
            messages_added=[reply],
            next_node="tools" if reply.tool_calls else "__end__",
        )

    graph = PromptGraph("agent-tools", mode="static")
    graph.add_node(agent)
    graph.add_node(tool_node)
    graph.set_entry("agent")
    return graph


def _tools_node(*tools: Any, **kwargs: Any) -> ToolNode:
    return ToolNode("tools", tools=list(tools), default_next="agent", **kwargs)


def _job_tool(completes_on: int = 3) -> tuple[Any, list[str]]:
    """A status check: the job reports completed from its *completes_on*-th
    check on (counted across every run that uses the tool)."""
    calls: list[str] = []

    @tool
    async def job_status(job_id: str) -> str:
        """Return the current status of a job."""
        calls.append(job_id)
        n = len(calls)
        return f"check {n}: " + ("completed" if n >= completes_on else "running")

    return job_status, calls


def _assert_every_call_answered_once(messages: Sequence[BaseMessage]) -> None:
    """Every ``tool_call_id`` the model used has exactly one ``ToolMessage``."""
    asked = [tc["id"] for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls]
    answered = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
    assert asked, "the model made no tool calls"
    assert sorted(answered) == sorted(asked)


def _results(messages: Sequence[BaseMessage]) -> list[str]:
    return [str(m.content) for m in messages if isinstance(m, ToolMessage)]


class _Recorder:
    """Engine hook recording what nodes report."""

    def __init__(self) -> None:
        self.reused: list[tuple[str, dict[str, Any], str]] = []
        self.tool_results: list[NodeResult] = []

    async def on_tool_reused(self, tool_name, args, result, state) -> None:  # type: ignore[no-untyped-def]
        self.reused.append((tool_name, args, result))

    async def post_node(self, node_obj, result, state):  # type: ignore[no-untyped-def]
        if result.node_type == "tool":
            self.tool_results.append(result)
        return result


class _ToolCallbacks(AsyncCallbackHandler):
    """Records the LangChain tool callbacks of a run."""

    def __init__(self) -> None:
        self.starts: list[tuple[str, dict[str, Any]]] = []
        self.ends = 0

    async def on_tool_start(self, serialized: Any, input_str: str, **kw: Any) -> None:
        self.starts.append((serialized["name"], kw.get("metadata") or {}))

    async def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        self.ends += 1


async def _engine_run(engine: PromptGraphEngine, question: str) -> list[BaseMessage]:
    result = await engine.ainvoke({"messages": [{"role": "user", "content": question}]})
    return list(result["messages"])


# ---------------------------------------------------------------------------
# 1. Runs on the same agent do not share the record
# ---------------------------------------------------------------------------


async def test_sequential_runs_on_the_same_agent_each_run_the_tool() -> None:
    job_status, calls = _job_tool(completes_on=2)
    model = _Conversations({"Is j1 done?": [[POLL]]})
    agent = await build_agent(
        model=model, servers={}, agent_pattern=_agent_graph(_tools_node(job_status))
    )
    try:
        first = await agent.ainvoke({"messages": [{"role": "user", "content": "Is j1 done?"}]})
        second = await agent.ainvoke({"messages": [{"role": "user", "content": "Is j1 done?"}]})
    finally:
        await agent.shutdown()

    # The second run's check reached the tool and saw the job complete.
    assert calls == ["j1", "j1"]
    assert _results(first["messages"]) == ["check 1: running"]
    assert _results(second["messages"]) == ["check 2: completed"]
    _assert_every_call_answered_once(first["messages"])
    _assert_every_call_answered_once(second["messages"])


async def test_concurrent_runs_on_the_same_agent_each_run_the_tool() -> None:
    calls: list[str] = []
    both_running = asyncio.Event()

    @tool
    async def job_status(job_id: str) -> str:
        """Return the current status of a job."""
        calls.append(job_id)
        if len(calls) == 2:
            both_running.set()
        # Each call waits for the other run's call: both are in flight at once.
        await asyncio.wait_for(both_running.wait(), timeout=5)
        return f"{job_id}: completed"

    model = _Conversations({"Is j1 done?": [[POLL]], "Check j1 for me": [[POLL]]})
    agent = await build_agent(
        model=model, servers={}, agent_pattern=_agent_graph(_tools_node(job_status))
    )
    try:
        a, b = await asyncio.gather(
            agent.ainvoke({"messages": [{"role": "user", "content": "Is j1 done?"}]}),
            agent.ainvoke({"messages": [{"role": "user", "content": "Check j1 for me"}]}),
        )
    finally:
        await agent.shutdown()

    assert calls == ["j1", "j1"]
    for run in (a, b):
        assert _results(run["messages"]) == ["j1: completed"]
        _assert_every_call_answered_once(run["messages"])


async def test_concurrent_engine_runs_record_nothing_on_the_node() -> None:
    job_status, calls = _job_tool(completes_on=1)
    tools = _tools_node(job_status)
    questions = [f"Is j1 done? ({i})" for i in range(4)]
    engine = PromptGraphEngine(
        graph=_agent_graph(tools),
        model=_Conversations({q: [[POLL]] for q in questions}),
    )

    node_before = {k: repr(v) for k, v in vars(tools).items()}

    runs = await asyncio.gather(*(_engine_run(engine, q) for q in questions))

    assert len(calls) == 4
    for messages in runs:
        _assert_every_call_answered_once(messages)
    # Nothing about any run is kept on the node, which every run shares.
    assert {k: repr(v) for k, v in vars(tools).items()} == node_before


# ---------------------------------------------------------------------------
# 2. Within a run: one model turn, not the whole run
# ---------------------------------------------------------------------------


async def test_a_status_check_repeated_in_a_later_turn_runs_the_tool() -> None:
    job_status, calls = _job_tool(completes_on=3)
    engine = PromptGraphEngine(
        graph=_agent_graph(_tools_node(job_status)),
        model=_Conversations({"Wait for j1": [[POLL], [POLL], [POLL]]}),
    )

    messages = await _engine_run(engine, "Wait for j1")

    assert calls == ["j1"] * 3
    assert _results(messages) == ["check 1: running", "check 2: running", "check 3: completed"]
    _assert_every_call_answered_once(messages)
    assert messages[-1].content == "Answer to: Wait for j1"


async def test_a_selector_driven_poll_runs_the_tool_on_every_pass() -> None:
    job_status, calls = _job_tool(completes_on=3)

    @node("check")
    async def check(state: GraphState) -> NodeResult:
        done = "completed" in str(state.messages[-1].content)
        return NodeResult(node_name="check", next_node="__end__" if done else "poll")

    graph = PromptGraph("poll-loop", mode="static")
    graph.add_node(
        ToolNode(
            "poll",
            tools=[job_status],
            tool_selector=lambda state: POLL,
            default_next="check",
        )
    )
    graph.add_node(check)
    graph.set_entry("poll")
    engine = PromptGraphEngine(graph=graph, model=_Conversations({}))

    messages = await _engine_run(engine, "Wait for j1")

    assert calls == ["j1"] * 3
    assert _results(messages)[-1] == "check 3: completed"


async def test_identical_calls_in_one_turn_run_once_and_each_gets_a_result() -> None:
    lookups: list[str] = []

    @tool
    async def lookup_user(user_id: str) -> str:
        """Look up a user by id."""
        lookups.append(user_id)
        return f"user {user_id}: Dana"

    lookup = ("lookup_user", {"user_id": "u1"})
    other = ("lookup_user", {"user_id": "u2"})
    recorder = _Recorder()
    engine = PromptGraphEngine(
        graph=_agent_graph(_tools_node(lookup_user)),
        model=_Conversations({"Who are u1 and u2?": [[lookup, other, lookup]]}),
        hooks=[recorder],
    )

    messages = await _engine_run(engine, "Who are u1 and u2?")

    assert lookups == ["u1", "u2"]
    _assert_every_call_answered_once(messages)
    assert _results(messages) == ["user u1: Dana", "user u2: Dana", "user u1: Dana"]
    # The repeat is reported as reused, like a ContextCompaction(reuse_results) call.
    assert recorder.reused == [("lookup_user", {"user_id": "u1"}, "user u1: Dana")]
    [result] = recorder.tool_results
    assert result.tool_calls_deduplicated == 1
    assert [tc.get("reused", False) for tc in result.tool_calls] == [False, False, True]
    assert result.tool_calls[2]["result"] == "user u1: Dana"
    assert result.tool_calls[2]["success"] is True


async def test_a_failed_call_is_not_reused_in_the_same_turn() -> None:
    attempts: list[str] = []

    @tool
    async def flaky(key: str) -> str:
        """Fails on its first call."""
        attempts.append(key)
        if len(attempts) == 1:
            raise RuntimeError("busy, retry")
        return f"{key}: ok"

    call = ("flaky", {"key": "k"})
    engine = PromptGraphEngine(
        graph=_agent_graph(_tools_node(flaky)),
        model=_Conversations({"Try k": [[call, call]]}),
    )

    messages = await _engine_run(engine, "Try k")

    assert attempts == ["k", "k"]  # an error is not a result to reuse
    assert _results(messages) == ["Error: RuntimeError: busy, retry", "k: ok"]
    _assert_every_call_answered_once(messages)


async def test_deduplicate_false_runs_every_call() -> None:
    job_status, calls = _job_tool(completes_on=2)
    engine = PromptGraphEngine(
        graph=_agent_graph(_tools_node(job_status, deduplicate=False)),
        model=_Conversations({"Check j1 twice": [[POLL, POLL]]}),
    )

    messages = await _engine_run(engine, "Check j1 twice")

    assert calls == ["j1", "j1"]
    assert _results(messages) == ["check 1: running", "check 2: completed"]
    _assert_every_call_answered_once(messages)


async def test_running_the_node_again_on_an_answered_turn_runs_and_answers_nothing() -> None:
    """A graph that reaches the ToolNode twice for one model turn: the calls
    already have their results, so they neither run again nor get a second
    ``ToolMessage``."""
    job_status, calls = _job_tool(completes_on=1)
    passes: list[int] = []

    @node("audit")
    async def audit(state: GraphState) -> NodeResult:
        passes.append(1)
        return NodeResult(node_name="audit", next_node="tools" if len(passes) == 1 else "agent")

    graph = _agent_graph(ToolNode("tools", tools=[job_status], default_next="audit"))
    graph.add_node(audit)
    engine = PromptGraphEngine(
        graph=graph,
        model=_Conversations({"Is j1 done?": [[POLL, ("job_status", {"job_id": "j2"})]]}),
    )

    messages = await _engine_run(engine, "Is j1 done?")

    assert passes == [1, 1]
    assert calls == ["j1", "j2"]
    _assert_every_call_answered_once(messages)


# ---------------------------------------------------------------------------
# 3. A deduplicated call is visible
# ---------------------------------------------------------------------------


async def test_trace_tools_observer_and_callbacks_show_the_reused_call(capsys) -> None:
    lookups: list[str] = []

    @tool
    async def lookup_user(user_id: str) -> str:
        """Look up a user by id."""
        lookups.append(user_id)
        return f"user {user_id}: Dana"

    lookup = ("lookup_user", {"user_id": "u1"})
    collector = ObservabilityCollector("toolnode-dedupe")
    callbacks = _ToolCallbacks()
    agent = await build_agent(
        model=_Conversations({"Who is u1?": [[lookup, lookup]]}),
        servers={},
        agent_pattern=_agent_graph(_tools_node(lookup_user)),
        trace_tools=True,
        observer=collector,
    )
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Who is u1?"}]},
            config={"callbacks": [callbacks]},
        )
    finally:
        await agent.shutdown()

    assert lookups == ["u1"]
    _assert_every_call_answered_once(result["messages"])
    assert _results(result["messages"]) == ["user u1: Dana"] * 2

    out = capsys.readouterr().out
    assert "↺ Reusing an earlier result for tool: lookup_user with {'user_id': 'u1'}" in out
    assert "✔ Tool result from lookup_user (reused): user u1: Dana" in out

    reused = [
        (e.event_type.value, e.metadata["tool_name"])
        for e in collector.query(event_types=["tool.call", "tool.result"])
        if e.metadata.get("reused")
    ]
    assert reused == [("tool.call", "lookup_user"), ("tool.result", "lookup_user")]

    # Callbacks see both calls (the tool's own run and the reused one), the
    # reused one marked in its metadata.
    assert [name for name, _ in callbacks.starts] == ["lookup_user", "lookup_user"]
    assert [bool(meta.get(REUSED_TOOL_METADATA_KEY)) for _, meta in callbacks.starts] == [
        False,
        True,
    ]
    assert callbacks.ends == 2


async def test_streamed_tool_end_event_marks_the_repeat_as_reused() -> None:
    from promptise.streaming import ToolEndEvent

    @tool
    async def lookup_user(user_id: str) -> str:
        """Look up a user by id."""
        return f"user {user_id}: Dana"

    lookup = ("lookup_user", {"user_id": "u1"})
    agent = await build_agent(
        model=_Conversations({"Who is u1?": [[lookup, lookup]]}),
        servers={},
        agent_pattern=_agent_graph(_tools_node(lookup_user)),
    )
    try:
        ends = [
            event
            async for event in agent.astream_with_tools(
                {"messages": [{"role": "user", "content": "Who is u1?"}]}
            )
            if isinstance(event, ToolEndEvent)
        ]
    finally:
        await agent.shutdown()

    assert [e.reused for e in ends] == [False, True]


async def test_streamed_runs_on_the_same_engine_each_run_the_tool() -> None:
    job_status, calls = _job_tool(completes_on=2)
    engine = PromptGraphEngine(
        graph=_agent_graph(_tools_node(job_status)),
        model=_Conversations({"Wait for j1": [[POLL, POLL], [POLL]]}),
    )
    question = {"messages": [{"role": "user", "content": "Wait for j1"}]}

    for _ in range(2):
        chunks = [c async for c in engine.astream(question)]
        _assert_every_call_answered_once(chunks[-1]["messages"])
    events = [e async for e in engine.astream_events(question)]

    assert events
    # Per run: the duplicated pair runs the tool once, the next turn's check once.
    assert len(calls) == 6
