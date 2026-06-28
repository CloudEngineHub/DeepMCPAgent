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
    "Dana Cole": ("Executive", 300000), "Priya Anand": ("Engineering", 210000),
    "Alex Kim": ("Engineering", 140000), "Jo Park": ("Engineering", 135000),
    "Ravi Shah": ("Engineering", 150000), "Sam Ortiz": ("Finance", 195000),
    "Eva Lund": ("Finance", 125000), "Lin Wei": ("Analytics", 190000),
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

    program = (
        "a = get_number('alpha')\n"
        "b = get_number('beta')\n"
        "print('RESULT:', a + b)"
    )
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
            {"messages": [{"role": "user", "content":
                "What is the combined annual salary of everyone in the "
                "Engineering department?"}]}
        )
        answer = ""
        for msg in reversed(result["messages"]):
            if getattr(msg, "type", "") == "ai" and msg.content:
                answer = msg.content
                break
        assert "635000" in str(answer), f"got: {answer!r}"
    finally:
        await agent.shutdown()
