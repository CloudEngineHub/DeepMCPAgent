"""Offline unit tests for the code-action reasoning pattern.

No Docker and no API key required: an in-memory fake sandbox session and a
canned fake model exercise the full node orchestration (prompt -> code ->
write -> execute -> parse), the repair loop, the host-side tool bridge, the
pure helpers, and the prebuilt/agent wiring. The real Docker + real API paths
are covered separately in ``tests/test_code_action_integration.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from promptise.engine import CodeActionNode, PromptGraph
from promptise.engine.code_action import (
    _extract_task,
    _render_tools_module,
    extract_code,
    parse_result,
    render_api_spec,
)
from promptise.engine.state import GraphState
from promptise.sandbox.session import CommandResult


# ──────────────────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────────────────
class FakeModel:
    """Returns canned AIMessages (one per ainvoke), tracking call count."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls = 0

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        text = self._responses.pop(0) if self._responses else ""
        return AIMessage(content=text)


class FakeSession:
    """In-memory sandbox: supports mkdir, base64-write, cat, python3, ls."""

    def __init__(self, program_results: list[CommandResult]):
        self.fs: dict[str, str] = {}
        self.program_results = list(program_results)
        self.cleaned = False
        self.execs: list[str] = []

    async def execute(self, command: str, timeout=None, workdir=None) -> CommandResult:
        self.execs.append(command)
        if command.startswith("mkdir"):
            return CommandResult(0, "", "")
        if "| base64 -d >" in command:
            left, path = command.split("| base64 -d >")
            path = path.strip().strip("'")
            b64 = left.replace("printf %s", "").strip().strip("'")
            self.fs[path] = base64.b64decode(b64).decode()
            return CommandResult(0, "", "")
        if command.startswith("cat "):
            path = command[4:].strip().strip("'")
            if path in self.fs:
                return CommandResult(0, self.fs[path], "")
            return CommandResult(1, "", "No such file")
        if command.startswith("python3"):
            return self.program_results.pop(0)
        return CommandResult(0, "", "")

    async def write_file(self, path: str, content: str) -> None:
        self.execs.append(f"write {path}")
        self.fs[path] = content

    async def list_files(self, directory: str) -> list[str]:
        prefix = directory.rstrip("/") + "/"
        return [
            p[len(prefix) :]
            for p in self.fs
            if p.startswith(prefix) and "/" not in p[len(prefix) :]
        ]

    async def cleanup(self) -> None:
        self.cleaned = True


def _factory(session: FakeSession):
    async def make():
        return session

    return make


# ──────────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────────
def test_extract_code_from_fence():
    assert extract_code("blah\n```python\nprint(1)\n```\nmore") == "print(1)"
    assert extract_code("```\nx = 2\n```") == "x = 2"
    assert extract_code("no fence here") == "no fence here"


def test_parse_result_marker_and_fallback():
    assert parse_result("noise\nRESULT: 635\n", "RESULT:") == "635"
    assert parse_result("a\nRESULT: 1\nRESULT: 2\n", "RESULT:") == "2"  # last wins
    assert parse_result("only line", "RESULT:") == "only line"  # fallback
    assert parse_result("", "RESULT:") == ""


def test_render_api_spec_and_tools_module():
    @tool("get_employee")
    def get_employee(name: str) -> str:
        """Get an employee record by name."""
        return name

    spec = render_api_spec([get_employee])
    assert "def get_employee(name)" in spec
    assert "Get an employee record" in spec

    module = _render_tools_module([get_employee], call_timeout=10)
    assert "def _call(" in module
    assert "def get_employee(name):" in module
    assert "_call('get_employee', {'name': name})" in module
    # No data functions → graceful spec
    assert "no data functions" in render_api_spec([]).lower()


def test_extract_task_objects_and_dicts():
    s1 = GraphState(messages=[HumanMessage(content="What is 2+2?")])
    assert _extract_task(s1) == "What is 2+2?"
    s2 = GraphState(messages=[{"role": "user", "content": "hello"}])
    assert _extract_task(s2) == "hello"


# ──────────────────────────────────────────────────────────────────────────
# Node orchestration
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_happy_path_writes_runs_parses():
    session = FakeSession([CommandResult(0, "RESULT: 42\n", "")])
    model = FakeModel(["```python\nprint('RESULT:', 6 * 7)\n```"])
    node = CodeActionNode("reason", sandbox_factory=_factory(session))
    state = GraphState(messages=[HumanMessage(content="6 times 7?")])

    result = await node.execute(state, {"_engine_model": model})

    assert result.error is None
    assert result.output == "42"
    assert model.calls == 1
    # program + tools module were written into the sandbox
    assert "/workspace/program.py" in session.fs
    assert "from promptise_tools import *" in session.fs["/workspace/program.py"]
    # final answer appended as an AIMessage
    assert any(isinstance(m, AIMessage) and m.content == "42" for m in state.messages)
    assert session.cleaned is True


@pytest.mark.asyncio
async def test_repair_loop_recovers_from_crash():
    session = FakeSession(
        [
            CommandResult(1, "", "NameError: name 'x' is not defined"),  # first attempt crashes
            CommandResult(0, "RESULT: 7\n", ""),  # repaired attempt
        ]
    )
    model = FakeModel(
        [
            "```python\nprint(x)\n```",  # buggy
            "```python\nprint('RESULT:', 7)\n```",  # fixed
        ]
    )
    node = CodeActionNode("reason", sandbox_factory=_factory(session), max_repairs=1)
    state = GraphState(messages=[HumanMessage(content="give 7")])

    result = await node.execute(state, {"_engine_model": model})

    assert result.error is None
    assert result.output == "7"
    assert model.calls == 2  # one repair


@pytest.mark.asyncio
async def test_exhausted_repairs_reports_error():
    session = FakeSession(
        [
            CommandResult(1, "", "boom"),
            CommandResult(1, "", "boom again"),
        ]
    )
    model = FakeModel(["```python\nbad\n```", "```python\nstill bad\n```"])
    node = CodeActionNode("reason", sandbox_factory=_factory(session), max_repairs=1)
    state = GraphState(messages=[HumanMessage(content="x")])

    result = await node.execute(state, {"_engine_model": model})
    assert result.error is not None
    assert "failed" in result.error


@pytest.mark.asyncio
async def test_missing_model_and_missing_sandbox_error_cleanly():
    node_no_sandbox = CodeActionNode("reason", sandbox_factory=None)
    r1 = await node_no_sandbox.execute(
        GraphState(messages=[HumanMessage(content="x")]), {"_engine_model": FakeModel(["x"])}
    )
    assert r1.error and "sandbox" in r1.error.lower()

    node = CodeActionNode("reason", sandbox_factory=_factory(FakeSession([])))
    r2 = await node.execute(GraphState(messages=[HumanMessage(content="x")]), {})
    assert r2.error and "model" in r2.error.lower()


# ──────────────────────────────────────────────────────────────────────────
# Host-side tool bridge
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_bridge_services_a_tool_request():
    @tool("add")
    def add(a: int, b: int) -> str:
        """Add two numbers."""
        return str(a + b)

    session = FakeSession([])
    # Pre-seed a pending request as if the in-sandbox program wrote it.
    session.fs["/workspace/_rpc/req_abc.json"] = json.dumps(
        {"tool": "add", "args": {"a": 1, "b": 2}}
    )

    node = CodeActionNode("reason", tools=[add], sandbox_factory=_factory(session))
    state = GraphState(messages=[])
    from promptise.engine.state import NodeResult

    result = NodeResult(node_name="reason")
    stop = asyncio.Event()
    task = asyncio.create_task(node._bridge_loop(session, {"add": add}, state, {}, result, stop))
    # give the loop a few cycles to service the request, then stop
    for _ in range(50):
        if "/workspace/_rpc/resp_abc.done" in session.fs:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await task

    assert json.loads(session.fs["/workspace/_rpc/resp_abc.json"]) == {"result": "3"}
    assert state.observations and state.observations[0]["result"] == "3"
    assert result.tool_calls and result.tool_calls[0]["name"] == "add"


@pytest.mark.asyncio
async def test_bridge_unknown_tool_returns_error():
    session = FakeSession([])
    node = CodeActionNode("reason", sandbox_factory=_factory(session))
    from promptise.engine.state import NodeResult

    resp = await node._invoke_tool({}, GraphState(messages=[]), {}, "nope", {}, NodeResult())
    assert "error" in resp and "unknown tool" in resp["error"]


# ──────────────────────────────────────────────────────────────────────────
# Prebuilt + pattern wiring
# ──────────────────────────────────────────────────────────────────────────
def test_prebuilt_builds_code_action_graph():
    g = PromptGraph.code_action(tools=[], system_prompt="hi")
    assert g.name == "code-action"
    node = g.get_node("reason")
    assert isinstance(node, CodeActionNode)
    assert node.default_next == "__end__"


# ──────────────────────────────────────────────────────────────────────────
# Security: hard tool-call cap + bridge request-id validation
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_max_tool_calls_cap_is_enforced():
    @tool("ping")
    def ping() -> str:
        """Return pong."""
        return "pong"

    from promptise.engine.state import NodeResult

    node = CodeActionNode("reason", sandbox_factory=_factory(FakeSession([])), max_tool_calls=2)
    result = NodeResult()
    state = GraphState(messages=[])
    r1 = await node._invoke_tool({"ping": ping}, state, {}, "ping", {}, result)
    r2 = await node._invoke_tool({"ping": ping}, state, {}, "ping", {}, result)
    r3 = await node._invoke_tool({"ping": ping}, state, {}, "ping", {}, result)  # over the cap

    assert r1 == {"result": "pong"} and r2 == {"result": "pong"}
    assert "error" in r3 and "budget" in r3["error"]
    assert len(result.tool_calls) == 2  # the 3rd call never executed the tool


def test_safe_rid_regex_blocks_traversal():
    from promptise.engine.code_action import _SAFE_RID

    assert _SAFE_RID.match("a1b2c3deadbeef")
    assert _SAFE_RID.match("abc-123_DEF")
    assert not _SAFE_RID.match("..")
    assert not _SAFE_RID.match("a/b")
    assert not _SAFE_RID.match("a b")
    assert not _SAFE_RID.match("")
    assert not _SAFE_RID.match("x" * 65)  # too long


@pytest.mark.asyncio
async def test_bridge_ignores_unsafe_request_id():
    session = FakeSession([])
    # A request file whose id contains a space — must NOT be serviced.
    session.fs["/workspace/_rpc/req_bad id.json"] = json.dumps({"tool": "x", "args": {}})
    node = CodeActionNode("reason", sandbox_factory=_factory(session))
    from promptise.engine.state import NodeResult

    result = NodeResult()
    stop = asyncio.Event()
    task = asyncio.create_task(
        node._bridge_loop(session, {}, GraphState(messages=[]), {}, result, stop)
    )
    await asyncio.sleep(0.15)
    stop.set()
    await task

    # No response was written for the unsafe id, and no tool was invoked.
    assert not any(k.startswith("/workspace/_rpc/resp_") for k in session.fs)
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_post_tool_hook_gets_the_result_and_can_rewrite_it():
    """post_tool(tool_name, result, args, state): the program sees what it returns."""

    @tool
    def lookup(customer_id: str) -> dict:
        """Look up a customer."""
        return {"id": customer_id, "email": "alice@example.com"}

    from promptise.engine.state import NodeResult

    seen: dict = {}

    class Redact:
        async def post_tool(self, tool_name, result, args, state):
            seen.update(tool_name=tool_name, result=result, args=args)
            return result.replace("alice@example.com", "[EMAIL]")

    node = CodeActionNode("reason", sandbox_factory=_factory(FakeSession([])))
    result = NodeResult()
    state = GraphState(messages=[])
    out = await node._invoke_tool(
        {"lookup": lookup},
        state,
        {"_engine_hooks": [Redact()]},
        "lookup",
        {"customer_id": "c1"},
        result,
    )

    assert seen["tool_name"] == "lookup"
    assert seen["args"] == {"customer_id": "c1"}
    assert json.loads(seen["result"]) == {"id": "c1", "email": "alice@example.com"}
    assert out == {"result": {"id": "c1", "email": "[EMAIL]"}}
    assert "alice@example.com" not in result.tool_calls[0]["result"]
    assert "alice@example.com" not in state.observations[-1]["result"]


# ──────────────────────────────────────────────────────────────────────────
# 1.3.3: the tool-call budget counts every call and is never silent; MCP JSON
# results arrive as data; the report counts the calls
# ──────────────────────────────────────────────────────────────────────────
class _BudgetError(RuntimeError):
    """Stands in for the in-sandbox ToolBudgetExceeded."""


class BridgingSession(FakeSession):
    """A FakeSession whose ``python3`` run executes a scripted async program.

    The program calls tools through the same filesystem RPC the real
    in-sandbox client uses (request file, then wait for the ``.done``
    marker), so the node's real bridge loop services the calls.
    """

    def __init__(self, programs):
        super().__init__([])
        self.programs = list(programs)

    async def call(self, tool_name, args):
        import uuid

        rid = uuid.uuid4().hex
        self.fs[f"/workspace/_rpc/req_{rid}.json"] = json.dumps({"tool": tool_name, "args": args})
        while f"/workspace/_rpc/resp_{rid}.done" not in self.fs:
            await asyncio.sleep(0.002)
        data = json.loads(self.fs[f"/workspace/_rpc/resp_{rid}.json"])
        if data.get("budget_exceeded"):
            raise _BudgetError(data["error"])
        if "error" in data:
            raise RuntimeError(data["error"])
        return data["result"]

    async def execute(self, command: str, timeout=None, workdir=None) -> CommandResult:
        if command.startswith("python3"):
            self.execs.append(command)
            program = self.programs.pop(0)
            try:
                return CommandResult(0, await program(self.call), "")
            except Exception as exc:  # an uncaught error crashes the program
                return CommandResult(1, "", f"Traceback ...\n{type(exc).__name__}: {exc}")
        return await super().execute(command, timeout=timeout, workdir=workdir)

    async def cleanup(self) -> None:
        # Each run gets a fresh container: forget the previous run's files.
        await super().cleanup()
        self.fs.clear()


def _calls_then_print(count, tool_name="price", catch=True):
    """A program that calls *tool_name* *count* times and prints the sum."""

    async def program(call):
        total = 0
        for i in range(count):
            try:
                total += await call(tool_name, {"sku": str(i)})
            except Exception:
                if not catch:
                    raise
        return f"RESULT: {total}\n"

    return program


@pytest.mark.asyncio
async def test_failed_calls_count_against_max_tool_calls():
    """A failing tool used to be callable without limit: only successful calls
    were counted against max_tool_calls."""
    executed = []

    @tool
    def lookup(key: str) -> str:
        """Look up a key."""
        executed.append(key)
        raise ValueError(f"no such key {key}")

    from promptise.engine.state import NodeResult

    node = CodeActionNode("reason", sandbox_factory=_factory(FakeSession([])), max_tool_calls=3)
    result = NodeResult()
    state = GraphState(messages=[])
    responses = [
        await node._invoke_tool({"lookup": lookup}, state, {}, "lookup", {"key": str(i)}, result)
        for i in range(10)
    ]

    assert len(executed) == 3  # the 4th..10th calls never reached the tool
    assert all("ValueError" in r["error"] for r in responses[:3])
    assert all(r.get("budget_exceeded") for r in responses[3:])
    # The program is told the cap was reached, not its value (1.3.5).
    assert "budget exhausted" in responses[3]["error"]
    assert "3" not in responses[3]["error"]
    assert result.tool_calls_failed == 3


@pytest.mark.asyncio
async def test_unknown_tool_calls_count_against_the_budget():
    from promptise.engine.code_action import _ToolBudget
    from promptise.engine.state import NodeResult

    node = CodeActionNode("reason", sandbox_factory=_factory(FakeSession([])), max_tool_calls=2)
    budget = _ToolBudget(2)
    result = NodeResult()
    state = GraphState(messages=[])
    for _ in range(3):
        resp = await node._invoke_tool({}, state, {}, "nope", {}, result, budget)
    assert budget.used == 2 and budget.exhausted
    assert resp.get("budget_exceeded")


@pytest.mark.asyncio
async def test_a_program_that_reaches_the_budget_is_repaired_with_a_fresh_budget(caplog):
    """The program catches the budget error and prints a wrong total: that must
    not be the answer. The repair is told why and gets its own budget."""
    executed = []

    @tool
    def price(sku: str) -> int:
        """Get a price."""
        executed.append(sku)
        return 10

    session = BridgingSession([_calls_then_print(6), _calls_then_print(3)])
    model = FakeModel(["```python\n# loop 6\n```", "```python\n# loop 3\n```"])
    node = CodeActionNode(
        "reason", tools=[price], sandbox_factory=_factory(session), max_tool_calls=3
    )
    state = GraphState(messages=[HumanMessage(content="sum the prices")])

    with caplog.at_level("WARNING", logger="promptise.engine.code_action"):
        result = await node.execute(state, {"_engine_model": model})

    assert result.error is None
    assert result.output == "30"  # the repaired program's answer, not the first's
    assert model.calls == 2
    assert len(executed) == 6  # 3 per program: each program had its own budget
    assert any("max_tool_calls=3" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_budget_exhausted_with_no_repairs_left_fails_the_run():
    """An engine run whose last program reached the budget raises; it is never
    reported as a successful answer."""
    from promptise.engine import GraphExecutionError, PromptGraphEngine

    @tool
    def price(sku: str) -> int:
        """Get a price."""
        return 10

    session = BridgingSession([_calls_then_print(5)])
    graph = PromptGraph.code_action(
        tools=[price], sandbox_factory=_factory(session), max_tool_calls=2, max_repairs=0
    )
    engine = PromptGraphEngine(graph=graph, model=FakeModel(["```python\nloop\n```"]))

    with pytest.raises(GraphExecutionError, match="max_tool_calls=2") as info:
        await engine.ainvoke({"messages": [{"role": "user", "content": "go"}]})
    assert engine.last_report.error and "tool-call budget" in engine.last_report.error
    # 1.3.5: a dedicated subclass, so callers can tell "needs a bigger budget"
    # from a crash.
    from promptise.engine import CodeActionBudgetError

    assert isinstance(info.value, CodeActionBudgetError)
    assert info.value.max_tool_calls == 2 and info.value.attempts == 1


@pytest.mark.asyncio
async def test_a_crash_after_the_budget_is_reported_as_the_budget():
    @tool
    def price(sku: str) -> int:
        """Get a price."""
        return 10

    session = BridgingSession([_calls_then_print(5, catch=False)])
    node = CodeActionNode(
        "reason",
        tools=[price],
        sandbox_factory=_factory(session),
        max_tool_calls=2,
        max_repairs=0,
    )
    result = await node.execute(
        GraphState(messages=[HumanMessage(content="go")]), {"_engine_model": FakeModel(["x"])}
    )
    assert result.output == ""
    assert "tool-call budget (max_tool_calls=2)" in result.error


@pytest.mark.asyncio
async def test_report_counts_code_action_tool_calls():
    """ExecutionReport.tool_calls was always 0 for code-action runs."""
    from promptise.engine import PromptGraphEngine

    @tool
    def price(sku: str) -> int:
        """Get a price."""
        if sku == "2":
            raise ValueError("unknown sku")
        return 10

    session = BridgingSession([_calls_then_print(4)])
    graph = PromptGraph.code_action(tools=[price], sandbox_factory=_factory(session))
    engine = PromptGraphEngine(graph=graph, model=FakeModel(["```python\nloop\n```"]))
    out = await engine.ainvoke({"messages": [{"role": "user", "content": "go"}]})

    assert out["messages"][-1].content == "30"
    assert engine.last_report.tool_calls == 4  # 3 successful + 1 failed
    assert engine.last_report.error is None


# ── MCP results ──────────────────────────────────────────────────────────
class _TextMulti:
    """Stands in for MCPMultiClient: every call returns the given text."""

    def __init__(self, text: str, *, is_error: bool = False):
        self.text = text
        self.is_error = is_error

    async def call_tool(self, name, arguments, **kwargs):
        from mcp.types import CallToolResult, TextContent

        return CallToolResult(
            content=[TextContent(type="text", text=self.text)], isError=self.is_error
        )


def _mcp_tool(text: str, *, is_error: bool = False):
    from pydantic import BaseModel

    from promptise.mcp.client._tool_adapter import _PromptiseMCPTool

    class _Args(BaseModel):
        month: str

    return _PromptiseMCPTool(
        name="list_orders",
        description="List orders.",
        args_schema=_Args,
        tool_name="list_orders",
        multi=_TextMulti(text, is_error=is_error),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('["SO-1", "SO-2"]', ["SO-1", "SO-2"]),
        ('{"sku": "KB-101", "unit_price": 89}', {"sku": "KB-101", "unit_price": 89}),
        ("42", 42),
        ("12.5", 12.5),
        ("shipped", "shipped"),  # not JSON: stays text
        ("line one\nline two", "line one\nline two"),
    ],
)
async def test_mcp_tool_json_text_reaches_the_program_as_data(text, expected):
    from promptise.engine.state import NodeResult

    tool_ = _mcp_tool(text)
    node = CodeActionNode("reason", sandbox_factory=_factory(FakeSession([])))
    result = NodeResult()
    state = GraphState(messages=[])
    resp = await node._invoke_tool(
        {"list_orders": tool_}, state, {}, "list_orders", {"month": "2026-09"}, result
    )
    assert resp == {"result": expected}
    # The record keeps the server's text.
    assert result.tool_calls[0]["result"] == text
    assert state.observations[-1]["result"] == text


@pytest.mark.asyncio
async def test_mcp_tool_behind_a_wrapper_is_parsed_too():
    from promptise.agent import _TracedTool
    from promptise.engine.state import NodeResult

    inner = _mcp_tool('{"total": 3}')
    wrapped = _TracedTool(inner, on_before=None, on_after=None, on_error=None)
    node = CodeActionNode("reason", sandbox_factory=_factory(FakeSession([])))
    resp = await node._invoke_tool(
        {"list_orders": wrapped},
        GraphState(messages=[]),
        {},
        "list_orders",
        {"month": "2026-09"},
        NodeResult(),
    )
    assert resp == {"result": {"total": 3}}


@pytest.mark.asyncio
async def test_mcp_tool_errors_reach_the_program_as_errors():
    from promptise.engine.code_action import _parse_mcp_text
    from promptise.engine.state import NodeResult
    from promptise.mcp.client import MCPToolError

    envelope = '{"error": {"code": "NOT_FOUND", "message": "no order SO-9"}}'
    node = CodeActionNode("reason", sandbox_factory=_factory(FakeSession([])))
    result = NodeResult()
    resp = await node._invoke_tool(
        {"list_orders": _mcp_tool(envelope)},
        GraphState(messages=[]),
        {},
        "list_orders",
        {"month": "x"},
        result,
    )
    assert "MCPToolError" in resp["error"] and "no order SO-9" in resp["error"]
    assert result.tool_calls_failed == 1 and result.tool_calls == []

    # An envelope that reaches the node as text (a tool that handles its own
    # errors) is an error as well, not a dict the program would misread.
    with pytest.raises(MCPToolError, match="no order SO-9"):
        _parse_mcp_text("list_orders", envelope)


@pytest.mark.asyncio
async def test_python_tool_string_results_are_not_parsed():
    """Only MCP tools carry data as JSON text; a Python tool's str stays a str."""
    from promptise.engine.state import NodeResult

    @tool
    def code() -> str:
        """Return a code."""
        return "007"

    node = CodeActionNode("reason", sandbox_factory=_factory(FakeSession([])))
    resp = await node._invoke_tool(
        {"code": code}, GraphState(messages=[]), {}, "code", {}, NodeResult()
    )
    assert resp == {"result": "007"}


# ──────────────────────────────────────────────────────────────────────────
# 1.3.5: a job that needs more calls than max_tool_calls fails; it is never
# answered from part of the data
# ──────────────────────────────────────────────────────────────────────────
class RecordingModel(FakeModel):
    """A FakeModel that keeps every message list it was sent."""

    def __init__(self, responses):
        super().__init__(responses)
        self.prompts = []

    async def ainvoke(self, messages, config=None):
        self.prompts.append(messages)
        return await super().ainvoke(messages, config)


@tool
def price(sku: str) -> int:
    """Get a price."""
    return 10


def _engine(programs, responses, **graph_kwargs):
    from promptise.engine import PromptGraphEngine

    session = BridgingSession(programs)
    graph = PromptGraph.code_action(
        tools=[price], sandbox_factory=_factory(session), **graph_kwargs
    )
    model = RecordingModel(responses)
    return PromptGraphEngine(graph=graph, model=model), model, session


def test_the_system_prompt_does_not_invite_fitting_the_data_to_the_cap():
    """1.3.4 said "may make at most N function calls ... so plan the calls you
    need": models wrote their own counter (`if calls >= 50: break`) or sliced
    the data (`orders[:49]`), and the partial result was the answer."""
    node = CodeActionNode("reason", max_tool_calls=37)
    system = node._build_prompt("q", [price])[0].content

    assert "37" not in system  # the value is not given, so it cannot be fitted to
    assert "at most" not in system and "plan the calls" not in system
    assert "Never skip, sample or truncate records" in system
    assert "never count your own function calls" in system
    assert "ToolBudgetExceeded" in system and "let it raise" in system


def test_without_a_cap_the_prompt_does_not_mention_one():
    system = CodeActionNode("reason", max_tool_calls=0)._build_prompt("q", [price])[0].content
    assert "ToolBudgetExceeded" not in system and "cap" not in system


@pytest.mark.asyncio
async def test_the_repair_after_a_budget_hit_asks_for_the_whole_job():
    """1.3.4 asked for "a program that needs at most N calls", and the model
    wrote one that read only the first N records."""
    from promptise.engine.code_action import BUDGET_GIVE_UP

    engine, model, _ = _engine(
        [_calls_then_print(40, catch=False), _calls_then_print(3)],
        ["```python\nA\n```", "```python\nB\n```"],
        max_tool_calls=37,
    )
    out = await engine.ainvoke({"messages": [{"role": "user", "content": "go"}]})

    assert out["messages"][-1].content == "30"
    feedback = model.prompts[1][-1].content
    assert "WHOLE job with fewer calls" in feedback
    assert "Do NOT skip, sample or truncate records" in feedback
    assert BUDGET_GIVE_UP in feedback
    assert "at most" not in feedback
    # Neither the message nor the program's own error output (fed back too)
    # gives the cap's value.
    assert "37" not in feedback
    assert "ToolBudgetExceeded" in feedback


@pytest.mark.asyncio
async def test_a_model_that_cannot_fit_the_job_ends_the_run_with_an_error():
    from promptise.engine import CodeActionBudgetError, GraphExecutionError
    from promptise.engine.code_action import BUDGET_GIVE_UP, CallBudgetFailure

    engine, model, session = _engine(
        [_calls_then_print(5)],  # catches the budget error and prints a partial sum
        ["```python\nA\n```", BUDGET_GIVE_UP],
        max_tool_calls=2,
        max_repairs=2,
    )
    with pytest.raises(CodeActionBudgetError, match="max_tool_calls=2") as info:
        await engine.ainvoke({"messages": [{"role": "user", "content": "go"}]})

    err = info.value
    assert isinstance(err, GraphExecutionError)  # old handlers still catch it
    assert isinstance(err.__cause__, CallBudgetFailure)
    assert err.max_tool_calls == 2 and err.attempts == 2
    assert "cannot be done in fewer calls" in err.error
    assert model.calls == 2  # no further repair after the model gave up
    assert session.programs == []  # the give-up reply was not run as a program
    assert len([c for c in session.execs if c.startswith("python3")]) == 1


@pytest.mark.asyncio
async def test_the_give_up_line_inside_a_fence_is_recognised():
    from promptise.engine import CodeActionBudgetError
    from promptise.engine.code_action import BUDGET_GIVE_UP

    engine, model, _ = _engine(
        [_calls_then_print(5, catch=False)],
        ["```python\nA\n```", f"```\n{BUDGET_GIVE_UP}\n```"],
        max_tool_calls=2,
    )
    with pytest.raises(CodeActionBudgetError):
        await engine.ainvoke({"messages": [{"role": "user", "content": "go"}]})
    assert model.calls == 2


@pytest.mark.asyncio
async def test_every_program_over_the_cap_ends_with_a_budget_error():
    from promptise.engine import CodeActionBudgetError

    engine, model, _ = _engine(
        [_calls_then_print(9), _calls_then_print(9), _calls_then_print(9)],
        ["```python\nA\n```", "```python\nB\n```", "```python\nC\n```"],
        max_tool_calls=4,
        max_repairs=2,
    )
    with pytest.raises(CodeActionBudgetError) as info:
        await engine.ainvoke({"messages": [{"role": "user", "content": "go"}]})
    assert info.value.attempts == 3 and model.calls == 3
    assert engine.last_report.error and "max_tool_calls=4" in engine.last_report.error


@pytest.mark.asyncio
async def test_a_crash_is_still_a_plain_execution_error():
    from promptise.engine import CodeActionBudgetError, GraphExecutionError

    async def crash(call):
        raise ZeroDivisionError("boom")

    engine, _, _ = _engine([crash], ["```python\nA\n```"], max_repairs=0)
    with pytest.raises(GraphExecutionError, match="ZeroDivisionError") as info:
        await engine.ainvoke({"messages": [{"role": "user", "content": "go"}]})
    assert not isinstance(info.value, CodeActionBudgetError)


@pytest.mark.asyncio
async def test_the_give_up_line_is_only_honoured_after_a_budget_hit():
    """Before any program reached the cap, the line is just a broken program
    (it crashes and is repaired like any other)."""
    from promptise.engine.code_action import BUDGET_GIVE_UP

    async def name_error(call):
        raise NameError(f"name {BUDGET_GIVE_UP!r} is not defined")

    engine, model, _ = _engine(
        [name_error, _calls_then_print(2)],
        [BUDGET_GIVE_UP, "```python\nB\n```"],
        max_tool_calls=5,
    )
    out = await engine.ainvoke({"messages": [{"role": "user", "content": "go"}]})
    assert out["messages"][-1].content == "20" and model.calls == 2


@pytest.mark.asyncio
async def test_a_program_within_the_cap_answers_normally(caplog):
    engine, model, _ = _engine([_calls_then_print(4)], ["```python\nA\n```"], max_tool_calls=4)
    with caplog.at_level("WARNING", logger="promptise.engine.code_action"):
        out = await engine.ainvoke({"messages": [{"role": "user", "content": "go"}]})

    assert out["messages"][-1].content == "40"
    assert model.calls == 1
    assert engine.last_report.error is None and engine.last_report.tool_calls == 4
    assert not caplog.records


@pytest.mark.asyncio
async def test_an_answer_after_a_budget_hit_is_logged(caplog):
    engine, _, _ = _engine(
        [_calls_then_print(9), _calls_then_print(2)],
        ["```python\nA\n```", "```python\nB\n```"],
        max_tool_calls=4,
    )
    with caplog.at_level("WARNING", logger="promptise.engine.code_action"):
        await engine.ainvoke({"messages": [{"role": "user", "content": "go"}]})
    assert any(
        "after 1 earlier program(s) reached max_tool_calls=4" in r.getMessage()
        for r in caplog.records
    )
