"""End-to-end tests for the prompt loading subsystem.

Tests load_prompt, save_prompt, load_directory, load_url, YAML features,
and TemplateEngine across realistic scenarios.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# 1. load_prompt from YAML
# ---------------------------------------------------------------------------


class TestLoadPromptFromYAML:
    """Tests for loading prompts from .prompt YAML files."""

    def test_valid_yaml_parses_to_prompt(self, tmp_path: Path):
        """A well-formed .prompt YAML file produces a Prompt with correct fields."""
        from promptise.prompts.loader import load_prompt

        content = """\
name: summarize
version: "1.2.0"
description: Summarization prompt
author: team-nlp
tags: [summary, text]
template: "Summarize the following text: {text}"
arguments:
  text:
    description: The text to summarize
    required: true
"""
        f = tmp_path / "summarize.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert p.name == "summarize"
        assert p._version == "1.2.0"
        assert p._description == "Summarization prompt"
        assert p._author == "team-nlp"
        assert p._tags == ["summary", "text"]
        assert "Summarize the following text: {text}" in p.template

    def test_invalid_yaml_syntax_raises(self, tmp_path: Path):
        """Malformed YAML raises PromptFileError."""
        from promptise.prompts.loader import PromptFileError, load_prompt

        f = tmp_path / "bad_syntax.prompt"
        f.write_text("name: test\n  bad: [indent")

        with pytest.raises(PromptFileError, match="YAML parse error"):
            load_prompt(f)

    def test_missing_required_name_raises(self, tmp_path: Path):
        """YAML without the required 'name' field raises PromptValidationError."""
        from promptise.prompts.loader import PromptValidationError, load_prompt

        f = tmp_path / "no_name.prompt"
        f.write_text("template: 'Hello {x}'")

        with pytest.raises(PromptValidationError):
            load_prompt(f)

    def test_nonexistent_file_raises_file_not_found(self):
        """Loading a file that does not exist raises PromptFileError."""
        from promptise.prompts.loader import PromptFileError, load_prompt

        with pytest.raises(PromptFileError, match="File not found"):
            load_prompt("/nonexistent/nowhere/missing.prompt")

    def test_empty_file_raises_error(self, tmp_path: Path):
        """An empty .prompt file raises PromptFileError (not a dict)."""
        from promptise.prompts.loader import PromptFileError, load_prompt

        f = tmp_path / "empty.prompt"
        f.write_text("")

        with pytest.raises(PromptFileError, match="expected dict"):
            load_prompt(f)


# ---------------------------------------------------------------------------
# 2. save_prompt
# ---------------------------------------------------------------------------


class TestSavePrompt:
    """Tests for saving prompts to YAML files."""

    def test_save_creates_valid_yaml(self, tmp_path: Path):
        """save_prompt writes a file parseable as valid YAML with expected keys."""
        from promptise.prompts.core import prompt
        from promptise.prompts.loader import save_prompt

        @prompt(model="openai:gpt-5-mini")
        async def classify(text: str) -> str:
            """Classify this text: {text}"""

        f = tmp_path / "classify.prompt"
        save_prompt(classify, f, version="1.0.0", author="ml-team")

        data = yaml.safe_load(f.read_text())
        assert data["name"] == "classify"
        assert data["version"] == "1.0.0"
        assert data["author"] == "ml-team"
        assert "Classify this text" in data["template"]

    def test_save_load_roundtrip_produces_equivalent_prompt(self, tmp_path: Path):
        """A saved prompt can be loaded back and yields an equivalent Prompt."""
        from promptise.prompts.core import prompt
        from promptise.prompts.loader import load_prompt, save_prompt
        from promptise.prompts.strategies import ChainOfThoughtStrategy

        @prompt(model="openai:gpt-5-mini")
        async def review(text: str) -> str:
            """Review this: {text}"""

        configured = review.with_strategy(ChainOfThoughtStrategy()).with_constraints(
            "Be thorough", "Under 500 words"
        )

        f = tmp_path / "review.prompt"
        save_prompt(
            configured,
            f,
            version="2.0.0",
            description="Code review prompt",
            tags=["review"],
        )

        loaded = load_prompt(f)
        assert loaded.name == "review"
        assert loaded._version == "2.0.0"
        assert loaded._description == "Code review prompt"
        assert loaded._tags == ["review"]
        assert loaded._strategy is not None
        assert "Be thorough" in loaded._constraints
        assert "Under 500 words" in loaded._constraints
        # Rendering should contain the same core content
        original_render = configured.render(text="sample")
        loaded_render = loaded.render(text="sample")
        assert "Review this: sample" in original_render
        assert "Review this: sample" in loaded_render


# ---------------------------------------------------------------------------
# 3. load_directory
# ---------------------------------------------------------------------------


class TestLoadDirectory:
    """Tests for loading all .prompt files from a directory."""

    def test_discovers_yaml_and_yml_files(self, tmp_path: Path):
        """load_directory finds .prompt, .prompt.yaml, and .prompt.yml files."""
        from promptise.prompts.loader import load_directory

        (tmp_path / "alpha.prompt").write_text(
            "name: alpha\nversion: '1.0.0'\ntemplate: 'Alpha: {x}'"
        )
        (tmp_path / "beta.prompt.yaml").write_text(
            "name: beta\nversion: '1.0.0'\ntemplate: 'Beta: {x}'"
        )
        (tmp_path / "gamma.prompt.yml").write_text(
            "name: gamma\nversion: '1.0.0'\ntemplate: 'Gamma: {x}'"
        )
        (tmp_path / "ignore.txt").write_text("not a prompt")

        reg = load_directory(tmp_path)
        listing = reg.list()
        assert "alpha" in listing
        assert "beta" in listing
        assert "gamma" in listing
        assert len(listing) == 3

    def test_handles_nested_directories(self, tmp_path: Path):
        """load_directory recursively discovers prompts in subdirectories."""
        from promptise.prompts.loader import load_directory

        sub = tmp_path / "nested" / "deep"
        sub.mkdir(parents=True)
        (tmp_path / "top.prompt").write_text("name: top\nversion: '1.0.0'\ntemplate: 'Top: {x}'")
        (sub / "bottom.prompt").write_text(
            "name: bottom\nversion: '1.0.0'\ntemplate: 'Bottom: {x}'"
        )

        reg = load_directory(tmp_path)
        listing = reg.list()
        assert "top" in listing
        assert "bottom" in listing


# ---------------------------------------------------------------------------
# 4. load_url
# ---------------------------------------------------------------------------


class TestLoadUrl:
    """Tests for loading prompts from HTTP URLs."""

    @pytest.mark.asyncio
    async def test_success_with_mock_httpx(self):
        """load_url fetches YAML from URL and constructs a Prompt."""
        from promptise.prompts.loader import load_url

        yaml_content = """\
name: remote_prompt
version: "3.0.0"
description: Loaded from URL
template: "Remote task: {input}"
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
            p = await load_url("https://example.com/prompts/remote.prompt")

        assert p.name == "remote_prompt"
        assert p._version == "3.0.0"
        assert p._description == "Loaded from URL"
        assert "Remote task: {input}" in p.template

    @pytest.mark.asyncio
    async def test_404_error_raises(self):
        """A failed HTTP request raises PromptFileError."""
        from promptise.prompts.loader import PromptFileError, load_url

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("404 Not Found"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        mock_httpx = MagicMock()
        mock_httpx.AsyncClient = lambda **kwargs: mock_client
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            with pytest.raises(PromptFileError, match="Failed to fetch"):
                await load_url("https://example.com/nonexistent.prompt")


# ---------------------------------------------------------------------------
# 5. YAML features
# ---------------------------------------------------------------------------


class TestYAMLFeatures:
    """Tests for advanced YAML fields in .prompt files."""

    def test_blocks_in_yaml_constraints(self, tmp_path: Path):
        """Constraints defined in YAML are loaded onto the Prompt."""
        from promptise.prompts.loader import load_prompt

        content = """\
name: constrained
template: "Analyze: {data}"
constraints:
  - Must include confidence scores
  - Respond in bullet points
  - Keep under 300 words
"""
        f = tmp_path / "constrained.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert len(p._constraints) == 3
        assert "Must include confidence scores" in p._constraints
        assert "Respond in bullet points" in p._constraints
        assert "Keep under 300 words" in p._constraints
        rendered = p.render(data="test")
        assert "Must include confidence scores" in rendered

    def test_model_override(self, tmp_path: Path):
        """The 'model' field in YAML overrides the default model."""
        from promptise.prompts.loader import load_prompt

        content = """\
name: custom_model
template: "Do: {task}"
model: anthropic:claude-sonnet-4-20250514
"""
        f = tmp_path / "custom_model.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert p.model == "anthropic:claude-sonnet-4-20250514"

    def test_return_type_field(self, tmp_path: Path):
        """The 'return_type' field sets the Prompt return type."""
        from promptise.prompts.loader import load_prompt

        content = """\
name: typed_prompt
template: "Count items: {text}"
return_type: int
"""
        f = tmp_path / "typed_prompt.prompt"
        f.write_text(content)

        p = load_prompt(f)
        assert p.return_type is int

    def test_template_variables_with_arguments(self, tmp_path: Path):
        """Arguments define the prompt signature and defaults."""
        from promptise.prompts.loader import load_prompt

        content = """\
name: search
template: "Search for {query} in {scope} with depth {depth}"
arguments:
  query:
    description: Search query
    required: true
  scope:
    description: The search scope
    required: false
    default: all
  depth:
    description: Search depth
    required: false
    default: shallow
"""
        f = tmp_path / "search.prompt"
        f.write_text(content)

        p = load_prompt(f)
        params = list(p._sig.parameters.values())
        assert params[0].name == "query"
        assert params[0].default is inspect.Parameter.empty
        assert params[1].name == "scope"
        assert params[1].default == "all"
        assert params[2].name == "depth"
        assert params[2].default == "shallow"

        # Render with explicit values
        rendered = p.render(query="python", scope="docs", depth="deep")
        assert "Search for python in docs with depth deep" in rendered


# ---------------------------------------------------------------------------
# 6. TemplateEngine
# ---------------------------------------------------------------------------


class TestTemplateEngine:
    """Tests for the TemplateEngine rendering."""

    def test_render_simple_template(self):
        """Simple {variable} interpolation works."""
        from promptise.prompts.template import TemplateEngine

        engine = TemplateEngine()
        result = engine.render(
            "Hello, {name}! You are {age} years old.",
            {"name": "Alice", "age": 30},
        )
        assert result == "Hello, Alice! You are 30 years old."

    def test_render_with_loop_variables(self):
        """The for-loop directive iterates over a collection."""
        from promptise.prompts.template import TemplateEngine

        engine = TemplateEngine()
        template = "Items: {% for item in items %}- {item}\n{% endfor %}"
        result = engine.render(template, {"items": ["alpha", "beta", "gamma"]})
        assert "- alpha" in result
        assert "- beta" in result
        assert "- gamma" in result

    def test_render_with_conditionals(self):
        """The if/else directive selects content based on truthiness."""
        from promptise.prompts.template import TemplateEngine

        engine = TemplateEngine()
        template = "{% if verbose %}Detailed output{% else %}Brief output{% endif %}"

        result_verbose = engine.render(template, {"verbose": True})
        assert result_verbose == "Detailed output"

        result_brief = engine.render(template, {"verbose": False})
        assert result_brief == "Brief output"
