"""Comprehensive tests for the Prompt & Context Engineering Framework.

All LLM calls are mocked — no real API keys required.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Template engine tests
# ---------------------------------------------------------------------------


class TestTemplateEngine:
    """Tests for prompts/template.py."""

    def test_simple_variable(self):
        from promptise.prompts.template import render_template

        result = render_template("Hello {name}", {"name": "World"})
        assert result == "Hello World"

    def test_multiple_variables(self):
        from promptise.prompts.template import render_template

        result = render_template(
            "Summarize in {words} words: {text}",
            {"words": 100, "text": "article"},
        )
        assert "100" in result
        assert "article" in result

    def test_conditional_true(self):
        from promptise.prompts.template import render_template

        result = render_template("{% if verbose %}Show details{% endif %}", {"verbose": True})
        assert "Show details" in result

    def test_conditional_false(self):
        from promptise.prompts.template import render_template

        result = render_template("{% if verbose %}Show details{% endif %}", {"verbose": False})
        assert "Show details" not in result

    def test_if_else(self):
        from promptise.prompts.template import render_template

        template = "{% if expert %}Advanced{% else %}Basic{% endif %} mode"
        assert render_template(template, {"expert": True}) == "Advanced mode"
        assert render_template(template, {"expert": False}) == "Basic mode"

    def test_for_loop(self):
        from promptise.prompts.template import render_template

        template = "Items: {% for item in items %}{item}, {% endfor %}"
        result = render_template(template, {"items": ["a", "b", "c"]})
        assert "a" in result
        assert "b" in result
        assert "c" in result

    def test_literal_braces(self):
        from promptise.prompts.template import render_template

        result = render_template("Use {{braces}} for JSON", {})
        assert result == "Use {braces} for JSON"

    def test_include(self):
        from promptise.prompts.template import render_template

        result = render_template(
            '{% include "header" %} Body',
            {},
            includes={"header": "# Title"},
        )
        assert "# Title" in result
        assert "Body" in result

    def test_include_missing_raises(self):
        from promptise.prompts.template import render_template

        with pytest.raises(ValueError, match="not found"):
            render_template('{% include "missing" %}', {})

    def test_missing_variable_raises(self):
        from promptise.prompts.template import render_template

        with pytest.raises(KeyError):
            render_template("Hello {name}", {})

    def test_engine_class(self):
        from promptise.prompts.template import TemplateEngine

        engine = TemplateEngine(includes={"sig": "-- Bot"})
        result = engine.render('{greeting} {% include "sig" %}', {"greeting": "Hi"})
        assert result == "Hi -- Bot"


# ---------------------------------------------------------------------------
# BaseContext tests
# ---------------------------------------------------------------------------


class TestBaseContext:
    """Tests for BaseContext and predefined context classes."""

    def test_create_with_kwargs(self):
        from promptise.prompts.context import BaseContext

        ctx = BaseContext(project="alpha", priority="high")
        assert ctx.project == "alpha"
        assert ctx.priority == "high"

    def test_getattr_missing_returns_none(self):
        from promptise.prompts.context import BaseContext

        ctx = BaseContext()
        assert ctx.nonexistent is None

    def test_setattr(self):
        from promptise.prompts.context import BaseContext

        ctx = BaseContext()
        ctx.budget = 50000
        assert ctx.budget == 50000

    def test_getitem(self):
        from promptise.prompts.context import BaseContext

        ctx = BaseContext(name="test")
        assert ctx["name"] == "test"

    def test_setitem(self):
        from promptise.prompts.context import BaseContext

        ctx = BaseContext()
        ctx["key"] = "value"
        assert ctx["key"] == "value"

    def test_contains(self):
        from promptise.prompts.context import BaseContext

        ctx = BaseContext(x=1)
        assert "x" in ctx
        assert "y" not in ctx

    def test_get_default(self):
        from promptise.prompts.context import BaseContext

        ctx = BaseContext(a=1)
        assert ctx.get("a") == 1
        assert ctx.get("b", 42) == 42

    def test_to_dict(self):
        from promptise.prompts.context import BaseContext

        ctx = BaseContext(x=1, y=2)
        d = ctx.to_dict()
        assert d == {"x": 1, "y": 2}

    def test_merge(self):
        from promptise.prompts.context import BaseContext

        a = BaseContext(x=1, y=2)
        b = BaseContext(y=3, z=4)
        merged = a.merge(b)
        assert merged.to_dict() == {"x": 1, "y": 3, "z": 4}

    def test_keys(self):
        from promptise.prompts.context import BaseContext

        ctx = BaseContext(a=1, b=2)
        assert set(ctx.keys()) == {"a", "b"}

    def test_repr(self):
        from promptise.prompts.context import BaseContext

        ctx = BaseContext(a=1)
        assert "BaseContext" in repr(ctx)
        assert "a=1" in repr(ctx)


class TestUserContext:
    """Tests for UserContext."""

    def test_predefined_fields(self):
        from promptise.prompts.context import UserContext

        user = UserContext(user_id="123", name="Alice")
        assert user.user_id == "123"
        assert user.name == "Alice"
        assert user.expertise_level == "intermediate"  # default

    def test_custom_kwargs(self):
        from promptise.prompts.context import UserContext

        user = UserContext(user_id="123", department="eng", clearance="top")
        assert user.department == "eng"
        assert user.clearance == "top"

    def test_extends_base(self):
        from promptise.prompts.context import BaseContext, UserContext

        assert isinstance(UserContext(), BaseContext)


class TestEnvironmentContext:
    """Tests for EnvironmentContext."""

    def test_defaults(self):
        from promptise.prompts.context import EnvironmentContext

        env = EnvironmentContext()
        assert env.timestamp is not None
        assert isinstance(env.timestamp, float)

    def test_custom_fields(self):
        from promptise.prompts.context import EnvironmentContext

        env = EnvironmentContext(region="us-west-2", gpu_available=True)
        assert env.region == "us-west-2"
        assert env.gpu_available is True


# ---------------------------------------------------------------------------
# Guard tests
# ---------------------------------------------------------------------------


class TestGuards:
    """Tests for Guard protocol and built-in guards."""

    @pytest.mark.asyncio
    async def test_content_filter_blocked(self):
        from promptise.prompts.guards import ContentFilterGuard, GuardError

        g = ContentFilterGuard(blocked=["bad"])
        with pytest.raises(GuardError, match="blocked"):
            await g.check_input("this is bad")

    @pytest.mark.asyncio
    async def test_content_filter_pass(self):
        from promptise.prompts.guards import ContentFilterGuard

        g = ContentFilterGuard(blocked=["bad"])
        result = await g.check_input("this is good")
        assert result == "this is good"

    @pytest.mark.asyncio
    async def test_content_filter_required(self):
        from promptise.prompts.guards import ContentFilterGuard, GuardError

        g = ContentFilterGuard(required=["citation"])
        with pytest.raises(GuardError, match="missing required"):
            await g.check_output("no references here")

    @pytest.mark.asyncio
    async def test_length_guard(self):
        from promptise.prompts.guards import GuardError, LengthGuard

        g = LengthGuard(min_length=10, max_length=20)
        await g.check_output("12345678901")  # 11 chars, OK
        with pytest.raises(GuardError, match="too short"):
            await g.check_output("short")
        with pytest.raises(GuardError, match="too long"):
            await g.check_output("x" * 30)

    @pytest.mark.asyncio
    async def test_input_validator_guard(self):
        from promptise.prompts.guards import InputValidatorGuard

        def upper(text: str) -> str:
            return text.upper()

        g = InputValidatorGuard(upper)
        result = await g.check_input("hello")
        assert result == "HELLO"

    @pytest.mark.asyncio
    async def test_output_validator_guard(self):
        from promptise.prompts.guards import OutputValidatorGuard

        async def validate(output: str) -> str:
            return output.strip()

        g = OutputValidatorGuard(validate)
        result = await g.check_output("  padded  ")
        assert result == "padded"

    def test_convenience_constructors(self):
        from promptise.prompts.guards import (
            SchemaStrictGuard,
            content_filter,
            input_validator,
            length,
            output_validator,
            schema_strict,
        )

        assert content_filter(blocked=["x"])._blocked == ["x"]
        assert isinstance(schema_strict(), SchemaStrictGuard)
        assert length(min_length=10)._min == 10
        assert input_validator(lambda x: x)._fn is not None
        assert output_validator(lambda x: x)._fn is not None

    def test_guard_error(self):
        from promptise.prompts.guards import GuardError

        err = GuardError("too long", guard_name="length")
        assert err.guard_name == "length"
        assert err.reason == "too long"
        assert str(err) == "too long"


# ---------------------------------------------------------------------------
# Context provider tests
# ---------------------------------------------------------------------------


class TestContextProviders:
    """Tests for built-in context providers."""

    @pytest.mark.asyncio
    async def test_static_provider(self):
        from promptise.prompts.context import PromptContext, StaticContextProvider

        p = StaticContextProvider("Always be helpful.", header="Instructions")
        ctx = PromptContext()
        result = await p.provide(ctx)
        assert "Always be helpful." in result
        assert "Instructions" in result

    @pytest.mark.asyncio
    async def test_callable_provider(self):
        from promptise.prompts.context import CallableContextProvider, PromptContext

        async def my_fn(ctx: PromptContext) -> str:
            return f"Model is {ctx.model}"

        p = CallableContextProvider(my_fn, header="Info")
        ctx = PromptContext(model="gpt-4o")
        result = await p.provide(ctx)
        assert "gpt-4o" in result

    @pytest.mark.asyncio
    async def test_user_context_provider(self):
        from promptise.prompts.context import (
            PromptContext,
            UserContext,
            UserContextProvider,
        )

        ctx = PromptContext()
        ctx.world["user"] = UserContext(name="Alice", expertise_level="expert")
        p = UserContextProvider()
        result = await p.provide(ctx)
        assert "Alice" in result
        assert "expert" in result

    @pytest.mark.asyncio
    async def test_user_context_provider_empty(self):
        from promptise.prompts.context import PromptContext, UserContextProvider

        ctx = PromptContext()
        p = UserContextProvider()
        result = await p.provide(ctx)
        assert result == ""

    @pytest.mark.asyncio
    async def test_environment_provider(self):
        from promptise.prompts.context import (
            EnvironmentContext,
            EnvironmentContextProvider,
            PromptContext,
        )

        ctx = PromptContext()
        ctx.world["environment"] = EnvironmentContext(platform="linux")
        p = EnvironmentContextProvider()
        result = await p.provide(ctx)
        assert "linux" in result

    @pytest.mark.asyncio
    async def test_error_context_provider(self):
        from promptise.prompts.context import (
            ErrorContext,
            ErrorContextProvider,
            PromptContext,
        )

        ctx = PromptContext()
        ctx.world["errors"] = ErrorContext(last_error="timeout", retry_count=2)
        p = ErrorContextProvider()
        result = await p.provide(ctx)
        assert "timeout" in result

    @pytest.mark.asyncio
    async def test_conditional_provider(self):
        from promptise.prompts.context import (
            ConditionalContextProvider,
            PromptContext,
            StaticContextProvider,
        )

        inner = StaticContextProvider("secret data")
        p = ConditionalContextProvider(
            condition=lambda ctx: ctx.model == "gpt-4o",
            provider=inner,
        )

        ctx_match = PromptContext(model="gpt-4o")
        result = await p.provide(ctx_match)
        assert "secret data" in result

        ctx_no_match = PromptContext(model="gpt-3.5")
        result = await p.provide(ctx_no_match)
        assert result == ""

    @pytest.mark.asyncio
    async def test_world_context_provider(self):
        from promptise.prompts.context import (
            BaseContext,
            PromptContext,
            WorldContextProvider,
        )

        ctx = PromptContext()
        ctx.world["project"] = BaseContext(name="Alpha", sprint="Q1")
        p = WorldContextProvider("project")
        result = await p.provide(ctx)
        assert "Alpha" in result
        assert "Q1" in result

    @pytest.mark.asyncio
    async def test_output_context_provider(self):
        from promptise.prompts.context import (
            OutputContext,
            OutputContextProvider,
            PromptContext,
        )

        ctx = PromptContext()
        ctx.world["output"] = OutputContext(
            format="json",
            constraints=["under 500 words"],
        )
        p = OutputContextProvider()
        result = await p.provide(ctx)
        assert "json" in result


# ---------------------------------------------------------------------------
# Strategy tests
# ---------------------------------------------------------------------------


class TestStrategies:
    """Tests for reasoning strategies."""

    def test_chain_of_thought_wrap(self):
        from promptise.prompts.context import PromptContext
        from promptise.prompts.strategies import ChainOfThoughtStrategy

        s = ChainOfThoughtStrategy()
        ctx = PromptContext()
        wrapped = s.wrap("Solve this problem", ctx)
        assert "step-by-step" in wrapped
        assert "---ANSWER---" in wrapped

    def test_chain_of_thought_parse(self):
        from promptise.prompts.context import PromptContext
        from promptise.prompts.strategies import ChainOfThoughtStrategy

        s = ChainOfThoughtStrategy()
        ctx = PromptContext()
        raw = "Step 1: blah\nStep 2: blah\n---ANSWER---\nThe final answer is 42."
        parsed = s.parse(raw, ctx)
        assert parsed == "The final answer is 42."

    def test_self_critique_wrap(self):
        from promptise.prompts.context import PromptContext
        from promptise.prompts.strategies import SelfCritiqueStrategy

        s = SelfCritiqueStrategy(rounds=2)
        ctx = PromptContext()
        wrapped = s.wrap("Analyze this", ctx)
        assert "Critique" in wrapped
        assert "round 1" in wrapped
        assert "round 2" in wrapped

    def test_plan_and_execute(self):
        from promptise.prompts.context import PromptContext
        from promptise.prompts.strategies import PlanAndExecuteStrategy

        s = PlanAndExecuteStrategy()
        ctx = PromptContext()
        wrapped = s.wrap("Build a plan", ctx)
        assert "Plan" in wrapped
        assert "Execute" in wrapped
        assert "Synthesize" in wrapped

    def test_decompose(self):
        from promptise.prompts.context import PromptContext
        from promptise.prompts.strategies import DecomposeStrategy

        s = DecomposeStrategy()
        ctx = PromptContext()
        wrapped = s.wrap("Complex problem", ctx)
        assert "subproblems" in wrapped

    def test_structured_reasoning_custom_steps(self):
        from promptise.prompts.context import PromptContext
        from promptise.prompts.strategies import StructuredReasoningStrategy

        s = StructuredReasoningStrategy(steps=["Define", "Research", "Decide"])
        ctx = PromptContext()
        wrapped = s.wrap("Question", ctx)
        assert "Define" in wrapped
        assert "Research" in wrapped
        assert "Decide" in wrapped

    def test_composite_strategy(self):
        from promptise.prompts.context import PromptContext
        from promptise.prompts.strategies import (
            ChainOfThoughtStrategy,
            SelfCritiqueStrategy,
        )

        combined = ChainOfThoughtStrategy() + SelfCritiqueStrategy()
        ctx = PromptContext()
        wrapped = combined.wrap("Test", ctx)
        assert "step-by-step" in wrapped
        assert "Critique" in wrapped

    def test_composite_parse_reverse_order(self):
        from promptise.prompts.context import PromptContext
        from promptise.prompts.strategies import (
            ChainOfThoughtStrategy,
            SelfCritiqueStrategy,
        )

        combined = ChainOfThoughtStrategy() + SelfCritiqueStrategy()
        ctx = PromptContext()
        # Parse applies in reverse — SelfCritique first, then CoT
        raw = "blah\n---ANSWER---\nFinal answer"
        parsed = combined.parse(raw, ctx)
        assert parsed == "Final answer"


# ---------------------------------------------------------------------------
# Perspective tests
# ---------------------------------------------------------------------------


class TestPerspectives:
    """Tests for cognitive perspectives."""

    def test_analyst(self):
        from promptise.prompts.context import PromptContext
        from promptise.prompts.strategies import AnalystPerspective

        p = AnalystPerspective()
        ctx = PromptContext()
        result = p.apply("Evaluate options", ctx)
        assert "analyst" in result.lower()
        assert "Evaluate options" in result

    def test_critic(self):
        from promptise.prompts.context import PromptContext
        from promptise.prompts.strategies import CriticPerspective

        p = CriticPerspective()
        ctx = PromptContext()
        result = p.apply("Review proposal", ctx)
        assert "critic" in result.lower()

    def test_advisor(self):
        from promptise.prompts.context import PromptContext
        from promptise.prompts.strategies import AdvisorPerspective

        p = AdvisorPerspective()
        ctx = PromptContext()
        result = p.apply("Help decide", ctx)
        assert "advisor" in result.lower()

    def test_creative(self):
        from promptise.prompts.context import PromptContext
        from promptise.prompts.strategies import CreativePerspective

        p = CreativePerspective()
        ctx = PromptContext()
        result = p.apply("Brainstorm ideas", ctx)
        assert "creativ" in result.lower()

    def test_custom_perspective(self):
        from promptise.prompts.context import PromptContext
        from promptise.prompts.strategies import CustomPerspective

        p = CustomPerspective(
            role="security auditor",
            instructions="Focus on OWASP top 10.",
        )
        ctx = PromptContext()
        result = p.apply("Review code", ctx)
        assert "security auditor" in result
        assert "OWASP" in result


# ---------------------------------------------------------------------------
# Prompt decorator tests
# ---------------------------------------------------------------------------


class TestPromptDecorator:
    """Tests for @prompt decorator and Prompt class."""

    def test_creates_prompt(self):
        from promptise.prompts.core import Prompt, prompt

        @prompt(model="openai:gpt-5-mini")
        async def summarize(text: str) -> str:
            """Summarize: {text}"""

        assert isinstance(summarize, Prompt)
        assert summarize.name == "summarize"
        assert summarize.model == "openai:gpt-5-mini"
        assert summarize.template == "Summarize: {text}"
        assert summarize.return_type is str

    def test_with_model(self):
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Test: {text}"""

        modified = fn.with_model("openai:gpt-4o")
        assert modified.model == "openai:gpt-4o"
        assert fn.model == "openai:gpt-5-mini"  # original unchanged

    def test_with_context(self):
        from promptise.prompts.context import StaticContextProvider
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Test: {text}"""

        modified = fn.with_context(StaticContextProvider("extra"))
        assert len(modified._context_providers) == 1
        assert len(fn._context_providers) == 0  # original unchanged

    def test_with_strategy(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.strategies import ChainOfThoughtStrategy

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Test: {text}"""

        modified = fn.with_strategy(ChainOfThoughtStrategy())
        assert modified._strategy is not None
        assert fn._strategy is None

    def test_with_perspective(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.strategies import AnalystPerspective

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Test: {text}"""

        modified = fn.with_perspective(AnalystPerspective())
        assert modified._perspective is not None

    def test_with_constraints(self):
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Test: {text}"""

        modified = fn.with_constraints("Under 100 words", "Be concise")
        assert len(modified._constraints) == 2

    def test_with_guards(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.guards import ContentFilterGuard

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Test: {text}"""

        modified = fn.with_guards(ContentFilterGuard(blocked=["bad"]))
        assert len(modified._input_guards) == 1

    def test_with_world(self):
        from promptise.prompts.context import BaseContext, UserContext
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Test: {text}"""

        modified = fn.with_world(
            user=UserContext(name="Alice"),
            project=BaseContext(name="Alpha"),
        )
        assert "user" in modified._world
        assert "project" in modified._world


# ---------------------------------------------------------------------------
# Prompt execution tests (mocked LLM)
# ---------------------------------------------------------------------------


class TestPromptExecution:
    """Tests for Prompt.__call__() with mocked LLM."""

    @pytest.mark.asyncio
    async def test_basic_execution(self):
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def greet(name: str) -> str:
            """Hello {name}, how are you?"""

        mock_msg = MagicMock()
        mock_msg.content = "I'm great, thanks!"
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_msg)

        with patch(
            "promptise.prompts.core.init_chat_model",
            return_value=mock_model,
        ):
            result = await greet("Alice")

        assert result == "I'm great, thanks!"
        assert greet.last_stats is not None
        assert greet.last_stats.prompt_name == "greet"

    @pytest.mark.asyncio
    async def test_strategy_applied(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.strategies import ChainOfThoughtStrategy

        @prompt(model="openai:gpt-5-mini")
        async def solve(problem: str) -> str:
            """Solve: {problem}"""

        configured = solve.with_strategy(ChainOfThoughtStrategy())

        mock_msg = MagicMock()
        mock_msg.content = "Reasoning...\n---ANSWER---\n42"
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_msg)

        with patch(
            "promptise.prompts.core.init_chat_model",
            return_value=mock_model,
        ):
            result = await configured("What is 6*7?")

        assert result == "42"

    @pytest.mark.asyncio
    async def test_guard_blocks_input(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.guards import ContentFilterGuard, GuardError

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Process: {text}"""

        configured = fn.with_guards(ContentFilterGuard(blocked=["forbidden"]))

        with pytest.raises(GuardError, match="blocked"):
            await configured("this is forbidden content")

    @pytest.mark.asyncio
    async def test_context_providers_run(self):
        from promptise.prompts.context import StaticContextProvider
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Process: {text}"""

        configured = fn.with_context(StaticContextProvider("Rule: Be brief", header="Rules"))

        mock_msg = MagicMock()
        mock_msg.content = "Brief response"
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_msg)

        with patch(
            "promptise.prompts.core.init_chat_model",
            return_value=mock_model,
        ):
            await configured("test input")

        # Verify the SystemMessage contained context
        call_args = mock_model.ainvoke.call_args[0][0]
        system_text = call_args[0].content
        assert "Rule: Be brief" in system_text


# ---------------------------------------------------------------------------
# Prompt render tests (no LLM)
# ---------------------------------------------------------------------------


class TestPromptRender:
    """Tests for render() and render_async() — no LLM call."""

    def test_render_basic(self):
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Analyze: {text}"""

        rendered = fn.render(text="hello")
        assert "Analyze: hello" in rendered

    def test_render_with_constraints(self):
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Analyze: {text}"""

        configured = fn.with_constraints("Be concise")
        rendered = configured.render(text="test")
        assert "Be concise" in rendered

    def test_render_with_perspective(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.strategies import AnalystPerspective

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Analyze: {text}"""

        configured = fn.with_perspective(AnalystPerspective())
        rendered = configured.render(text="data")
        assert "analyst" in rendered.lower()

    @pytest.mark.asyncio
    async def test_render_async_with_providers(self):
        from promptise.prompts.context import StaticContextProvider
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Analyze: {text}"""

        configured = fn.with_context(StaticContextProvider("Extra context"))
        rendered = await configured.render_async(text="data")
        assert "Extra context" in rendered
        assert "Analyze: data" in rendered


# ---------------------------------------------------------------------------
# Constraint tests
# ---------------------------------------------------------------------------


class TestConstraints:
    """Tests for @constraint decorator."""

    def test_constraint_decorator(self):
        from promptise.prompts.core import constraint, prompt

        @prompt(model="openai:gpt-5-mini")
        @constraint("Must cite sources")
        @constraint("Under 300 words")
        async def fn(topic: str) -> str:
            """Write about: {topic}"""

        assert "Must cite sources" in fn._constraints
        assert "Under 300 words" in fn._constraints


# ---------------------------------------------------------------------------
# Lifecycle hook tests
# ---------------------------------------------------------------------------


class TestLifecycleHooks:
    """Tests for on_before, on_after, on_error hooks."""

    @pytest.mark.asyncio
    async def test_on_before(self):
        from promptise.prompts.core import prompt

        called = []

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Process: {text}"""

        configured = fn.on_before(lambda ctx: called.append("before"))

        mock_msg = MagicMock()
        mock_msg.content = "ok"
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_msg)

        with patch(
            "promptise.prompts.core.init_chat_model",
            return_value=mock_model,
        ):
            await configured("test")

        assert "before" in called

    @pytest.mark.asyncio
    async def test_on_after(self):
        from promptise.prompts.core import prompt

        results = []

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Process: {text}"""

        configured = fn.on_after(lambda ctx, r: results.append(r))

        mock_msg = MagicMock()
        mock_msg.content = "done"
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_msg)

        with patch(
            "promptise.prompts.core.init_chat_model",
            return_value=mock_model,
        ):
            await configured("test")

        assert "done" in results

    @pytest.mark.asyncio
    async def test_on_error(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.guards import ContentFilterGuard

        errors = []

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Process: {text}"""

        configured = fn.with_guards(ContentFilterGuard(blocked=["bad"])).on_error(
            lambda ctx, exc: errors.append(type(exc).__name__)
        )

        with pytest.raises(Exception):
            await configured("bad content")

        assert "GuardError" in errors


# ---------------------------------------------------------------------------
# Structured output tests
# ---------------------------------------------------------------------------


class TestStructuredOutput:
    """Tests for output parsing with different return types."""

    @pytest.mark.asyncio
    async def test_int_output(self):
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> int:
            """Count: {text}"""

        mock_msg = MagicMock()
        mock_msg.content = "The answer is 42."
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_msg)

        with patch(
            "promptise.prompts.core.init_chat_model",
            return_value=mock_model,
        ):
            result = await fn("test")

        assert result == 42

    @pytest.mark.asyncio
    async def test_list_output(self):
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> list:
            """List: {text}"""

        mock_msg = MagicMock()
        mock_msg.content = '```json\n["a", "b", "c"]\n```'
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_msg)

        with patch(
            "promptise.prompts.core.init_chat_model",
            return_value=mock_model,
        ):
            result = await fn("test")

        assert result == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_dataclass_output(self):
        from promptise.prompts.core import Prompt

        @dataclass
        class Result:
            score: float
            label: str

        # Build prompt manually to avoid get_type_hints issues
        # with locally-defined types under `from __future__ import annotations`
        async def fn(text: str) -> Result:
            """Classify: {text}"""

        fn.__annotations__["return"] = Result
        p = Prompt(fn, model="openai:gpt-5-mini")

        mock_msg = MagicMock()
        mock_msg.content = '{"score": 0.95, "label": "positive"}'
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_msg)

        with patch(
            "promptise.prompts.core.init_chat_model",
            return_value=mock_model,
        ):
            result = await p("great product")

        assert isinstance(result, Result)
        assert result.score == 0.95
        assert result.label == "positive"


# ---------------------------------------------------------------------------
# PromptBuilder tests
# ---------------------------------------------------------------------------


class TestPromptBuilder:
    """Tests for PromptBuilder fluent API."""

    def test_basic_build(self):
        from promptise.prompts.builder import PromptBuilder
        from promptise.prompts.core import Prompt

        p = PromptBuilder("test_prompt").template("Hello {name}").model("openai:gpt-4o").build()
        assert isinstance(p, Prompt)
        assert p.name == "test_prompt"
        assert p.model == "openai:gpt-4o"

    def test_full_builder(self):
        from promptise.prompts.builder import PromptBuilder
        from promptise.prompts.context import StaticContextProvider, UserContext
        from promptise.prompts.guards import ContentFilterGuard
        from promptise.prompts.strategies import (
            AnalystPerspective,
            ChainOfThoughtStrategy,
        )

        p = (
            PromptBuilder("analysis")
            .system("You are an expert analyst.")
            .template("Analyze: {data}")
            .user(UserContext(name="Alice"))
            .context(StaticContextProvider("Rule: cite sources"))
            .strategy(ChainOfThoughtStrategy())
            .perspective(AnalystPerspective())
            .constraint("Under 500 words")
            .guard(ContentFilterGuard(blocked=["classified"]))
            .model("openai:gpt-4o")
            .build()
        )

        assert p._strategy is not None
        assert p._perspective is not None
        assert len(p._constraints) == 1
        assert len(p._input_guards) == 1
        assert "user" in p._world

    def test_builder_no_template(self):
        from promptise.prompts.builder import PromptBuilder

        p = PromptBuilder("empty").build()
        assert p.template != ""  # should have default text


# ---------------------------------------------------------------------------
# PromptSuite tests
# ---------------------------------------------------------------------------


class TestPromptSuite:
    """Tests for PromptSuite."""

    def test_suite_discovery(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.suite import PromptSuite

        class MySuite(PromptSuite):
            @prompt(model="openai:gpt-5-mini")
            async def task_a(self, text: str) -> str:
                """Task A: {text}"""

            @prompt(model="openai:gpt-5-mini")
            async def task_b(self, text: str) -> str:
                """Task B: {text}"""

        suite = MySuite()
        prompts = suite.prompts
        assert "task_a" in prompts
        assert "task_b" in prompts

    def test_suite_defaults_applied(self):
        from promptise.prompts.context import StaticContextProvider
        from promptise.prompts.core import prompt
        from promptise.prompts.suite import PromptSuite

        class MySuite(PromptSuite):
            context_providers = [StaticContextProvider("suite context")]
            default_constraints = ["Be concise"]

            @prompt(model="openai:gpt-5-mini")
            async def task(self, text: str) -> str:
                """Do: {text}"""

        suite = MySuite()
        p = suite.prompts["task"]
        assert len(p._context_providers) >= 1
        assert "Be concise" in p._constraints

    def test_suite_system_prompt(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.suite import PromptSuite

        class MySuite(PromptSuite):
            @prompt(model="openai:gpt-5-mini")
            async def analyze(self, text: str) -> str:
                """Analyze data"""

        suite = MySuite()
        system = suite.system_prompt()
        assert "analyze" in system


# ---------------------------------------------------------------------------
# Chain operator tests
# ---------------------------------------------------------------------------


class TestChainOperators:
    """Tests for chain, parallel, branch, retry, fallback."""

    def test_chain_requires_two(self):
        from promptise.prompts.chain import chain
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Do: {text}"""

        with pytest.raises(ValueError, match="at least 2"):
            chain(fn)

    def test_parallel_requires_two(self):
        from promptise.prompts.chain import parallel
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Do: {text}"""

        with pytest.raises(ValueError, match="at least 2"):
            parallel(a=fn)

    def test_branch_creation(self):
        from promptise.prompts.chain import branch
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn_a(text: str) -> str:
            """A: {text}"""

        @prompt(model="openai:gpt-5-mini")
        async def fn_b(text: str) -> str:
            """B: {text}"""

        b = branch(
            condition=lambda text: "a" if "alpha" in text else "b",
            routes={"a": fn_a, "b": fn_b},
        )
        assert b is not None

    def test_retry_creation(self):
        from promptise.prompts.chain import retry
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Do: {text}"""

        r = retry(fn, max_retries=3, backoff=0.5)
        assert r._max_retries == 3

    def test_fallback_creation(self):
        from promptise.prompts.chain import fallback
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def primary(text: str) -> str:
            """Primary: {text}"""

        @prompt(model="openai:gpt-5-mini")
        async def backup(text: str) -> str:
            """Backup: {text}"""

        f = fallback(primary, backup)
        assert len(f._prompts) == 2


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistry:
    """Tests for PromptRegistry."""

    def test_register_and_get(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.registry import PromptRegistry

        reg = PromptRegistry()

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Do: {text}"""

        reg.register("fn", "1.0.0", fn)
        assert reg.get("fn") is fn
        assert reg.get("fn", "1.0.0") is fn

    def test_versions(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.registry import PromptRegistry

        reg = PromptRegistry()

        @prompt(model="openai:gpt-5-mini")
        async def fn_v1(text: str) -> str:
            """V1: {text}"""

        @prompt(model="openai:gpt-4o")
        async def fn_v2(text: str) -> str:
            """V2: {text}"""

        reg.register("fn", "1.0.0", fn_v1)
        reg.register("fn", "2.0.0", fn_v2)

        assert reg.get("fn") is fn_v2  # latest
        assert reg.get("fn", "1.0.0") is fn_v1
        assert reg.latest_version("fn") == "2.0.0"
        assert reg.list() == {"fn": ["1.0.0", "2.0.0"]}

    def test_rollback(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.registry import PromptRegistry

        reg = PromptRegistry()

        @prompt(model="openai:gpt-5-mini")
        async def fn_v1(text: str) -> str:
            """V1: {text}"""

        @prompt(model="openai:gpt-4o")
        async def fn_v2(text: str) -> str:
            """V2: {text}"""

        reg.register("fn", "1.0.0", fn_v1)
        reg.register("fn", "2.0.0", fn_v2)
        rolled_back = reg.rollback("fn")
        assert rolled_back is fn_v1
        assert reg.latest_version("fn") == "1.0.0"

    def test_duplicate_version_raises(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.registry import PromptRegistry

        reg = PromptRegistry()

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Do: {text}"""

        reg.register("fn", "1.0.0", fn)
        with pytest.raises(ValueError, match="already registered"):
            reg.register("fn", "1.0.0", fn)

    def test_not_found_raises(self):
        from promptise.prompts.registry import PromptRegistry

        reg = PromptRegistry()
        with pytest.raises(KeyError, match="not found"):
            reg.get("nonexistent")

    def test_clear(self):
        from promptise.prompts.core import prompt
        from promptise.prompts.registry import PromptRegistry

        reg = PromptRegistry()

        @prompt(model="openai:gpt-5-mini")
        async def fn(text: str) -> str:
            """Do: {text}"""

        reg.register("fn", "1.0.0", fn)
        reg.clear()
        assert reg.list() == {}


# ---------------------------------------------------------------------------
# Observability events test
# ---------------------------------------------------------------------------


class TestObservabilityEvents:
    """Tests for PROMPT_* event types."""

    def test_prompt_events_exist(self):
        from promptise.observability import TimelineEventType

        assert hasattr(TimelineEventType, "PROMPT_START")
        assert hasattr(TimelineEventType, "PROMPT_END")
        assert hasattr(TimelineEventType, "PROMPT_ERROR")
        assert hasattr(TimelineEventType, "PROMPT_GUARD_BLOCK")
        assert hasattr(TimelineEventType, "PROMPT_CONTEXT")

    def test_prompt_events_categorized(self):
        from promptise.observability import (
            TimelineEventCategory,
            TimelineEventType,
            _derive_category,
        )

        cat = _derive_category(TimelineEventType.PROMPT_START)
        assert cat == TimelineEventCategory.PROMPT


# ---------------------------------------------------------------------------
# Orchestration agent integration test
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Top-level import tests
# ---------------------------------------------------------------------------


class TestTopLevelImports:
    """Tests for top-level package imports."""

    def test_prompt_imports(self):
        from promptise import BaseContext, Prompt, PromptBuilder, PromptContext, PromptSuite, prompt

        assert Prompt is not None
        assert prompt is not None
        assert PromptBuilder is not None
        assert PromptSuite is not None
        assert BaseContext is not None
        assert PromptContext is not None

    def test_subpackage_imports(self):
        from promptise.prompts import (
            Guard,
            GuardError,
            Perspective,
            Strategy,
            chain,
            chain_of_thought,
            fallback,
            parallel,
            registry,
            self_critique,
        )

        assert chain is not None
        assert parallel is not None
        assert fallback is not None
        assert chain_of_thought is not None
        assert self_critique is not None
        assert Guard is not None
        assert GuardError is not None
        assert Strategy is not None
        assert Perspective is not None
        assert registry is not None


# ---------------------------------------------------------------------------
# MCP Prompt Bridge tests
# ---------------------------------------------------------------------------


class TestMCPPromptBridge:
    """Tests for server.include_prompts() — bridging Promptise prompts to MCP."""

    def test_include_single_prompt(self):
        from promptise.mcp.server import MCPServer
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def analyze(text: str, depth: str = "basic") -> str:
            """Analyze the following data: {text}"""

        server = MCPServer(name="test")
        server.include_prompts(analyze)

        # Verify PromptDef was registered
        pdef = server._prompt_registry.get("analyze")
        assert pdef is not None
        assert pdef.name == "analyze"
        assert "Analyze the following data" in pdef.description
        assert "model: openai:gpt-5-mini" in pdef.description
        # Check arguments
        arg_names = [a["name"] for a in pdef.arguments]
        assert "text" in arg_names
        assert "depth" in arg_names
        # text is required (no default), depth is optional
        text_arg = next(a for a in pdef.arguments if a["name"] == "text")
        depth_arg = next(a for a in pdef.arguments if a["name"] == "depth")
        assert text_arg["required"] is True
        assert depth_arg["required"] is False

    def test_include_registry(self):
        from promptise.mcp.server import MCPServer
        from promptise.prompts.core import prompt
        from promptise.prompts.registry import PromptRegistry

        reg = PromptRegistry()

        @prompt(model="openai:gpt-5-mini")
        async def summarize(text: str) -> str:
            """Summarize: {text}"""

        @prompt(model="openai:gpt-4o")
        async def translate(text: str, lang: str) -> str:
            """Translate to {lang}: {text}"""

        reg.register("summarize", "1.0.0", summarize)
        reg.register("translate", "2.1.0", translate)

        server = MCPServer(name="test")
        server.include_prompts(reg)

        # Both prompts should be registered
        assert server._prompt_registry.get("summarize") is not None
        assert server._prompt_registry.get("translate") is not None

        # Version info should appear in description
        sum_def = server._prompt_registry.get("summarize")
        assert "v1.0.0" in sum_def.description

        trans_def = server._prompt_registry.get("translate")
        assert "v2.1.0" in trans_def.description

    def test_include_suite(self):
        from promptise.mcp.server import MCPServer
        from promptise.prompts.core import prompt
        from promptise.prompts.suite import PromptSuite

        class MySuite(PromptSuite):
            @prompt(model="openai:gpt-5-mini")
            async def task_a(self, text: str) -> str:
                """Task A: {text}"""

            @prompt(model="openai:gpt-5-mini")
            async def task_b(self, query: str) -> str:
                """Task B: {query}"""

        suite = MySuite()
        server = MCPServer(name="test")
        server.include_prompts(suite)

        assert server._prompt_registry.get("task_a") is not None
        assert server._prompt_registry.get("task_b") is not None

    @pytest.mark.asyncio
    async def test_handler_calls_render_async(self):
        from promptise.mcp.server import MCPServer
        from promptise.prompts.core import prompt

        @prompt(model="openai:gpt-5-mini")
        async def greet(name: str) -> str:
            """Hello, {name}! Welcome."""

        server = MCPServer(name="test")
        server.include_prompts(greet)

        pdef = server._prompt_registry.get("greet")
        result = await pdef.handler(name="Alice")
        assert "Hello, Alice! Welcome." in result

    def test_include_mixed_sources(self):
        from promptise.mcp.server import MCPServer
        from promptise.prompts.core import prompt
        from promptise.prompts.registry import PromptRegistry
        from promptise.prompts.suite import PromptSuite

        reg = PromptRegistry()

        @prompt(model="openai:gpt-5-mini")
        async def reg_prompt(text: str) -> str:
            """Registry prompt: {text}"""

        reg.register("reg_prompt", "1.0.0", reg_prompt)

        @prompt(model="openai:gpt-5-mini")
        async def solo_prompt(text: str) -> str:
            """Solo prompt: {text}"""

        class Mini(PromptSuite):
            @prompt(model="openai:gpt-5-mini")
            async def suite_prompt(self, text: str) -> str:
                """Suite prompt: {text}"""

        suite = Mini()

        server = MCPServer(name="test")
        server.include_prompts(reg, solo_prompt, suite)

        assert server._prompt_registry.get("reg_prompt") is not None
        assert server._prompt_registry.get("solo_prompt") is not None
        assert server._prompt_registry.get("suite_prompt") is not None

    def test_include_invalid_type_raises(self):
        from promptise.mcp.server import MCPServer

        server = MCPServer(name="test")
        with pytest.raises(TypeError, match="include_prompts"):
            server.include_prompts("not a prompt")

    def test_metadata_in_description(self):
        from promptise.mcp.server import MCPServer
        from promptise.prompts.core import prompt
        from promptise.prompts.strategies import AnalystPerspective, ChainOfThoughtStrategy

        @prompt(model="openai:gpt-4o")
        async def analyze(data: str) -> str:
            """Analyze metrics: {data}"""

        configured = (
            analyze.with_strategy(ChainOfThoughtStrategy())
            .with_perspective(AnalystPerspective())
            .with_constraints("Be concise", "Cite sources")
        )

        server = MCPServer(name="test")
        server.include_prompts(configured)

        pdef = server._prompt_registry.get("analyze")
        assert "strategy: ChainOfThoughtStrategy()" in pdef.description
        assert "perspective: AnalystPerspective()" in pdef.description
        assert "constraints: 2" in pdef.description

    @pytest.mark.asyncio
    async def test_handler_applies_strategy(self):
        from promptise.mcp.server import MCPServer
        from promptise.prompts.core import prompt
        from promptise.prompts.strategies import ChainOfThoughtStrategy

        @prompt(model="openai:gpt-5-mini")
        async def analyze(text: str) -> str:
            """Analyze: {text}"""

        configured = analyze.with_strategy(ChainOfThoughtStrategy())

        server = MCPServer(name="test")
        server.include_prompts(configured)

        pdef = server._prompt_registry.get("analyze")
        result = await pdef.handler(text="test data")
        # Strategy wraps with chain-of-thought instructions
        assert "step-by-step" in result.lower()
        assert "Analyze: test data" in result
