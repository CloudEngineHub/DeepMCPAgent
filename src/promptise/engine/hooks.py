"""Hook protocol and built-in hooks for the PromptGraph engine.

Hooks intercept execution at key points without modifying node logic.
They run in your application process, not inside the LLM's context.

Built-in hooks:
- ``LoggingHook`` — log every node execution
- ``TimingHook`` — enforce per-node time budgets
- ``CycleDetectionHook`` — detect infinite loops
"""

from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

from .base import BaseNode
from .state import GraphMutation, GraphState, NodeResult

logger = logging.getLogger("promptise.engine.hooks")


@runtime_checkable
class Hook(Protocol):
    """Protocol for engine hooks.

    All methods are optional — implement only the ones you need.
    The engine checks ``hasattr`` before calling each method.
    """

    async def pre_node(self, node: BaseNode, state: GraphState) -> GraphState:
        """Called before a node executes.  Can modify state."""
        ...

    async def post_node(self, node: BaseNode, result: NodeResult, state: GraphState) -> NodeResult:
        """Called after a node executes.  Can modify result."""
        ...

    async def pre_tool(self, tool_name: str, args: dict, state: GraphState) -> dict:
        """Called before a tool executes.  Can modify args.  Return modified args."""
        ...

    async def post_tool(self, tool_name: str, result: str, args: dict, state: GraphState) -> str:
        """Called after a tool executes.  Can modify result.  Return modified result."""
        ...

    async def on_tool_reused(
        self, tool_name: str, args: dict, result: str, state: GraphState
    ) -> None:
        """Called instead of ``pre_tool`` / ``post_tool`` when a call is answered
        from an earlier result without running the tool (tools opted in with
        ``ContextCompaction(reuse_results=...)``, and a repeat of an identical
        call in one model turn of a ``ToolNode(deduplicate=True)``)."""
        ...

    async def on_error(self, node: BaseNode, error: Exception, state: GraphState) -> str | None:
        """Called when a node fails (it raised, or its result has an
        ``error``), before ``post_node`` and before the engine routes the
        run.  *error* is the exception behind the failure (a
        ``RuntimeError`` carrying the error text when the node recorded
        none).  Return a node name (or ``"__end__"``) to go there next, or
        ``None`` for the usual handling (``on_error`` edges, transitions;
        a run that ends on the failure raises ``GraphExecutionError``).
        A ``CRITICAL`` node aborts the run whatever the hook returns."""
        ...

    async def on_graph_mutation(self, mutation: GraphMutation, state: GraphState) -> bool:
        """Called before a graph mutation is applied (a node's
        ``graph_mutations`` and the LLM's ``add_node`` / ``add_edge``
        actions).  Return ``False`` to block it; a hook that raises blocks
        it too."""
        ...


class LoggingHook:
    """Log every node execution with timing and token counts."""

    def __init__(self, level: int = logging.INFO) -> None:
        self._level = level

    async def pre_node(self, node: BaseNode, state: GraphState) -> GraphState:
        """Log node entry."""
        logger.log(
            self._level,
            "▶ Node %r (iter=%d, type=%s)",
            node.name,
            state.iteration,
            type(node).__name__,
        )
        return state

    async def post_node(self, node: BaseNode, result: NodeResult, state: GraphState) -> NodeResult:
        """Log node exit with timing."""
        tools_str = f", tools={len(result.tool_calls)}" if result.tool_calls else ""
        error_str = f", error={result.error}" if result.error else ""
        logger.log(
            self._level,
            "◀ Node %r → %s (%.0fms, tokens=%d%s%s)",
            node.name,
            result.next_node or "(resolve)",
            result.duration_ms,
            result.total_tokens,
            tools_str,
            error_str,
        )
        return result


class TimingHook:
    """Enforce per-node time budgets.

    If a node exceeds its time budget, the hook sets an error
    on the result but does NOT abort the graph (the engine's
    error recovery handles that).

    Args:
        default_budget_ms: Default time budget in milliseconds.
        per_node_budgets: Override budgets per node name.
    """

    def __init__(
        self,
        default_budget_ms: float = 30000,
        per_node_budgets: dict[str, float] | None = None,
    ) -> None:
        self._default = default_budget_ms
        self._budgets = dict(per_node_budgets) if per_node_budgets else {}
        self._starts: dict[str, float] = {}

    async def pre_node(self, node: BaseNode, state: GraphState) -> GraphState:
        """Record start time."""
        self._starts[node.name] = time.monotonic()
        return state

    async def post_node(self, node: BaseNode, result: NodeResult, state: GraphState) -> NodeResult:
        """Check if node exceeded its time budget."""
        start = self._starts.pop(node.name, None)
        if start is None:
            return result

        elapsed_ms = (time.monotonic() - start) * 1000
        budget = self._budgets.get(node.name, self._default)

        if elapsed_ms > budget:
            logger.warning(
                "Node %r exceeded time budget: %.0fms > %.0fms",
                node.name,
                elapsed_ms,
                budget,
            )
            if not result.error:
                result.error = f"Time budget exceeded: {elapsed_ms:.0f}ms > {budget:.0f}ms"

        return result


class CycleDetectionHook:
    """Detect infinite loops by tracking visit patterns.

    If the same sequence of N nodes repeats ``max_repeats`` times,
    the hook ends the run before the next node executes (the engine
    respects a ``pre_node`` stop: the node does not run).

    A node re-entered for its own tool loop (it called tools and the
    engine runs it again with their results) is one step, not a repeat:
    a single ReAct node making many tool calls is not a cycle.  Use
    ``max_agent_iterations`` / ``max_iterations`` to bound those.

    Args:
        sequence_length: Length of the pattern to detect.
        max_repeats: How many times the pattern must repeat
            before triggering.
    """

    def __init__(self, sequence_length: int = 3, max_repeats: int = 3) -> None:
        self._seq_len = sequence_length
        self._max_repeats = max_repeats

    @staticmethod
    def _steps(state: GraphState) -> list[str]:
        """``state.visited`` with tool-loop re-entries folded into one step.

        ``visited[i]`` was executed as ``node_history[i]`` (the last entry,
        the node about to run, has no result yet).  A visit is a re-entry
        when the previous visit was the same node and ended with tool calls.
        """
        visited = state.visited
        history = state.node_history
        if len(history) != len(visited) - 1:
            return list(visited)
        steps: list[str] = []
        for index, name in enumerate(visited):
            if (
                index > 0
                and visited[index - 1] == name
                and history[index - 1].transition_reason == "tool_calls_present"
            ):
                continue
            steps.append(name)
        return steps

    async def pre_node(self, node: BaseNode, state: GraphState) -> GraphState:
        """Check for repeating patterns in visited nodes."""
        visited = self._steps(state)
        if len(visited) < self._seq_len * self._max_repeats:
            return state

        # Extract the last `sequence_length` nodes
        pattern = visited[-self._seq_len :]

        # Count how many times this pattern appears at the end
        repeats = 0
        for i in range(len(visited) - self._seq_len, -1, -self._seq_len):
            segment = visited[i : i + self._seq_len]
            if segment == pattern:
                repeats += 1
            else:
                break

        if repeats >= self._max_repeats:
            logger.warning(
                "Cycle detected: pattern %s repeated %d times. Forcing end.",
                pattern,
                repeats,
            )
            state.current_node = "__end__"

        return state

    async def post_node(self, node: BaseNode, result: NodeResult, state: GraphState) -> NodeResult:
        """No-op for post_node."""
        return result


class MetricsHook:
    """Collects per-node metrics: tokens, latency, errors, call counts.

    Access metrics via ``hook.metrics`` dict after execution.
    """

    def __init__(self) -> None:
        self.metrics: dict[str, dict[str, float]] = {}

    async def pre_node(self, node: BaseNode, state: GraphState) -> GraphState:
        """Initialize metrics for this node."""
        if node.name not in self.metrics:
            self.metrics[node.name] = {
                "calls": 0,
                "total_tokens": 0,
                "total_duration_ms": 0.0,
                "errors": 0,
                "tool_calls": 0,
            }
        return state

    async def post_node(self, node: BaseNode, result: NodeResult, state: GraphState) -> NodeResult:
        """Accumulate metrics."""
        m = self.metrics.get(node.name, {})
        m["calls"] = m.get("calls", 0) + 1
        m["total_tokens"] = m.get("total_tokens", 0) + result.total_tokens
        m["total_duration_ms"] = m.get("total_duration_ms", 0.0) + result.duration_ms
        m["tool_calls"] = m.get("tool_calls", 0) + len(result.tool_calls)
        if result.error:
            m["errors"] = m.get("errors", 0) + 1
        self.metrics[node.name] = m
        return result

    def summary(self) -> dict[str, dict[str, float]]:
        """Return collected metrics."""
        return dict(self.metrics)

    def reset(self) -> None:
        """Clear all metrics."""
        self.metrics.clear()


class BudgetHook:
    """Enforces a token and/or cost budget for each graph run.

    After every node, the hook adds the node's tokens to the run's total
    (``GraphState.total_tokens``, so the budget is per run: one hook on an
    engine caps every run separately, also concurrent ones).  When the run
    goes over ``max_tokens`` or ``max_cost_usd``, the hook ends the run
    (``state.current_node = "__end__"``) and writes the reason to the node's
    ``NodeResult.error``.

    A run the budget stops after the model asked for tools, before it saw
    their results, has no answer: the engine raises
    :class:`~promptise.engine.GraphBudgetExceededError` instead of returning
    the last tool result.  A run whose model answered on the node that went
    over budget returns that answer.

    Attributes:
        tokens_used: Tokens of the latest run (as of its last node).
        cost_used: Estimated cost of the latest run, from ``tokens_used``
            and ``cost_per_1k_tokens``.
    """

    def __init__(
        self,
        *,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
        cost_per_1k_tokens: float = 0.002,
    ) -> None:
        self.max_tokens = max_tokens
        self.max_cost_usd = max_cost_usd
        self.cost_per_1k_tokens = cost_per_1k_tokens
        self.tokens_used = 0
        self.cost_used = 0.0

    async def pre_node(self, node: BaseNode, state: GraphState) -> GraphState:
        """Check budget before node executes."""
        return state

    async def post_node(self, node: BaseNode, result: NodeResult, state: GraphState) -> NodeResult:
        """Track the run's tokens and enforce the budget."""
        # The engine adds this node's tokens to state.total_tokens after the
        # post-node hooks, so the run's total so far is that plus this node.
        tokens = state.total_tokens + result.total_tokens
        cost = (tokens / 1000) * self.cost_per_1k_tokens
        self.tokens_used = tokens
        self.cost_used = cost

        if self.max_tokens and tokens > self.max_tokens:
            logger.warning("Token budget exceeded: %d > %d", tokens, self.max_tokens)
            state.current_node = "__end__"
            result.error = f"Token budget exceeded: {tokens}/{self.max_tokens} tokens"

        if self.max_cost_usd and cost > self.max_cost_usd:
            logger.warning("Cost budget exceeded: $%.4f > $%.4f", cost, self.max_cost_usd)
            state.current_node = "__end__"
            result.error = f"Cost budget exceeded: ${cost:.4f}/${self.max_cost_usd:.4f}"

        return result
