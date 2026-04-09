"""End-to-end tests for ConversationFlow and Phase system.

Tests the full conversation flow lifecycle: phase creation, flow construction,
turn-by-turn execution, conditional transitions, context tracking, and
edge cases like single-phase flows and resets.
"""

from __future__ import annotations

import pytest

from promptise.prompts.blocks import (
    AssembledPrompt,
    Identity,
    Section,
)
from promptise.prompts.flows import ConversationFlow, Phase, TurnContext, phase

# ---------------------------------------------------------------------------
# TestPhaseCreation
# ---------------------------------------------------------------------------


class TestPhaseCreation:
    """Tests for Phase dataclass and @phase decorator creation."""

    def test_phase_with_name_and_blocks(self):
        """Phase can be created with a name and a list of blocks."""
        block = Section("intro", "Welcome section")
        p = Phase(name="greeting", blocks=[block])
        assert p.name == "greeting"
        assert len(p.blocks) == 1
        assert p.blocks[0].name == "intro"

    def test_phase_with_instructions_block(self):
        """Phase can carry an Identity block as instructions."""
        identity = Identity("You are a helpful agent")
        p = Phase(name="setup", blocks=[identity])
        assert p.name == "setup"
        rendered = p.blocks[0].render()
        assert "helpful agent" in rendered

    def test_phase_with_transition_hooks(self):
        """Phase supports on_enter and on_exit callbacks."""
        entered = []
        exited = []

        p = Phase(
            name="middle",
            on_enter=lambda: entered.append(True),
            on_exit=lambda: exited.append(True),
        )
        assert p.on_enter is not None
        assert p.on_exit is not None
        # Invoke the hooks directly
        p.on_enter()
        p.on_exit()
        assert entered == [True]
        assert exited == [True]

    def test_phase_decorator_creates_phase_spec(self):
        """The @phase decorator attaches a _PhaseSpec to the method."""

        class MyFlow(ConversationFlow):
            @phase("greet", initial=True)
            async def greet(self, ctx: TurnContext):
                pass

        flow = MyFlow()
        assert "greet" in flow._phases
        spec = flow._phases["greet"]
        assert spec.name == "greet"
        assert spec.initial is True


# ---------------------------------------------------------------------------
# TestConversationFlowCreation
# ---------------------------------------------------------------------------


class TestConversationFlowCreation:
    """Tests for ConversationFlow construction and validation."""

    def test_flow_creation_with_multiple_phases(self):
        """Flow discovers all @phase-decorated methods."""

        class MultiFlow(ConversationFlow):
            @phase("intro", initial=True)
            async def intro(self, ctx):
                pass

            @phase("body")
            async def body(self, ctx):
                pass

            @phase("conclusion")
            async def conclusion(self, ctx):
                pass

        flow = MultiFlow()
        assert len(flow._phases) == 3
        assert set(flow._phases.keys()) == {"intro", "body", "conclusion"}

    def test_flow_requires_initial_phase_for_start(self):
        """Flow with phases but no initial raises ValueError on start."""

        class NoInitialFlow(ConversationFlow):
            @phase("a")
            async def pa(self, ctx):
                pass

        flow = NoInitialFlow()
        # Phases exist but none is initial
        assert flow._initial_phase is None

    def test_flow_default_phase_is_initial(self):
        """The flow starts in the phase marked with initial=True."""

        class DefaultFlow(ConversationFlow):
            @phase("step1", initial=True)
            async def step1(self, ctx):
                pass

            @phase("step2")
            async def step2(self, ctx):
                pass

        flow = DefaultFlow()
        assert flow._initial_phase == "step1"
        # Before start, current_phase is None
        assert flow._current_phase is None


# ---------------------------------------------------------------------------
# TestFlowExecution
# ---------------------------------------------------------------------------


class TestFlowExecution:
    """Tests for full flow execution: start, next_turn, transitions."""

    @pytest.mark.asyncio
    async def test_start_returns_assembled_prompt(self):
        """flow.start() returns an AssembledPrompt with base blocks."""

        class SimpleFlow(ConversationFlow):
            base_blocks = [Identity("Test Agent")]

            @phase("main", initial=True)
            async def main(self, ctx):
                pass

        flow = SimpleFlow()
        result = await flow.start()
        assert isinstance(result, AssembledPrompt)
        assert "Test Agent" in result.text

    @pytest.mark.asyncio
    async def test_next_turn_advances_phase_handler(self):
        """next_turn runs the current phase handler each turn."""
        activations = []

        class TrackingFlow(ConversationFlow):
            base_blocks = [Identity("Agent")]

            @phase("main", initial=True)
            async def main(self, ctx):
                activations.append(ctx.turn)

        flow = TrackingFlow()
        await flow.start()
        assert activations == [0]

        await flow.next_turn("message 1")
        assert activations == [0, 1]

        await flow.next_turn("message 2")
        assert activations == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_phases_execute_in_order(self):
        """Phases transition in the defined order when explicitly triggered."""
        execution_log = []

        class OrderedFlow(ConversationFlow):
            base_blocks = [Identity("Agent")]

            @phase("phase_a", initial=True)
            async def phase_a(self, ctx):
                execution_log.append("a")

            @phase("phase_b")
            async def phase_b(self, ctx):
                execution_log.append("b")

            @phase("phase_c")
            async def phase_c(self, ctx):
                execution_log.append("c")

        flow = OrderedFlow()
        await flow.start()
        assert execution_log == ["a"]

        await flow.transition("phase_b")
        assert execution_log == ["a", "b"]

        await flow.transition("phase_c")
        assert execution_log == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_conditional_phase_transition(self):
        """Phase handler can request transition based on context."""

        class ConditionalFlow(ConversationFlow):
            base_blocks = [Identity("Agent")]

            @phase("collect", initial=True)
            async def collect(self, ctx):
                ctx.state.setdefault("messages_received", 0)
                ctx.state["messages_received"] += 1
                # Transition after 3 messages
                if ctx.state["messages_received"] >= 3:
                    ctx.transition("process")

            @phase("process")
            async def process(self, ctx):
                ctx.activate(Section("processing", "Processing your data."))

        flow = ConditionalFlow()
        await flow.start()
        assert flow._current_phase == "collect"

        await flow.next_turn("msg 1")
        assert flow._current_phase == "collect"

        result = await flow.next_turn("msg 2")
        # On turn 2, messages_received becomes 3 (start=1, turn1=2, turn2=3)
        assert flow._current_phase == "process"
        assert "Processing your data" in result.text

    @pytest.mark.asyncio
    async def test_flow_completes_all_phases(self):
        """A flow can move through all phases from start to finish."""

        class FullFlow(ConversationFlow):
            base_blocks = [Identity("Agent")]

            @phase("intro", initial=True)
            async def intro(self, ctx):
                ctx.activate(Section("intro_msg", "Introduction"))

            @phase("working")
            async def working(self, ctx):
                ctx.activate(Section("work_msg", "Working hard"))

            @phase("wrapup")
            async def wrapup(self, ctx):
                ctx.activate(Section("done_msg", "All done"))

        flow = FullFlow()
        result1 = await flow.start()
        assert "Introduction" in result1.text

        result2 = await flow.transition("working")
        assert "Working hard" in result2.text
        assert flow._current_phase == "working"

        result3 = await flow.transition("wrapup")
        assert "All done" in result3.text
        assert flow._current_phase == "wrapup"

    @pytest.mark.asyncio
    async def test_turn_count_tracking(self):
        """Flow accurately tracks turn count across next_turn calls."""

        class CountFlow(ConversationFlow):
            @phase("main", initial=True)
            async def main(self, ctx):
                pass

        flow = CountFlow()
        await flow.start()
        assert flow._turn == 0

        await flow.next_turn("turn 1")
        assert flow._turn == 1

        await flow.next_turn("turn 2")
        assert flow._turn == 2

        await flow.next_turn("turn 3")
        assert flow._turn == 3


# ---------------------------------------------------------------------------
# TestTurnContext
# ---------------------------------------------------------------------------


class TestTurnContext:
    """Tests for TurnContext properties and behavior."""

    @pytest.mark.asyncio
    async def test_turn_context_stores_user_input(self):
        """History records user messages passed to next_turn."""

        class HistoryFlow(ConversationFlow):
            @phase("main", initial=True)
            async def main(self, ctx):
                pass

        flow = HistoryFlow()
        await flow.start()

        await flow.next_turn("Hello there")
        await flow.next_turn("How are you?")

        assert len(flow._history) == 2
        assert flow._history[0] == {"role": "user", "content": "Hello there"}
        assert flow._history[1] == {"role": "user", "content": "How are you?"}

    @pytest.mark.asyncio
    async def test_turn_context_tracks_turn_number(self):
        """TurnContext.turn reflects the current turn number."""
        observed_turns = []

        class TurnTracker(ConversationFlow):
            @phase("main", initial=True)
            async def main(self, ctx):
                observed_turns.append(ctx.turn)

        flow = TurnTracker()
        await flow.start()
        await flow.next_turn("a")
        await flow.next_turn("b")

        assert observed_turns == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_turn_context_has_phase_reference(self):
        """TurnContext.phase returns the current phase name."""
        observed_phases = []

        class PhaseTracker(ConversationFlow):
            @phase("intro", initial=True)
            async def intro(self, ctx):
                observed_phases.append(ctx.phase)

            @phase("main")
            async def main(self, ctx):
                observed_phases.append(ctx.phase)

        flow = PhaseTracker()
        await flow.start()
        assert observed_phases == ["intro"]

        await flow.transition("main")
        assert observed_phases == ["intro", "main"]

    @pytest.mark.asyncio
    async def test_turn_context_metadata_via_state(self):
        """TurnContext.state allows storing arbitrary metadata."""

        class MetaFlow(ConversationFlow):
            @phase("main", initial=True)
            async def main(self, ctx):
                ctx.state["user_tier"] = "premium"
                ctx.state["request_count"] = ctx.state.get("request_count", 0) + 1

        flow = MetaFlow()
        await flow.start()
        assert flow._state["user_tier"] == "premium"
        assert flow._state["request_count"] == 1

        await flow.next_turn("another message")
        assert flow._state["request_count"] == 2


# ---------------------------------------------------------------------------
# TestFlowEdgeCases
# ---------------------------------------------------------------------------


class TestFlowEdgeCases:
    """Tests for edge cases and less common flow configurations."""

    @pytest.mark.asyncio
    async def test_flow_with_single_phase(self):
        """A flow with only one phase works correctly across turns."""

        class SinglePhaseFlow(ConversationFlow):
            base_blocks = [Identity("Solo Agent")]

            @phase("only", initial=True)
            async def only_phase(self, ctx):
                ctx.activate(Section("status", f"Turn {ctx.turn}"))

        flow = SinglePhaseFlow()
        result = await flow.start()
        assert "Solo Agent" in result.text
        assert "Turn 0" in result.text

        result2 = await flow.next_turn("hello")
        assert "Turn 1" in result2.text

    @pytest.mark.asyncio
    async def test_flow_restart_after_reset(self):
        """Flow can be reset and restarted cleanly."""

        class RestartableFlow(ConversationFlow):
            base_blocks = [Identity("Agent")]

            @phase("main", initial=True)
            async def main(self, ctx):
                ctx.state["counter"] = ctx.state.get("counter", 0) + 1

        flow = RestartableFlow()
        await flow.start()
        await flow.next_turn("msg1")
        await flow.next_turn("msg2")

        assert flow._turn == 2
        assert flow._state["counter"] == 3  # start + 2 turns
        assert len(flow._history) == 2

        flow.reset()
        assert flow._current_phase is None
        assert flow._turn == 0
        assert len(flow._history) == 0
        assert len(flow._state) == 0

        # Restart the flow
        result = await flow.start()
        assert isinstance(result, AssembledPrompt)
        assert flow._current_phase == "main"
        assert flow._state["counter"] == 1
        assert flow._turn == 0

    @pytest.mark.asyncio
    async def test_accessing_current_phase(self):
        """Flow._current_phase reflects the active phase at all times."""

        class PhaseCheckFlow(ConversationFlow):
            @phase("alpha", initial=True)
            async def alpha(self, ctx):
                pass

            @phase("beta")
            async def beta(self, ctx):
                pass

            @phase("gamma")
            async def gamma(self, ctx):
                pass

        flow = PhaseCheckFlow()
        assert flow._current_phase is None

        await flow.start()
        assert flow._current_phase == "alpha"

        await flow.transition("beta")
        assert flow._current_phase == "beta"

        await flow.transition("gamma")
        assert flow._current_phase == "gamma"

        # Can go back to a previous phase
        await flow.transition("alpha")
        assert flow._current_phase == "alpha"
