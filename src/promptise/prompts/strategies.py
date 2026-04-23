"""Reasoning strategies and cognitive perspectives for prompts.

Strategies control HOW the agent reasons (chain-of-thought, self-critique,
plan-and-execute, etc.).  Perspectives control FROM WHERE the agent reasons
(analyst, critic, advisor, creative).

Both are composable and orthogonal — any strategy can pair with any
perspective, and strategies compose via the ``+`` operator.

Example::

    from promptise.prompts import prompt
    from promptise.prompts.strategies import (
        chain_of_thought, self_critique, analyst,
    )

    @prompt(model="openai:gpt-5-mini")
    async def analyze(text: str) -> str:
        \"""Analyze the following text: {text}\"""

    configured = (
        analyze
        .with_strategy(chain_of_thought + self_critique)
        .with_perspective(analyst)
    )
"""

from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable

from .context import PromptContext

__all__ = [
    "Strategy",
    "Perspective",
    "ChainOfThoughtStrategy",
    "StructuredReasoningStrategy",
    "SelfCritiqueStrategy",
    "PlanAndExecuteStrategy",
    "DecomposeStrategy",
    "CompositeStrategy",
    "AnalystPerspective",
    "CriticPerspective",
    "AdvisorPerspective",
    "CreativePerspective",
    "CustomPerspective",
    "chain_of_thought",
    "structured_reasoning",
    "self_critique",
    "plan_and_execute",
    "decompose",
    "analyst",
    "critic",
    "advisor",
    "creative",
    "perspective",
]


# ---------------------------------------------------------------------------
# Strategy protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Strategy(Protocol):
    """Protocol for reasoning strategies.

    ``wrap()`` transforms the prompt text before the LLM call to inject
    reasoning instructions.  ``parse()`` extracts the final answer from
    the LLM's raw output.
    """

    def wrap(self, prompt_text: str, ctx: PromptContext) -> str:
        """Wrap *prompt_text* with reasoning instructions."""
        ...

    def parse(self, raw_output: str, ctx: PromptContext) -> str:
        """Extract the final answer from *raw_output*."""
        ...


# ---------------------------------------------------------------------------
# Perspective protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Perspective(Protocol):
    """Protocol for cognitive perspectives.

    Different from Strategy:
    - **Strategy** = HOW to reason (step-by-step, critique, decompose)
    - **Perspective** = FROM WHERE to reason (analyst, critic, advisor)

    They are orthogonal and composable.
    """

    def apply(self, prompt_text: str, ctx: PromptContext) -> str:
        """Prepend or inject the perspective framing into *prompt_text*."""
        ...


# ---------------------------------------------------------------------------
# Built-in strategies
# ---------------------------------------------------------------------------

_ANSWER_MARKER = "---ANSWER---"


class ChainOfThoughtStrategy:
    """Step-by-step reasoning with an answer marker.

    Instructs the LLM to think step-by-step, then produce a final
    answer after the ``---ANSWER---`` marker.
    """

    def wrap(self, prompt_text: str, ctx: PromptContext) -> str:
        return (
            f"{prompt_text}\n\n"
            "Think through this step-by-step:\n"
            "1. Break down the problem\n"
            "2. Analyze each component\n"
            "3. Draw conclusions\n\n"
            f"After your reasoning, write '{_ANSWER_MARKER}' on its own "
            "line, then provide your final answer."
        )

    def parse(self, raw_output: str, ctx: PromptContext) -> str:
        if _ANSWER_MARKER in raw_output:
            return raw_output.split(_ANSWER_MARKER, 1)[1].strip()
        return raw_output

    def __add__(self, other: Strategy) -> CompositeStrategy:
        return CompositeStrategy([self, other])

    def __repr__(self) -> str:
        return "ChainOfThoughtStrategy()"


class StructuredReasoningStrategy:
    """Multi-step structured reasoning with customizable phases.

    Default phases: Understand, Analyze, Evaluate, Conclude.
    Override with any list of phase names.
    """

    def __init__(self, steps: list[str] | None = None) -> None:
        self.steps = steps or ["Understand", "Analyze", "Evaluate", "Conclude"]

    def wrap(self, prompt_text: str, ctx: PromptContext) -> str:
        steps_text = "\n".join(f"{i}. **{step}**: " for i, step in enumerate(self.steps, 1))
        return (
            f"{prompt_text}\n\n"
            "Work through the following reasoning phases:\n"
            f"{steps_text}\n\n"
            f"After your reasoning, write '{_ANSWER_MARKER}' on its own "
            "line, then provide your final answer."
        )

    def parse(self, raw_output: str, ctx: PromptContext) -> str:
        if _ANSWER_MARKER in raw_output:
            return raw_output.split(_ANSWER_MARKER, 1)[1].strip()
        return raw_output

    def __add__(self, other: Strategy) -> CompositeStrategy:
        return CompositeStrategy([self, other])

    def __repr__(self) -> str:
        return f"StructuredReasoningStrategy(steps={self.steps!r})"


class SelfCritiqueStrategy:
    """Generate, critique, and improve.

    Instructs the LLM to produce an initial answer, critique it for
    flaws, then produce an improved version.  Supports multiple rounds.
    """

    def __init__(self, rounds: int = 1) -> None:
        self.rounds = rounds

    def wrap(self, prompt_text: str, ctx: PromptContext) -> str:
        critique_block = ""
        for i in range(1, self.rounds + 1):
            label = f" (round {i})" if self.rounds > 1 else ""
            critique_block += (
                f"\n**Initial Answer{label}**: Provide your best answer.\n"
                f"**Critique{label}**: Identify flaws, gaps, or improvements.\n"
                f"**Improved Answer{label}**: Revise based on your critique.\n"
            )
        return (
            f"{prompt_text}\n\n"
            "Follow this self-critique process:\n"
            f"{critique_block}\n"
            f"After all rounds, write '{_ANSWER_MARKER}' on its own line, "
            "then provide your final, best answer."
        )

    def parse(self, raw_output: str, ctx: PromptContext) -> str:
        if _ANSWER_MARKER in raw_output:
            return raw_output.split(_ANSWER_MARKER, 1)[1].strip()
        # Fallback: look for the last "Improved Answer" section
        parts = re.split(r"\*\*Improved Answer[^*]*\*\*:\s*", raw_output, flags=re.IGNORECASE)
        if len(parts) > 1:
            return parts[-1].strip()
        return raw_output

    def __add__(self, other: Strategy) -> CompositeStrategy:
        return CompositeStrategy([self, other])

    def __repr__(self) -> str:
        return f"SelfCritiqueStrategy(rounds={self.rounds})"


class PlanAndExecuteStrategy:
    """Plan first, then execute each step.

    Instructs the LLM to create a plan, execute each step, then
    synthesize a final answer.
    """

    def wrap(self, prompt_text: str, ctx: PromptContext) -> str:
        return (
            f"{prompt_text}\n\n"
            "Follow a plan-and-execute approach:\n"
            "1. **Plan**: Create a numbered plan of steps needed\n"
            "2. **Execute**: Work through each step, showing your work\n"
            "3. **Synthesize**: Combine the results into a coherent answer\n\n"
            f"After your work, write '{_ANSWER_MARKER}' on its own line, "
            "then provide your final synthesized answer."
        )

    def parse(self, raw_output: str, ctx: PromptContext) -> str:
        if _ANSWER_MARKER in raw_output:
            return raw_output.split(_ANSWER_MARKER, 1)[1].strip()
        return raw_output

    def __add__(self, other: Strategy) -> CompositeStrategy:
        return CompositeStrategy([self, other])

    def __repr__(self) -> str:
        return "PlanAndExecuteStrategy()"


class DecomposeStrategy:
    """Decompose into subproblems, solve each, then combine.

    Instructs the LLM to identify subproblems, solve them
    independently, then synthesize a combined answer.
    """

    def wrap(self, prompt_text: str, ctx: PromptContext) -> str:
        return (
            f"{prompt_text}\n\n"
            "Use a decomposition approach:\n"
            "1. **Identify subproblems**: Break this into independent parts\n"
            "2. **Solve each**: Address each subproblem separately\n"
            "3. **Combine**: Merge the solutions into a unified answer\n\n"
            f"After your work, write '{_ANSWER_MARKER}' on its own line, "
            "then provide your final combined answer."
        )

    def parse(self, raw_output: str, ctx: PromptContext) -> str:
        if _ANSWER_MARKER in raw_output:
            return raw_output.split(_ANSWER_MARKER, 1)[1].strip()
        return raw_output

    def __add__(self, other: Strategy) -> CompositeStrategy:
        return CompositeStrategy([self, other])

    def __repr__(self) -> str:
        return "DecomposeStrategy()"


# ---------------------------------------------------------------------------
# Composite strategy
# ---------------------------------------------------------------------------


class CompositeStrategy:
    """Multiple strategies applied in sequence.

    ``wrap()`` applies strategies in order (left → right).
    ``parse()`` applies strategies in reverse order (right → left).

    Created via the ``+`` operator::

        combined = chain_of_thought + self_critique
    """

    def __init__(self, strategies: list[Any]) -> None:
        # Flatten nested composites
        flat: list[Any] = []
        for s in strategies:
            if isinstance(s, CompositeStrategy):
                flat.extend(s._strategies)
            else:
                flat.append(s)
        self._strategies: list[Any] = flat

    def wrap(self, prompt_text: str, ctx: PromptContext) -> str:
        text = prompt_text
        for strategy in self._strategies:
            text = strategy.wrap(text, ctx)
        return text

    def parse(self, raw_output: str, ctx: PromptContext) -> str:
        result = raw_output
        for strategy in reversed(self._strategies):
            result = strategy.parse(result, ctx)
        return result

    def __add__(self, other: Strategy) -> CompositeStrategy:
        if isinstance(other, CompositeStrategy):
            return CompositeStrategy(self._strategies + other._strategies)
        return CompositeStrategy([*self._strategies, other])

    def __repr__(self) -> str:
        names = " + ".join(repr(s) for s in self._strategies)
        return f"CompositeStrategy({names})"


# ---------------------------------------------------------------------------
# Built-in perspectives
# ---------------------------------------------------------------------------


class AnalystPerspective:
    """Evidence-based analysis perspective.

    Frames the agent to focus on data, patterns, measurable outcomes,
    and evidence-based conclusions.
    """

    def apply(self, prompt_text: str, ctx: PromptContext) -> str:
        return (
            "Approach this as an analyst. Focus on evidence, data patterns, "
            "and measurable outcomes. Support claims with specific observations "
            "and quantifiable metrics where possible.\n\n"
            f"{prompt_text}"
        )

    def __repr__(self) -> str:
        return "AnalystPerspective()"


class CriticPerspective:
    """Critical evaluation perspective.

    Frames the agent to challenge assumptions, identify weaknesses,
    and stress-test ideas.
    """

    def apply(self, prompt_text: str, ctx: PromptContext) -> str:
        return (
            "Approach this as a critic. Challenge assumptions, identify "
            "weaknesses, potential failure modes, and blind spots. Be "
            "constructive but thorough in your evaluation.\n\n"
            f"{prompt_text}"
        )

    def __repr__(self) -> str:
        return "CriticPerspective()"


class AdvisorPerspective:
    """Balanced advisory perspective.

    Frames the agent to provide balanced recommendations with
    trade-off analysis and actionable next steps.
    """

    def apply(self, prompt_text: str, ctx: PromptContext) -> str:
        return (
            "Approach this as a trusted advisor. Provide balanced "
            "recommendations, weigh trade-offs explicitly, and suggest "
            "concrete, actionable next steps.\n\n"
            f"{prompt_text}"
        )

    def __repr__(self) -> str:
        return "AdvisorPerspective()"


class CreativePerspective:
    """Creative exploration perspective.

    Frames the agent to think unconventionally, explore novel
    combinations, and challenge conventional approaches.
    """

    def apply(self, prompt_text: str, ctx: PromptContext) -> str:
        return (
            "Approach this creatively. Explore unconventional solutions, "
            "novel combinations, and ideas that challenge conventional "
            "thinking. Prioritize originality and innovation.\n\n"
            f"{prompt_text}"
        )

    def __repr__(self) -> str:
        return "CreativePerspective()"


class CustomPerspective:
    """Developer-defined cognitive perspective.

    Args:
        role: The role or identity to adopt (e.g. "security auditor").
        instructions: Additional framing instructions.
    """

    def __init__(self, role: str, instructions: str = "") -> None:
        self.role = role
        self.instructions = instructions

    def apply(self, prompt_text: str, ctx: PromptContext) -> str:
        framing = f"Approach this as a {self.role}."
        if self.instructions:
            framing += f" {self.instructions}"
        return f"{framing}\n\n{prompt_text}"

    def __repr__(self) -> str:
        return f"CustomPerspective(role={self.role!r})"


# ---------------------------------------------------------------------------
# Convenience singletons and constructors
# ---------------------------------------------------------------------------

chain_of_thought = ChainOfThoughtStrategy()
structured_reasoning = StructuredReasoningStrategy()
self_critique = SelfCritiqueStrategy()
plan_and_execute = PlanAndExecuteStrategy()
decompose = DecomposeStrategy()

analyst = AnalystPerspective()
critic = CriticPerspective()
advisor = AdvisorPerspective()
creative = CreativePerspective()


def perspective(role: str, instructions: str = "") -> CustomPerspective:
    """Create a :class:`CustomPerspective`."""
    return CustomPerspective(role=role, instructions=instructions)
