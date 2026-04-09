"""Prompt Inspector — full assembly and execution tracing.

See exactly how your prompt was assembled, which blocks made it in,
token usage per block, what was dropped, and the exact text sent to
the LLM.  No other framework gives you this level of visibility.

Example::

    from promptise.prompts.inspector import PromptInspector

    inspector = PromptInspector()

    @prompt(model="openai:gpt-5-mini", inspect=inspector)
    @blocks(Identity("Analyst"), Rules(["Be precise"]))
    async def analyze(text: str) -> str:
        \"""Analyze: {text}\"""

    result = await analyze("quarterly data")
    trace = inspector.last()
    print(trace.blocks_included)         # ["identity", "rules"]
    print(trace.total_tokens_estimated)  # 45
    print(trace.input_text)              # Full prompt sent to LLM
    print(inspector.summary())           # Human-readable summary
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .blocks import AssembledPrompt, BlockTrace

__all__ = [
    "PromptTrace",
    "ContextTrace",
    "NodeTrace",
    "GraphTrace",
    "PromptInspector",
]


# ---------------------------------------------------------------------------
# Trace dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ContextTrace:
    """Trace of a single context provider execution."""

    provider_name: str
    chars_injected: int
    render_time_ms: float


@dataclass
class PromptTrace:
    """Complete trace of a prompt assembly + execution."""

    timestamp: float
    prompt_name: str
    model: str

    # Assembly trace
    blocks: list[BlockTrace] = field(default_factory=list)
    total_tokens_estimated: int = 0
    blocks_included: list[str] = field(default_factory=list)
    blocks_excluded: list[str] = field(default_factory=list)

    # Context trace
    context_providers: list[ContextTrace] = field(default_factory=list)

    # Execution trace
    input_text: str = ""
    output_text: str = ""
    latency_ms: float = 0.0
    guards_passed: list[str] = field(default_factory=list)
    guards_failed: list[str] = field(default_factory=list)

    # Flow trace
    flow_phase: str = ""
    flow_turn: int = 0


@dataclass
class NodeTrace:
    """Trace of a single graph node execution."""

    node_id: str
    node_type: str
    duration_ms: float
    status: str
    input_state: dict[str, Any] = field(default_factory=dict)
    output_update: dict[str, Any] = field(default_factory=dict)
    prompt_trace: PromptTrace | None = None
    error: str | None = None


@dataclass
class GraphTrace:
    """Trace of a complete graph execution."""

    graph_name: str
    node_traces: list[NodeTrace] = field(default_factory=list)
    total_duration_ms: float = 0.0
    path: list[str] = field(default_factory=list)
    iterations: int = 0
    final_state: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# PromptInspector
# ---------------------------------------------------------------------------


class PromptInspector:
    """Collects and displays prompt assembly and execution traces.

    Attach to prompts, flows, or graphs to record every step of
    prompt composition and execution.
    """

    def __init__(self) -> None:
        self._traces: list[PromptTrace] = []
        self._graph_traces: list[GraphTrace] = []

    @property
    def traces(self) -> list[PromptTrace]:
        """All recorded prompt traces."""
        return list(self._traces)

    @property
    def graph_traces(self) -> list[GraphTrace]:
        """All recorded graph traces."""
        return list(self._graph_traces)

    def record_assembly(
        self,
        assembled: AssembledPrompt,
        prompt_name: str,
        model: str,
    ) -> PromptTrace:
        """Record a prompt assembly.  Returns the trace for further updates."""
        trace = PromptTrace(
            timestamp=time.time(),
            prompt_name=prompt_name,
            model=model,
            blocks=list(assembled.block_details),
            total_tokens_estimated=assembled.estimated_tokens,
            blocks_included=list(assembled.included),
            blocks_excluded=list(assembled.excluded),
            input_text=assembled.text,
        )
        self._traces.append(trace)
        return trace

    def record_execution(
        self,
        trace: PromptTrace,
        output: str,
        latency_ms: float,
    ) -> None:
        """Update a trace with execution results."""
        trace.output_text = output
        trace.latency_ms = latency_ms

    def record_context(
        self,
        trace: PromptTrace,
        provider_name: str,
        chars_injected: int,
        render_time_ms: float,
    ) -> None:
        """Record a context provider execution within a trace."""
        trace.context_providers.append(
            ContextTrace(
                provider_name=provider_name,
                chars_injected=chars_injected,
                render_time_ms=render_time_ms,
            )
        )

    def record_guard(
        self,
        trace: PromptTrace,
        guard_name: str,
        passed: bool,
    ) -> None:
        """Record a guard check result."""
        if passed:
            trace.guards_passed.append(guard_name)
        else:
            trace.guards_failed.append(guard_name)

    def record_graph(
        self,
        graph_name: str,
        node_traces: list[NodeTrace],
        total_duration_ms: float,
        path: list[str],
        iterations: int,
        final_state: dict[str, Any],
    ) -> GraphTrace:
        """Record a complete graph execution."""
        trace = GraphTrace(
            graph_name=graph_name,
            node_traces=node_traces,
            total_duration_ms=total_duration_ms,
            path=path,
            iterations=iterations,
            final_state=final_state,
        )
        self._graph_traces.append(trace)
        return trace

    def last(self) -> PromptTrace | None:
        """Most recent prompt trace, or ``None``."""
        return self._traces[-1] if self._traces else None

    def last_graph(self) -> GraphTrace | None:
        """Most recent graph trace, or ``None``."""
        return self._graph_traces[-1] if self._graph_traces else None

    def clear(self) -> None:
        """Discard all recorded traces."""
        self._traces.clear()
        self._graph_traces.clear()

    def summary(self) -> str:
        """Human-readable summary of all recorded traces."""
        lines: list[str] = []

        if self._traces:
            lines.append(f"=== Prompt Traces ({len(self._traces)}) ===")
            for i, trace in enumerate(self._traces):
                lines.append(f"\n--- Trace {i + 1}: {trace.prompt_name} ---")
                lines.append(f"  Model: {trace.model}")
                lines.append(f"  Tokens: {trace.total_tokens_estimated} estimated")
                if trace.blocks_included:
                    lines.append(f"  Blocks included: {', '.join(trace.blocks_included)}")
                if trace.blocks_excluded:
                    lines.append(f"  Blocks excluded: {', '.join(trace.blocks_excluded)}")
                if trace.context_providers:
                    providers = [
                        f"{c.provider_name} ({c.chars_injected} chars)"
                        for c in trace.context_providers
                    ]
                    lines.append(f"  Context: {', '.join(providers)}")
                if trace.latency_ms > 0:
                    lines.append(f"  Latency: {trace.latency_ms:.1f}ms")
                if trace.guards_passed:
                    lines.append(f"  Guards passed: {', '.join(trace.guards_passed)}")
                if trace.guards_failed:
                    lines.append(f"  Guards FAILED: {', '.join(trace.guards_failed)}")
                if trace.flow_phase:
                    lines.append(f"  Flow: phase={trace.flow_phase} turn={trace.flow_turn}")

        if self._graph_traces:
            lines.append(f"\n=== Graph Traces ({len(self._graph_traces)}) ===")
            for i, gt in enumerate(self._graph_traces):
                lines.append(f"\n--- Graph {i + 1}: {gt.graph_name} ---")
                lines.append(f"  Path: {' -> '.join(gt.path)}")
                lines.append(f"  Iterations: {gt.iterations}")
                lines.append(f"  Duration: {gt.total_duration_ms:.1f}ms")
                for nt in gt.node_traces:
                    status_marker = "+" if nt.status == "success" else "x"
                    lines.append(
                        f"  [{status_marker}] {nt.node_id} ({nt.node_type}, {nt.duration_ms:.1f}ms)"
                    )
                    if nt.error:
                        lines.append(f"      Error: {nt.error}")

        if not lines:
            return "No traces recorded."

        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"<PromptInspector traces={len(self._traces)} graph_traces={len(self._graph_traces)}>"
        )
