"""A tool behaves the same whichever node runs it: ``ToolNode`` and ``PromptNode``.

Regressions (v1.3.2 before this fix), all in ``ToolNode``:

1. It emitted no ``on_tool_start`` / ``on_tool_end`` stream events, so its
   calls (reused ones included) were missing from ``astream_events()`` and
   ``astream_with_tools()``.  An MCP tool error reached the model as
   ``"Error: MCPToolError: ..."`` instead of the server's error envelope.
2. It never called the engine hooks' ``pre_tool`` / ``post_tool``, so a hook
   could neither change nor block its calls.
3. ``validate_inputs=True`` (the default) did nothing: invalid arguments
   reached the tool and came back as a raw pydantic error.
4. A ``tool_selector``-only graph stopped by an iteration limit raised
   ``GraphIterationLimitError`` saying "a call to ? whose result the model
   never saw".

The deduplication of the two 1.3.2 fixes still holds with streaming, hooks
and validation: reuse is visible, every ``tool_call_id`` has exactly one
``ToolMessage``, and nothing is reused across runs.

The agents run on scripted chat models (no LLM API).
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Sequence
from typing import Annotated, Any

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import InjectedToolCallId, tool
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, PrivateAttr

from promptise import build_agent
from promptise.engine import (
    GraphIterationLimitError,
    GraphState,
    NodeResult,
    PromptGraph,
    PromptGraphEngine,
    ToolNode,
    node,
)
from promptise.mcp.client._tool_adapter import _PromptiseMCPTool
from promptise.mcp.server import ToolError
from promptise.streaming import DoneEvent, ToolEndEvent, ToolStartEvent
from promptise.tools import ToolArgumentError

Call = tuple[str, dict[str, Any]]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

_ids = itertools.count()


class _Conversations(BaseChatModel):
    """Plays each conversation's own script: the *n*-th model turn of the
    conversation that asks *question* makes the calls ``scripts[question][n]``
    (each with a fresh ``tool_call_id``); past the end of the script it
    answers.  Every request is recorded in ``seen``."""

    _scripts: dict[str, list[list[Call]]] = PrivateAttr()
    _seen: list[list[BaseMessage]] = PrivateAttr()

    def __init__(self, scripts: dict[str, list[list[Call]]]) -> None:
        super().__init__()
        self._scripts = scripts
        self._seen = []

    @property
    def seen(self) -> list[list[BaseMessage]]:
        return self._seen

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
        self._seen.append(list(messages))
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


def _tool_node_graph(*tools: Any, **kwargs: Any) -> PromptGraph:
    return _agent_graph(ToolNode("tools", tools=list(tools), default_next="agent", **kwargs))


def _prompt_node_graph(*tools: Any) -> PromptGraph:
    return PromptGraph.react(tools=list(tools))


def _users_tool(delays: dict[str, float] | None = None) -> tuple[Any, list[str]]:
    lookups: list[str] = []

    @tool
    async def lookup_user(user_id: str) -> str:
        """Look up a user by id."""
        lookups.append(user_id)
        await asyncio.sleep((delays or {}).get(user_id, 0.0))
        return f"user {user_id}: Dana"

    return lookup_user, lookups


def _job_tool() -> tuple[Any, list[str]]:
    calls: list[str] = []

    @tool
    async def job_status(job_id: str) -> str:
        """Return the current status of a job."""
        calls.append(job_id)
        return f"check {len(calls)}: running"

    return job_status, calls


class _ErrorResultMulti:
    """Stands in for ``MCPMultiClient``: every call returns an MCP tool error."""

    def __init__(self, text: str) -> None:
        self.text = text

    async def call_tool(
        self, name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=self.text)], isError=True)


class _OrderArgs(BaseModel):
    order_id: str


def _mcp_tool_returning_error(message: str) -> _PromptiseMCPTool:
    """An MCP-backed tool whose server answers with ``ToolError(message)``."""
    return _PromptiseMCPTool(
        name="get_order_status",
        description="Get an order's status.",
        args_schema=_OrderArgs,
        tool_name="get_order_status",
        multi=_ErrorResultMulti(ToolError(message).to_text()),  # type: ignore[arg-type]
    )


class _ToolHooks:
    """Engine hook that logs tool hooks, rewrites arguments and results, and
    blocks calls to the tools in *block* by raising."""

    def __init__(self, *, block: frozenset[str] = frozenset()) -> None:
        self.block = block
        self.log: list[tuple[Any, ...]] = []
        self.tool_results: list[NodeResult] = []

    async def pre_tool(self, tool_name: str, args: dict, state: GraphState) -> dict:
        self.log.append(("pre_tool", tool_name, dict(args)))
        if tool_name in self.block:
            raise PermissionError(f"{tool_name} is not allowed for this user")
        # Normalise ids: the tool must get the changed arguments.
        return {k: v.lower() if isinstance(v, str) else v for k, v in args.items()}

    async def post_tool(self, tool_name: str, result: str, args: dict, state: GraphState) -> str:
        self.log.append(("post_tool", tool_name, result, dict(args)))
        return f"{result} [audited]"

    async def on_tool_reused(self, tool_name, args, result, state) -> None:  # type: ignore[no-untyped-def]
        self.log.append(("on_tool_reused", tool_name, dict(args), result))

    async def post_node(self, node_obj, result, state):  # type: ignore[no-untyped-def]
        if result.tool_calls:
            self.tool_results.append(result)
        return result


class _ToolCallbacks(AsyncCallbackHandler):
    """Records the LangChain tool callbacks of a run."""

    def __init__(self) -> None:
        self.starts: list[str] = []
        self.errors: list[BaseException] = []

    async def on_tool_start(self, serialized: Any, input_str: str, **kw: Any) -> None:
        self.starts.append(serialized["name"])

    async def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        self.errors.append(error)


def _ask(question: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": question}]}


async def _engine_run(engine: PromptGraphEngine, question: str) -> list[BaseMessage]:
    result = await engine.ainvoke(_ask(question))
    return list(result["messages"])


def _tool_messages(messages: Sequence[BaseMessage]) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage)]


def _assert_every_call_answered_once(messages: Sequence[BaseMessage]) -> None:
    """Every ``tool_call_id`` the model used has exactly one ``ToolMessage``."""
    asked = [tc["id"] for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls]
    answered = [m.tool_call_id for m in _tool_messages(messages)]
    assert asked, "the model made no tool calls"
    assert sorted(answered) == sorted(asked)


async def _stream(agent: Any, question: str) -> list[Any]:
    return [event async for event in agent.astream_with_tools(_ask(question))]


# ---------------------------------------------------------------------------
# 1. A ToolNode's calls stream
# ---------------------------------------------------------------------------


async def test_toolnode_calls_stream_through_astream_with_tools() -> None:
    lookup_user, lookups = _users_tool(delays={"u1": 0.15})
    u1, u2 = ("lookup_user", {"user_id": "u1"}), ("lookup_user", {"user_id": "u2"})
    agent = await build_agent(
        model=_Conversations({"Who are u1 and u2?": [[u1, u2, u1]]}),
        servers={},
        agent_pattern=_tool_node_graph(lookup_user),
    )
    try:
        events = await _stream(agent, "Who are u1 and u2?")
    finally:
        await agent.shutdown()

    starts = [e for e in events if isinstance(e, ToolStartEvent)]
    ends = [e for e in events if isinstance(e, ToolEndEvent)]
    assert [(s.tool_name, s.arguments, s.tool_index) for s in starts] == [
        ("lookup_user", {"user_id": "u1"}, 0),
        ("lookup_user", {"user_id": "u2"}, 1),
        ("lookup_user", {"user_id": "u1"}, 2),
    ]
    # The repeat in the same turn is answered from the first call: visible,
    # marked reused, and the tool ran once per distinct call.
    assert [(e.tool_index, e.success, e.reused, e.tool_summary) for e in ends] == [
        (0, True, False, "user u1: Dana"),
        (1, True, False, "user u2: Dana"),
        (2, True, True, "user u1: Dana"),
    ]
    assert lookups == ["u1", "u2"]
    # duration_ms is the call's own: the slow lookup took its 150 ms.
    assert ends[0].duration_ms >= 100
    assert ends[2].duration_ms < 100
    # The calls run one after another: each start is followed by its end.
    tool_events = [e.type for e in events if e.type in ("tool_start", "tool_end")]
    assert tool_events == ["tool_start", "tool_end"] * 3
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert [c["success"] for c in done.tool_calls] == [True, True, True]


async def test_mcp_tool_error_in_a_toolnode_streams_its_message_and_code() -> None:
    model = _Conversations({"Where is Z-9?": [[("get_order_status", {"order_id": "Z-9"})]]})
    agent = await build_agent(
        model=model,
        servers={},
        agent_pattern=_tool_node_graph(_mcp_tool_returning_error("No order found with ID Z-9.")),
    )
    try:
        events = await _stream(agent, "Where is Z-9?")
    finally:
        await agent.shutdown()

    [tool_end] = [e for e in events if isinstance(e, ToolEndEvent)]
    assert tool_end.success is False
    assert tool_end.tool_summary == "No order found with ID Z-9."
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].tool_calls == [
        {"name": "get_order_status", "summary": "No order found with ID Z-9.", "success": False}
    ]
    # The model sees the server's error envelope (with its code), flagged as an error.
    [tool_message] = _tool_messages(model.seen[-1])
    assert tool_message.status == "error"
    assert json.loads(str(tool_message.content))["error"] == {
        "code": "TOOL_ERROR",
        "message": "No order found with ID Z-9.",
        "retryable": False,
    }


async def test_a_raising_tool_in_a_toolnode_streams_as_failed_without_its_text() -> None:
    @tool
    async def get_order_status(order_id: str) -> str:
        """Get an order's status."""
        raise RuntimeError("connect to postgres://admin:hunter2@db failed")

    agent = await build_agent(
        model=_Conversations({"Where is A-1?": [[("get_order_status", {"order_id": "A-1"})]]}),
        servers={},
        agent_pattern=_tool_node_graph(get_order_status),
    )
    try:
        events = await _stream(agent, "Where is A-1?")
    finally:
        await agent.shutdown()

    [tool_end] = [e for e in events if isinstance(e, ToolEndEvent)]
    assert (tool_end.success, tool_end.tool_summary, tool_end.tool_index) == (
        False,
        "Tool call failed",
        0,
    )
    assert "hunter2" not in "".join(e.to_json() for e in events)


async def test_toolnode_engine_events_pair_by_run_id_and_match_ainvoke() -> None:
    lookup_user, lookups = _users_tool()
    u1, u2 = ("lookup_user", {"user_id": "u1"}), ("lookup_user", {"user_id": "u2"})
    engine = PromptGraphEngine(
        graph=_tool_node_graph(lookup_user),
        model=_Conversations({"Who?": [[u1, u1], [u2]]}),
        hooks=[_ToolHooks()],
    )

    events = [e async for e in engine.astream_events(_ask("Who?"))]

    tool_events = [e for e in events if e["event"] in ("on_tool_start", "on_tool_end")]
    assert [(e["event"], e["name"]) for e in tool_events] == [
        ("on_tool_start", "lookup_user"),
        ("on_tool_end", "lookup_user"),
    ] * 3
    for start, end in zip(tool_events[::2], tool_events[1::2], strict=True):
        assert start["run_id"] == end["run_id"]
        assert end["data"]["status"] == "success"
        assert isinstance(end["data"]["duration_ms"], float)
    assert [bool(e["data"].get("reused")) for e in tool_events] == [
        False,
        False,
        True,
        True,
        False,
        False,
    ]
    assert lookups == ["u1", "u2"]

    # The stream is a view of the same run ainvoke() does.
    streamed = [c async for c in engine.astream(_ask("Who?"))][-1]["messages"]
    invoked = await _engine_run(engine, "Who?")
    for messages in (streamed, invoked):
        _assert_every_call_answered_once(messages)
        assert [(m.content, m.status, m.name) for m in _tool_messages(messages)] == [
            ("user u1: Dana [audited]", "success", "lookup_user"),
            ("user u1: Dana [audited]", "success", "lookup_user"),
            ("user u2: Dana [audited]", "success", "lookup_user"),
        ]


async def test_streamed_runs_with_hooks_and_validation_never_reuse_across_runs() -> None:
    """Dedupe with streaming, hooks and validation on: a repeat in one turn
    is reused (and visible), every tool_call_id gets one ToolMessage, and a
    second run on the same agent runs the tool again."""
    job_status, calls = _job_tool()
    poll = ("job_status", {"job_id": "j1"})
    hooks = _ToolHooks()
    engine = PromptGraphEngine(
        graph=_tool_node_graph(job_status, validate_inputs=True),
        model=_Conversations({"Wait for j1": [[poll, poll], [poll]]}),
        hooks=[hooks],
    )

    for run in range(2):
        chunks = [c async for c in engine.astream(_ask("Wait for j1"))]
        messages = chunks[-1]["messages"]
        _assert_every_call_answered_once(messages)
        assert len(_tool_messages(messages)) == 3
        # Per run: the duplicated pair runs the tool once, the next turn's check once.
        assert len(calls) == 2 * (run + 1)

    events = [e async for e in engine.astream_events(_ask("Wait for j1"))]
    ends = [e for e in events if e["event"] == "on_tool_end"]
    assert [bool(e["data"].get("reused")) for e in ends] == [False, True, False]
    assert len(calls) == 6
    reused = [entry for entry in hooks.log if entry[0] == "on_tool_reused"]
    assert len(reused) == 3  # one per run, never pre_tool / post_tool for it
    assert sum(entry[0] == "pre_tool" for entry in hooks.log) == 6


# ---------------------------------------------------------------------------
# 2. pre_tool / post_tool hooks
# ---------------------------------------------------------------------------


async def _run_with_hooks(graph_of: Any, question: str, script: list[list[Call]]) -> Any:
    lookup_user, lookups = _users_tool()
    deleted: list[str] = []

    @tool
    async def delete_user(user_id: str) -> str:
        """Delete a user."""
        deleted.append(user_id)
        return f"deleted {user_id}"

    hooks = _ToolHooks(block=frozenset({"delete_user"}))
    engine = PromptGraphEngine(
        graph=graph_of(lookup_user, delete_user),
        model=_Conversations({question: script}),
        hooks=[hooks],
    )
    events = [e async for e in engine.astream_events(_ask(question))]
    messages = await _engine_run(engine, question)
    return messages, events, hooks, lookups, deleted


async def test_pre_and_post_tool_hooks_change_and_block_toolnode_calls_as_in_prompt_node() -> None:
    script = [[("lookup_user", {"user_id": "U1"}), ("delete_user", {"user_id": "U2"})]]
    outcomes = {}
    for label, graph_of in (("prompt", _prompt_node_graph), ("tool", _tool_node_graph)):
        messages, events, hooks, lookups, deleted = await _run_with_hooks(
            graph_of, "Look up U1, delete U2", script
        )
        # pre_tool changed the arguments the tool got; a pre_tool hook that
        # raised blocked the call: the tool never ran.
        assert lookups == ["u1", "u1"]  # astream_events run + ainvoke run
        assert deleted == []
        tool_messages = sorted((str(m.content), m.status, m.name) for m in _tool_messages(messages))
        hook_calls = sorted((entry[0], entry[1]) for entry in hooks.log[: len(hooks.log) // 2])
        stream = sorted(
            (e["event"], e["name"])
            for e in events
            if e["event"] in ("on_tool_start", "on_tool_end", "on_tool_error")
        )
        outcomes[label] = (
            tool_messages,
            hook_calls,
            stream,
            hooks.tool_results[-1].tool_calls_failed,
        )

    assert outcomes["tool"] == outcomes["prompt"]
    tool_messages, hook_calls, stream, failed = outcomes["tool"]
    assert tool_messages == [
        ("Error: delete_user is not allowed for this user", "error", "delete_user"),
        ("user u1: Dana [audited]", "success", "lookup_user"),
    ]
    # A blocked call gets no post_tool.
    assert hook_calls == [
        ("post_tool", "lookup_user"),
        ("pre_tool", "delete_user"),
        ("pre_tool", "lookup_user"),
    ]
    assert stream == [
        ("on_tool_end", "lookup_user"),
        ("on_tool_error", "delete_user"),
        ("on_tool_start", "delete_user"),
        ("on_tool_start", "lookup_user"),
    ]
    assert failed == 1


async def test_toolnode_post_tool_sees_the_changed_arguments_and_reuse_skips_the_hooks() -> None:
    script = [[("lookup_user", {"user_id": "U1"}), ("lookup_user", {"user_id": "U1"})]]
    messages, _events, hooks, lookups, _deleted = await _run_with_hooks(
        _tool_node_graph, "Look up U1 twice", script
    )

    assert lookups == ["u1", "u1"]  # once per run: the repeat is reused
    run = hooks.log[len(hooks.log) // 2 :]
    assert run == [
        ("pre_tool", "lookup_user", {"user_id": "U1"}),
        ("post_tool", "lookup_user", "user u1: Dana", {"user_id": "u1"}),
        ("on_tool_reused", "lookup_user", {"user_id": "U1"}, "user u1: Dana [audited]"),
    ]
    assert [m.content for m in _tool_messages(messages)] == ["user u1: Dana [audited]"] * 2
    [result] = hooks.tool_results[-1:]
    assert [tc["args"] for tc in result.tool_calls] == [{"user_id": "u1"}, {"user_id": "U1"}]


async def test_a_blocked_call_is_not_reused_and_blocks_on_every_selector_pass() -> None:
    deleted: list[str] = []

    @tool
    async def delete_user(user_id: str) -> str:
        """Delete a user."""
        deleted.append(user_id)
        return "deleted"

    hooks = _ToolHooks(block=frozenset({"delete_user"}))
    blocked = ("delete_user", {"user_id": "u2"})
    engine = PromptGraphEngine(
        graph=_tool_node_graph(delete_user),
        model=_Conversations({"Delete u2": [[blocked, blocked]]}),
        hooks=[hooks],
    )

    messages = await _engine_run(engine, "Delete u2")

    assert deleted == []
    _assert_every_call_answered_once(messages)
    assert [(m.content, m.status) for m in _tool_messages(messages)] == [
        ("Error: delete_user is not allowed for this user", "error")
    ] * 2
    assert [entry[0] for entry in hooks.log] == ["pre_tool", "pre_tool"]
    [result] = hooks.tool_results
    assert (result.tool_calls_failed, result.tool_calls_deduplicated) == (2, 0)


# ---------------------------------------------------------------------------
# 3. validate_inputs
# ---------------------------------------------------------------------------


def _order_tool() -> tuple[Any, list[tuple[str, int]]]:
    orders: list[tuple[str, int]] = []

    @tool
    async def create_order(sku: str, qty: int) -> str:
        """Create an order."""
        orders.append((sku, qty))
        return f"order {sku} x{qty}"

    return create_order, orders


async def test_validate_inputs_rejects_invalid_arguments_so_the_model_can_correct_them() -> None:
    create_order, orders = _order_tool()
    bad = ("create_order", {"sku": "A", "qty": "two"})
    good = ("create_order", {"sku": "A", "qty": 2})
    hooks = _ToolHooks()
    callbacks = _ToolCallbacks()
    model = _Conversations({"Order two of A": [[bad, bad], [good]]})
    engine = PromptGraphEngine(graph=_tool_node_graph(create_order), model=model, hooks=[hooks])

    result = await engine.ainvoke(_ask("Order two of A"), config={"callbacks": [callbacks]})
    messages = result["messages"]

    # The invalid calls never reached the tool; the corrected one did.
    assert orders == [("a", 2)]
    _assert_every_call_answered_once(messages)
    first, repeat, corrected = _tool_messages(messages)
    expected = (
        "Invalid arguments for tool 'create_order':\n"
        '- qty: Input should be a valid integer, unable to parse string as an integer (got "two")\n'
        "Correct the arguments and call the tool again."
    )
    # Each invalid call is an error the model reads (not reused: it failed).
    assert (first.content, first.status) == (expected + " [audited]", "error")
    assert (repeat.content, repeat.status) == (expected + " [audited]", "error")
    assert (corrected.content, corrected.status) == ("order a x2 [audited]", "success")
    # The model's next turn reads the error.
    assert first.content in [m.content for m in _tool_messages(model.seen[1])]
    rejected = hooks.tool_results[0]
    assert (rejected.tool_calls_failed, rejected.tool_calls_deduplicated) == (2, 0)
    # The rejected calls are visible to callbacks, as an MCP tool's are.
    assert callbacks.starts == ["create_order"] * 3
    assert [type(e) for e in callbacks.errors] == [ToolArgumentError, ToolArgumentError]
    assert all(e.code == "INVALID_ARGUMENTS" for e in callbacks.errors)  # type: ignore[attr-defined]

    # validate_inputs=False hands the arguments to the tool as they are: the
    # tool's own parsing fails, with its own error.
    create_order, orders = _order_tool()
    engine = PromptGraphEngine(
        graph=_tool_node_graph(create_order, validate_inputs=False),
        model=_Conversations({"Order two of A": [[bad]]}),
    )
    [unchecked] = _tool_messages(await _engine_run(engine, "Order two of A"))
    assert orders == []
    assert str(unchecked.content).startswith("Error: ValidationError:")
    assert "Correct the arguments" not in str(unchecked.content)


class _RecordingMulti:
    """Stands in for ``MCPMultiClient``: records the calls that reach the server."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> CallToolResult:
        self.calls.append(arguments)
        return CallToolResult(content=[TextContent(type="text", text="shipped")], isError=False)


async def test_an_mcp_tool_in_a_toolnode_rejects_invalid_arguments_through_its_own_hooks() -> None:
    """An MCP tool checks its own arguments (1.3.1): the node leaves it to,
    so the rejected call reaches the tool's trace hooks once, the model gets
    the same ToolArgumentError message, and the server gets nothing."""
    multi = _RecordingMulti()
    traced: list[tuple[str, Any]] = []
    get_order_status = _PromptiseMCPTool(
        name="get_order_status",
        description="Get an order's status.",
        args_schema=_OrderArgs,
        tool_name="get_order_status",
        multi=multi,  # type: ignore[arg-type]
        on_before=lambda name, args: traced.append(("before", args)),
        on_error=lambda name, exc: traced.append(("error", exc)),
    )
    callbacks = _ToolCallbacks()
    script = [
        [("get_order_status", {"order_id": None})],
        [("get_order_status", {"order_id": "A-1"})],
    ]
    engine = PromptGraphEngine(
        graph=_tool_node_graph(get_order_status),
        model=_Conversations({"Where is my order?": script}),
    )

    result = await engine.ainvoke(_ask("Where is my order?"), config={"callbacks": [callbacks]})

    rejected, answered = _tool_messages(result["messages"])
    assert rejected.status == "error"
    assert rejected.content == (
        "Invalid arguments for tool 'get_order_status':\n"
        "- order_id: Input should be a valid string (got null)\n"
        "Correct the arguments and call the tool again."
    )
    assert (answered.content, answered.status) == ("shipped", "success")
    assert multi.calls == [{"order_id": "A-1"}]
    # Reported once, by the tool itself.
    assert [kind for kind, _ in traced] == ["before", "error", "before"]
    assert isinstance(traced[1][1], ToolArgumentError)
    assert [type(e) for e in callbacks.errors] == [ToolArgumentError]


async def test_validate_inputs_streams_the_rejected_call_as_a_failed_tool() -> None:
    create_order, orders = _order_tool()
    agent = await build_agent(
        model=_Conversations({"Order A": [[("create_order", {"sku": "A"})]]}),
        servers={},
        agent_pattern=_tool_node_graph(create_order),
    )
    try:
        events = await _stream(agent, "Order A")
    finally:
        await agent.shutdown()

    assert orders == []
    [start] = [e for e in events if isinstance(e, ToolStartEvent)]
    [end] = [e for e in events if isinstance(e, ToolEndEvent)]
    assert (start.tool_index, end.tool_index, end.success, end.reused) == (0, 0, False, False)
    assert end.tool_summary.startswith("Invalid arguments for tool 'create_order'")


async def test_validate_inputs_treats_null_for_an_optional_parameter_as_not_given() -> None:
    searches: list[tuple[str, int]] = []

    @tool
    async def search(query: str, limit: int = 10) -> str:
        """Search the catalogue."""
        searches.append((query, limit))
        return f"{limit} results for {query}"

    engine = PromptGraphEngine(
        graph=_tool_node_graph(search),
        model=_Conversations({"Find lamps": [[("search", {"query": "lamps", "limit": None})]]}),
    )

    [message] = _tool_messages(await _engine_run(engine, "Find lamps"))

    assert searches == [("lamps", 10)]
    assert (message.content, message.status) == ("10 results for lamps", "success")


async def test_validate_inputs_checks_only_what_the_model_supplies() -> None:
    """Injected arguments are not the model's to give: they do not fail
    validation, and the tool gets them (it is invoked as a tool call)."""

    @tool
    async def add_note(text: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        """Add a note."""
        return f"noted {text!r} for {tool_call_id}"

    engine = PromptGraphEngine(
        graph=_tool_node_graph(add_note),
        model=_Conversations({"Note hi": [[("add_note", {"text": "hi"})]]}),
    )

    messages = await _engine_run(engine, "Note hi")

    [message] = _tool_messages(messages)
    assert message.status == "success"
    assert message.content == f"noted 'hi' for {message.tool_call_id}"


# ---------------------------------------------------------------------------
# 4. The iteration-limit error names the tool of a tool_selector-only graph
# ---------------------------------------------------------------------------


async def test_a_selector_only_graph_names_its_tool_in_the_limit_error() -> None:
    job_status, calls = _job_tool()
    graph = PromptGraph("poll-forever", mode="static")
    graph.add_node(
        ToolNode(
            "poll",
            tools=[job_status],
            tool_selector=lambda state: ("job_status", {"job_id": "j1"}),
            default_next="poll",
            max_iterations=3,
        )
    )
    graph.set_entry("poll")
    engine = PromptGraphEngine(graph=graph, model=_Conversations({}))

    with pytest.raises(GraphIterationLimitError) as info:
        await engine.ainvoke(_ask("Wait for j1"))

    err = info.value
    assert len(calls) == 3
    assert (err.node_name, err.limit, err.pending_tools) == ("poll", 3, ["job_status"])
    assert "after a call to job_status whose result the model never saw" in str(err)
    assert "?" not in str(err)


async def test_a_selector_after_an_answer_names_the_selected_tool_not_an_earlier_one() -> None:
    lookup_user, _ = _users_tool()
    job_status, _ = _job_tool()

    @node("agent")
    async def agent(state: GraphState, config: dict[str, Any]) -> NodeResult:
        reply = await config["_engine_model"].ainvoke(state.messages)
        state.messages.append(reply)
        return NodeResult(
            node_name="agent",
            messages_added=[reply],
            next_node="tools" if reply.tool_calls else "poll",
        )

    graph = PromptGraph("answer-then-poll", mode="static")
    graph.add_node(agent)
    graph.add_node(ToolNode("tools", tools=[lookup_user], default_next="agent"))
    graph.add_node(
        ToolNode(
            "poll",
            tools=[job_status],
            tool_selector=lambda state: ("job_status", {"job_id": "j1"}),
            default_next="poll",
            max_iterations=2,
        )
    )
    graph.set_entry("agent")
    engine = PromptGraphEngine(
        graph=graph,
        model=_Conversations({"Who is u1?": [[("lookup_user", {"user_id": "u1"})]]}),
    )

    with pytest.raises(GraphIterationLimitError) as info:
        await engine.ainvoke(_ask("Who is u1?"))

    assert info.value.pending_tools == ["job_status"]
