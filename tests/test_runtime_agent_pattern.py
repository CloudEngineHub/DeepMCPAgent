"""A runtime process chooses its reasoning pattern: ``ProcessConfig(agent_pattern=...)``
and the ``.agent`` manifest's ``agent_pattern:`` reach :func:`build_agent`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from promptise.engine import PromptGraph
from promptise.engine.reasoning_nodes import ThinkNode
from promptise.runtime import AgentProcess, ProcessConfig
from promptise.runtime.manifest import (
    AgentManifestSchema,
    load_manifest,
    manifest_to_process_config,
)
from promptise.testing import ScriptedModel


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _graph_nodes(process: AgentProcess) -> list[str]:
    return list(process._agent._inner.graph._nodes)


class TestProcessConfigAgentPattern:
    def test_default_is_none(self) -> None:
        assert ProcessConfig().agent_pattern is None

    @pytest.mark.parametrize("name", ["react", "peoatr", "research", "debate", "code-action"])
    def test_accepts_every_pattern_name(self, name: str) -> None:
        assert ProcessConfig(agent_pattern=name).agent_pattern == name

    def test_unknown_name_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="Unknown agent_pattern 'peoatrr'"):
            ProcessConfig(agent_pattern="peoatrr")

    def test_pipeline_name_explains_what_to_pass(self) -> None:
        with pytest.raises(ValidationError, match="PromptGraph.pipeline"):
            ProcessConfig(agent_pattern="pipeline")

    def test_non_pattern_type_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="pattern name such as 'peoatr'"):
            ProcessConfig(agent_pattern=42)

    def test_name_is_serialised(self) -> None:
        config = ProcessConfig(agent_pattern="peoatr")
        assert config.model_dump()["agent_pattern"] == "peoatr"
        assert ProcessConfig.model_validate(config.model_dump()).agent_pattern == "peoatr"

    def test_graph_is_code_only(self) -> None:
        graph = PromptGraph("mine", nodes=[ThinkNode("think", is_terminal=True)])
        config = ProcessConfig(agent_pattern=graph)
        assert config.agent_pattern is graph
        assert "agent_pattern" not in config.model_dump()
        assert "agent_pattern" not in config.model_dump(mode="json")


class TestProcessBuildsThePattern:
    async def test_process_builds_its_agent_with_the_pattern(self) -> None:
        process = AgentProcess(
            "planner",
            ProcessConfig(model=ScriptedModel(["done"]), agent_pattern="peoatr"),
        )
        await process.start()
        try:
            assert process._agent._inner.graph.name == "peoatr"
            assert _graph_nodes(process) == ["plan", "act", "think", "reflect", "answer"]
        finally:
            await process.stop()

    async def test_default_is_still_react(self) -> None:
        process = AgentProcess("plain", ProcessConfig(model=ScriptedModel(["done"])))
        await process.start()
        try:
            assert _graph_nodes(process) == ["reason"]
        finally:
            await process.stop()

    async def test_pattern_is_passed_to_build_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import promptise.agent as agent_module

        seen: list[Any] = []
        real = agent_module.build_agent

        async def spy(**kwargs: Any) -> Any:
            seen.append(kwargs.get("agent_pattern"))
            return await real(**kwargs)

        monkeypatch.setattr(agent_module, "build_agent", spy)
        process = AgentProcess(
            "researcher", ProcessConfig(model=ScriptedModel(["done"]), agent_pattern="research")
        )
        await process.start()
        try:
            # A rebuild for new instructions keeps the pattern.
            process._instructions_changed = True
            await process._apply_instruction_change()
        finally:
            await process.stop()
        assert seen == ["research", "research"]

    async def test_graph_object_is_passed_through(self) -> None:
        graph = PromptGraph("mine", nodes=[ThinkNode("think", is_terminal=True)])
        process = AgentProcess(
            "custom", ProcessConfig(model=ScriptedModel(["done"]), agent_pattern=graph)
        )
        await process.start()
        try:
            assert process._agent._inner.graph is graph
        finally:
            await process.stop()


class TestManifestAgentPattern:
    def test_top_level_key(self, tmp_path: Path) -> None:
        path = tmp_path / "planner.agent"
        path.write_text(
            "version: '1.0'\nname: planner\nmodel: openai:gpt-5-mini\nagent_pattern: peoatr\n",
            encoding="utf-8",
        )
        config = manifest_to_process_config(load_manifest(path))
        assert config.agent_pattern == "peoatr"

    def test_config_block_key(self) -> None:
        manifest = AgentManifestSchema(name="planner", config={"agent_pattern": "verify"})
        assert manifest_to_process_config(manifest).agent_pattern == "verify"

    def test_unset_runs_the_default(self) -> None:
        manifest = AgentManifestSchema(name="plain")
        assert manifest_to_process_config(manifest).agent_pattern is None

    def test_unknown_name_fails_validation(self) -> None:
        with pytest.raises(ValidationError, match="Unknown agent_pattern"):
            AgentManifestSchema(name="planner", agent_pattern="plan-and-execute")

    def test_set_twice_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="also set by the manifest"):
            AgentManifestSchema(
                name="planner", agent_pattern="peoatr", config={"agent_pattern": "react"}
            )
