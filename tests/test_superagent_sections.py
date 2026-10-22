"""``.superagent`` sections that map to build_agent objects: optimize_tools,
cache and adaptive."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from promptise.exceptions import SuperAgentValidationError
from promptise.superagent import SuperAgentLoader
from promptise.tool_optimization import OptimizationLevel, ToolOptimizationConfig, _resolve_config

BASE = """version: "1.0"
agent:
  model: "openai:gpt-5-mini"
servers:
  test:
    type: http
    url: "http://test"
"""


def _kwargs(tmp_path: Path, extra: str) -> dict:
    path = tmp_path / "agent.superagent"
    path.write_text(BASE + extra)
    return SuperAgentLoader.from_file(path).to_agent_config().to_build_kwargs()


# ---------------------------------------------------------------------------
# optimize_tools
# ---------------------------------------------------------------------------


def test_optimize_tools_mapping_is_honoured(tmp_path: Path) -> None:
    kwargs = _kwargs(
        tmp_path,
        """optimize_tools:
  level: semantic
  top_k: 4
  context_turns: 2
  embedding_model: /models/minilm
  max_description_length: 80
  preserve_tools: [search]
""",
    )
    config = kwargs["optimize_tools"]
    assert isinstance(config, ToolOptimizationConfig)
    resolved = _resolve_config(config)
    assert resolved.semantic_selection is True
    assert resolved.semantic_top_k == 4
    assert resolved.semantic_context_turns == 2
    assert resolved.embedding_model == "/models/minilm"
    assert resolved.max_description_length == 80
    assert resolved.preserve_tools == frozenset({"search"})


def test_optimize_tools_mapping_defaults_to_the_level_preset(tmp_path: Path) -> None:
    config = _kwargs(tmp_path, "optimize_tools:\n  level: standard\n")["optimize_tools"]
    assert config.level is OptimizationLevel.STANDARD
    resolved = _resolve_config(config)
    assert resolved.semantic_selection is False
    assert resolved.max_schema_depth == 3


@pytest.mark.parametrize("value", ["true", "minimal", "standard", "semantic"])
def test_optimize_tools_scalars_pass_through(tmp_path: Path, value: str) -> None:
    expected = True if value == "true" else value
    assert _kwargs(tmp_path, f"optimize_tools: {value}\n")["optimize_tools"] == expected


def test_unknown_level_is_a_validation_error(tmp_path: Path) -> None:
    with pytest.raises(SuperAgentValidationError, match="optimize_tools"):
        _kwargs(tmp_path, "optimize_tools: aggressive\n")


def test_score_threshold_is_accepted_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="promptise.superagent")
    _kwargs(tmp_path, "optimize_tools:\n  level: semantic\n  score_threshold: 0.3\n")
    assert "score_threshold has no effect" in caplog.text


def test_schema_description_matches_true_meaning() -> None:
    from promptise.superagent_schema import SuperAgentSchema

    description = SuperAgentSchema.model_fields["optimize_tools"].description or ""
    assert "true (the 'minimal' level)" in description


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def test_cache_mapping_builds_a_semantic_cache(tmp_path: Path) -> None:
    from promptise.cache import SemanticCache

    cache = _kwargs(
        tmp_path,
        """cache:
  backend: memory
  similarity_threshold: 0.8
  default_ttl: 60
  scope: per_session
  embedding_model: /models/minilm
  cache_tool_turns: true
  write_tools: ["create_*"]
""",
    )["cache"]
    assert isinstance(cache, SemanticCache)
    assert cache._threshold == 0.8
    assert cache._default_ttl == 60
    assert cache._scope == "per_session"
    assert cache._embedding._model_name == "/models/minilm"
    assert cache._cache_tool_turns is True
    assert len(cache._write_tools) == 1


def test_minimal_cache_mapping_builds(tmp_path: Path) -> None:
    from promptise.cache import SemanticCache

    assert isinstance(_kwargs(tmp_path, "cache:\n  backend: memory\n")["cache"], SemanticCache)


# ---------------------------------------------------------------------------
# adaptive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adaptive", ["adaptive: true\n", "adaptive:\n  synthesis_threshold: 3\n"])
def test_adaptive_without_memory_is_a_validation_error(tmp_path: Path, adaptive: str) -> None:
    with pytest.raises(SuperAgentValidationError, match="'adaptive' needs a 'memory' section"):
        _kwargs(tmp_path, adaptive)


def test_adaptive_with_memory_or_disabled_is_valid(tmp_path: Path) -> None:
    kwargs = _kwargs(tmp_path, "adaptive: true\nmemory:\n  provider: in_memory\n")
    assert kwargs["adaptive"] is True
    _kwargs(tmp_path, "adaptive:\n  enabled: false\n")
    _kwargs(tmp_path, "adaptive: false\n")
