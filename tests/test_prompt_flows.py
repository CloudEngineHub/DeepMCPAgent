"""Tests for promptise.prompts.flows — conversation flow state machine."""

from __future__ import annotations

import pytest

from promptise.prompts.blocks import Identity, OutputFormat, Rules, Section
from promptise.prompts.flows import ConversationFlow, phase

# ---------------------------------------------------------------------------
# Phase decorator
# ---------------------------------------------------------------------------


class TestPhaseDecorator:
    def test_marks_method(self):
        class MyFlow(ConversationFlow):
            @phase("greeting", initial=True)
            async def greet(self, ctx):
                pass

        flow = MyFlow()
        assert "greeting" in flow._phases
        assert flow._initial_phase == "greeting"

    def test_multiple_phases(self):
        class MyFlow(ConversationFlow):
            @phase("a", initial=True)
            async def phase_a(self, ctx):
                pass

            @phase("b")
            async def phase_b(self, ctx):
                pass

        flow = MyFlow()
        assert len(flow._phases) == 2
        assert "a" in flow._phases
        assert "b" in flow._phases

    def test_multiple_initial_raises(self):
        with pytest.raises(ValueError, match="Multiple initial"):

            class BadFlow(ConversationFlow):
                @phase("a", initial=True)
                async def pa(self, ctx):
                    pass

                @phase("b", initial=True)
                async def pb(self, ctx):
                    pass

            BadFlow()

    def test_phase_with_blocks(self):
        block = OutputFormat(format="json")

        class MyFlow(ConversationFlow):
            @phase("working", initial=True, blocks=[block])
            async def work(self, ctx):
                pass

        flow = MyFlow()
        spec = flow._phases["working"]
        assert len(spec.blocks) == 1


# ---------------------------------------------------------------------------
# ConversationFlow — start
# ---------------------------------------------------------------------------


class TestConversationFlowStart:
    @pytest.mark.asyncio
    async def test_start_enters_initial_phase(self):
        class MyFlow(ConversationFlow):
            base_blocks = [Identity("Agent")]

            @phase("greeting", initial=True)
            async def greet(self, ctx):
                ctx.activate(Section("greet", "Hello!"))

        flow = MyFlow()
        result = await flow.start()
        assert "Agent" in result.text
        assert "Hello!" in result.text
        assert flow._current_phase == "greeting"

    @pytest.mark.asyncio
    async def test_start_no_phases_returns_base(self):
        class EmptyFlow(ConversationFlow):
            base_blocks = [Identity("Bot")]

        flow = EmptyFlow()
        result = await flow.start()
        assert "Bot" in result.text

    @pytest.mark.asyncio
    async def test_start_no_initial_raises(self):
        class BadFlow(ConversationFlow):
            @phase("a")
            async def pa(self, ctx):
                pass

        flow = BadFlow()
        with pytest.raises(ValueError, match="No initial phase"):
            await flow.start()


# ---------------------------------------------------------------------------
# ConversationFlow — next_turn
# ---------------------------------------------------------------------------


class TestConversationFlowNextTurn:
    @pytest.mark.asyncio
    async def test_advances_turn(self):
        class MyFlow(ConversationFlow):
            base_blocks = [Identity("Agent")]

            @phase("main", initial=True)
            async def main(self, ctx):
                pass

        flow = MyFlow()
        await flow.start()
        await flow.next_turn("hello")
        assert flow._turn == 1
        assert len(flow._history) == 1
        assert flow._history[0]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_records_assistant_message(self):
        class MyFlow(ConversationFlow):
            @phase("main", initial=True)
            async def main(self, ctx):
                pass

        flow = MyFlow()
        await flow.start()
        await flow.next_turn("hi", assistant_message="hello back")
        assert len(flow._history) == 2
        assert flow._history[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_transition_to(self):
        class MyFlow(ConversationFlow):
            base_blocks = [Identity("Agent")]

            @phase("a", initial=True)
            async def pa(self, ctx):
                ctx.activate(Section("section_a", "Phase A"))

            @phase("b")
            async def pb(self, ctx):
                ctx.activate(Section("section_b", "Phase B"))

        flow = MyFlow()
        await flow.start()
        assert flow._current_phase == "a"

        result = await flow.next_turn("switch", transition_to="b")
        assert flow._current_phase == "b"
        assert "Phase B" in result.text

    @pytest.mark.asyncio
    async def test_handler_requested_transition(self):
        class MyFlow(ConversationFlow):
            @phase("start", initial=True)
            async def start_phase(self, ctx):
                if ctx.turn > 0:
                    ctx.transition("done")

            @phase("done")
            async def done_phase(self, ctx):
                ctx.activate(Section("done_msg", "All done!"))

        flow = MyFlow()
        await flow.start()
        result = await flow.next_turn("go")
        assert flow._current_phase == "done"
        assert "All done!" in result.text


# ---------------------------------------------------------------------------
# ConversationFlow — transition
# ---------------------------------------------------------------------------


class TestConversationFlowTransition:
    @pytest.mark.asyncio
    async def test_explicit_transition(self):
        class MyFlow(ConversationFlow):
            base_blocks = [Identity("Bot")]

            @phase("intro", initial=True)
            async def intro(self, ctx):
                pass

            @phase("working")
            async def working(self, ctx):
                ctx.activate(Section("work", "Working now."))

        flow = MyFlow()
        await flow.start()
        result = await flow.transition("working")
        assert flow._current_phase == "working"
        assert "Working now." in result.text

    @pytest.mark.asyncio
    async def test_unknown_phase_raises(self):
        class MyFlow(ConversationFlow):
            @phase("a", initial=True)
            async def pa(self, ctx):
                pass

        flow = MyFlow()
        await flow.start()
        with pytest.raises(ValueError, match="Unknown phase"):
            await flow.transition("nonexistent")


# ---------------------------------------------------------------------------
# TurnContext
# ---------------------------------------------------------------------------


class TestTurnContext:
    @pytest.mark.asyncio
    async def test_activate_deactivate(self):
        class MyFlow(ConversationFlow):
            @phase("main", initial=True)
            async def main(self, ctx):
                ctx.activate(Section("temp", "Temporary"))

        flow = MyFlow()
        await flow.start()
        assert "temp" in flow._active_blocks

        # Deactivate
        class FlowWithDeactivate(ConversationFlow):
            @phase("main", initial=True)
            async def main(self, ctx):
                ctx.activate(Section("temp", "Temporary"))
                ctx.deactivate("temp")

        flow2 = FlowWithDeactivate()
        await flow2.start()
        assert "temp" not in flow2._active_blocks

    @pytest.mark.asyncio
    async def test_fill_slot(self):
        class MyFlow(ConversationFlow):
            @phase("main", initial=True)
            async def main(self, ctx):
                ctx.fill_slot("data", "injected data")

        flow = MyFlow()
        await flow.start()
        assert flow._slot_fills.get("data") == "injected data"

    @pytest.mark.asyncio
    async def test_state_mutation(self):
        class MyFlow(ConversationFlow):
            @phase("main", initial=True)
            async def main(self, ctx):
                ctx.state["counter"] = ctx.state.get("counter", 0) + 1

        flow = MyFlow()
        await flow.start()
        assert flow._state["counter"] == 1
        await flow.next_turn("msg")
        assert flow._state["counter"] == 2

    @pytest.mark.asyncio
    async def test_get_prompt(self):
        class MyFlow(ConversationFlow):
            base_blocks = [Identity("Agent")]

            @phase("main", initial=True)
            async def main(self, ctx):
                result = ctx.get_prompt()
                assert "Agent" in result.text

        flow = MyFlow()
        await flow.start()

    @pytest.mark.asyncio
    async def test_history_read_only(self):
        class MyFlow(ConversationFlow):
            @phase("main", initial=True)
            async def main(self, ctx):
                h = ctx.history
                # Should be a copy
                h.append({"role": "test", "content": "injected"})

        flow = MyFlow()
        await flow.start()
        await flow.next_turn("hello")
        # The injected entry should NOT be in the real history
        assert not any(m.get("role") == "test" for m in flow._history)


# ---------------------------------------------------------------------------
# Flow — base_blocks always present
# ---------------------------------------------------------------------------


class TestBaseBlocks:
    @pytest.mark.asyncio
    async def test_base_blocks_always_in_prompt(self):
        class MyFlow(ConversationFlow):
            base_blocks = [
                Identity("Support Agent"),
                Rules(["Be polite"]),
            ]

            @phase("main", initial=True)
            async def main(self, ctx):
                pass

        flow = MyFlow()
        result = await flow.start()
        assert "Support Agent" in result.text
        assert "Be polite" in result.text

        result2 = await flow.next_turn("hello")
        assert "Support Agent" in result2.text


# ---------------------------------------------------------------------------
# Flow — get_prompt & reset
# ---------------------------------------------------------------------------


class TestFlowUtilities:
    @pytest.mark.asyncio
    async def test_get_prompt_no_advance(self):
        class MyFlow(ConversationFlow):
            base_blocks = [Identity("Bot")]

            @phase("main", initial=True)
            async def main(self, ctx):
                pass

        flow = MyFlow()
        await flow.start()
        result = flow.get_prompt()
        assert "Bot" in result.text
        assert flow._turn == 0  # Did not advance

    @pytest.mark.asyncio
    async def test_reset(self):
        class MyFlow(ConversationFlow):
            @phase("main", initial=True)
            async def main(self, ctx):
                ctx.state["x"] = 1

        flow = MyFlow()
        await flow.start()
        assert flow._state.get("x") == 1

        flow.reset()
        assert flow._current_phase is None
        assert flow._turn == 0
        assert len(flow._history) == 0
        assert len(flow._state) == 0

    def test_repr(self):
        class MyFlow(ConversationFlow):
            pass

        flow = MyFlow()
        r = repr(flow)
        assert "MyFlow" in r


# ---------------------------------------------------------------------------
# Phase auto-blocks
# ---------------------------------------------------------------------------


class TestPhaseAutoBlocks:
    @pytest.mark.asyncio
    async def test_phase_blocks_activated_on_enter(self):
        json_format = OutputFormat(format="json")

        class MyFlow(ConversationFlow):
            @phase("data", initial=True, blocks=[json_format])
            async def data_phase(self, ctx):
                pass

        flow = MyFlow()
        result = await flow.start()
        assert "json" in result.text.lower()

    @pytest.mark.asyncio
    async def test_phase_blocks_deactivated_on_exit(self):
        json_format = OutputFormat(format="json")

        class MyFlow(ConversationFlow):
            @phase("data", initial=True, blocks=[json_format])
            async def data_phase(self, ctx):
                pass

            @phase("text")
            async def text_phase(self, ctx):
                pass

        flow = MyFlow()
        await flow.start()
        assert "output_format" in flow._active_blocks

        await flow.transition("text")
        assert "output_format" not in flow._active_blocks
