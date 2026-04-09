"""Pre-configured node factories (skills) for common agent patterns.

Skills are functions that return configured nodes.  They encode
best-practice prompt engineering for specific tasks so developers
don't have to start from scratch.

Usage::

    from promptise.engine.skills import web_researcher, summarizer

    graph = PromptGraph("assistant")
    graph.add_node(web_researcher("search", tools=my_search_tools))
    graph.add_node(summarizer("conclude"))
    graph.add_edge("search", "conclude")
    graph.set_entry("search")
"""

from __future__ import annotations

from typing import Any

from .nodes import GuardNode, PromptNode, RouterNode, TransformNode


def web_researcher(
    name: str = "web_research",
    *,
    tools: list | None = None,
    max_iterations: int = 5,
    **kwargs: Any,
) -> PromptNode:
    """A node tuned for web research with search tools.

    Includes instructions for thorough multi-source research,
    source citation, and fact verification.
    """
    return PromptNode(
        name,
        instructions=(
            "You are a thorough web researcher. Search for information "
            "from multiple sources. Always cite your sources. "
            "Verify claims by cross-referencing at least 2 sources. "
            "If search results are insufficient, try different queries."
        ),
        tools=tools,
        tool_choice="auto",
        max_iterations=max_iterations,
        **kwargs,
    )


def code_reviewer(
    name: str = "code_review",
    *,
    tools: list | None = None,
    **kwargs: Any,
) -> PromptNode:
    """A node tuned for code review and analysis.

    Includes instructions for security analysis, performance
    review, and best-practice suggestions.
    """
    return PromptNode(
        name,
        instructions=(
            "You are an expert code reviewer. Analyze code for: "
            "1) Security vulnerabilities (injection, auth bypass, data leaks) "
            "2) Performance issues (N+1 queries, memory leaks, blocking I/O) "
            "3) Best practice violations (naming, error handling, testing) "
            "4) Logic errors and edge cases. "
            "Be specific — cite exact lines and suggest concrete fixes."
        ),
        tools=tools,
        **kwargs,
    )


def data_analyst(
    name: str = "analyze",
    *,
    tools: list | None = None,
    **kwargs: Any,
) -> PromptNode:
    """A node tuned for data analysis with structured output.

    Uses chain-of-thought reasoning and requires data-backed claims.
    """
    return PromptNode(
        name,
        instructions=(
            "You are a data analyst. Approach every question with evidence: "
            "1) Identify the data sources available "
            "2) Query for specific metrics and patterns "
            "3) Compare and correlate across sources "
            "4) Quantify every claim with numbers "
            "5) Flag uncertainty with confidence levels "
            "Never speculate without data."
        ),
        tools=tools,
        **kwargs,
    )


def fact_checker(
    name: str = "verify",
    *,
    guards: list | None = None,
    on_pass: str = "__end__",
    on_fail: str | None = None,
    **kwargs: Any,
) -> GuardNode:
    """A guard node that validates findings for accuracy.

    Runs content filters and custom validators.
    """
    return GuardNode(
        name,
        instructions="Verify all claims are factual and well-sourced.",
        guards=guards or [],
        on_pass=on_pass,
        on_fail=on_fail,
        **kwargs,
    )


def summarizer(
    name: str = "summarize",
    *,
    max_length: int = 2000,
    **kwargs: Any,
) -> PromptNode:
    """A node tuned for summarizing information concisely.

    Compresses gathered information into a clear, structured answer.
    """
    return PromptNode(
        name,
        instructions=(
            f"Summarize all gathered information into a clear, concise response. "
            f"Maximum {max_length} characters. Structure: "
            "1) Key finding (1-2 sentences) "
            "2) Supporting details (bullet points) "
            "3) Sources and confidence level. "
            "Be direct — no filler."
        ),
        tools=None,
        default_next="__end__",
        **kwargs,
    )


def planner(
    name: str = "plan",
    *,
    max_subgoals: int = 4,
    **kwargs: Any,
) -> PromptNode:
    """A node tuned for creating step-by-step plans.

    Produces structured plans with self-evaluation.
    """
    return PromptNode(
        name,
        instructions=(
            f"Create a step-by-step plan with {max_subgoals} or fewer subgoals. "
            "Each subgoal should be: specific, measurable, achievable. "
            "Order by priority — most impactful first. "
            "Self-evaluate: rate plan quality 1-5. If below 3, revise."
        ),
        tools=None,
        **kwargs,
    )


def decision_router(
    name: str = "decide",
    *,
    routes: dict[str, str] | None = None,
    **kwargs: Any,
) -> RouterNode:
    """A routing node that decides the next step based on progress.

    Uses lightweight LLM call to choose between named routes.
    """
    return RouterNode(
        name,
        instructions=(
            "Based on the current progress and available information, "
            "decide which path to take next. Consider: "
            "1) Do you have enough data? "
            "2) Are there unresolved questions? "
            "3) Is the quality sufficient? "
            "Choose the most productive next step."
        ),
        routes=routes,
        **kwargs,
    )


def formatter(
    name: str = "format",
    *,
    output_key: str = "formatted_result",
    transform: Any = None,
    **kwargs: Any,
) -> TransformNode:
    """A transform node for formatting final output.

    Pure data transformation — no LLM call.
    """
    return TransformNode(
        name,
        instructions="Format the results for final output.",
        transform=transform,
        output_key=output_key,
        default_next="__end__",
        **kwargs,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Reasoning node skills — pre-configured specialized nodes
# ═══════════════════════════════════════════════════════════════════════════════


def thinker(name: str = "think", **kwargs: Any) -> ThinkNode:  # noqa: F821
    """Pre-configured ThinkNode for gap analysis and next-step reasoning."""
    from .reasoning_nodes import ThinkNode

    return ThinkNode(name, **kwargs)


def reflector(name: str = "reflect", **kwargs: Any) -> ReflectNode:  # noqa: F821
    """Pre-configured ReflectNode for self-evaluation."""
    from .reasoning_nodes import ReflectNode

    return ReflectNode(name, **kwargs)


def critic(name: str = "critique", **kwargs: Any) -> CritiqueNode:  # noqa: F821
    """Pre-configured CritiqueNode for adversarial review."""
    from .reasoning_nodes import CritiqueNode

    return CritiqueNode(name, **kwargs)


def justifier(name: str = "justify", **kwargs: Any) -> JustifyNode:  # noqa: F821
    """Pre-configured JustifyNode for audit trail."""
    from .reasoning_nodes import JustifyNode

    return JustifyNode(name, **kwargs)


def synthesizer(name: str = "synthesize", **kwargs: Any) -> SynthesizeNode:  # noqa: F821
    """Pre-configured SynthesizeNode for final answer composition."""
    from .reasoning_nodes import SynthesizeNode

    return SynthesizeNode(name, **kwargs)


def validator_node(
    name: str = "validate", *, criteria: list[str] | None = None, **kwargs: Any
) -> ValidateNode:  # noqa: F821
    """Pre-configured ValidateNode with custom criteria."""
    from .reasoning_nodes import ValidateNode

    return ValidateNode(name, criteria=criteria, **kwargs)


def observer_node(name: str = "observe", **kwargs: Any) -> ObserveNode:  # noqa: F821
    """Pre-configured ObserveNode for tool result interpretation."""
    from .reasoning_nodes import ObserveNode

    return ObserveNode(name, **kwargs)
