"""Pre-built graph patterns for common agent architectures.

Factory methods that return fully configured ``PromptGraph`` instances
for the most common reasoning patterns.  Each can be customized by
passing blocks, tools, strategies, and instructions.

Usage::

    from promptise.engine import PromptGraph

    # Standard ReAct agent
    graph = PromptGraph.react(tools=my_tools, system_prompt="You are helpful.")

    # PEOATR for complex tasks
    graph = PromptGraph.peoatr(tools=my_tools)

    # Research pipeline
    graph = PromptGraph.research(search_tools=search_tools)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("promptise.engine")

from langchain_core.tools import BaseTool

from .base import BaseNode
from .code_action import CodeActionNode
from .graph import PromptGraph
from .nodes import GuardNode, PromptNode


def build_react_graph(
    tools: list[BaseTool] | None = None,
    system_prompt: str = "",
    *,
    blocks: list[Any] | None = None,
    max_node_iterations: int = 15,
) -> PromptGraph:
    """Build a standard ReAct agent graph.

    Single PromptNode with tools.  The LLM decides when to call tools
    and when to produce the final answer.  The engine handles the
    tool-calling loop automatically.

    This is the default graph used by ``build_agent()``.

    Args:
        tools: Tools available to the agent.
        system_prompt: System prompt text.
        blocks: Optional PromptBlocks for the reasoning node.
        max_node_iterations: Max tool-calling iterations.

    Returns:
        A ``PromptGraph`` with a single ``PromptNode("reason")``.
    """
    graph = PromptGraph(name="react")

    node_blocks = list(blocks) if blocks else []

    graph.add_node(
        PromptNode(
            "reason",
            instructions=system_prompt,
            blocks=node_blocks,
            tools=list(tools) if tools else [],
            tool_choice="auto",
            max_iterations=max_node_iterations,
            # When the LLM calls a tool, rule 1 in _resolve_transition
            # re-enters this node automatically.  When it produces a
            # tool-free final answer, we must terminate — otherwise the
            # graph loops back to `reason` forever until max_iterations.
            default_next="__end__",
        )
    )

    graph.set_entry("reason")
    return graph


def build_peoatr_graph(
    tools: list[BaseTool] | None = None,
    system_prompt: str = "",
    *,
    planning_instructions: str = "",
    acting_instructions: str = "",
    thinking_instructions: str = "",
    reflecting_instructions: str = "",
    blocks: list[Any] | None = None,
) -> PromptGraph:
    """Build a PEOATR (Plan → Act → Think → Reflect) graph.

    Four-stage reasoning pattern where the agent:
    1. Plans subgoals with self-evaluation
    2. Executes tools to achieve subgoals
    3. Analyzes tool results (think)
    4. Reflects on progress and routes (replan/continue/answer)

    Args:
        tools: Tools available during the Act stage.
        system_prompt: Base system prompt prepended to all stages.
        planning_instructions: Extra instructions for the Plan stage.
        acting_instructions: Extra instructions for the Act stage.
        thinking_instructions: Extra instructions for the Think stage.
        reflecting_instructions: Extra instructions for the Reflect stage.
        blocks: Optional PromptBlocks shared across all stages.

    Returns:
        A ``PromptGraph`` with plan → act → think → reflect nodes.
    """
    graph = PromptGraph(name="peoatr")
    base_blocks = list(blocks) if blocks else []

    # Plan: create subgoals, self-evaluate
    graph.add_node(
        PromptNode(
            "plan",
            instructions=(
                f"{system_prompt}\n\n"
                f"{planning_instructions or 'Create a step-by-step plan with 2-4 subgoals. '}"
                "Evaluate the plan quality (1-5). If quality < 3, set proceed=false. "
                "Output JSON with: subgoals (list), active_subgoal (str), "
                "plan_quality (int), proceed (bool)."
            ),
            blocks=base_blocks,
            tools=None,
            transitions={
                "proceed": "act",
                "replan": "plan",
            },
            default_next="act",
            max_iterations=3,
        )
    )

    # Act: execute tools
    graph.add_node(
        PromptNode(
            "act",
            instructions=(
                f"{system_prompt}\n\n"
                f"{acting_instructions or 'Execute the current subgoal using available tools. '}"
                "Call ONE tool per turn. If you have the final answer, "
                "respond without tool calls."
            ),
            blocks=base_blocks,
            tools=list(tools) if tools else [],
            tool_choice="auto",
            default_next="think",
            max_iterations=8,
        )
    )

    # Think: analyze results
    graph.add_node(
        PromptNode(
            "think",
            instructions=(
                f"{system_prompt}\n\n"
                f"{thinking_instructions or 'Analyze the tool result. '}"
                "Assess: is the subgoal complete? What gap remains? "
                "If another tool call is immediately needed, set route=continue. "
                "If you need to step back and evaluate, set route=reflect."
            ),
            blocks=base_blocks,
            tools=None,
            transitions={
                "continue": "act",
                "reflect": "reflect",
            },
            default_next="reflect",
        )
    )

    # Reflect: evaluate progress
    graph.add_node(
        PromptNode(
            "reflect",
            instructions=(
                f"{system_prompt}\n\n"
                f"{reflecting_instructions or 'Reflect on progress so far. '}"
                "Rate confidence (1-5), progress (1-5), blockers (1-5). "
                "Route: answer (confidence>=4, progress>=4), "
                "replan (confidence<=2 or blockers>=4), "
                "continue (otherwise)."
            ),
            blocks=base_blocks,
            tools=None,
            transitions={
                "answer": "act",  # Final answer via act node
                "replan": "plan",
                "continue": "act",
            },
            default_next="act",
        )
    )

    graph.set_entry("plan")
    return graph


def build_research_graph(
    search_tools: list[BaseTool] | None = None,
    synthesis_tools: list[BaseTool] | None = None,
    system_prompt: str = "",
    *,
    blocks: list[Any] | None = None,
    verify: bool = True,
) -> PromptGraph:
    """Build a Search → Verify → Synthesize research pipeline.

    Three-stage pattern for research tasks:
    1. Search: use search tools to gather information
    2. Verify: cross-check findings (optional)
    3. Synthesize: combine findings into a final answer

    Args:
        search_tools: Tools for the search stage.
        synthesis_tools: Optional tools for the synthesis stage.
        system_prompt: Base system prompt.
        blocks: Optional PromptBlocks shared across stages.
        verify: Whether to include a verification stage.

    Returns:
        A ``PromptGraph`` with search → verify → synthesize nodes.
    """
    graph = PromptGraph(name="research")
    base_blocks = list(blocks) if blocks else []

    # Search
    graph.add_node(
        PromptNode(
            "search",
            instructions=(
                f"{system_prompt}\n\n"
                "Search for relevant information using available tools. "
                "Be thorough — gather from multiple sources when possible."
            ),
            blocks=base_blocks,
            tools=list(search_tools) if search_tools else [],
            default_next="verify" if verify else "synthesize",
            max_iterations=5,
        )
    )

    # Verify (optional)
    if verify:
        graph.add_node(
            GuardNode(
                "verify",
                instructions="Cross-check the findings for consistency.",
                guards=[],  # User can add custom guards
                on_pass="synthesize",
                on_fail="search",
            )
        )

    # Synthesize
    graph.add_node(
        PromptNode(
            "synthesize",
            instructions=(
                f"{system_prompt}\n\n"
                "Synthesize all gathered information into a clear, "
                "comprehensive answer. Cite sources where possible."
            ),
            blocks=base_blocks,
            tools=list(synthesis_tools) if synthesis_tools else [],
            default_next="__end__",
        )
    )

    graph.set_entry("search")
    return graph


def build_autonomous_graph(
    node_pool: list | None = None,
    system_prompt: str = "",
    *,
    tools: list[BaseTool] | None = None,
    max_steps: int = 15,
) -> PromptGraph:
    """Build an autonomous agent that composes its own reasoning path.

    The agent receives a pool of configured nodes and dynamically
    decides which to execute at each step.  If no node_pool is provided,
    a default set is created from the tools.

    Args:
        node_pool: Pre-configured nodes the agent can choose from.
        system_prompt: Base instructions for the autonomous planner.
        tools: Tools to create default nodes from (if no pool given).
        max_steps: Maximum reasoning steps.

    Returns:
        A ``PromptGraph`` with a single ``AutonomousNode``.
    """
    from .nodes import AutonomousNode, PromptNode

    # If no pool provided, create default nodes from tools
    if not node_pool and tools:
        node_pool = [
            PromptNode(
                "reason",
                instructions=system_prompt or "Reason about the task and use tools as needed.",
                tools=list(tools),
                tool_choice="auto",
            ),
            PromptNode(
                "analyze",
                instructions="Analyze the results gathered so far. Identify gaps.",
                tools=None,
            ),
            PromptNode(
                "synthesize",
                instructions="Synthesize all findings into a final, comprehensive answer.",
                tools=None,
            ),
        ]

    graph = PromptGraph(name="autonomous")
    graph.add_node(
        AutonomousNode(
            "agent",
            node_pool=node_pool or [],
            planner_instructions=system_prompt
            or "You are an autonomous agent. Select the best reasoning step for the current task.",
            max_steps=max_steps,
        )
    )
    graph.set_entry("agent")
    return graph


def build_deliberate_graph(
    tools: list[BaseTool] | None = None,
    system_prompt: str = "",
    **kwargs: Any,
) -> PromptGraph:
    """Build a deliberate reasoning graph: Think → Plan → Act → Observe → Reflect.

    Slower but higher quality. The agent thinks before acting, observes
    results carefully, and reflects before continuing.
    """
    from .reasoning_nodes import ObserveNode, PlanNode, ReflectNode, ThinkNode

    graph = PromptGraph(name="deliberate", mode="static")

    graph.add_node(ThinkNode("think", is_entry=True))
    graph.add_node(PlanNode("plan"))
    graph.add_node(
        PromptNode(
            "act",
            tools=list(tools) if tools else [],
            instructions=system_prompt or "Execute the plan.",
            inject_tools=True,
        )
    )
    graph.add_node(ObserveNode("observe"))
    graph.add_node(
        ReflectNode(
            "reflect", transitions={"answer": "__end__", "continue": "act", "replan": "plan"}
        )
    )

    graph.sequential("think", "plan", "act", "observe", "reflect")
    graph.set_entry("think")
    return graph


def build_debate_graph(
    system_prompt: str = "",
    *,
    max_rounds: int = 5,
    **kwargs: Any,
) -> PromptGraph:
    """Build a debate graph: Proposer → Critic → Judge.

    Two adversarial nodes alternate until consensus.
    """
    from .reasoning_nodes import CritiqueNode, ValidateNode

    graph = PromptGraph(name="debate", mode="static")

    graph.add_node(
        PromptNode(
            "proposer",
            instructions=system_prompt or "Propose an answer.",
            is_entry=True,
            max_iterations=max_rounds,
        )
    )
    graph.add_node(CritiqueNode("critic", severity_threshold=0.3))
    graph.add_node(ValidateNode("judge", on_pass="__end__", on_fail="proposer"))

    graph.sequential("proposer", "critic", "judge")
    graph.set_entry("proposer")
    return graph


def build_pipeline_graph(*nodes: BaseNode) -> PromptGraph:
    """Build a simple sequential pipeline from a list of nodes.

    Usage::

        graph = PromptGraph.pipeline(
            planner("plan"),
            web_researcher("search", tools=my_tools),
            summarizer("conclude"),
        )
    """
    if not nodes:
        logger.warning("build_pipeline_graph() called with no nodes — returning empty graph")
        return PromptGraph(name="pipeline", mode="static")

    graph = PromptGraph(name="pipeline", mode="static")
    names = []
    for node in nodes:
        graph.add_node(node)
        names.append(node.name)

    if names:
        graph.sequential(*names)
        graph.set_entry(names[0])
        # Last node defaults to __end__
        last = graph.get_node(names[-1])
        if not last.default_next:
            last.default_next = "__end__"

    return graph


def build_verify_graph(
    tools: list[BaseTool] | None = None,
    system_prompt: str = "",
    *,
    blocks: list[Any] | None = None,
    max_node_iterations: int = 6,
) -> PromptGraph:
    """Single-pass self-verifying reasoning — one LLM call, but the model
    must plan, solve, and **check its own answer** within that generation.

    It gives a model the benefit of an explicit verification step at
    **one-turn** latency (no multi-call overhead). On models that already
    reason internally it matches a direct prompt; on weaker models the forced
    self-check recovers errors a single pass would miss. A good default when
    you want a self-checking answer without a multi-stage pipeline.

    Args:
        tools: Optional tools for the reasoning node.
        system_prompt: Base system prompt.
        blocks: Optional PromptBlocks for the node.
        max_node_iterations: Tool-loop budget.

    Returns:
        A single-node ``PromptGraph`` that plans, solves, and verifies in one
        generation.
    """
    graph = PromptGraph(name="verify")
    graph.add_node(
        PromptNode(
            "reason",
            instructions=(
                f"{system_prompt}\n\n"
                "Answer in a single response, thinking carefully:\n"
                "1) PLAN — restate the problem and outline the steps.\n"
                "2) SOLVE — work the steps, showing each computation.\n"
                "3) VERIFY — independently re-check the answer a different way "
                "or against the constraints; if it is wrong, fix it.\n"
                "4) Then give the final answer clearly."
            ).strip(),
            blocks=list(blocks) if blocks else [],
            tools=list(tools) if tools else [],
            tool_choice="auto",
            default_next="__end__",
            max_iterations=max_node_iterations,
        )
    )
    graph.set_entry("reason")
    return graph


def build_managed_graph(
    tools: list[BaseTool] | None = None,
    system_prompt: str = "",
    *,
    blocks: list[Any] | None = None,
    max_node_iterations: int = 30,
) -> PromptGraph:
    """Tool agent with **context lifecycle management** for long tool chains.

    A single tool-using node, but run with ``context_scope="ledger"``: instead
    of feeding the model an ever-growing transcript of tool calls and results
    (where it loses track and re-queries the same facts dozens of times), each
    turn it sees the task plus a compact, **deduplicated "facts gathered"
    ledger** built from the tool results so far. Context stays bounded and the
    model stops re-looking-up what it already knows.

    Use this for **deep multi-tool tasks** — traversing a database/graph,
    gathering many facts then aggregating. It cuts redundant tool calls and
    bounds token growth on long chains at equal accuracy — an efficiency
    primitive, not an accuracy claim.

    Args:
        tools: Tools the agent can call.
        system_prompt: Base system prompt.
        blocks: Optional PromptBlocks for the node.
        max_node_iterations: Tool-loop budget (higher than ReAct's — deep
            tasks make many calls).

    Returns:
        A single-node ``PromptGraph`` whose node manages its tool-loop context.
    """
    graph = PromptGraph(name="managed")
    graph.add_node(
        PromptNode(
            "reason",
            instructions=(
                f"{system_prompt}\n\n"
                "Gather the facts you need with tools, then answer. A ledger of "
                "facts you have already gathered is provided each turn — consult "
                "it and never re-fetch a fact you already have."
            ).strip(),
            blocks=list(blocks) if blocks else [],
            tools=list(tools) if tools else [],
            tool_choice="auto",
            context_scope="ledger",
            default_next="__end__",
            max_iterations=max_node_iterations,
        )
    )
    graph.set_entry("reason")
    return graph


def build_code_action_graph(
    tools: list[BaseTool] | None = None,
    system_prompt: str = "",
    *,
    blocks: list[Any] | None = None,
    sandbox_factory: Any | None = None,
    max_repairs: int = 1,
    exec_timeout: int = 120,
) -> PromptGraph:
    """Code-action reasoning — the model writes **one program**, not a tool chain.

    For aggregation / data-traversal tasks (gather many facts then compute),
    chaining dozens of conversational tool calls is slow, expensive, and
    error-prone. ``code-action`` changes the action space: in a single LLM turn
    the model writes one Python program that calls the available tools (bridged
    into a hardened Docker sandbox) and computes the answer deterministically.
    Validated on agentic tasks — a large accuracy gain at a fraction of the
    tokens and latency, in one turn.

    Requires a sandbox: ``build_agent(agent_pattern="code-action")`` auto-enables
    it (Docker must be installed and running). The generated code runs with a
    read-only rootfs, dropped capabilities, seccomp, and **no network** — it can
    only reach the outside world through the bridged host tools, so every tool
    call still passes through the engine's hooks (budget, health, audit).

    Args:
        tools: Tools the program may call (bridged to the host).
        system_prompt: Base system prompt.
        blocks: Accepted for signature parity (unused in the program prompt).
        sandbox_factory: ``async () -> SandboxSession`` (injected by ``build_agent``).
        max_repairs: Times to feed a crash's stderr back for a fix (default 1).
        exec_timeout: Max seconds the program may run inside the sandbox.

    Returns:
        A single-node ``PromptGraph`` that writes and runs a program.
    """
    graph = PromptGraph(name="code-action")
    graph.add_node(
        CodeActionNode(
            "reason",
            tools=list(tools) if tools else [],
            system_prompt=system_prompt,
            blocks=list(blocks) if blocks else [],
            sandbox_factory=sandbox_factory,
            max_repairs=max_repairs,
            exec_timeout=exec_timeout,
            default_next="__end__",
        )
    )
    graph.set_entry("reason")
    return graph


# ---------------------------------------------------------------------------
# Register factory methods on PromptGraph class
# ---------------------------------------------------------------------------

PromptGraph.react = staticmethod(build_react_graph)  # type: ignore[attr-defined]
PromptGraph.managed = staticmethod(build_managed_graph)  # type: ignore[attr-defined]
PromptGraph.code_action = staticmethod(build_code_action_graph)  # type: ignore[attr-defined]
PromptGraph.verify = staticmethod(build_verify_graph)  # type: ignore[attr-defined]
PromptGraph.peoatr = staticmethod(build_peoatr_graph)  # type: ignore[attr-defined]
PromptGraph.research = staticmethod(build_research_graph)  # type: ignore[attr-defined]
PromptGraph.autonomous = staticmethod(build_autonomous_graph)  # type: ignore[attr-defined]
PromptGraph.deliberate = staticmethod(build_deliberate_graph)  # type: ignore[attr-defined]
PromptGraph.debate = staticmethod(build_debate_graph)  # type: ignore[attr-defined]
PromptGraph.pipeline = staticmethod(build_pipeline_graph)  # type: ignore[attr-defined]
