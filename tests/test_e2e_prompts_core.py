"""End-to-end tests for the core prompts subsystem of Promptise.

Covers blocks, assembler, @prompt decorator, context providers, guards,
strategies, builder, registry, template engine, and inspector.

All LLM calls are mocked -- no real API keys required.
"""

from __future__ import annotations

import pytest

from promptise.prompts.blocks import (
    AssembledPrompt,
    BlockContext,
    Composite,
    Conditional,
    ContextSlot,
    Examples,
    Identity,
    OutputFormat,
    PromptAssembler,
    Rules,
    Section,
    blocks,
)
from promptise.prompts.builder import PromptBuilder
from promptise.prompts.context import (
    ConversationContext,
    EnvironmentContext,
    PromptContext,
    UserContext,
    context,
)
from promptise.prompts.core import Prompt, constraint, prompt
from promptise.prompts.guards import (
    ContentFilterGuard,
    GuardError,
    LengthGuard,
    guard,
)
from promptise.prompts.inspector import PromptInspector, PromptTrace
from promptise.prompts.registry import PromptRegistry
from promptise.prompts.strategies import (
    ChainOfThoughtStrategy,
    CustomPerspective,
    StructuredReasoningStrategy,
    chain_of_thought,
    perspective,
    structured_reasoning,
)
from promptise.prompts.template import TemplateEngine, render_template

# ---------------------------------------------------------------------------
# 1. TestBlocks -- 9 tests for concrete block types
# ---------------------------------------------------------------------------


class TestBlocks:
    """One test for each concrete block type."""

    def test_identity_renders_with_description(self):
        """Identity block renders with 'You are ...' phrasing."""
        block = Identity("an expert data analyst")
        text = block.render()
        assert "You are an expert data analyst." in text
        assert block.name == "identity"
        assert block.priority == 10

    def test_rules_renders_numbered_rules(self):
        """Rules block adds numbered rule text."""
        block = Rules(["Always cite sources", "Be concise"])
        text = block.render()
        assert "Rules:" in text
        assert "1. Always cite sources" in text
        assert "2. Be concise" in text
        assert block.priority == 9

    def test_output_format_adds_format_spec(self):
        """OutputFormat block adds format specification."""
        block = OutputFormat(format="json", instructions="Include all fields")
        text = block.render()
        assert "JSON" in text or "json" in text.lower()
        assert "Include all fields" in text
        assert block.priority == 8

    def test_context_slot_is_placeholder(self):
        """ContextSlot renders default when unfilled, content when filled."""
        slot = ContextSlot("user_data", default="[no data]")
        assert slot.render() == "[no data]"
        assert slot.name == "user_data"
        # Fill produces a new slot with content
        filled = slot.fill("actual data from user")
        assert filled.render() == "actual data from user"
        # Original unchanged
        assert slot.render() == "[no data]"

    def test_section_wraps_content(self):
        """Section wraps static or callable content."""
        block = Section("guidelines", "Follow PEP 8 style.", priority=7)
        text = block.render()
        assert "Follow PEP 8 style." in text
        assert block.name == "guidelines"
        assert block.priority == 7

    def test_examples_adds_few_shot(self):
        """Examples block renders few-shot examples."""
        block = Examples(
            [
                {"input": "What is 2+2?", "output": "4"},
                {"input": "What is 3+3?", "output": "6"},
            ]
        )
        text = block.render()
        assert "Examples:" in text
        assert "What is 2+2?" in text
        assert "4" in text
        assert "What is 3+3?" in text
        assert block.priority == 4

    def test_conditional_renders_only_when_true(self):
        """Conditional renders inner block only when condition is True."""
        inner = Section("inner", "visible content")
        true_cond = Conditional("show", inner, condition=lambda ctx: True)
        false_cond = Conditional("hide", inner, condition=lambda ctx: False)

        assert "visible content" in true_cond.render()
        ctx = BlockContext(state={}, active_tools=[], metadata={})
        assert false_cond.render(ctx) == ""

    def test_composite_combines_blocks(self):
        """Composite groups multiple blocks with separator."""
        comp = Composite(
            "group",
            [
                Section("a", "Part A"),
                Section("b", "Part B"),
            ],
        )
        text = comp.render()
        assert "Part A" in text
        assert "Part B" in text
        # Priority is max of children
        assert comp.priority == 5  # Both sections default to 5

    def test_blocks_helper_creates_composite_decorator(self):
        """blocks() helper decorates a function with pending blocks."""

        @blocks(Identity("Agent"), Rules(["Rule 1"]))
        def my_func():
            """Template"""

        assert hasattr(my_func, "_pending_blocks")
        assert len(my_func._pending_blocks) == 2


# ---------------------------------------------------------------------------
# 2. TestPromptAssembler -- 3 tests
# ---------------------------------------------------------------------------


class TestPromptAssembler:
    """Test composing blocks into text via PromptAssembler."""

    def test_compose_blocks_into_text(self):
        """Assembler joins multiple blocks into a single text."""
        assembler = PromptAssembler(
            Identity("Expert analyst"),
            Rules(["Be thorough", "Cite sources"]),
            Section("scope", "Focus on financial data."),
        )
        result = assembler.assemble()
        assert isinstance(result, AssembledPrompt)
        assert "Expert analyst" in result.text
        assert "Be thorough" in result.text
        assert "Focus on financial data." in result.text

    def test_activate_deactivate_blocks(self):
        """Assembler can add and remove blocks."""
        assembler = PromptAssembler()
        assembler.add(Identity("Agent"))
        assembler.add(Rules(["Rule 1"]))
        # Verify both present
        result = assembler.assemble()
        assert "Agent" in result.text
        assert "Rule 1" in result.text

        # Remove rules
        assembler.remove("rules")
        result = assembler.assemble()
        assert "Agent" in result.text
        assert "Rule 1" not in result.text

    def test_assembled_prompt_has_text_and_metadata(self):
        """AssembledPrompt carries text, included list, and token estimate."""
        assembler = PromptAssembler(
            Identity("Analyzer"),
            Rules(["Be precise"]),
        )
        result = assembler.assemble()
        assert len(result.text) > 0
        assert "identity" in result.included
        assert "rules" in result.included
        assert result.estimated_tokens > 0
        assert len(result.block_details) == 2


# ---------------------------------------------------------------------------
# 3. TestPromptDecorator -- 3 tests
# ---------------------------------------------------------------------------


class TestPromptDecorator:
    """Test @prompt() decorator and constraint()."""

    def test_prompt_decorator_creates_prompt(self):
        """@prompt() turns a function into a Prompt instance."""

        @prompt(model="openai:gpt-5-mini")
        async def summarize(text: str) -> str:
            """Summarize: {text}"""

        assert isinstance(summarize, Prompt)

    def test_prompt_has_name_and_description(self):
        """Prompt exposes name (from fn name) and template (from docstring)."""

        @prompt(model="openai:gpt-5-mini")
        async def analyze(text: str) -> str:
            """Analyze the following: {text}"""

        assert analyze.name == "analyze"
        assert "Analyze the following: {text}" in analyze.template
        assert analyze.model == "openai:gpt-5-mini"

    def test_constraint_adds_constraint(self):
        """constraint() adds a constraint to a Prompt."""

        @prompt(model="openai:gpt-5-mini")
        @constraint("Under 300 words")
        @constraint("Must cite sources")
        async def write_arg(topic: str) -> str:
            """Write about: {topic}"""

        assert "Under 300 words" in write_arg._constraints
        assert "Must cite sources" in write_arg._constraints


# ---------------------------------------------------------------------------
# 4. TestContextProviders -- 4 tests
# ---------------------------------------------------------------------------


class TestContextProviders:
    """Test context classes and @context decorator."""

    def test_user_context_provides_user_info(self):
        """UserContext stores user identity fields."""
        user = UserContext(user_id="u123", name="Alice", expertise_level="expert")
        assert user.user_id == "u123"
        assert user.name == "Alice"
        assert user.expertise_level == "expert"
        data = user.to_dict()
        assert data["user_id"] == "u123"

    def test_environment_context_provides_env_info(self):
        """EnvironmentContext stores runtime environment data."""
        env = EnvironmentContext(
            timezone="UTC",
            platform="linux",
            available_apis=["openai", "anthropic"],
        )
        assert env.timezone == "UTC"
        assert env.platform == "linux"
        assert env.available_apis == ["openai", "anthropic"]
        # timestamp is auto-set
        assert env.timestamp is not None and env.timestamp > 0

    def test_conversation_context_provides_history(self):
        """ConversationContext stores message history."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        conv = ConversationContext(messages=messages, turn_count=2)
        assert conv.turn_count == 2
        assert len(conv.messages) == 2
        assert conv.messages[0]["role"] == "user"

    def test_context_decorator_attaches_providers(self):
        """@context() decorator attaches providers to a function or Prompt."""

        class MockProvider:
            async def provide(self, ctx: PromptContext) -> str:
                return "mock context"

        provider = MockProvider()

        @context(provider)
        def my_func():
            """Template"""

        assert hasattr(my_func, "_pending_context")
        assert len(my_func._pending_context) == 1
        assert my_func._pending_context[0] is provider


# ---------------------------------------------------------------------------
# 5. TestGuards -- 3 tests
# ---------------------------------------------------------------------------


class TestGuards:
    """Test guard validation and @guard decorator."""

    @pytest.mark.asyncio
    async def test_guard_validates_output(self):
        """ContentFilterGuard passes clean output."""
        g = ContentFilterGuard(blocked=["secret"])
        result = await g.check_output("This is a safe response")
        assert result == "This is a safe response"

    @pytest.mark.asyncio
    async def test_guard_error_raised_on_invalid(self):
        """ContentFilterGuard raises GuardError when blocked word found."""
        g = ContentFilterGuard(blocked=["secret"])
        with pytest.raises(GuardError) as exc_info:
            await g.check_output("This contains a secret word")
        assert "secret" in str(exc_info.value)
        assert exc_info.value.guard_name == "content_filter"

    def test_guard_decorator_attaches_guards(self):
        """@guard() decorator attaches guards to Prompt."""
        length_guard = LengthGuard(max_length=1000)

        @prompt(model="openai:gpt-5-mini")
        @guard(length_guard)
        async def my_prompt(text: str) -> str:
            """Do something with: {text}"""

        assert length_guard in my_prompt._input_guards
        assert length_guard in my_prompt._output_guards


# ---------------------------------------------------------------------------
# 6. TestStrategies -- 3 tests
# ---------------------------------------------------------------------------


class TestStrategies:
    """Test reasoning strategies and perspectives."""

    def test_chain_of_thought_creates_strategy(self):
        """chain_of_thought is a ChainOfThoughtStrategy that wraps text."""
        assert isinstance(chain_of_thought, ChainOfThoughtStrategy)
        ctx = PromptContext(prompt_name="test", model="test")
        wrapped = chain_of_thought.wrap("Solve this problem.", ctx)
        assert "step-by-step" in wrapped.lower()
        assert "Solve this problem." in wrapped
        assert "---ANSWER---" in wrapped

    def test_structured_reasoning_creates_strategy(self):
        """structured_reasoning is a StructuredReasoningStrategy with phases."""
        assert isinstance(structured_reasoning, StructuredReasoningStrategy)
        ctx = PromptContext(prompt_name="test", model="test")
        wrapped = structured_reasoning.wrap("Evaluate options.", ctx)
        assert "Understand" in wrapped
        assert "Analyze" in wrapped
        assert "Evaluate" in wrapped or "Evaluate options." in wrapped
        assert "Conclude" in wrapped

    def test_perspective_creates_custom_perspective(self):
        """perspective() factory creates a CustomPerspective."""
        p = perspective("security auditor", "Focus on vulnerabilities.")
        assert isinstance(p, CustomPerspective)
        ctx = PromptContext(prompt_name="test", model="test")
        result = p.apply("Review this code.", ctx)
        assert "security auditor" in result
        assert "Focus on vulnerabilities." in result
        assert "Review this code." in result


# ---------------------------------------------------------------------------
# 7. TestPromptBuilder -- 3 tests
# ---------------------------------------------------------------------------


class TestPromptBuilder:
    """Test fluent PromptBuilder API."""

    def test_fluent_api_builds_prompt(self):
        """PromptBuilder chain produces a Prompt."""
        p = (
            PromptBuilder("my_prompt")
            .system("You are a helpful assistant.")
            .template("Help with: {task}")
            .model("openai:gpt-5-mini")
            .build()
        )
        assert isinstance(p, Prompt)
        assert p.name == "my_prompt"
        assert p.model == "openai:gpt-5-mini"

    def test_builder_adds_blocks_strategies_guards(self):
        """Builder attaches strategies, guards, and constraints."""
        length_g = LengthGuard(max_length=500)
        p = (
            PromptBuilder("advanced")
            .template("Analyze: {data}")
            .strategy(chain_of_thought)
            .constraint("Be concise")
            .guard(length_g)
            .build()
        )
        assert p._strategy is chain_of_thought
        assert "Be concise" in p._constraints
        assert length_g in p._input_guards
        assert length_g in p._output_guards

    def test_builder_produces_callable_prompt(self):
        """Built prompt renders template with variables."""
        p = PromptBuilder("render_test").template("Summarize in {words} words: {text}").build()
        rendered = p.render(words="100", text="long article content")
        assert "100" in rendered
        assert "long article content" in rendered


# ---------------------------------------------------------------------------
# 8. TestPromptRegistry -- 3 tests
# ---------------------------------------------------------------------------


class TestPromptRegistry:
    """Test prompt version registry."""

    def _make_prompt(self, name: str) -> Prompt:
        """Create a minimal Prompt for testing."""

        async def fn():
            """Template"""

        fn.__name__ = name
        return Prompt(fn, model="openai:gpt-5-mini")

    def test_register_and_retrieve(self):
        """Register a prompt and retrieve it by name."""
        reg = PromptRegistry()
        p = self._make_prompt("summarize")
        reg.register("summarize", "1.0.0", p)

        retrieved = reg.get("summarize", "1.0.0")
        assert retrieved is p

        # latest retrieval
        latest = reg.get("summarize")
        assert latest is p

    def test_version_tracking(self):
        """Multiple versions tracked; latest returns most recent."""
        reg = PromptRegistry()
        v1 = self._make_prompt("analyze")
        v2 = self._make_prompt("analyze")
        reg.register("analyze", "1.0.0", v1)
        reg.register("analyze", "2.0.0", v2)

        assert reg.get("analyze") is v2
        assert reg.get("analyze", "1.0.0") is v1
        assert reg.latest_version("analyze") == "2.0.0"

        # List shows both versions
        listing = reg.list()
        assert "analyze" in listing
        assert "1.0.0" in listing["analyze"]
        assert "2.0.0" in listing["analyze"]

    def test_duplicate_version_raises(self):
        """Registering same name+version twice raises ValueError."""
        reg = PromptRegistry()
        p1 = self._make_prompt("dup")
        p2 = self._make_prompt("dup")
        reg.register("dup", "1.0.0", p1)

        with pytest.raises(ValueError, match="already registered"):
            reg.register("dup", "1.0.0", p2)


# ---------------------------------------------------------------------------
# 9. TestTemplateEngine -- 2 tests
# ---------------------------------------------------------------------------


class TestTemplateEngine:
    """Test template rendering with variables and conditionals."""

    def test_render_template_with_variables(self):
        """render_template substitutes {variables} in template text."""
        result = render_template(
            "Summarize in {max_words} words: {text}",
            {"max_words": 100, "text": "some long article"},
        )
        assert "100" in result
        assert "some long article" in result

    def test_template_conditionals(self):
        """TemplateEngine handles {% if ... %} blocks."""
        engine = TemplateEngine()

        # True condition
        result_true = engine.render(
            "Start.{% if verbose %} Details here.{% endif %} End.",
            {"verbose": True},
        )
        assert "Details here." in result_true

        # False condition
        result_false = engine.render(
            "Start.{% if verbose %} Details here.{% endif %} End.",
            {"verbose": False},
        )
        assert "Details here." not in result_false
        assert "Start." in result_false
        assert "End." in result_false


# ---------------------------------------------------------------------------
# 10. TestPromptInspector -- 2 tests
# ---------------------------------------------------------------------------


class TestPromptInspector:
    """Test inspector trace creation and entries."""

    def test_inspector_creates_trace(self):
        """Inspector records an assembly as a PromptTrace."""
        inspector = PromptInspector()
        assembled = PromptAssembler(
            Identity("Analyst"),
            Rules(["Be precise"]),
        ).assemble()

        trace = inspector.record_assembly(
            assembled, prompt_name="analyze", model="openai:gpt-5-mini"
        )
        assert isinstance(trace, PromptTrace)
        assert trace.prompt_name == "analyze"
        assert trace.model == "openai:gpt-5-mini"
        assert inspector.last() is trace
        assert len(inspector.traces) == 1

    def test_trace_has_block_entries(self):
        """PromptTrace contains block details with names and priorities."""
        inspector = PromptInspector()
        assembled = PromptAssembler(
            Identity("Agent"),
            Rules(["Rule 1", "Rule 2"]),
            Section("scope", "Focus on security."),
        ).assemble()

        trace = inspector.record_assembly(assembled, prompt_name="review", model="openai:gpt-4o")
        assert len(trace.blocks) == 3
        block_names = [b.name for b in trace.blocks]
        assert "identity" in block_names
        assert "rules" in block_names
        assert "scope" in block_names
        assert trace.total_tokens_estimated > 0
        assert "identity" in trace.blocks_included
        assert "rules" in trace.blocks_included
        assert "scope" in trace.blocks_included


# ---------------------------------------------------------------------------
# Integration: Cross-subsystem tests
# ---------------------------------------------------------------------------


class TestCrossSubsystemIntegration:
    """Integration tests wiring multiple prompt subsystems together."""

    def test_prompt_render_with_blocks_and_constraints(self):
        """Prompt.render() integrates blocks and constraints into text."""

        @prompt(model="openai:gpt-5-mini")
        @constraint("Under 200 words")
        @blocks(Identity("Expert"), Rules(["Be accurate"]))
        async def analyze(text: str) -> str:
            """Analyze the following: {text}"""

        rendered = analyze.render(text="quarterly report")
        assert "Expert" in rendered
        assert "Be accurate" in rendered
        assert "quarterly report" in rendered
        assert "Under 200 words" in rendered

    def test_prompt_render_with_strategy_and_perspective(self):
        """Prompt with strategy and perspective renders combined text."""

        @prompt(model="openai:gpt-5-mini")
        async def solve(problem: str) -> str:
            """Solve: {problem}"""

        configured = solve.with_strategy(chain_of_thought).with_perspective(
            perspective("mathematician")
        )
        rendered = configured.render(problem="factor x^2 - 1")
        assert "mathematician" in rendered
        assert "step-by-step" in rendered.lower()
        assert "factor x^2 - 1" in rendered

    def test_builder_with_world_context_renders(self):
        """Builder with world context produces renderable Prompt."""
        p = (
            PromptBuilder("contextualized")
            .template("Help the user with: {task}")
            .user(UserContext(name="Bob", expertise_level="beginner"))
            .build()
        )
        assert isinstance(p, Prompt)
        assert p._world.get("user") is not None
        rendered = p.render(task="debugging")
        assert "debugging" in rendered

    def test_inspector_with_assembler_records_trace(self):
        """Inspector records assembly trace from PromptAssembler."""
        inspector = PromptInspector()
        assembler = PromptAssembler(
            Identity("Security auditor"),
            Rules(["Follow OWASP guidelines"]),
            Examples(
                [
                    {"input": "SQL injection", "output": "Vulnerable"},
                ]
            ),
        )
        result = assembler.assemble()
        trace = inspector.record_assembly(result, prompt_name="audit", model="openai:gpt-4o")

        summary = inspector.summary()
        assert "audit" in summary
        assert "identity" in summary or "identity" in str(trace.blocks_included)

    @pytest.mark.asyncio
    async def test_guard_integration_with_content_filter(self):
        """ContentFilterGuard blocks input containing forbidden words."""
        g = ContentFilterGuard(blocked=["password", "token"])

        # Clean input passes
        clean = await g.check_input("Please analyze this code")
        assert clean == "Please analyze this code"

        # Blocked input raises
        with pytest.raises(GuardError):
            await g.check_input("Here is my password")

    @pytest.mark.asyncio
    async def test_length_guard_validates_output_bounds(self):
        """LengthGuard enforces min/max character limits on output."""
        g = LengthGuard(min_length=10, max_length=100)

        # Valid length passes
        result = await g.check_output("This is a valid length response.")
        assert result == "This is a valid length response."

        # Too short
        with pytest.raises(GuardError, match="too short"):
            await g.check_output("Short")

        # Too long
        with pytest.raises(GuardError, match="too long"):
            await g.check_output("x" * 200)
