"""Tests for promptise.prompts.blocks — composable prompt components."""

from __future__ import annotations

from promptise.prompts.blocks import (
    AssembledPrompt,
    BlockContext,
    BlockTrace,
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

# ---------------------------------------------------------------------------
# Block types — render correctly
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_basic_render(self):
        block = Identity("Expert data analyst")
        text = block.render()
        assert "Expert data analyst" in text

    def test_with_traits(self):
        block = Identity("Analyst", traits=["precise", "thorough"])
        text = block.render()
        assert "Analyst" in text
        assert "precise" in text
        assert "thorough" in text

    def test_priority(self):
        assert Identity("x").priority == 10

    def test_name(self):
        assert Identity("x").name == "identity"


class TestRules:
    def test_render(self):
        block = Rules(["Be concise", "Cite sources"])
        text = block.render()
        assert "Be concise" in text
        assert "Cite sources" in text

    def test_numbered(self):
        block = Rules(["First", "Second"])
        text = block.render()
        assert "1." in text
        assert "2." in text

    def test_priority(self):
        assert Rules(["x"]).priority == 9

    def test_empty_rules(self):
        block = Rules([])
        assert block.render() == ""


class TestOutputFormat:
    def test_format_only(self):
        block = OutputFormat(format="json")
        text = block.render()
        assert "json" in text.lower()

    def test_with_instructions(self):
        block = OutputFormat(format="markdown", instructions="Use headers")
        text = block.render()
        assert "markdown" in text.lower()
        assert "Use headers" in text

    def test_priority(self):
        assert OutputFormat(format="json").priority == 8


class TestContextSlot:
    def test_empty_default(self):
        slot = ContextSlot("data")
        text = slot.render()
        assert text == ""

    def test_with_default(self):
        slot = ContextSlot("data", default="no data available")
        text = slot.render()
        assert "no data available" in text

    def test_fill(self):
        slot = ContextSlot("data")
        filled = slot.fill("hello world")
        assert "hello world" in filled.render()
        # Original unchanged
        assert slot.render() == ""

    def test_priority(self):
        assert ContextSlot("x").priority == 6


class TestSection:
    def test_string_content(self):
        block = Section("intro", "Welcome to the analysis.")
        assert "Welcome to the analysis." in block.render()

    def test_callable_content(self):
        block = Section("dynamic", lambda: "computed value")
        assert "computed value" in block.render()

    def test_callable_with_ctx(self):
        ctx = BlockContext(state={"x": 42}, active_tools=[], metadata={})
        block = Section("dynamic", lambda ctx: f"val={ctx.state['x']}" if ctx else "no ctx")
        text = block.render(ctx)
        assert "val=42" in text

    def test_custom_priority(self):
        block = Section("x", "y", priority=7)
        assert block.priority == 7

    def test_default_priority(self):
        assert Section("x", "y").priority == 5


class TestExamples:
    def test_render(self):
        block = Examples(
            [
                {"input": "2+2", "output": "4"},
                {"input": "3+3", "output": "6"},
            ]
        )
        text = block.render()
        assert "2+2" in text
        assert "4" in text
        assert "3+3" in text

    def test_max_count(self):
        exs = [{"input": str(i), "output": str(i)} for i in range(10)]
        block = Examples(exs, max_count=3)
        text = block.render()
        # Should only include 3 examples
        assert "0" in text
        assert "2" in text

    def test_priority(self):
        assert Examples([]).priority == 4


class TestConditional:
    def test_true_condition(self):
        inner = Section("inner", "visible")
        block = Conditional("cond", inner, condition=lambda ctx: True)
        assert "visible" in block.render()

    def test_false_condition(self):
        inner = Section("inner", "hidden")
        block = Conditional("cond", inner, condition=lambda ctx: False)
        ctx = BlockContext(state={}, active_tools=[], metadata={})
        assert block.render(ctx) == ""

    def test_inherits_priority(self):
        inner = Section("inner", "x", priority=7)
        block = Conditional("cond", inner, condition=lambda ctx: True)
        assert block.priority == 7


class TestComposite:
    def test_renders_all(self):
        block = Composite(
            "group",
            [
                Section("a", "part A"),
                Section("b", "part B"),
            ],
        )
        text = block.render()
        assert "part A" in text
        assert "part B" in text

    def test_priority_is_max(self):
        block = Composite(
            "group",
            [
                Section("a", "x", priority=3),
                Section("b", "x", priority=7),
            ],
        )
        assert block.priority == 7


# ---------------------------------------------------------------------------
# PromptAssembler
# ---------------------------------------------------------------------------


class TestPromptAssembler:
    def test_basic_assembly(self):
        assembler = PromptAssembler(
            Identity("Analyst"),
            Rules(["Be precise"]),
        )
        result = assembler.assemble()
        assert isinstance(result, AssembledPrompt)
        assert "Analyst" in result.text
        assert "Be precise" in result.text
        assert len(result.included) == 2
        assert len(result.excluded) == 0

    def test_insertion_order(self):
        assembler = PromptAssembler(
            Section("first", "AAA", priority=1),
            Section("second", "BBB", priority=10),
        )
        result = assembler.assemble()
        # Even though "second" has higher priority, insertion order is preserved
        assert result.text.index("AAA") < result.text.index("BBB")

    def test_slot_filling(self):
        assembler = PromptAssembler(
            ContextSlot("data", default="none"),
        )
        filled = assembler.fill_slot("data", "real data here")
        result = filled.assemble()
        assert "real data here" in result.text

    def test_chaining(self):
        result = PromptAssembler().add(Identity("Agent")).add(Rules(["Rule 1"])).assemble()
        assert "Agent" in result.text
        assert "Rule 1" in result.text

    def test_remove(self):
        assembler = PromptAssembler().add(Identity("Agent")).add(Rules(["Rule 1"])).remove("rules")
        result = assembler.assemble()
        assert "Agent" in result.text
        assert "Rule 1" not in result.text

    def test_empty_blocks_filtered(self):
        assembler = PromptAssembler(
            Identity("Agent"),
            Rules([]),  # Empty — should be filtered
        )
        result = assembler.assemble()
        assert "identity" in result.included
        # Empty rules should not be included
        assert len(result.block_details) >= 1

    def test_block_trace(self):
        assembler = PromptAssembler(Identity("Test"))
        result = assembler.assemble()
        assert len(result.block_details) >= 1
        trace = result.block_details[0]
        assert isinstance(trace, BlockTrace)
        assert trace.name == "identity"
        assert trace.priority == 10
        assert trace.included is True
        assert trace.rendered_length > 0

    def test_token_estimation(self):
        assembler = PromptAssembler(Section("text", "hello world"))
        result = assembler.assemble()
        assert result.estimated_tokens > 0

    def test_with_context(self):
        ctx = BlockContext(
            state={"key": "value"},
            turn=3,
            phase="working",
            active_tools=["search"],
            metadata={},
        )
        assembler = PromptAssembler(Identity("Agent"))
        result = assembler.assemble(ctx)
        assert "Agent" in result.text


# ---------------------------------------------------------------------------
# @blocks decorator
# ---------------------------------------------------------------------------


class TestBlocksDecorator:
    def test_attaches_to_function(self):
        @blocks(Identity("Test"), Rules(["Rule"]))
        def my_func():
            """Template"""

        assert hasattr(my_func, "_pending_blocks")
        assert len(my_func._pending_blocks) == 2
