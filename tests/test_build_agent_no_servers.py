"""``build_agent`` without MCP servers, and the "no tools" notice.

``servers`` used to be a required keyword, so the Quick Start's first agent
(``build_agent(model=..., instructions=...)``) raised ``TypeError``.  An agent
with no tools also printed a notice to stdout on every build, which landed in
the application's output (once per mission evaluation, for example).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from promptise import build_agent
from promptise.config import StdioServerSpec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scripted_model import Scripted  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src"


async def test_servers_is_optional(capsys):
    agent = await build_agent(model=Scripted(), instructions="Be concise.")
    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "hi"}]})
        assert result["messages"][-1].content == "ok"
    finally:
        await agent.shutdown()
    assert capsys.readouterr().out == ""


async def test_empty_servers_writes_nothing_to_stdout(capsys, caplog):
    caplog.set_level(logging.DEBUG, logger="promptise.agent")
    agent = await build_agent(servers={}, model=Scripted())
    await agent.shutdown()
    assert capsys.readouterr().out == ""
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_configured_servers_without_tools_log_a_warning(tmp_path, capsys, caplog):
    script = tmp_path / "empty_server.py"
    script.write_text(
        "from promptise.mcp.server import MCPServer\nMCPServer('empty').run(transport='stdio')\n",
        encoding="utf-8",
    )
    spec = StdioServerSpec(command=sys.executable, args=[str(script)], env={"PYTHONPATH": str(SRC)})
    caplog.set_level(logging.WARNING, logger="promptise.agent")
    agent = await build_agent(servers={"empty": spec}, model=Scripted())
    await agent.shutdown()
    assert capsys.readouterr().out == ""
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("No tools discovered from MCP servers empty" in w for w in warnings), warnings
