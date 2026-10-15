"""Every ``python`` block on the Quick Start page runs as written.

The Quick Start is the first code a new user runs, so each block is executed
here with a scripted chat model standing in for the provider (no network, no
API key).  Only the model (plus Docker for ``code-action`` and the guardrail
classifier download) is swapped: ``build_agent`` itself is the real one,
so a missing or renamed argument fails here exactly as it would for a reader.

The "Add Tools" block is saved as ``tools.py`` and run as ``__main__``, so the
agent really launches the stdio server the way the page says.  The child
process imports a ``sitecustomize`` that makes ``build_agent`` raise: if the
example spawned its own agent again instead of the server, the connection
fails fast here instead of recursing.
"""

from __future__ import annotations

import ast
import asyncio
import re
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import promptise
from promptise.config import StdioServerSpec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scripted_model import Scripted  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "getting-started" / "quickstart.md"

# Names a block uses that an earlier block (or the reader) defines.
PLACEHOLDERS = {"my_servers": {}}


def _blocks() -> list[tuple[str, str]]:
    """``(heading, code)`` for every python fence, tabs dedented."""
    out: list[tuple[str, str]] = []
    heading = ""
    fence: list[str] | None = None
    indent = ""
    for line in PAGE.read_text(encoding="utf-8").splitlines(keepends=True):
        if fence is None:
            m = re.match(r"(#+) (.*?)\s*$", line)
            if m:
                heading = m.group(2)
            m = re.match(r"(\s*)```python\b", line)
            if m:
                fence, indent = [], m.group(1)
            continue
        if line.strip() == "```" and line.startswith(indent):
            out.append((heading, textwrap.dedent("".join(fence))))
            fence = None
            continue
        fence.append(line)
    return out


BLOCKS = _blocks()

_GUARD = """\
import promptise

async def _refuse(*args, **kwargs):
    raise RuntimeError("the stdio child built an agent instead of serving its tools")

promptise.build_agent = _refuse
"""


class _NoDockerSandbox:
    """Stands in for Docker, which ``agent_pattern="code-action"`` probes at build."""

    def __init__(self, config: Any) -> None:
        self.config = SimpleNamespace(backend="test")

    async def create_session(self) -> Any:
        async def cleanup() -> None:
            return None

        return SimpleNamespace(cleanup=cleanup)


@pytest.fixture
def fake_model(monkeypatch, tmp_path):
    """Swap the model for a scripted one; record each ``build_agent`` call."""
    real = promptise.build_agent
    built: list[dict[str, Any]] = []
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(_GUARD, encoding="utf-8")
    pythonpath = f"{guard}:{ROOT / 'src'}"

    async def build_agent(*args: Any, **kwargs: Any) -> Any:
        built.append(kwargs)
        servers = kwargs.get("servers")
        if servers:
            kwargs["servers"] = {
                name: spec.model_copy(update={"env": {**spec.env, "PYTHONPATH": pythonpath}})
                if isinstance(spec, StdioServerSpec)
                else spec
                for name, spec in servers.items()
            }
            kwargs["model"] = Scripted(mode="tool:get_weather", args={"city": "Berlin"})
        else:
            kwargs["model"] = Scripted()
        return await real(*args, **kwargs)

    monkeypatch.setattr(promptise, "build_agent", build_agent)
    monkeypatch.setattr("promptise.sandbox.SandboxManager", _NoDockerSandbox)
    # guardrails=True: a local classifier instead of a Hugging Face download.
    monkeypatch.setattr(
        "promptise.guardrails._load_classifier",
        lambda name: lambda texts: [{"label": "SAFE", "score": 0.99} for _ in texts],
    )
    monkeypatch.chdir(tmp_path)
    return built


def test_the_page_has_the_blocks_this_test_expects():
    headings = [h for h, _ in BLOCKS]
    assert headings.count("Your First Agent (30 seconds)") == 3
    assert "Add Tools (2 minutes)" in headings
    assert len(BLOCKS) >= 7


@pytest.mark.parametrize(
    "index", range(len(BLOCKS)), ids=[f"{i}-{h}" for i, (h, _) in enumerate(BLOCKS)]
)
def test_quickstart_block_runs(index, fake_model, tmp_path, capsys):
    heading, code = BLOCKS[index]
    # "build_agent(..., agent_pattern=...)" stands for "your usual arguments".
    code = code.replace("build_agent(...,", "build_agent(**_args,")
    (tmp_path / "tools.py").write_text(code, encoding="utf-8")
    ns: dict[str, Any] = {
        "__name__": "__main__",
        "__file__": str(tmp_path / "tools.py"),
        "build_agent": promptise.build_agent,
        "_args": {"model": "openai:gpt-4o-mini"},
        **PLACEHOLDERS,
    }
    compiled = compile(
        code, f"quickstart block {index}", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
    )
    result = eval(compiled, ns)  # noqa: S307 - the docs' own code
    if asyncio.iscoroutine(result):
        # A fragment with top-level ``await`` leaves its agent open; close it
        # in the same event loop.
        async def run_and_close() -> None:
            await result
            agent = ns.get("agent")
            if agent is not None:
                await agent.shutdown()

        asyncio.run(run_and_close())

    assert fake_model, f"block under {heading!r} never built an agent"
    if heading == "Add Tools (2 minutes)":
        # The agent reached the server and called its tool.
        assert "ANSWER: Sunny, 22°C in Berlin" in capsys.readouterr().out
