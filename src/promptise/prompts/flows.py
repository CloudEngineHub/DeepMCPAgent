"""Conversation flow state machine — turn-aware prompt evolution.

The system prompt is a living document that adapts to conversation state.
Phases control which blocks are active, and the prompt evolves across turns.

Example::

    from promptise.prompts.flows import ConversationFlow, phase
    from promptise.prompts.blocks import Identity, Rules, Section, OutputFormat

    class SupportFlow(ConversationFlow):
        base_blocks = [
            Identity("Customer support agent"),
            Rules(["Be empathetic", "Ask before escalating"]),
        ]

        @phase("greeting", initial=True)
        async def greet(self, ctx):
            ctx.activate(Section("greet", "Ask how you can help today."))

        @phase("investigate")
        async def investigate(self, ctx):
            ctx.deactivate("greet")
            ctx.activate(Section("investigate", "Ask clarifying questions."))

        @phase("resolve")
        async def resolve(self, ctx):
            ctx.activate(OutputFormat(format="markdown"))

    flow = SupportFlow()
    prompt = await flow.start()                       # Enter greeting phase
    prompt = await flow.next_turn("My app crashed")   # Still in greeting
    await flow.transition("investigate")              # Move to investigate
    prompt = await flow.next_turn("It crashes on login")
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .blocks import (
    AssembledPrompt,
    Block,
    BlockContext,
    PromptAssembler,
)

__all__ = [
    "Phase",
    "TurnContext",
    "ConversationFlow",
    "phase",
]


# ---------------------------------------------------------------------------
# Phase
# ---------------------------------------------------------------------------


@dataclass
class Phase:
    """A named phase in a conversation flow.

    Phases can carry blocks that are automatically activated on entry
    and deactivated on exit, plus optional lifecycle hooks.
    """

    name: str
    blocks: list[Block] = field(default_factory=list)
    on_enter: Callable[..., Any] | None = None
    on_exit: Callable[..., Any] | None = None


# ---------------------------------------------------------------------------
# TurnContext — mutable view given to phase handlers
# ---------------------------------------------------------------------------


class TurnContext:
    """Mutable context passed to ``@phase`` handlers each turn.

    Phase handlers use this to activate/deactivate blocks, fill
    context slots, and trigger phase transitions.
    """

    def __init__(
        self,
        flow: ConversationFlow,
        turn: int,
        phase_name: str,
        state: dict[str, Any],
        history: list[dict[str, str]],
    ) -> None:
        self._flow = flow
        self._turn = turn
        self._phase_name = phase_name
        self._state = state
        self._history = history
        self._pending_transition: str | None = None

    # -- Read-only properties -----------------------------------------------

    @property
    def turn(self) -> int:
        """Current turn number (0-based)."""
        return self._turn

    @property
    def phase(self) -> str:
        """Current phase name."""
        return self._phase_name

    @property
    def state(self) -> dict[str, Any]:
        """Arbitrary flow state dict.  Mutate freely."""
        return self._state

    @property
    def history(self) -> list[dict[str, str]]:
        """Conversation message history (read-only view)."""
        return list(self._history)

    # -- Block manipulation -------------------------------------------------

    def activate(self, block: Block) -> None:
        """Add a block to the active prompt composition."""
        self._flow._activate_block(block)

    def deactivate(self, name: str) -> None:
        """Remove a block by name from the active composition."""
        self._flow._deactivate_block(name)

    # -- Slot filling -------------------------------------------------------

    def fill_slot(self, name: str, content: str) -> None:
        """Fill a :class:`ContextSlot` block by name."""
        self._flow._fill_slot(name, content)

    # -- Phase transitions --------------------------------------------------

    def transition(self, phase_name: str) -> None:
        """Request a transition to another phase.

        The transition happens after the current handler completes.
        """
        self._pending_transition = phase_name

    # -- Prompt access ------------------------------------------------------

    def get_prompt(self) -> AssembledPrompt:
        """Assemble the current prompt (base + active blocks)."""
        return self._flow._assemble(
            turn=self._turn,
            phase=self._phase_name,
        )


# ---------------------------------------------------------------------------
# ConversationFlow
# ---------------------------------------------------------------------------


class ConversationFlow:
    """Base class for conversation flow state machines.

    Subclass this and use the ``@phase`` decorator to define phase
    handlers.  Set ``base_blocks`` for blocks that are always active.

    Attributes:
        base_blocks: Blocks always included in the prompt (class-level).
    """

    base_blocks: ClassVar[list[Block]] = []

    def __init__(self) -> None:
        # Discover @phase-decorated methods
        self._phases: dict[str, _PhaseSpec] = {}
        self._initial_phase: str | None = None
        self._current_phase: str | None = None

        # Active blocks beyond base_blocks
        self._active_blocks: dict[str, Block] = {}

        # Conversation state
        self._state: dict[str, Any] = {}
        self._history: list[dict[str, str]] = []
        self._turn: int = 0

        # Slot fills carried across turns
        self._slot_fills: dict[str, str] = {}

        self._discover_phases()

    # -- Phase discovery ----------------------------------------------------

    def _discover_phases(self) -> None:
        """Scan for methods decorated with ``@phase``."""
        for attr_name in dir(self):
            try:
                method = getattr(self, attr_name)
            except AttributeError:
                continue
            spec = getattr(method, "_phase_spec", None)
            if spec is not None:
                self._phases[spec.name] = _PhaseSpec(
                    name=spec.name,
                    handler=method,
                    blocks=list(spec.blocks),
                    on_enter=spec.on_enter,
                    on_exit=spec.on_exit,
                    initial=spec.initial,
                )
                if spec.initial:
                    if self._initial_phase is not None:
                        raise ValueError(
                            f"Multiple initial phases: {self._initial_phase!r} and {spec.name!r}"
                        )
                    self._initial_phase = spec.name

    # -- Internal block management ------------------------------------------

    def _activate_block(self, block: Block) -> None:
        self._active_blocks[block.name] = block

    def _deactivate_block(self, name: str) -> None:
        self._active_blocks.pop(name, None)

    def _fill_slot(self, name: str, content: str) -> None:
        self._slot_fills[name] = content

    # -- Assembly -----------------------------------------------------------

    def _assemble(
        self,
        turn: int = 0,
        phase: str = "",
    ) -> AssembledPrompt:
        """Build the current prompt from base + active blocks."""
        all_blocks: list[Block] = list(self.base_blocks) + list(self._active_blocks.values())

        assembler = PromptAssembler(*all_blocks)

        # Apply slot fills
        for slot_name, content in self._slot_fills.items():
            assembler = assembler.fill_slot(slot_name, content)

        ctx = BlockContext(
            state=self._state,
            turn=turn,
            phase=phase,
            active_tools=[],
            metadata={},
        )
        return assembler.assemble(ctx)

    # -- Phase transitions --------------------------------------------------

    _MAX_TRANSITION_DEPTH = 5

    async def _enter_phase(
        self,
        phase_name: str,
        *,
        _depth: int = 0,
    ) -> None:
        """Enter a phase: run on_exit for old, on_enter for new, activate blocks.

        Recursive transitions (when an on_enter hook triggers another
        transition) are limited to ``_MAX_TRANSITION_DEPTH`` to prevent
        infinite loops.
        """
        if _depth >= self._MAX_TRANSITION_DEPTH:
            raise RecursionError(
                f"Phase transition depth limit ({self._MAX_TRANSITION_DEPTH}) "
                f"exceeded.  Check for circular transitions."
            )
        if phase_name not in self._phases:
            raise ValueError(
                f"Unknown phase {phase_name!r}. Available: {list(self._phases.keys())}"
            )

        # Exit current phase
        if self._current_phase is not None:
            old_spec = self._phases.get(self._current_phase)
            if old_spec is not None:
                # Deactivate phase-specific blocks
                for block in old_spec.blocks:
                    self._active_blocks.pop(block.name, None)
                # Run on_exit hook
                if old_spec.on_exit is not None:
                    result = old_spec.on_exit()
                    if hasattr(result, "__await__"):
                        await result

        # Enter new phase
        new_spec = self._phases[phase_name]
        self._current_phase = phase_name

        # Activate phase-specific blocks
        for block in new_spec.blocks:
            self._active_blocks[block.name] = block

        # Run on_enter hook
        if new_spec.on_enter is not None:
            result = new_spec.on_enter()
            if hasattr(result, "__await__"):
                await result

    # -- Public API ---------------------------------------------------------

    async def start(self) -> AssembledPrompt:
        """Initialize the flow and enter the initial phase.

        Returns the first assembled prompt.

        Raises:
            ValueError: If no initial phase is defined.
        """
        if self._initial_phase is None:
            if not self._phases:
                # No phases defined — just return base blocks
                return self._assemble()
            raise ValueError(
                "No initial phase defined. Use @phase('name', initial=True) "
                "on one of your phase handlers."
            )

        await self._enter_phase(self._initial_phase)

        # Run the initial phase handler
        ctx = TurnContext(
            flow=self,
            turn=0,
            phase_name=self._current_phase or "",
            state=self._state,
            history=self._history,
        )
        spec = self._phases[self._initial_phase]
        result = spec.handler(ctx)
        if hasattr(result, "__await__"):
            await result

        # Handle any transition requested by the handler
        if ctx._pending_transition is not None:
            await self._enter_phase(ctx._pending_transition)
            # Run the new phase's handler
            new_spec = self._phases.get(ctx._pending_transition)
            if new_spec is not None:
                new_ctx = TurnContext(
                    flow=self,
                    turn=0,
                    phase_name=self._current_phase or "",
                    state=self._state,
                    history=self._history,
                )
                new_result = new_spec.handler(new_ctx)
                if hasattr(new_result, "__await__"):
                    await new_result

        return self._assemble(
            turn=0,
            phase=self._current_phase or "",
        )

    async def next_turn(
        self,
        user_message: str,
        *,
        assistant_message: str = "",
        transition_to: str | None = None,
    ) -> AssembledPrompt:
        """Process a conversation turn.

        Records the message in history, runs the current phase handler,
        and returns the updated prompt.

        Args:
            user_message: The user's message this turn.
            assistant_message: Optional assistant reply to record.
            transition_to: Force a phase transition before processing.

        Returns:
            The assembled prompt for this turn.
        """
        self._turn += 1

        # Record messages
        self._history.append({"role": "user", "content": user_message})
        if assistant_message:
            self._history.append({"role": "assistant", "content": assistant_message})

        # Handle explicit transition
        if transition_to is not None:
            await self._enter_phase(transition_to)

        # Run current phase handler
        if self._current_phase is not None:
            spec = self._phases.get(self._current_phase)
            if spec is not None:
                ctx = TurnContext(
                    flow=self,
                    turn=self._turn,
                    phase_name=self._current_phase,
                    state=self._state,
                    history=self._history,
                )
                result = spec.handler(ctx)
                if hasattr(result, "__await__"):
                    await result

                # Handle transition requested by handler
                if ctx._pending_transition is not None:
                    await self._enter_phase(ctx._pending_transition)
                    # Run the new phase's handler
                    new_spec = self._phases.get(ctx._pending_transition)
                    if new_spec is not None:
                        new_ctx = TurnContext(
                            flow=self,
                            turn=self._turn,
                            phase_name=self._current_phase or "",
                            state=self._state,
                            history=self._history,
                        )
                        new_result = new_spec.handler(new_ctx)
                        if hasattr(new_result, "__await__"):
                            await new_result

        return self._assemble(
            turn=self._turn,
            phase=self._current_phase or "",
        )

    async def transition(self, phase_name: str) -> AssembledPrompt:
        """Explicitly transition to a new phase.

        Returns the updated prompt after the transition.
        """
        await self._enter_phase(phase_name)

        # Run the new phase handler
        spec = self._phases.get(phase_name)
        if spec is not None:
            ctx = TurnContext(
                flow=self,
                turn=self._turn,
                phase_name=phase_name,
                state=self._state,
                history=self._history,
            )
            result = spec.handler(ctx)
            if hasattr(result, "__await__"):
                await result

            if ctx._pending_transition is not None:
                await self._enter_phase(ctx._pending_transition)

        return self._assemble(
            turn=self._turn,
            phase=self._current_phase or "",
        )

    def get_prompt(self) -> AssembledPrompt:
        """Get the current prompt without advancing the turn counter."""
        return self._assemble(
            turn=self._turn,
            phase=self._current_phase or "",
        )

    def reset(self) -> None:
        """Reset the flow to its initial state."""
        self._current_phase = None
        self._active_blocks.clear()
        self._state.clear()
        self._history.clear()
        self._slot_fills.clear()
        self._turn = 0

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"phase={self._current_phase!r} "
            f"turn={self._turn} "
            f"blocks={len(self._active_blocks)}>"
        )


# ---------------------------------------------------------------------------
# Internal phase spec
# ---------------------------------------------------------------------------


@dataclass
class _PhaseSpec:
    """Internal storage for phase metadata."""

    name: str
    handler: Callable[..., Any]
    blocks: list[Block] = field(default_factory=list)
    on_enter: Callable[..., Any] | None = None
    on_exit: Callable[..., Any] | None = None
    initial: bool = False


# ---------------------------------------------------------------------------
# @phase decorator
# ---------------------------------------------------------------------------


def phase(
    name: str,
    *,
    initial: bool = False,
    blocks: list[Block] | None = None,
    on_enter: Callable[..., Any] | None = None,
    on_exit: Callable[..., Any] | None = None,
) -> Callable[..., Any]:
    """Decorator that marks a method as a phase handler.

    Usage::

        class MyFlow(ConversationFlow):
            @phase("greeting", initial=True)
            async def greet(self, ctx: TurnContext):
                ctx.activate(Section("greet", "Say hello."))

            @phase("working", blocks=[OutputFormat(format="json")])
            async def work(self, ctx: TurnContext):
                ...

    Args:
        name: Phase name (used for transitions).
        initial: Whether this is the starting phase.
        blocks: Blocks auto-activated when entering this phase.
        on_enter: Callback invoked on phase entry.
        on_exit: Callback invoked on phase exit.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn._phase_spec = _PhaseSpec(  # type: ignore[attr-defined]
            name=name,
            handler=fn,  # temporary: overwritten when the flow collects phases
            blocks=list(blocks or []),
            on_enter=on_enter,
            on_exit=on_exit,
            initial=initial,
        )
        return fn

    return decorator
