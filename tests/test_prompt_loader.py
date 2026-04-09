"""Tests for the .prompt file loader and saver."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestPromptFileSchema:
    """Tests for PromptFileSchema validation."""

    def test_minimal_valid(self):
        from promptise.prompts.loader import PromptFileSchema

        schema = PromptFileSchema(name="test", template="Hello {name}")
        assert schema.name == "test"
        assert schema.template == "Hello {name}"
        assert schema.version == "0.0.0"
        assert schema.model is None
        assert schema.description == ""
        assert schema.author == ""
        assert schema.tags == []

    def test_full_schema(self):
        from promptise.prompts.loader import PromptFileSchema

        schema = PromptFileSchema(
            name="analyze",
            version="1.0.0",
            description="Analysis prompt",
            author="team-data",
            tags=["analysis"],
            template="Analyze: {text}",
            strategy="chain_of_thought",
            perspective="analyst",
            constraints=["Be concise"],
            model="openai:gpt-4o",
        )
        assert schema.version == "1.0.0"
        assert schema.description == "Analysis prompt"
        assert schema.author == "team-data"
        assert schema.tags == ["analysis"]
        assert schema.strategy == "chain_of_thought"
        assert schema.model == "openai:gpt-4o"

    def test_missing_name_raises(self):
        from pydantic import ValidationError

        from promptise.prompts.loader import PromptFileSchema

        with pytest.raises(ValidationError):
            PromptFileSchema(template="Hello")

    def test_missing_template_raises(self):
        from pydantic import ValidationError

        from promptise.prompts.loader import PromptFileSchema

        with pytest.raises(ValidationError):
            PromptFileSchema(name="test")

    def test_arguments_schema(self):
        from promptise.prompts.loader import ArgumentSchema, PromptFileSchema

        schema = PromptFileSchema(
            name="test",
            template="{text}",
            arguments={
                "text": ArgumentSchema(description="Input text", required=True),
                "depth": ArgumentSchema(description="Depth", required=False, default="basic"),
            },
        )
        assert schema.arguments["text"].required is True
        assert schema.arguments["depth"].default == "basic"

    def test_guard_schema_extra_fields(self):
        from promptise.prompts.loader import GuardSchema

        guard = GuardSchema(type="max_tokens", input=4000, output=2000)
        dumped = guard.model_dump()
        assert dumped["type"] == "max_tokens"
        assert dumped["input"] == 4000
        assert dumped["output"] == 2000


# ---------------------------------------------------------------------------
# Load prompt tests
# ---------------------------------------------------------------------------


class TestLoadPrompt:
    """Tests for load_prompt() from file."""

    def test_load_basic(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = """
name: greet
template: "Hello, {name}! Welcome."
"""
        f = tmp_path / "greet.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert p.name == "greet"
        assert "Hello, {name}!" in p.template

    def test_load_with_metadata(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = """
name: analyze
version: "2.0.0"
description: Expert analysis prompt
author: data-team
tags: [analysis, reporting]
template: "Analyze: {text}"
"""
        f = tmp_path / "analyze.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert p._description == "Expert analysis prompt"
        assert p._author == "data-team"
        assert p._tags == ["analysis", "reporting"]
        assert p._version == "2.0.0"

    def test_load_with_strategy(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = """
name: think
template: "Think about: {topic}"
strategy: chain_of_thought
"""
        f = tmp_path / "think.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert p._strategy is not None
        rendered = p.render(topic="AI")
        assert "step-by-step" in rendered.lower()

    def test_load_with_composite_strategy(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt
        from promptise.prompts.strategies import CompositeStrategy

        content = """
name: deep
template: "Analyze: {text}"
strategy:
  - chain_of_thought
  - self_critique
"""
        f = tmp_path / "deep.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert isinstance(p._strategy, CompositeStrategy)

    def test_load_with_perspective(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = """
name: review
template: "Review: {text}"
perspective: critic
"""
        f = tmp_path / "review.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert p._perspective is not None
        rendered = p.render(text="test")
        assert "critic" in rendered.lower()

    def test_load_with_custom_perspective(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt
        from promptise.prompts.strategies import CustomPerspective

        content = """
name: audit
template: "Audit: {code}"
perspective:
  role: security auditor
  instructions: Focus on OWASP Top 10
"""
        f = tmp_path / "audit.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert isinstance(p._perspective, CustomPerspective)
        assert p._perspective.role == "security auditor"

    def test_load_with_guards(self, tmp_path: Path):
        from promptise.prompts.guards import ContentFilterGuard, LengthGuard
        from promptise.prompts.loader import load_prompt

        content = """
name: guarded
template: "Process: {text}"
guards:
  - type: content_filter
    blocked: [secret]
  - type: length
    max_length: 2000
"""
        f = tmp_path / "guarded.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert len(p._input_guards) == 2
        assert isinstance(p._input_guards[0], ContentFilterGuard)
        assert isinstance(p._input_guards[1], LengthGuard)

    def test_load_with_constraints(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = """
name: strict
template: "Write: {topic}"
constraints:
  - Must cite sources
  - Under 500 words
"""
        f = tmp_path / "strict.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert "Must cite sources" in p._constraints
        assert "Under 500 words" in p._constraints

    def test_load_with_arguments(self, tmp_path: Path):
        import inspect

        from promptise.prompts.loader import load_prompt

        content = """
name: search
template: "Search for {query} in {scope}"
arguments:
  query:
    description: Search query
    required: true
  scope:
    description: Search scope
    required: false
    default: all
"""
        f = tmp_path / "search.prompt"
        f.write_text(content)

        p = load_prompt(f)
        params = list(p._sig.parameters.values())
        assert params[0].name == "query"
        assert params[0].default is inspect.Parameter.empty
        assert params[1].name == "scope"
        assert params[1].default == "all"

    def test_load_with_world(self, tmp_path: Path):
        from promptise.prompts.context import BaseContext, UserContext
        from promptise.prompts.loader import load_prompt

        content = """
name: contextual
template: "Process: {text}"
world:
  user:
    type: UserContext
    name: Alice
    expertise_level: expert
  project:
    name: Alpha
    deadline: "2026-03-15"
"""
        f = tmp_path / "contextual.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert "user" in p._world
        assert isinstance(p._world["user"], UserContext)
        assert p._world["user"].name == "Alice"
        assert isinstance(p._world["project"], BaseContext)
        assert p._world["project"].name == "Alpha"

    def test_load_with_model(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = """
name: specific
template: "Do: {task}"
model: anthropic:claude-sonnet-4-20250514
"""
        f = tmp_path / "specific.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert p.model == "anthropic:claude-sonnet-4-20250514"

    def test_load_without_model_uses_default(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = """
name: generic
template: "Do: {task}"
"""
        f = tmp_path / "generic.prompt"
        f.write_text(content)

        p = load_prompt(f)
        # Builder default — caller can override with .with_model()
        assert p.model == "openai:gpt-5-mini"

    def test_load_prompt_yaml_extension(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = """
name: alt
template: "Hello {name}"
"""
        f = tmp_path / "alt.prompt.yaml"
        f.write_text(content)

        p = load_prompt(f)
        assert p.name == "alt"

    def test_load_prompt_yml_extension(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = """
name: alt2
template: "Hello {name}"
"""
        f = tmp_path / "alt2.prompt.yml"
        f.write_text(content)

        p = load_prompt(f)
        assert p.name == "alt2"

    def test_load_file_not_found(self):
        from promptise.prompts.loader import PromptFileError, load_prompt

        with pytest.raises(PromptFileError, match="not found"):
            load_prompt("/nonexistent/path.prompt")

    def test_load_invalid_extension(self, tmp_path: Path):
        from promptise.prompts.loader import PromptFileError, load_prompt

        f = tmp_path / "bad.txt"
        f.write_text("name: test\ntemplate: hello")

        with pytest.raises(PromptFileError, match="Invalid file extension"):
            load_prompt(f)

    def test_load_invalid_yaml(self, tmp_path: Path):
        from promptise.prompts.loader import PromptFileError, load_prompt

        f = tmp_path / "bad.prompt"
        f.write_text("name: test\n  bad indent: [")

        with pytest.raises(PromptFileError, match="YAML parse error"):
            load_prompt(f)

    def test_load_invalid_schema(self, tmp_path: Path):
        from promptise.prompts.loader import PromptValidationError, load_prompt

        f = tmp_path / "bad.prompt"
        f.write_text("name: test\nunknown_field: value\ntemplate: hello")

        with pytest.raises(PromptValidationError):
            load_prompt(f)

    def test_load_with_register(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt
        from promptise.prompts.registry import PromptRegistry

        content = """
name: registered
version: "1.0.0"
template: "Do: {task}"
"""
        f = tmp_path / "registered.prompt"
        f.write_text(content)

        reg = PromptRegistry()
        # Patch the singleton in the registry module so the local import picks it up
        with patch("promptise.prompts.registry.registry", reg):
            p = load_prompt(f, register=True)
            assert p.name == "registered"
            assert reg.get("registered", "1.0.0") is p

    def test_load_unknown_strategy_raises(self, tmp_path: Path):
        from promptise.prompts.loader import PromptFileError, load_prompt

        content = """
name: bad
template: "Do: {task}"
strategy: nonexistent_strategy
"""
        f = tmp_path / "bad.prompt"
        f.write_text(content)

        with pytest.raises(PromptFileError, match="Unknown strategy"):
            load_prompt(f)

    def test_load_unknown_perspective_raises(self, tmp_path: Path):
        from promptise.prompts.loader import PromptFileError, load_prompt

        content = """
name: bad
template: "Do: {task}"
perspective: nonexistent_perspective
"""
        f = tmp_path / "bad.prompt"
        f.write_text(content)

        with pytest.raises(PromptFileError, match="Unknown perspective"):
            load_prompt(f)

    def test_load_unknown_guard_raises(self, tmp_path: Path):
        from promptise.prompts.loader import PromptFileError, load_prompt

        content = """
name: bad
template: "Do: {task}"
guards:
  - type: nonexistent_guard
"""
        f = tmp_path / "bad.prompt"
        f.write_text(content)

        with pytest.raises(PromptFileError, match="Unknown guard"):
            load_prompt(f)

    def test_load_unknown_return_type_raises(self, tmp_path: Path):
        from promptise.prompts.loader import PromptFileError, load_prompt

        content = """
name: bad
template: "Do: {task}"
return_type: bytes
"""
        f = tmp_path / "bad.prompt"
        f.write_text(content)

        with pytest.raises(PromptFileError, match="Unknown return_type"):
            load_prompt(f)


# ---------------------------------------------------------------------------
# Suite loading tests
# ---------------------------------------------------------------------------


class TestLoadSuite:
    """Tests for loading multi-prompt suite files."""

    def test_load_suite(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt
        from promptise.prompts.suite import PromptSuite

        content = """
name: my_suite
description: Test suite
author: test-team
tags: [testing]
suite: true

defaults:
  constraints:
    - Be concise

prompts:
  task_a:
    template: "Task A: {text}"
  task_b:
    template: "Task B: {query}"
"""
        f = tmp_path / "suite.prompt"
        f.write_text(content)

        suite = load_prompt(f)
        assert isinstance(suite, PromptSuite)
        prompts = suite.prompts
        assert "task_a" in prompts
        assert "task_b" in prompts

    def test_suite_defaults_applied(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = """
name: suite_defaults
suite: true

defaults:
  strategy: chain_of_thought
  constraints:
    - Must be thorough

prompts:
  analyze:
    template: "Analyze: {text}"
"""
        f = tmp_path / "defaults.prompt"
        f.write_text(content)

        suite = load_prompt(f)
        p = suite.prompts["analyze"]
        assert p._strategy is not None
        assert "Must be thorough" in p._constraints

    def test_suite_per_prompt_override(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = """
name: suite_override
suite: true

defaults:
  strategy: chain_of_thought

prompts:
  task_a:
    template: "A: {text}"
  task_b:
    template: "B: {text}"
    strategy: self_critique
"""
        f = tmp_path / "override.prompt"
        f.write_text(content)

        suite = load_prompt(f)
        # task_b overrides the default strategy
        p_b = suite.prompts["task_b"]
        from promptise.prompts.strategies import SelfCritiqueStrategy

        assert isinstance(p_b._strategy, SelfCritiqueStrategy)

    def test_suite_constraints_not_duplicated(self, tmp_path: Path):
        """Suite constraints should appear once, not be doubled."""
        from promptise.prompts.loader import load_prompt

        content = """
name: dedup_suite
suite: true
defaults:
  constraints:
    - Be thorough
    - Cite sources
prompts:
  analyze:
    template: "Analyze: {text}"
"""
        f = tmp_path / "dedup.prompt"
        f.write_text(content)

        suite = load_prompt(f)
        p = suite.prompts["analyze"]
        assert p._constraints.count("Be thorough") == 1
        assert p._constraints.count("Cite sources") == 1

    def test_suite_missing_prompts_raises(self, tmp_path: Path):
        from promptise.prompts.loader import PromptValidationError, load_prompt

        content = """
name: bad_suite
suite: true
template: ignored
"""
        f = tmp_path / "bad.prompt"
        f.write_text(content)

        with pytest.raises(PromptValidationError):
            load_prompt(f)


# ---------------------------------------------------------------------------
# Save prompt tests
# ---------------------------------------------------------------------------


class TestSavePrompt:
    """Tests for save_prompt() to file."""

    def test_save_basic(self, tmp_path: Path):
        from promptise.prompts.core import prompt
        from promptise.prompts.loader import save_prompt

        @prompt(model="openai:gpt-5-mini")
        async def greet(name: str) -> str:
            """Hello, {name}!"""

        f = tmp_path / "greet.prompt"
        save_prompt(greet, f, version="1.0.0")

        data = yaml.safe_load(f.read_text())
        assert data["name"] == "greet"
        assert data["version"] == "1.0.0"
        assert "Hello" in data["template"]

    def test_save_with_metadata(self, tmp_path: Path):
        from promptise.prompts.core import prompt
        from promptise.prompts.loader import save_prompt

        @prompt(model="openai:gpt-5-mini")
        async def analyze(text: str) -> str:
            """Analyze: {text}"""

        f = tmp_path / "analyze.prompt"
        save_prompt(
            analyze,
            f,
            version="2.0.0",
            author="data-team",
            description="Expert analysis",
            tags=["analysis"],
        )

        data = yaml.safe_load(f.read_text())
        assert data["version"] == "2.0.0"
        assert data["author"] == "data-team"
        assert data["description"] == "Expert analysis"
        assert data["tags"] == ["analysis"]

    def test_save_with_strategy(self, tmp_path: Path):
        from promptise.prompts.core import prompt
        from promptise.prompts.loader import save_prompt
        from promptise.prompts.strategies import ChainOfThoughtStrategy

        @prompt(model="openai:gpt-5-mini")
        async def think(text: str) -> str:
            """Think: {text}"""

        configured = think.with_strategy(ChainOfThoughtStrategy())
        f = tmp_path / "think.prompt"
        save_prompt(configured, f)

        data = yaml.safe_load(f.read_text())
        assert data["strategy"] == "chain_of_thought"

    def test_save_with_perspective(self, tmp_path: Path):
        from promptise.prompts.core import prompt
        from promptise.prompts.loader import save_prompt
        from promptise.prompts.strategies import AnalystPerspective

        @prompt(model="openai:gpt-5-mini")
        async def analyze(text: str) -> str:
            """Analyze: {text}"""

        configured = analyze.with_perspective(AnalystPerspective())
        f = tmp_path / "analyze.prompt"
        save_prompt(configured, f)

        data = yaml.safe_load(f.read_text())
        assert data["perspective"] == "analyst"

    def test_save_with_custom_perspective(self, tmp_path: Path):
        from promptise.prompts.core import prompt
        from promptise.prompts.loader import save_prompt
        from promptise.prompts.strategies import CustomPerspective

        @prompt(model="openai:gpt-5-mini")
        async def audit(code: str) -> str:
            """Audit: {code}"""

        configured = audit.with_perspective(CustomPerspective("auditor", "Focus on security"))
        f = tmp_path / "audit.prompt"
        save_prompt(configured, f)

        data = yaml.safe_load(f.read_text())
        assert data["perspective"]["role"] == "auditor"
        assert data["perspective"]["instructions"] == "Focus on security"

    def test_save_with_constraints(self, tmp_path: Path):
        from promptise.prompts.core import prompt
        from promptise.prompts.loader import save_prompt

        @prompt(model="openai:gpt-5-mini")
        async def write(topic: str) -> str:
            """Write: {topic}"""

        configured = write.with_constraints("Cite sources", "Under 300 words")
        f = tmp_path / "write.prompt"
        save_prompt(configured, f)

        data = yaml.safe_load(f.read_text())
        assert "Cite sources" in data["constraints"]
        assert "Under 300 words" in data["constraints"]

    def test_save_with_guards(self, tmp_path: Path):
        from promptise.prompts.core import prompt
        from promptise.prompts.guards import ContentFilterGuard, LengthGuard
        from promptise.prompts.loader import save_prompt

        @prompt(model="openai:gpt-5-mini")
        async def guarded(text: str) -> str:
            """Process: {text}"""

        configured = guarded.with_guards(
            ContentFilterGuard(blocked=["secret"]), LengthGuard(max_length=2000)
        )
        f = tmp_path / "guarded.prompt"
        save_prompt(configured, f)

        data = yaml.safe_load(f.read_text())
        assert len(data["guards"]) == 2
        assert data["guards"][0]["type"] == "content_filter"
        assert data["guards"][0]["blocked"] == ["secret"]
        assert data["guards"][1]["type"] == "length"
        assert data["guards"][1]["max_length"] == 2000

    def test_save_all_guard_types_preserve_params(self, tmp_path: Path):
        """Verify all guard types serialize their parameters correctly."""
        from promptise.prompts.core import prompt
        from promptise.prompts.guards import (
            LengthGuard,
            SchemaStrictGuard,
        )
        from promptise.prompts.loader import save_prompt

        @prompt(model="openai:gpt-5-mini")
        async def guarded(text: str) -> str:
            """Do: {text}"""

        configured = guarded.with_guards(
            LengthGuard(min_length=10, max_length=100),
            SchemaStrictGuard(),
        )
        f = tmp_path / "all_guards.prompt"
        save_prompt(configured, f)

        data = yaml.safe_load(f.read_text())
        guards_by_type = {g["type"]: g for g in data["guards"]}
        assert guards_by_type["length"]["min_length"] == 10
        assert guards_by_type["length"]["max_length"] == 100
        assert "schema_strict" in guards_by_type

    def test_save_model_omitted_when_default(self, tmp_path: Path):
        from promptise.prompts.core import prompt
        from promptise.prompts.loader import save_prompt

        @prompt(model="openai:gpt-5-mini")
        async def basic(text: str) -> str:
            """Do: {text}"""

        f = tmp_path / "basic.prompt"
        save_prompt(basic, f)

        data = yaml.safe_load(f.read_text())
        assert "model" not in data

    def test_save_model_included_when_custom(self, tmp_path: Path):
        from promptise.prompts.core import prompt
        from promptise.prompts.loader import save_prompt

        @prompt(model="openai:gpt-4o")
        async def custom(text: str) -> str:
            """Do: {text}"""

        f = tmp_path / "custom.prompt"
        save_prompt(custom, f)

        data = yaml.safe_load(f.read_text())
        assert data["model"] == "openai:gpt-4o"


# ---------------------------------------------------------------------------
# Roundtrip tests
# ---------------------------------------------------------------------------


class TestRoundtrip:
    """Test save → load roundtrip preserves prompt configuration."""

    def test_basic_roundtrip(self, tmp_path: Path):
        from promptise.prompts.core import prompt
        from promptise.prompts.loader import load_prompt, save_prompt

        @prompt(model="openai:gpt-5-mini")
        async def greet(name: str) -> str:
            """Hello, {name}! Welcome."""

        f = tmp_path / "greet.prompt"
        save_prompt(greet, f, version="1.0.0", description="Greeting prompt")

        loaded = load_prompt(f)
        assert loaded.name == "greet"
        assert "Hello, {name}!" in loaded.template
        assert loaded._version == "1.0.0"
        assert loaded._description == "Greeting prompt"

    def test_roundtrip_with_strategy_and_constraints(self, tmp_path: Path):
        from promptise.prompts.core import prompt
        from promptise.prompts.loader import load_prompt, save_prompt
        from promptise.prompts.strategies import ChainOfThoughtStrategy

        @prompt(model="openai:gpt-5-mini")
        async def analyze(text: str) -> str:
            """Analyze: {text}"""

        configured = analyze.with_strategy(ChainOfThoughtStrategy()).with_constraints(
            "Be concise", "Cite sources"
        )

        f = tmp_path / "analyze.prompt"
        save_prompt(configured, f, version="2.0.0")

        loaded = load_prompt(f)
        assert loaded._strategy is not None
        assert "Be concise" in loaded._constraints
        assert "Cite sources" in loaded._constraints

    def test_roundtrip_render_produces_same_output(self, tmp_path: Path):
        from promptise.prompts.core import prompt
        from promptise.prompts.loader import load_prompt, save_prompt
        from promptise.prompts.strategies import AnalystPerspective, ChainOfThoughtStrategy

        @prompt(model="openai:gpt-5-mini")
        async def analyze(text: str) -> str:
            """Analyze: {text}"""

        configured = analyze.with_strategy(ChainOfThoughtStrategy()).with_perspective(
            AnalystPerspective()
        )

        original_render = configured.render(text="test data")

        f = tmp_path / "analyze.prompt"
        save_prompt(configured, f)
        loaded = load_prompt(f)
        loaded_render = loaded.render(text="test data")

        # Both should contain the same key elements
        assert "Analyze: test data" in original_render
        assert "Analyze: test data" in loaded_render
        assert "step-by-step" in original_render.lower()
        assert "step-by-step" in loaded_render.lower()
        assert "analyst" in original_render.lower()
        assert "analyst" in loaded_render.lower()

    def test_roundtrip_argument_descriptions(self, tmp_path: Path):
        """Argument descriptions must survive save → load."""
        from promptise.prompts.loader import load_prompt, save_prompt

        content = """
name: search
template: "Search {query} in {scope}"
arguments:
  query:
    description: The search query string
    required: true
  scope:
    description: Search scope (all, docs, code)
    required: false
    default: all
"""
        orig = tmp_path / "search.prompt"
        orig.write_text(content)
        p = load_prompt(orig)

        saved = tmp_path / "search_saved.prompt"
        save_prompt(p, saved)
        data = yaml.safe_load(saved.read_text())

        assert data["arguments"]["query"]["description"] == "The search query string"
        assert data["arguments"]["scope"]["description"] == "Search scope (all, docs, code)"
        assert data["arguments"]["scope"]["default"] == "all"

    def test_roundtrip_all_guard_params(self, tmp_path: Path):
        """Guard params must survive save → load → save."""
        from promptise.prompts.loader import load_prompt, save_prompt

        content = """
name: guarded
template: "Do: {x}"
guards:
  - type: content_filter
    blocked: [secret, classified]
  - type: length
    min_length: 10
    max_length: 200
  - type: schema_strict
"""
        orig = tmp_path / "guarded.prompt"
        orig.write_text(content)
        p = load_prompt(orig)

        saved = tmp_path / "guarded_saved.prompt"
        save_prompt(p, saved)
        data = yaml.safe_load(saved.read_text())

        guards_by_type = {g["type"]: g for g in data["guards"]}
        assert guards_by_type["content_filter"]["blocked"] == ["secret", "classified"]
        assert guards_by_type["length"]["min_length"] == 10
        assert guards_by_type["length"]["max_length"] == 200
        assert "schema_strict" in guards_by_type


# ---------------------------------------------------------------------------
# URL loading tests
# ---------------------------------------------------------------------------


class TestLoadUrl:
    """Tests for load_url() from HTTP."""

    @pytest.mark.asyncio
    async def test_load_url_success(self):
        from promptise.prompts.loader import load_url

        yaml_content = """
name: remote
template: "Hello {name}"
version: "1.0.0"
description: Remote prompt
"""
        mock_response = AsyncMock()
        mock_response.text = yaml_content
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        mock_httpx = MagicMock()
        mock_httpx.AsyncClient = lambda **kwargs: mock_client
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            p = await load_url("https://example.com/test.prompt")

        assert p.name == "remote"
        assert p._version == "1.0.0"

    @pytest.mark.asyncio
    async def test_load_url_http_error(self):
        from promptise.prompts.loader import PromptFileError, load_url

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        mock_httpx = MagicMock()
        mock_httpx.AsyncClient = lambda **kwargs: mock_client
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            with pytest.raises(PromptFileError, match="Failed to fetch"):
                await load_url("https://example.com/test.prompt")

    @pytest.mark.asyncio
    async def test_load_url_missing_httpx(self):
        from promptise.prompts.loader import PromptFileError, load_url

        with patch.dict("sys.modules", {"httpx": None}):
            with pytest.raises(PromptFileError, match="httpx is required"):
                await load_url("https://example.com/test.prompt")


# ---------------------------------------------------------------------------
# Directory loading tests
# ---------------------------------------------------------------------------


class TestLoadDirectory:
    """Tests for load_directory() bulk loading."""

    def test_load_directory(self, tmp_path: Path):
        from promptise.prompts.loader import load_directory

        (tmp_path / "a.prompt").write_text("name: a\nversion: '1.0.0'\ntemplate: 'A: {x}'")
        (tmp_path / "b.prompt").write_text("name: b\nversion: '1.0.0'\ntemplate: 'B: {x}'")
        (tmp_path / "ignore.txt").write_text("not a prompt")

        reg = load_directory(tmp_path)
        listing = reg.list()
        assert "a" in listing
        assert "b" in listing
        assert len(listing) == 2

    def test_load_directory_recursive(self, tmp_path: Path):
        from promptise.prompts.loader import load_directory

        subdir = tmp_path / "sub"
        subdir.mkdir()
        (tmp_path / "root.prompt").write_text("name: root\nversion: '1.0.0'\ntemplate: 'Root: {x}'")
        (subdir / "nested.prompt").write_text(
            "name: nested\nversion: '1.0.0'\ntemplate: 'Nested: {x}'"
        )

        reg = load_directory(tmp_path)
        listing = reg.list()
        assert "root" in listing
        assert "nested" in listing

    def test_load_directory_not_found(self):
        from promptise.prompts.loader import PromptFileError, load_directory

        with pytest.raises(PromptFileError, match="Directory not found"):
            load_directory("/nonexistent/dir")

    def test_load_directory_prompt_yaml(self, tmp_path: Path):
        from promptise.prompts.loader import load_directory

        (tmp_path / "alt.prompt.yaml").write_text(
            "name: alt\nversion: '1.0.0'\ntemplate: 'Alt: {x}'"
        )

        reg = load_directory(tmp_path)
        assert "alt" in reg.list()


# ---------------------------------------------------------------------------
# Strategy resolution tests
# ---------------------------------------------------------------------------


class TestStrategyResolution:
    """Tests for strategy name → instance resolution."""

    @pytest.mark.parametrize(
        "name",
        [
            "chain_of_thought",
            "structured_reasoning",
            "self_critique",
            "plan_and_execute",
            "decompose",
        ],
    )
    def test_resolve_all_strategies(self, name: str, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = f"name: test\ntemplate: 'Do: {{x}}'\nstrategy: {name}"
        f = tmp_path / "test.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert p._strategy is not None


# ---------------------------------------------------------------------------
# Perspective resolution tests
# ---------------------------------------------------------------------------


class TestPerspectiveResolution:
    """Tests for perspective name → instance resolution."""

    @pytest.mark.parametrize(
        "name",
        [
            "analyst",
            "critic",
            "advisor",
            "creative",
        ],
    )
    def test_resolve_all_perspectives(self, name: str, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = f"name: test\ntemplate: 'Do: {{x}}'\nperspective: {name}"
        f = tmp_path / "test.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert p._perspective is not None


# ---------------------------------------------------------------------------
# Guard resolution tests
# ---------------------------------------------------------------------------


class TestGuardResolution:
    """Tests for guard type → instance resolution."""

    @pytest.mark.parametrize(
        "guard_yaml,expected_cls_name",
        [
            ("type: content_filter\n    blocked: [bad]", "ContentFilterGuard"),
            ("type: length\n    max_length: 1000", "LengthGuard"),
            ("type: schema_strict\n    max_retries: 3", "SchemaStrictGuard"),
        ],
    )
    def test_resolve_all_guards(self, guard_yaml: str, expected_cls_name: str, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = f"name: test\ntemplate: 'Do: {{x}}'\nguards:\n  - {guard_yaml}"
        f = tmp_path / "test.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert len(p._input_guards) == 1
        assert type(p._input_guards[0]).__name__ == expected_cls_name


# ---------------------------------------------------------------------------
# World context resolution tests
# ---------------------------------------------------------------------------


class TestWorldResolution:
    """Tests for world context type → instance resolution."""

    @pytest.mark.parametrize(
        "type_name",
        [
            "UserContext",
            "EnvironmentContext",
            "ConversationContext",
            "TeamContext",
            "ErrorContext",
            "OutputContext",
        ],
    )
    def test_resolve_typed_contexts(self, type_name: str, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = f"""
name: test
template: "Do: {{x}}"
world:
  ctx:
    type: {type_name}
"""
        f = tmp_path / "test.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert "ctx" in p._world
        assert type(p._world["ctx"]).__name__ == type_name

    def test_resolve_base_context_fallback(self, tmp_path: Path):
        from promptise.prompts.context import BaseContext
        from promptise.prompts.loader import load_prompt

        content = """
name: test
template: "Do: {x}"
world:
  project:
    name: Alpha
    budget: 50000
"""
        f = tmp_path / "test.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert isinstance(p._world["project"], BaseContext)
        assert p._world["project"].name == "Alpha"


# ---------------------------------------------------------------------------
# Env var resolution tests
# ---------------------------------------------------------------------------


class TestEnvVarResolution:
    """Tests for ${VAR} resolution in .prompt files."""

    def test_env_var_in_world(self, tmp_path: Path, monkeypatch):
        from promptise.prompts.loader import load_prompt

        monkeypatch.setenv("TEST_USER", "Bob")

        content = """
name: env_test
template: "Hello {name}"
world:
  user:
    type: UserContext
    name: "${TEST_USER}"
"""
        f = tmp_path / "env.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert p._world["user"].name == "Bob"

    def test_env_var_with_default(self, tmp_path: Path):
        from promptise.prompts.loader import load_prompt

        content = """
name: env_default
template: "Hello {name}"
world:
  project:
    name: "${MISSING_VAR:-FallbackProject}"
"""
        f = tmp_path / "env_default.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert p._world["project"].name == "FallbackProject"


# ---------------------------------------------------------------------------
# Import tests
# ---------------------------------------------------------------------------


class TestLoaderImports:
    """Tests for public imports."""

    def test_import_from_loader(self):
        from promptise.prompts.loader import (
            PromptFileError,
            PromptFileSchema,
            PromptValidationError,
            load_directory,
            load_prompt,
            load_url,
            save_prompt,
        )

        assert load_prompt is not None
        assert load_url is not None
        assert save_prompt is not None
        assert load_directory is not None
        assert PromptFileSchema is not None
        assert PromptFileError is not None
        assert PromptValidationError is not None

    def test_import_from_prompts_package(self):
        from promptise.prompts import (
            PromptFileError,
            PromptValidationError,
            load_directory,
            load_prompt,
            save_prompt,
        )

        assert load_prompt is not None
        assert save_prompt is not None
        assert load_directory is not None
        assert PromptFileError is not None
        assert PromptValidationError is not None
