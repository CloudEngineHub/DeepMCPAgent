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
from typing import Any, Literal

logger = logging.getLogger("promptise.engine")

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from .base import BaseNode
from .code_action import CodeActionNode
from .graph import PromptGraph
from .nodes import PromptNode


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

    This is the default graph used by ``build_agent()``. It runs with
    ``context_scope="auto"``: simple tasks see the full transcript (unchanged),
    but once a tool loop grows the node automatically switches to a bounded,
    deduplicated facts ledger — so context stays manageable and token-efficient
    on deep tasks without choosing a different pattern.

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
            context_scope="auto",
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


class PeoatrPlan(BaseModel):
    """Structured output of the PEOATR ``plan`` stage."""

    subgoals: list[str] = Field(description="2-4 concrete subgoals, in the order to do them")
    active_subgoal: str = Field(description="The subgoal to work on first")
    quality_score: int = Field(description="Plan quality from 1 (poor) to 5 (excellent)")


class PeoatrThought(BaseModel):
    """Structured output of the PEOATR ``think`` stage."""

    analysis: str = Field(description="What the results show and what gap remains")
    subgoal_complete: bool = Field(description="Whether the active subgoal is done")
    route: Literal["continue", "reflect"] = Field(
        description="continue: another tool call is needed right away; "
        "reflect: step back and evaluate progress"
    )


class PeoatrReflection(BaseModel):
    """Structured output of the PEOATR ``reflect`` stage."""

    progress: str = Field(description="One sentence on overall progress")
    mistake: str = Field(description="A mistake or wrong turn so far, or an empty string")
    correction: str = Field(description="How to correct it, or an empty string")
    confidence: int = Field(description="Confidence in the findings from 1 to 5")
    route: Literal["answer", "replan", "continue"] = Field(
        description="answer: the question can be answered now; "
        "replan: the plan is wrong; continue: more work is needed"
    )


def build_peoatr_graph(
    tools: list[BaseTool] | None = None,
    system_prompt: str = "",
    *,
    planning_instructions: str = "",
    acting_instructions: str = "",
    thinking_instructions: str = "",
    reflecting_instructions: str = "",
    blocks: list[Any] | None = None,
    max_act_steps: int = 12,
) -> PromptGraph:
    """Build a PEOATR (Plan → Act → Think → Reflect → Answer) graph.

    Five-stage reasoning pattern where the agent:

    1. Plans subgoals and rates the plan (re-plans below quality 3)
    2. Executes tools to achieve the active subgoal
    3. Analyzes the results (think): another tool call now, or reflect
    4. Reflects on progress and routes: answer, replan, or continue
    5. Writes the final answer

    Plan, think and reflect produce structured output, so their routing
    decisions always reach the engine. Every stage has an iteration budget;
    a stage that exhausts it hands over to ``answer`` (``plan`` hands over
    to ``act``), so the run always ends with a written answer.

    Args:
        tools: Tools available during the Act stage.
        system_prompt: Base system prompt prepended to all stages.
        planning_instructions: Extra instructions for the Plan stage.
        acting_instructions: Extra instructions for the Act stage.
        thinking_instructions: Extra instructions for the Think stage.
        reflecting_instructions: Extra instructions for the Reflect stage.
        blocks: Optional PromptBlocks shared across all stages.
        max_act_steps: Budget for the Act stage across the run — each model
            call (every tool-loop round) counts.

    Returns:
        A ``PromptGraph`` with plan → act → think → reflect → answer nodes.
    """
    from .reasoning_nodes import PlanNode, ReflectNode, SynthesizeNode, ThinkNode

    # Static: passed to build_agent as a graph, it must not run as a node pool.
    graph = PromptGraph(name="peoatr", mode="static")
    base_blocks = list(blocks) if blocks else []
    prefix = f"{system_prompt}\n\n" if system_prompt else ""

    graph.add_node(
        PlanNode(
            "plan",
            instructions=(
                f"{prefix}"
                f"{planning_instructions or 'Create a step-by-step plan with 2-4 subgoals. '}"
                "Each subgoal is one concrete step; never plan to ask the user for data "
                "a tool can fetch. Rate the plan from 1 to 5 as quality_score."
            ),
            blocks=base_blocks,
            output_schema=PeoatrPlan,
            default_next="act",
            # Exhausted (three weak plans): act on the latest plan anyway.
            transitions={"error": "act"},
            max_iterations=3,
        )
    )
    graph.add_node(
        PromptNode(
            "act",
            instructions=(
                f"{prefix}"
                f"{acting_instructions or 'Work on the active subgoal of the plan with your tools. '}"
                "When the subgoal is done, reply with a short summary of what you found, "
                "without a tool call."
            ),
            blocks=base_blocks,
            tools=list(tools) if tools else [],
            tool_choice="auto",
            default_next="think",
            transitions={"error": "answer"},
            max_iterations=max_act_steps,
        )
    )
    graph.add_node(
        ThinkNode(
            "think",
            instructions=(
                f"{prefix}"
                f"{thinking_instructions or 'Analyze the results gathered so far. '}"
                "Is the active subgoal complete? What gap remains? "
                "Route 'continue' if another tool call is needed right away, "
                "'reflect' to step back and evaluate progress."
            ),
            blocks=base_blocks,
            output_schema=PeoatrThought,
            transitions={"continue": "act", "reflect": "reflect", "error": "reflect"},
            default_next="reflect",
            max_iterations=max_act_steps,
        )
    )
    graph.add_node(
        ReflectNode(
            "reflect",
            instructions=(
                f"{prefix}"
                f"{reflecting_instructions or 'Reflect on progress so far. '}"
                "Name any mistake and its correction. Rate confidence from 1 to 5. "
                "Route 'answer' when the question can be answered from the results "
                "gathered, 'replan' when the plan is wrong, 'continue' when more "
                "tool work is needed."
            ),
            blocks=base_blocks,
            output_schema=PeoatrReflection,
            transitions={
                "answer": "answer",
                "replan": "plan",
                "continue": "act",
                "error": "answer",
            },
            default_next="answer",
            max_iterations=4,
        )
    )
    graph.add_node(
        SynthesizeNode(
            "answer",
            instructions=(
                f"{prefix}"
                "Write the final answer to the user's question from the results "
                "gathered. Use only facts from the tool results; say what could not "
                "be determined."
            ),
            blocks=base_blocks,
            default_next="__end__",
        )
    )

    graph.set_entry("plan")
    return graph


class ResearchVerdict(BaseModel):
    """Structured output of the research ``verify`` stage."""

    passes: bool = Field(
        description="true only when every claim in the findings is backed by a tool "
        "result, nothing contradicts the sources, and the question is fully covered"
    )
    issues: list[str] = Field(
        description="Each unsupported, contradicted or missing point, with what to look "
        "up to fix it; empty when the findings pass"
    )


def build_research_graph(
    search_tools: list[BaseTool] | None = None,
    synthesis_tools: list[BaseTool] | None = None,
    system_prompt: str = "",
    *,
    blocks: list[Any] | None = None,
    verify: bool = True,
    max_search_steps: int = 12,
    max_verify_rounds: int = 2,
) -> PromptGraph:
    """Build a Search → Verify → Synthesize research pipeline.

    Three-stage pattern for research tasks:

    1. Search: use search tools to gather information, then summarize the
       findings
    2. Verify: a model checks the findings against the tool results gathered
       (structured :class:`ResearchVerdict`). A failed check sends the run
       back to search with the issues; verify runs at most
       *max_verify_rounds* times, then the run moves on to synthesize.
    3. Synthesize: write the final answer from the findings

    Every stage has an iteration budget; a search that exhausts its budget
    hands over to synthesize, so the run always ends with a written answer.

    Args:
        search_tools: Tools for the search stage.
        synthesis_tools: Optional tools for the synthesis stage.
        system_prompt: Base system prompt.
        blocks: Optional PromptBlocks shared across stages.
        verify: Whether to include the verification stage.
        max_search_steps: Budget for the search stage across the run — each
            model call (every tool-loop round) counts.
        max_verify_rounds: How many times verify may run.

    Returns:
        A ``PromptGraph`` with search → verify → synthesize nodes.
    """
    from .reasoning_nodes import ValidateNode

    graph = PromptGraph(name="research", mode="static")
    base_blocks = list(blocks) if blocks else []
    prefix = f"{system_prompt}\n\n" if system_prompt else ""
    after_search = "verify" if verify else "synthesize"

    # Search
    graph.add_node(
        PromptNode(
            "search",
            instructions=(
                f"{prefix}"
                "Search for relevant information using available tools. "
                "Be thorough — gather from multiple sources when possible. "
                "If a verification lists issues, fix them: look up what is missing "
                "or unsupported. When you have what you need, reply with a summary "
                "of your findings, without a tool call."
            ),
            blocks=base_blocks,
            tools=list(search_tools) if search_tools else [],
            tool_choice="auto",
            input_keys=["verification"] if verify else None,
            default_next=after_search,
            # Exhausted: answer from what was found.
            transitions={"error": "synthesize"},
            max_iterations=max_search_steps,
        )
    )

    # Verify (optional): a model judgement with a structured verdict.
    if verify:
        graph.add_node(
            ValidateNode(
                "verify",
                instructions=(
                    "You are a fact checker. The latest reply holds research findings. "
                    "Check them against the tool results in this conversation:\n"
                    "1. Every claim must be backed by a tool result.\n"
                    "2. Nothing may contradict the tool results or another claim.\n"
                    "3. Everything the user's question asks for must be covered.\n"
                    "Set passes to true only if all three hold. List each problem in "
                    "issues, with what to look up to fix it."
                ),
                output_schema=ResearchVerdict,
                output_key="verification",
                on_pass="synthesize",
                on_fail="search",
                default_next="synthesize",
                # Exhausted: answer from the findings as they are.
                transitions={"error": "synthesize"},
                max_iterations=max_verify_rounds,
            )
        )

    # Synthesize
    graph.add_node(
        PromptNode(
            "synthesize",
            instructions=(
                f"{prefix}"
                "Synthesize all gathered information into a clear, "
                "comprehensive answer to the user's question. Use only facts from "
                "the tool results, cite sources where possible, and say what could "
                "not be determined."
            ),
            blocks=base_blocks,
            tools=list(synthesis_tools) if synthesis_tools else [],
            tool_choice="auto",
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
    blocks: list[Any] | None = None,
    max_tool_rounds: int = 10,
) -> PromptGraph:
    """Build an autonomous agent that composes its own reasoning path.

    The agent receives a pool of configured nodes and dynamically
    decides which to execute at each step.  If no node_pool is provided,
    a default pool is created: ``reason`` (with the tools), ``analyze``
    and ``synthesize`` (writes the final answer and ends the run).

    A chosen step with tools runs its whole tool loop (several model calls
    and tool calls) before the agent picks again.  When the agent finishes
    without running ``synthesize``, it runs before the run ends, so the reply
    is always a written answer.

    Args:
        node_pool: Pre-configured nodes the agent can choose from.
        system_prompt: Base instructions for the autonomous planner.
        tools: Tools for the default ``reason`` node (if no pool given).
        max_steps: Maximum reasoning steps.
        blocks: Optional PromptBlocks for the default nodes.
        max_tool_rounds: Model calls per ``reason`` step in the default pool.

    Returns:
        A ``PromptGraph`` with a single ``AutonomousNode``.
    """
    from .nodes import AutonomousNode
    from .reasoning_nodes import SynthesizeNode

    prefix = f"{system_prompt}\n\n" if system_prompt else ""
    base_blocks = list(blocks) if blocks else []
    if not node_pool:
        node_pool = [
            PromptNode(
                "reason",
                description="Gather facts with the tools; may make several tool calls",
                instructions=(
                    f"{prefix}"
                    "Work on the task with your tools. Make as many tool calls as you "
                    "need. When you have what you need, reply with a short summary of "
                    "what you found, without a tool call."
                ),
                blocks=base_blocks,
                tools=list(tools) if tools else [],
                tool_choice="auto",
                max_iterations=max_tool_rounds,
            ),
            PromptNode(
                "analyze",
                description="Analyze the results gathered so far and identify gaps",
                instructions=f"{prefix}Analyze the results gathered so far. Identify gaps.",
                blocks=base_blocks,
            ),
            SynthesizeNode(
                "synthesize",
                description="Write the final answer to the user and finish",
                instructions=(
                    f"{prefix}"
                    "Write the final answer to the user's question from the results "
                    "gathered. Use only facts from the tool results; say what could "
                    "not be determined."
                ),
                blocks=base_blocks,
                is_terminal=True,
            ),
        ]

    graph = PromptGraph(name="autonomous", mode="static")
    graph.add_node(
        AutonomousNode(
            "agent",
            node_pool=node_pool,
            planner_instructions=system_prompt
            or "You are an autonomous agent. Select the best reasoning step for the current task.",
            max_steps=max_steps,
            terminal_nodes=[n.name for n in node_pool if getattr(n, "is_terminal", False)],
            default_next="__end__",
        )
    )
    graph.set_entry("agent")
    return graph


def build_deliberate_graph(
    tools: list[BaseTool] | None = None,
    system_prompt: str = "",
    *,
    blocks: list[Any] | None = None,
    max_act_steps: int = 12,
    **kwargs: Any,
) -> PromptGraph:
    """Build a deliberate reasoning graph: Think → Plan → Act → Observe → Reflect → Answer.

    Slower but higher quality. The agent thinks before acting, observes
    results carefully, and reflects before continuing. Plan and reflect
    produce structured output (:class:`PeoatrPlan`,
    :class:`PeoatrReflection`), so the plan's quality gate and reflect's
    ``continue`` / ``replan`` / ``answer`` route always reach the engine.
    Every stage has an iteration budget and the run ends at ``answer``,
    which writes the reply.

    Args:
        tools: Tools for the act stage.
        system_prompt: Base system prompt prepended to all stages.
        blocks: Optional PromptBlocks shared across all stages.
        max_act_steps: Budget for the act stage across the run — each model
            call (every tool-loop round) counts.

    Returns:
        A ``PromptGraph`` with think → plan → act → observe → reflect → answer.
    """
    from .reasoning_nodes import ObserveNode, PlanNode, ReflectNode, SynthesizeNode, ThinkNode

    graph = PromptGraph(name="deliberate", mode="static")
    base_blocks = list(blocks) if blocks else []
    prefix = f"{system_prompt}\n\n" if system_prompt else ""

    graph.add_node(ThinkNode("think", blocks=base_blocks, is_entry=True, default_next="plan"))
    graph.add_node(
        PlanNode(
            "plan",
            blocks=base_blocks,
            output_schema=PeoatrPlan,
            default_next="act",
            # Exhausted (three weak plans): act on the latest plan anyway.
            transitions={"error": "act"},
            max_iterations=3,
        )
    )
    graph.add_node(
        PromptNode(
            "act",
            instructions=(
                f"{prefix}"
                "Execute the plan with your tools. When the active subgoal is done, "
                "reply with a short summary of what you found, without a tool call."
            ),
            blocks=base_blocks,
            tools=list(tools) if tools else [],
            tool_choice="auto",
            inject_tools=True,
            default_next="observe",
            transitions={"error": "answer"},
            max_iterations=max_act_steps,
        )
    )
    graph.add_node(ObserveNode("observe", blocks=base_blocks, default_next="reflect"))
    graph.add_node(
        ReflectNode(
            "reflect",
            blocks=base_blocks,
            output_schema=PeoatrReflection,
            transitions={
                "answer": "answer",
                "continue": "act",
                "replan": "plan",
                "error": "answer",
            },
            default_next="answer",
            max_iterations=4,
        )
    )
    graph.add_node(
        SynthesizeNode(
            "answer",
            instructions=(
                f"{prefix}"
                "Write the final answer to the user's question from the results "
                "gathered. Use only facts from the tool results; say what could not "
                "be determined."
            ),
            blocks=base_blocks,
            default_next="__end__",
        )
    )

    graph.set_entry("think")
    return graph


class DebateCritique(BaseModel):
    """Structured output of the debate ``critic`` stage."""

    weaknesses: list[str] = Field(description="Errors, gaps and unsupported claims in the answer")
    improvements: list[str] = Field(description="A specific fix for each weakness")
    severity: float = Field(
        description="0.0 = the answer is right and complete, 1.0 = it is wrong; "
        "above 0.3 the answer goes back for revision"
    )


class DebateVerdict(BaseModel):
    """Structured output of the debate ``judge`` stage."""

    passes: bool = Field(
        description="true when the answer is correct, complete and supported by the tool results"
    )
    issues: list[str] = Field(description="What is still wrong or missing; empty when it passes")


def build_debate_graph(
    system_prompt: str = "",
    *,
    tools: list[BaseTool] | None = None,
    blocks: list[Any] | None = None,
    max_rounds: int = 3,
    max_proposer_steps: int = 12,
    **kwargs: Any,
) -> PromptGraph:
    """Build a debate graph: Proposer ↔ Critic → Judge → Answer.

    The proposer answers (with tools), the critic challenges the answer and
    sends it back for revision when its severity is above 0.3, the judge
    renders a verdict (a failed verdict also sends it back), and ``answer``
    writes the reply. Critic and judge produce structured output
    (:class:`DebateCritique`, :class:`DebateVerdict`), so their routing
    always reaches the engine, and every stage has a budget, so the run
    always ends with a written answer.

    Args:
        system_prompt: Base system prompt prepended to the proposer and answer.
        tools: Tools the proposer can call.
        blocks: Optional PromptBlocks shared across stages.
        max_rounds: How many times the critic and the judge may each run.
        max_proposer_steps: Budget for the proposer across the run — each
            model call (every tool-loop round) counts.

    Returns:
        A ``PromptGraph`` with proposer → critic → judge → answer nodes.
    """
    from .reasoning_nodes import CritiqueNode, SynthesizeNode, ValidateNode

    graph = PromptGraph(name="debate", mode="static")
    base_blocks = list(blocks) if blocks else []
    prefix = f"{system_prompt}\n\n" if system_prompt else ""

    graph.add_node(
        PromptNode(
            "proposer",
            instructions=(
                f"{prefix}"
                "Propose an answer to the user's question. Use your tools to check "
                "facts. If a critique or verdict is shown, fix every issue it lists — "
                "check it with the tools — and give the revised answer."
            ),
            blocks=base_blocks,
            tools=list(tools) if tools else [],
            tool_choice="auto",
            input_keys=["critique", "verdict"],
            is_entry=True,
            default_next="critic",
            transitions={"error": "answer"},
            max_iterations=max_proposer_steps,
        )
    )
    graph.add_node(
        CritiqueNode(
            "critic",
            blocks=base_blocks,
            severity_threshold=0.3,
            output_schema=DebateCritique,
            transitions={"revise": "proposer", "error": "judge"},
            default_next="judge",
            max_iterations=max_rounds,
        )
    )
    graph.add_node(
        ValidateNode(
            "judge",
            blocks=base_blocks,
            output_schema=DebateVerdict,
            output_key="verdict",
            on_pass="answer",
            on_fail="proposer",
            default_next="answer",
            transitions={"error": "answer"},
            max_iterations=max_rounds,
        )
    )
    graph.add_node(
        SynthesizeNode(
            "answer",
            instructions=(
                f"{prefix}"
                "Write the final answer to the user's question: the latest proposal, "
                "corrected for any issue the critique or verdict still lists. Use only "
                "facts from the tool results; say what could not be determined."
            ),
            blocks=base_blocks,
            input_keys=["critique", "verdict"],
            default_next="__end__",
        )
    )

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
    model stops re-looking-up what it already knows.  A call the model does
    repeat runs the tool again (a status changes); to answer repeats of
    lookups whose results cannot change from the earlier result, pass
    ``build_agent(context_compaction=ContextCompaction(reuse_results=[...]))``.

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
                "it and don't re-fetch a fact you already have, unless the task "
                "is to wait for it to change, such as a job's status you are polling."
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
    max_tool_calls: int = 50,
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
        max_repairs: Times to feed a failed program (a crash, or one that
            reached ``max_tool_calls``) back for a fix (default 1).
        exec_timeout: Max seconds the program may run inside the sandbox.
        max_tool_calls: Hard cap on each program's bridged tool calls
            (default 50), failed calls included — a hook-independent safety
            bound against a program looping a tool. A program that reaches
            it fails (its output is discarded); a repair gets a fresh budget.

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
            max_tool_calls=max_tool_calls,
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
