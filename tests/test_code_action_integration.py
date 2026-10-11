"""Integration tests for the code-action pattern — REAL Docker + REAL API.

Deselected by default (``addopts = -m 'not integration'``). Run with:

    pytest tests/test_code_action_integration.py -m integration -v

The bridge test needs only Docker. The full-agent test also needs
``OPENAI_API_KEY`` (real gpt-5-mini writes the program).
"""

from __future__ import annotations

import os

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(scope="session")
def docker_available():
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        pytest.skip("Docker not available")
        raise AssertionError("unreachable")  # pytest.skip raises; clarifies control flow


@pytest.fixture(scope="session")
def openai_available():
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")
    return True


# A canned model so the bridge/sandbox can be tested without spending API calls.
class _FixedModel:
    def __init__(self, program: str):
        self._program = program
        self.calls = 0

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        return AIMessage(content=f"```python\n{self._program}\n```")


# Shared company tools that return STRUCTURED data (ideal for code-action).
_EMP = {
    "Dana Cole": ("Executive", 300000),
    "Priya Anand": ("Engineering", 210000),
    "Alex Kim": ("Engineering", 140000),
    "Jo Park": ("Engineering", 135000),
    "Ravi Shah": ("Engineering", 150000),
    "Sam Ortiz": ("Finance", 195000),
    "Eva Lund": ("Finance", 125000),
    "Lin Wei": ("Analytics", 190000),
    "Mae Tan": ("Analytics", 130000),
}


@tool("list_employees")
def list_employees() -> list:
    """Return a list of every employee name."""
    return list(_EMP)


@tool("get_employee")
def get_employee(name: str) -> dict:
    """Return {name, department, salary} for an employee by exact name."""
    rec = _EMP.get(name)
    if rec is None:
        return {"error": f"no employee named {name!r}"}
    return {"name": name, "department": rec[0], "salary": rec[1]}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_sandbox_tool_bridge(docker_available):
    """REAL Docker: a fixed program calls bridged host tools and computes a sum.

    No API key needed — the model is canned, so this isolates the sandbox +
    filesystem-RPC bridge against a real container.
    """
    from promptise.engine import CodeActionNode
    from promptise.engine.state import GraphState
    from promptise.sandbox import NetworkMode, SandboxConfig, SandboxManager

    @tool("get_number")
    def get_number(name: str) -> int:
        """Return a number by name."""
        return {"alpha": 300, "beta": 335}[name]

    manager = SandboxManager(SandboxConfig(network=NetworkMode.NONE, timeout=120))

    async def factory():
        return await manager.create_session()

    program = "a = get_number('alpha')\nb = get_number('beta')\nprint('RESULT:', a + b)"
    node = CodeActionNode("reason", tools=[get_number], sandbox_factory=factory)
    state = GraphState(messages=[HumanMessage(content="sum alpha and beta")])

    try:
        result = await node.execute(state, {"_engine_model": _FixedModel(program)})
    finally:
        await manager.cleanup_all()

    assert result.error is None, result.error
    assert result.output == "635"
    # Both tool calls were bridged from inside the container back to the host.
    assert len(result.tool_calls) == 2
    assert any(isinstance(m, AIMessage) and m.content == "635" for m in state.messages)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_code_action_agent(docker_available, openai_available):
    """REAL Docker + REAL gpt-5-mini end-to-end via build_agent.

    The model writes ONE program that traverses the org data (list -> look up
    each -> filter Engineering -> sum) and prints the answer. Expected: 635000.
    """
    from promptise import build_agent

    agent = await build_agent(
        servers={},
        model="openai:gpt-5-mini",
        agent_pattern="code-action",
        extra_tools=[list_employees, get_employee],
        instructions="You answer questions about the company by writing a program.",
    )
    try:
        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "What is the combined annual salary of everyone in the "
                        "Engineering department?",
                    }
                ]
            }
        )
        answer = ""
        for msg in reversed(result["messages"]):
            if getattr(msg, "type", "") == "ai" and msg.content:
                answer = msg.content
                break
        assert "635000" in str(answer), f"got: {answer!r}"
    finally:
        await agent.shutdown()


# ──────────────────────────────────────────────────────────────────────────
# 1.3.3 regressions — REAL Docker, canned model (no API key)
# ──────────────────────────────────────────────────────────────────────────
def _sandbox_factory():
    from promptise.sandbox import NetworkMode, SandboxConfig, SandboxManager

    manager = SandboxManager(SandboxConfig(network=NetworkMode.NONE, timeout=120))

    async def factory():
        return await manager.create_session()

    return manager, factory


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_sandbox_failed_calls_are_capped(docker_available):
    """A program looping a failing tool 120 times under max_tool_calls=10: the
    tool runs 10 times, the program gets ToolBudgetExceeded, and the run fails
    instead of answering 120."""
    from promptise.engine import CodeActionNode
    from promptise.engine.code_action import _ToolBudget
    from promptise.engine.state import GraphState, NodeResult

    executed = []

    @tool("lookup")
    def lookup(key: str) -> str:
        """Look up a key."""
        executed.append(key)
        raise ValueError(f"no such key {key}")

    program = (
        "failed = 0\n"
        "refused = None\n"
        "for i in range(120):\n"
        "    try:\n"
        "        lookup(str(i))\n"
        "    except ToolBudgetExceeded as exc:\n"
        "        refused = i\n"
        "        break\n"
        "    except Exception:\n"
        "        failed += 1\n"
        "print('RESULT:', failed, refused)"
    )
    manager, factory = _sandbox_factory()
    node = CodeActionNode(
        "reason", tools=[lookup], sandbox_factory=factory, max_tool_calls=10, max_repairs=0
    )
    try:
        budget = _ToolBudget(10)
        cmd = await node._run_program(
            program, {"lookup": lookup}, GraphState(messages=[]), {}, NodeResult(), budget
        )
        assert cmd.stdout.strip().endswith("RESULT: 10 10"), cmd.stdout + cmd.stderr
        assert len(executed) == 10 and budget.exhausted

        executed.clear()
        result = await node.execute(
            GraphState(messages=[HumanMessage(content="go")]),
            {"_engine_model": _FixedModel(program)},
        )
    finally:
        await manager.cleanup_all()

    assert len(executed) == 10
    assert result.output == ""
    assert result.error and "max_tool_calls=10" in result.error


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_sandbox_mcp_json_results_arrive_as_data(docker_available):
    from mcp.types import CallToolResult, TextContent
    from pydantic import BaseModel

    from promptise.engine import CodeActionNode
    from promptise.engine.state import GraphState
    from promptise.mcp.client._tool_adapter import _PromptiseMCPTool

    class _Multi:
        async def call_tool(self, name, arguments, **kwargs):
            return CallToolResult(
                content=[TextContent(type="text", text='["SO-9001", "SO-9002"]')],
                isError=False,
            )

    class _Args(BaseModel):
        month: str

    list_orders = _PromptiseMCPTool(
        name="list_orders",
        description="List order ids for a month.",
        args_schema=_Args,
        tool_name="list_orders",
        multi=_Multi(),  # type: ignore[arg-type]
    )
    program = "ids = list_orders('2026-09')\nprint('RESULT:', type(ids).__name__, len(ids), ids[0])"
    manager, factory = _sandbox_factory()
    node = CodeActionNode("reason", tools=[list_orders], sandbox_factory=factory)
    try:
        result = await node.execute(
            GraphState(messages=[HumanMessage(content="go")]),
            {"_engine_model": _FixedModel(program)},
        )
    finally:
        await manager.cleanup_all()

    assert result.error is None, result.error
    assert result.output == "list 2 SO-9001"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_sandbox_cleanup_does_not_wait_for_a_graceful_stop(docker_available):
    """Cleanup took the full 5 s docker-stop timeout (bash as PID 1 ignores
    SIGTERM); a non-persistent session's container is now killed at once."""
    import time

    import docker

    manager, factory = _sandbox_factory()
    session = await factory()
    container_id = session.container_id
    try:
        start = time.perf_counter()
        await session.cleanup()
        elapsed = time.perf_counter() - start
    finally:
        await manager.cleanup_all()

    assert elapsed < 3, f"cleanup took {elapsed:.2f}s"
    with pytest.raises(docker.errors.NotFound):
        docker.from_env().containers.get(container_id)
