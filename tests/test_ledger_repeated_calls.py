"""A repeated tool call runs the tool; a run cut off mid tool loop is an error.

Regressions (v1.3.1):

1. Once a ReAct run had 6 tool results, ``context_scope="auto"`` switched to
   the ledger, and the ledger answered every call with the same tool and
   arguments as an earlier one from that earlier result without running the
   tool.  A polling agent (``job_status(job_id)``) saw the 6th "running"
   forever although the job had completed, nothing reached the server, and
   ``trace_tools`` / callbacks showed nothing for those calls.  Every call now
   runs; answering a repeat from the earlier result is an explicit opt-in
   (``ContextCompaction(reuse_results=...)``) and is reported, marked reused.
2. A run whose ``reason`` node hit its iteration limit in the middle of a tool
   loop returned the last tool result as the agent's answer.  It now raises
   ``GraphIterationLimitError``.

The agents run on a scripted chat model (no LLM API); the MCP server binds
``127.0.0.1`` on an OS-assigned port in-process.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import uvicorn
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import ToolException, tool
from pydantic import PrivateAttr

from promptise import build_agent
from promptise.config import HTTPServerSpec
from promptise.engine import (
    ContextCompaction,
    GraphExecutionError,
    GraphIterationLimitError,
    PromptGraph,
    PromptGraphEngine,
    PromptNode,
)
from promptise.engine.compaction import LEDGER_HEADER
from promptise.engine.nodes import REUSED_TOOL_METADATA_KEY
from promptise.engine.serialization import node_from_config, node_to_config
from promptise.mcp.server import MCPServer
from promptise.observability import ObservabilityCollector
from promptise.streaming import ErrorEvent, ToolEndEvent

QUESTION = {"role": "user", "content": "Wait for job j1, then tell me when it is done."}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Scripted(BaseChatModel):
    """Replays scripted responses (``done`` when they run out) and records
    every request."""

    _script: Iterator[AIMessage] = PrivateAttr()
    _seen: list[list[BaseMessage]] = PrivateAttr()

    def __init__(self, script: Iterable[AIMessage]) -> None:
        super().__init__()
        self._script = iter(script)
        self._seen = []

    @property
    def seen(self) -> list[list[BaseMessage]]:
        return self._seen

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _Scripted:  # type: ignore[override]
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any) -> Any:
        raise NotImplementedError

    async def _agenerate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
        message = next(self._script, AIMessage(content="done"))
        return ChatResult(generations=[ChatGeneration(message=message)])


_ids = itertools.count()


def _step(*calls: tuple[str, dict[str, Any]]) -> AIMessage:
    """One model turn calling *calls* (in parallel when several)."""
    return AIMessage(
        content="",
        tool_calls=[{"name": n, "args": a, "id": f"call-{next(_ids)}"} for n, a in calls],
    )


def _polls(n: int) -> list[AIMessage]:
    return [_step(("job_status", {"job_id": "j1"})) for _ in range(n)]


def _status(n: int) -> str:
    """What the job reports on its *n*-th check: done from the 8th on."""
    return f"call {n}: " + ("completed" if n >= 8 else "running")


def _job_tool() -> tuple[Any, list[str]]:
    calls: list[str] = []

    @tool
    async def job_status(job_id: str) -> str:
        """Return the current status of a job."""
        calls.append(job_id)
        return _status(len(calls))

    return job_status, calls


def _text(messages: list[BaseMessage]) -> str:
    return "\n".join(str(m.content) for m in messages)


def _tool_results(result: dict[str, Any]) -> list[str]:
    return [str(m.content) for m in result["messages"] if isinstance(m, ToolMessage)]


async def _ask(model: _Scripted, tools: list[Any], **kwargs: Any) -> dict[str, Any]:
    agent = await build_agent(model=model, servers={}, extra_tools=tools, **kwargs)
    try:
        return await agent.ainvoke({"messages": [QUESTION]})
    finally:
        await agent.shutdown()


@asynccontextmanager
async def _serve(server: MCPServer, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """Run *server* over Streamable HTTP and yield its ``/mcp`` URL."""
    instances: list[uvicorn.Server] = []

    class _Recording(uvicorn.Server):
        def __init__(self, config: uvicorn.Config) -> None:
            super().__init__(config)
            instances.append(self)

    monkeypatch.setattr(uvicorn, "Server", _Recording)
    task = asyncio.ensure_future(server.run_async(transport="http", host="127.0.0.1", port=0))
    try:
        for _ in range(400):
            if task.done():
                task.result()
            if instances and instances[0].started:
                break
            await asyncio.sleep(0.025)
        else:
            raise RuntimeError("server did not start")
        instances[0].config.timeout_graceful_shutdown = 5
        port = instances[0].servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        if instances:
            instances[0].should_exit = True
        try:
            await asyncio.wait_for(task, 15)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            task.cancel()


class _ToolCallbacks(AsyncCallbackHandler):
    """Records the LangChain tool callbacks of a run's tool calls (not the
    nested runs of a wrapped tool)."""

    def __init__(self) -> None:
        self._runs: set[Any] = set()
        self._nested: set[Any] = set()
        self._starts: list[tuple[Any, str, dict[str, Any]]] = []
        self.ends = 0

    @property
    def starts(self) -> list[tuple[str, dict[str, Any]]]:
        return [(name, meta) for run, name, meta in self._starts if run not in self._nested]

    async def on_tool_start(
        self, serialized: Any, input_str: str, *, run_id: Any, parent_run_id: Any = None, **kw: Any
    ) -> None:
        self._runs.add(run_id)
        if parent_run_id in self._runs:
            self._nested.add(run_id)
        self._starts.append((run_id, serialized["name"], kw.get("metadata") or {}))

    async def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        if run_id not in self._nested:
            self.ends += 1


# ---------------------------------------------------------------------------
# 1. A repeated call runs the tool (the default)
# ---------------------------------------------------------------------------


async def test_polling_runs_the_tool_every_time_and_sees_the_job_complete(capsys) -> None:
    job_status, calls = _job_tool()
    model = _Scripted([*_polls(10), AIMessage(content="Job j1 is done.")])

    result = await _ask(model, [job_status], trace_tools=True)

    assert len(calls) == 10
    assert _tool_results(result) == [_status(n) for n in range(1, 11)]
    # The loop compacted (ledger from call 7 on) and the model still got the
    # current status, not the 6th "running".
    last_request = model.seen[-1]
    assert LEDGER_HEADER in _text(last_request)
    assert "call 10: completed" in _text(last_request)
    assert result["messages"][-1].content == "Job j1 is done."
    assert capsys.readouterr().out.count("→ Invoking tool: job_status") == 10


async def test_managed_pattern_runs_repeated_calls_too() -> None:
    job_status, calls = _job_tool()
    result = await _ask(_Scripted(_polls(10)), [job_status], agent_pattern="managed")
    assert len(calls) == 10
    assert _tool_results(result)[-1] == "call 10: completed"


async def test_polling_an_mcp_tool_reaches_the_server_every_time(monkeypatch, capsys) -> None:
    server = MCPServer("jobs")
    hits: list[str] = []

    @server.tool()
    async def job_status(job_id: str) -> str:
        """Return the current status of a job."""
        hits.append(job_id)
        return _status(len(hits))

    model = _Scripted([*_polls(10), AIMessage(content="Job j1 is done.")])
    async with _serve(server, monkeypatch) as url:
        agent = await build_agent(
            model=model, servers={"jobs": HTTPServerSpec(url=url)}, trace_tools=True
        )
        try:
            result = await agent.ainvoke({"messages": [QUESTION]})
        finally:
            await agent.shutdown()

    assert hits == ["j1"] * 10
    assert [r.strip() for r in _tool_results(result)][-3:] == [_status(n) for n in (8, 9, 10)]
    assert "call 10: completed" in _text(model.seen[-1])
    assert capsys.readouterr().out.count("→ Invoking tool: job_status") == 10


async def test_ledger_still_lists_each_call_once_with_its_latest_result() -> None:
    job_status, _ = _job_tool()
    model = _Scripted(_polls(10))
    await _ask(model, [job_status])
    ledger = next(m for m in model.seen[-1] if LEDGER_HEADER in str(m.content))
    assert str(ledger.content).count("job_status(") == 1
    assert "call 9: completed" in str(ledger.content)  # call 10 is the latest exchange


# ---------------------------------------------------------------------------
# 2. Reuse is an opt-in, and a reused call is visible
# ---------------------------------------------------------------------------


def _lookup_tool() -> tuple[Any, list[str]]:
    calls: list[str] = []

    @tool
    async def lookup_user(user_id: str) -> str:
        """Look up a user by id."""
        calls.append(user_id)
        return f"user {user_id}: Dana"

    return lookup_user, calls


def _reuse_script() -> list[AIMessage]:
    """Six distinct lookups (the ledger starts after six results), then a
    repeated lookup and two status checks."""
    lookups = [_step(("lookup_user", {"user_id": f"u{i}"})) for i in range(6)]
    return [
        *lookups,
        _step(("lookup_user", {"user_id": "u0"})),
        *_polls(2),
        AIMessage(content="answer"),
    ]


async def test_opt_in_reuse_answers_only_the_listed_tools_and_shows_it(capsys) -> None:
    lookup_user, lookups = _lookup_tool()
    job_status, polls = _job_tool()
    collector = ObservabilityCollector("reuse")
    callbacks = _ToolCallbacks()
    agent = await build_agent(
        model=_Scripted(_reuse_script()),
        servers={},
        extra_tools=[lookup_user, job_status],
        context_compaction=ContextCompaction(reuse_results=["lookup_*"]),
        trace_tools=True,
        observer=collector,
    )
    try:
        result = await agent.ainvoke({"messages": [QUESTION]}, config={"callbacks": [callbacks]})
    finally:
        await agent.shutdown()

    # The repeated lookup was answered from its earlier result; the status
    # tool, not listed, ran on both checks.
    assert lookups == [f"u{i}" for i in range(6)]
    assert len(polls) == 2
    assert _tool_results(result)[6] == "user u0: Dana"

    out = capsys.readouterr().out
    assert out.count("→ Invoking tool: lookup_user") == 6
    assert "↺ Reusing an earlier result for tool: lookup_user with {'user_id': 'u0'}" in out
    assert "✔ Tool result from lookup_user (reused): user u0: Dana" in out
    assert out.count("→ Invoking tool: job_status") == 2

    reused_calls = [
        e for e in collector.query(event_types=["tool.call"]) if e.metadata.get("reused")
    ]
    assert [e.metadata["tool_name"] for e in reused_calls] == ["lookup_user"]
    reused_results = [
        e for e in collector.query(event_types=["tool.result"]) if e.metadata.get("reused")
    ]
    assert [e.metadata["result_preview"] for e in reused_results] == ["user u0: Dana"]

    # LangChain callbacks see every call, the reused one marked in its metadata.
    assert [name for name, _ in callbacks.starts].count("lookup_user") == 7
    marked = [name for name, meta in callbacks.starts if meta.get(REUSED_TOOL_METADATA_KEY)]
    assert marked == ["lookup_user"]
    assert callbacks.ends == len(callbacks.starts) == 9


async def test_observe_true_marks_the_reused_call() -> None:
    lookup_user, _ = _lookup_tool()
    job_status, _ = _job_tool()
    agent = await build_agent(
        model=_Scripted(_reuse_script()),
        servers={},
        extra_tools=[lookup_user, job_status],
        context_compaction=ContextCompaction(reuse_results=True),
        observe=True,
    )
    try:
        await agent.ainvoke({"messages": [QUESTION]})
    finally:
        await agent.shutdown()
    entries = agent.collector.query(event_types=["tool.call", "tool.result"])
    reused = [
        (e.event_type.value, e.metadata["tool_name"]) for e in entries if e.metadata.get("reused")
    ]
    # reuse_results=True: the repeated lookup and the second status check.
    assert reused == [
        ("tool.call", "lookup_user"),
        ("tool.result", "lookup_user"),
        ("tool.call", "job_status"),
        ("tool.result", "job_status"),
    ]


async def test_stream_marks_the_reused_call() -> None:
    lookup_user, _ = _lookup_tool()
    job_status, _ = _job_tool()
    agent = await build_agent(
        model=_Scripted(_reuse_script()),
        servers={},
        extra_tools=[lookup_user, job_status],
        context_compaction=ContextCompaction(reuse_results=["lookup_user"]),
    )
    try:
        events = [e async for e in agent.astream_with_tools({"messages": [QUESTION]})]
    finally:
        await agent.shutdown()
    ends = [e for e in events if isinstance(e, ToolEndEvent)]
    assert len(ends) == 9
    assert [(e.tool_name, e.tool_index) for e in ends if e.reused] == [("lookup_user", 6)]


async def test_engine_reports_reuse_to_hooks_events_and_the_node_result() -> None:
    lookup_user, _ = _lookup_tool()
    job_status, _ = _job_tool()

    class Hook:
        def __init__(self) -> None:
            self.reused: list[tuple[str, dict[str, Any], str]] = []
            self.tool_calls: list[dict[str, Any]] = []

        async def on_tool_reused(self, tool_name, args, result, state) -> None:
            self.reused.append((tool_name, args, result))

        async def post_node(self, node, result, state):
            self.tool_calls.extend(result.tool_calls)
            return result

    hook = Hook()
    engine = PromptGraphEngine(
        graph=PromptGraph.react(tools=[lookup_user, job_status]),
        model=_Scripted(_reuse_script()),
        hooks=[hook],
        compaction=ContextCompaction(reuse_results=["lookup_user"]),
    )
    events = [e async for e in engine.astream_events({"messages": [QUESTION]})]

    assert hook.reused == [("lookup_user", {"user_id": "u0"}, "user u0: Dana")]
    assert [tc["name"] for tc in hook.tool_calls if tc.get("reused")] == ["lookup_user"]
    starts = [e for e in events if e["event"] == "on_tool_start" and e["data"].get("reused")]
    ends = [e for e in events if e["event"] == "on_tool_end" and e["data"].get("reused")]
    assert [e["name"] for e in starts] == [e["name"] for e in ends] == ["lookup_user"]


async def test_reuse_needs_the_ledger_and_never_reuses_a_failed_call() -> None:
    attempts: list[str] = []

    @tool
    async def flaky(key: str) -> str:
        """Always fails."""
        attempts.append(key)
        raise ToolException("busy, retry")

    flaky.handle_tool_error = True
    lookup_user, lookups = _lookup_tool()
    script = [
        # Below the compaction threshold: the repeat runs the tool.
        _step(("lookup_user", {"user_id": "u1"})),
        _step(("lookup_user", {"user_id": "u1"})),
        *[_step(("flaky", {"key": "k"})) for _ in range(6)],
    ]
    await _ask(
        _Scripted(script),
        [lookup_user, flaky],
        context_compaction=ContextCompaction(reuse_results=True),
    )
    assert lookups == ["u1", "u1"]
    assert attempts == ["k"] * 6  # an error is not a result to reuse


# ---------------------------------------------------------------------------
# 3. The reuse setting
# ---------------------------------------------------------------------------


def test_reuse_results_setting() -> None:
    assert ContextCompaction().reuse_results is False
    assert not ContextCompaction().reuses("anything")
    assert ContextCompaction(reuse_results=True).reuses("anything")
    names = ContextCompaction(reuse_results=["get_user", "lookup_*"])
    assert names.reuse_results == ("get_user", "lookup_*")
    assert names.reuses("get_user") and names.reuses("lookup_order")
    assert not names.reuses("job_status")
    hash(names)  # stays a hashable, frozen value
    with pytest.raises(TypeError):
        ContextCompaction(reuse_results="get_user")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ContextCompaction(reuse_results=["get_user", 3])  # type: ignore[list-item]


def test_reuse_results_survives_graph_serialization() -> None:
    node = PromptNode(
        "reason",
        context_scope="ledger",
        compaction=ContextCompaction(reuse_results=["lookup_*"]),
    )
    config = node_to_config(node)
    assert config["compaction"] == {"reuse_results": ["lookup_*"]}
    loaded = node_from_config(config)
    assert loaded.compaction == ContextCompaction(reuse_results=("lookup_*",))


# ---------------------------------------------------------------------------
# 4. An iteration limit in the middle of a tool loop is an error, not an answer
# ---------------------------------------------------------------------------


async def test_reason_node_limit_mid_tool_loop_raises() -> None:
    job_status, calls = _job_tool()
    agent = await build_agent(model=_Scripted(_polls(100)), servers={}, extra_tools=[job_status])
    try:
        with pytest.raises(GraphIterationLimitError) as info:
            await agent.ainvoke({"messages": [QUESTION]})
    finally:
        await agent.shutdown()

    err = info.value
    assert isinstance(err, GraphExecutionError)
    assert (err.node_name, err.limit, err.pending_tools) == ("reason", 15, ["job_status"])
    assert "reached its iteration limit (15) before the model gave an answer" in str(err)
    assert "job_status" in str(err)
    assert err.report is not None and err.report.error == err.error
    assert len(calls) == 15


async def test_stream_with_tools_ends_with_an_error_not_an_answer() -> None:
    job_status, _ = _job_tool()
    agent = await build_agent(model=_Scripted(_polls(100)), servers={}, extra_tools=[job_status])
    try:
        events = [e async for e in agent.astream_with_tools({"messages": [QUESTION]})]
    finally:
        await agent.shutdown()
    assert isinstance(events[-1], ErrorEvent)
    assert not [e for e in events if e.type == "done"]


async def test_engine_max_iterations_mid_tool_loop_raises_in_every_entry_point() -> None:
    def engine() -> PromptGraphEngine:
        job_status, _ = _job_tool()
        return PromptGraphEngine(
            graph=PromptGraph.react(tools=[job_status]),
            model=_Scripted(_polls(100)),
            max_iterations=4,
        )

    with pytest.raises(GraphIterationLimitError) as info:
        await engine().ainvoke({"messages": [QUESTION]})
    assert info.value.limit == 4
    assert "max_iterations (4)" in str(info.value)

    with pytest.raises(GraphIterationLimitError):
        async for _ in engine().astream({"messages": [QUESTION]}):
            pass
    with pytest.raises(GraphIterationLimitError):
        async for _ in engine().astream_events({"messages": [QUESTION]}):
            pass


async def test_an_answer_on_the_last_allowed_step_is_returned() -> None:
    job_status, calls = _job_tool()
    model = _Scripted([*_polls(14), AIMessage(content="Job j1 is done.")])
    result = await _ask(model, [job_status])
    assert len(calls) == 14
    assert result["messages"][-1].content == "Job j1 is done."


async def test_a_limit_the_graph_routes_is_not_an_error() -> None:
    job_status, calls = _job_tool()
    graph = PromptGraph("poll", mode="static")
    graph.add_node(
        PromptNode("ask", tools=[job_status], max_iterations=3, transitions={"error": "give_up"})
    )
    graph.add_node(PromptNode("give_up", default_next="__end__"))
    graph.set_entry("ask")
    model = _Scripted([*_polls(3), AIMessage(content="Still running; check back later.")])

    result = await PromptGraphEngine(graph=graph, model=model).ainvoke({"messages": [QUESTION]})

    assert len(calls) == 3
    assert result["messages"][-1].content == "Still running; check back later."
