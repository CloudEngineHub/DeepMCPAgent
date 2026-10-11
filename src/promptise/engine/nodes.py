"""Built-in node types for the PromptGraph engine.

Nine composable node types that cover 95% of agent reasoning patterns.
Developers configure nodes — they don't subclass them.

Node types:

- **PromptNode** — LLM reasoning with blocks, tools, guards, strategy
- **ToolNode** — Explicit tool execution with validation and dedup
- **RouterNode** — LLM-based routing to decide the next path
- **GuardNode** — Validate state and gate transitions
- **ParallelNode** — Run multiple nodes concurrently
- **LoopNode** — Repeat a subgraph until a condition is met
- **HumanNode** — Pause for human input or approval
- **TransformNode** — Pure data transformation (no LLM call)
- **SubgraphNode** — Embed a complete sub-graph
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, cast
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    SystemMessage,
    ToolMessage,
    message_chunk_to_message,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel as PydanticBaseModel

from .base import BaseNode
from .state import GraphState, NodeEvent, NodeResult

#: ``id`` of a leading :class:`SystemMessage` that carries the agent's
#: per-turn prompt (e.g. a conversation flow's).  :class:`PromptNode`
#: appends its own instructions and tool list to that message instead of
#: adding a second system message.
AGENT_PROMPT_MESSAGE_ID = "promptise-agent-prompt"


def _is_agent_prompt(message: Any) -> bool:
    return isinstance(message, SystemMessage) and message.id == AGENT_PROMPT_MESSAGE_ID


def _place_node_prompt(messages: list[Any], node_sys_msg: SystemMessage) -> bool:
    """Put a node's system message into *messages* (in place).

    When *messages* starts with the agent's prompt, *node_sys_msg* already
    carries it, so it replaces that message and the model gets one system
    message.  Returns ``True`` in that case.
    """
    if messages and _is_agent_prompt(messages[0]):
        messages[0] = node_sys_msg
        return True
    return False


logger = logging.getLogger("promptise.engine")

# A node records a failure as ``NodeResult.error`` (a string — what hooks,
# history and reports carry) and keeps the exception behind it on the result
# under this attribute, so the engine can raise with the provider error as
# ``__cause__`` when the run ends on that failure.  It is deliberately not a
# dataclass field: it never serializes, never compares, never prints.
_FAILURE_CAUSE_ATTR = "_failure_cause"

#: Run-config key for a tool selector: ``selector(candidates, state)`` returns
#: the subset of a node's candidate tools to offer on its next model call. It
#: runs before every LLM call, so the offered tools can change between steps of
#: one run (``build_agent(optimize_tools="semantic")`` installs one). Tool calls
#: still execute against all candidates.
TOOL_SELECTOR_KEY = "_tool_selector"

#: Callback ``metadata`` key set to ``True`` on the ``on_tool_start`` of a call
#: answered from an earlier result (``ContextCompaction(reuse_results=...)``,
#: or a repeat in one model turn of a ``ToolNode(deduplicate=True)``) instead
#: of running the tool.
REUSED_TOOL_METADATA_KEY = "promptise_reused"


async def _report_reused_call(
    tool: BaseTool | None,
    tool_name: str,
    args: dict[str, Any],
    result: str,
    tool_call_id: str,
    state: GraphState,
    config: dict[str, Any],
) -> None:
    """Report a call answered from an earlier result, so it is not invisible.

    Engine hooks get ``on_tool_reused(tool_name, args, result, state)`` (how
    ``build_agent`` feeds ``trace_tools`` and ``observer``), and the run's
    LangChain callbacks get the tool's start and end with
    :data:`REUSED_TOOL_METADATA_KEY` in the metadata.  A reporting failure is
    logged and never changes the call's result.
    """
    for hook in config.get("_engine_hooks", []):
        report = getattr(hook, "on_tool_reused", None)
        if report is None:
            continue
        try:
            outcome = report(tool_name, args, result, state)
            if asyncio.iscoroutine(outcome):
                await outcome
        except Exception as exc:
            logger.warning("on_tool_reused hook %r failed: %s", type(hook).__name__, exc)
    await _report_to_callbacks(
        tool,
        tool_name,
        args,
        tool_call_id,
        config,
        output=ToolMessage(content=result, tool_call_id=tool_call_id, name=tool_name),
        metadata={REUSED_TOOL_METADATA_KEY: True},
    )


async def _report_to_callbacks(
    tool: BaseTool | None,
    tool_name: str,
    args: Any,
    tool_call_id: str,
    config: dict[str, Any],
    *,
    output: Any = None,
    error: BaseException | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Give the run's LangChain callbacks the start and end (or *error*) of a
    call the tool itself never saw, as the tool would have reported it.
    A reporting failure is logged and never changes the call's result."""
    try:
        from langchain_core.callbacks import AsyncCallbackManager

        manager = AsyncCallbackManager.configure(
            inheritable_callbacks=config.get("callbacks"),
            inheritable_tags=config.get("tags"),
            inheritable_metadata=config.get("metadata"),
            local_metadata=metadata,
        )
        run = await manager.on_tool_start(
            {"name": tool_name, "description": getattr(tool, "description", "") or ""},
            str(args),
            inputs=args if isinstance(args, dict) else None,
            tool_call_id=tool_call_id,
        )
        if error is not None:
            await run.on_tool_error(error, name=tool_name)
        else:
            await run.on_tool_end(output, name=tool_name)
    except Exception as exc:
        logger.debug("Could not report a call to %r to callbacks: %s", tool_name, exc)


# ---------------------------------------------------------------------------
# Running one tool call — shared by PromptNode and ToolNode
# ---------------------------------------------------------------------------


def _checks_own_arguments(tool: BaseTool) -> bool:
    """Whether *tool* is a Promptise MCP tool, bare or behind Promptise's
    wrappers (approval gate, guardrail scanning, tool tracing).

    Since 1.3.1 such a tool checks its own arguments the way
    :func:`_checked_arguments` does, answers invalid ones with the same
    ``ToolArgumentError``, and reports the rejected call through its own
    trace hooks (``trace_tools``, ``observer``), which a check before the
    call would bypass.
    """
    from promptise.mcp.client._tool_adapter import _PromptiseMCPTool
    from promptise.tools import WrappingTool

    inner: Any = tool
    for _ in range(8):  # wrappers nest a few levels deep at most
        if not isinstance(inner, WrappingTool):
            break
        inner = getattr(inner, "_inner", None)
    return isinstance(inner, _PromptiseMCPTool)


def _checked_arguments(tool: BaseTool, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """*args* checked against *tool*'s parameter schema, as the tool gets them.

    The schema is the one the model is offered (``tool.tool_call_schema``:
    the tool's ``args_schema`` without injected arguments).  As for MCP tools
    since 1.3.1, an explicit ``null`` for an optional parameter that cannot
    be null counts as not given and is dropped.  Not checked here: a tool
    whose schema is a JSON-schema dict rather than a pydantic model (it
    validates its own input when it runs), and an MCP tool, which checks its
    arguments itself with the same result (:func:`_checks_own_arguments`).

    Raises:
        ToolArgumentError: The arguments do not match the schema; its
            message lists each problem by argument path for the model.
    """
    from promptise.tools import drop_disallowed_nulls, parse_tool_arguments

    if _checks_own_arguments(tool):
        return args
    try:
        schema = tool.tool_call_schema
    except Exception as exc:
        logger.debug("No argument schema for tool %r, not validating: %s", tool_name, exc)
        return args
    if not (isinstance(schema, type) and issubclass(schema, PydanticBaseModel)):
        return args
    parse_tool_arguments(tool_name, schema, args)
    return drop_disallowed_nulls(schema, args, tool_name=tool_name)


async def _invoke_tool(
    tool: BaseTool,
    tool_name: str,
    args: dict[str, Any],
    tool_call_id: str,
    config: dict[str, Any],
) -> tuple[str, bool, bool]:
    """Invoke *tool* as a tool call.  Returns ``(content, success, raised)``.

    Invoked as a tool call, a tool answers with a ``ToolMessage`` whose status
    says whether it failed (a tool that handles its ``ToolException``).  A
    tool that reports an error by raising ``ToolException`` -- an MCP error
    result raises ``MCPToolError``, invalid arguments ``ToolArgumentError`` --
    answers with the server's own text (its error envelope, with the error's
    code), so the model can correct the call.  Any other exception is a tool
    that raised (``raised`` is ``True``).
    """
    try:
        output = await tool.ainvoke(
            {"type": "tool_call", "name": tool_name, "args": args, "id": tool_call_id},
            config=cast("RunnableConfig | None", config),
        )
    except ToolException as exc:
        text = getattr(exc, "text", None)
        return (text if isinstance(text, str) and text else str(exc)), False, False
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}", False, True
    failed = False
    if isinstance(output, ToolMessage):
        failed = output.status == "error"
        output = output.content
    if output is None:
        content = ""
    elif isinstance(output, str):
        content = output
    else:
        content = str(output)
    return content, not failed, False


async def _call_tool(
    tool: BaseTool | None,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_call_id: str,
    state: GraphState,
    config: dict[str, Any],
    *,
    validate: bool = False,
    max_result_chars: int | None = None,
) -> tuple[dict[str, Any], str]:
    """Run one tool call the way every built-in node runs one.

    1. The engine hooks' ``pre_tool(tool_name, args, state)``, in order: each
       returns the arguments to use (it may change them).  A hook that raises
       blocks the call: the exception propagates (see :func:`_run_tool_call`)
       and neither the tool nor ``post_tool`` runs.
    2. With *validate*, the arguments are checked against the tool's
       parameter schema (:func:`_checked_arguments`).  Invalid arguments are
       the call's error result (``ToolArgumentError``'s message, which tells
       the model what to correct); the tool does not run, and the run's
       callbacks get the call's start and error.
    3. The tool, invoked as a tool call (:func:`_invoke_tool`).  A successful
       result is cut to *max_result_chars* when given.
    4. The engine hooks' ``post_tool(tool_name, result, args, state)``, in
       order: each returns the result to use.

    Returns ``(record, content)``: the ``NodeResult.tool_calls`` record
    (``name``, ``args``, ``result``, ``success``, and ``raised`` when the tool
    raised) and the content of the call's ``ToolMessage``.
    """
    hooks = config.get("_engine_hooks", [])
    for hook in hooks:
        if hasattr(hook, "pre_tool"):
            tool_args = await hook.pre_tool(tool_name, tool_args, state)

    record: dict[str, Any] = {"name": tool_name, "args": tool_args}
    if tool is None:
        content = f"Error: Unknown tool '{tool_name}'"
        record.update(result=content, success=False)
    else:
        from promptise.tools import ToolArgumentError

        call_args = tool_args
        rejected: ToolArgumentError | None = None
        if validate:
            try:
                call_args = _checked_arguments(tool, tool_name, tool_args)
            except ToolArgumentError as exc:
                rejected = exc
        if rejected is not None:
            await _report_to_callbacks(
                tool, tool_name, tool_args, tool_call_id, config, error=rejected
            )
            content = rejected.text
            record.update(result=content, success=False)
        else:
            content, success, raised = await _invoke_tool(
                tool, tool_name, call_args, tool_call_id, config
            )
            if success and max_result_chars is not None:
                content = content[:max_result_chars]
            record.update(result=content, success=success)
            if raised:
                record["raised"] = True

    for hook in hooks:
        if hasattr(hook, "post_tool"):
            content = await hook.post_tool(tool_name, content, tool_args, state)
    return record, content


async def _run_tool_call(
    tool: BaseTool | None,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_call_id: str,
    state: GraphState,
    config: dict[str, Any],
    *,
    validate: bool = False,
    max_result_chars: int | None = None,
) -> tuple[dict[str, Any], str, float]:
    """:func:`_call_tool`, timed; never raises.

    Returns ``(record, content, duration_ms)``.  A hook that raised (a
    ``pre_tool`` hook blocking the call, or a failing ``post_tool`` hook) makes
    the call a failed one whose result is ``"Error: <exception message>"``,
    reported like a tool that raised.
    """
    started = time.perf_counter()
    try:
        record, content = await _call_tool(
            tool,
            tool_name,
            tool_args,
            tool_call_id,
            state,
            config,
            validate=validate,
            max_result_chars=max_result_chars,
        )
    except Exception as exc:  # a hook raised
        content = f"Error: {exc}"
        record = {
            "name": tool_name,
            "args": tool_args,
            "result": content,
            "success": False,
            "raised": True,
        }
    return record, content, (time.perf_counter() - started) * 1000


def _tool_start_event(tool_name: str, args: Any, run_id: str, *, reused: bool = False) -> NodeEvent:
    """The ``on_tool_start`` stream event of one tool call."""
    data: dict[str, Any] = {"input": args, "run_id": run_id}
    if reused:
        data["reused"] = True
    return NodeEvent(event="on_tool_start", node_name=tool_name, data=data)


def _tool_end_event(record: dict[str, Any], content: str, ms: float, run_id: str) -> NodeEvent:
    """The stream event that ends one tool call: ``on_tool_error`` when the
    tool (or a hook) raised, else ``on_tool_end`` with ``status``
    (``"success"`` / ``"error"``) and ``reused`` for a reused result."""
    data: dict[str, Any] = {"output": content, "run_id": run_id, "duration_ms": ms}
    if record.get("raised"):
        data["error"] = content
        return NodeEvent(event="on_tool_error", node_name=record["name"], data=data)
    data["status"] = "success" if record.get("success", True) else "error"
    if record.get("reused"):
        data["reused"] = True
    return NodeEvent(event="on_tool_end", node_name=record["name"], data=data)


def _tool_message(record: dict[str, Any], content: str, tool_call_id: str) -> ToolMessage:
    """The ``ToolMessage`` that answers one tool call."""
    return ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        name=record["name"],
        status="success" if record.get("success", True) else "error",
    )


def record_failure(result: NodeResult, exc: BaseException, error: str | None = None) -> None:
    """Record *exc* as the failure of *result*.

    Sets ``result.error`` (to *error*, or ``"ExcType: message"``) and keeps
    the exception object for :func:`failure_cause`.

    Args:
        result: The node result to mark as failed.
        exc: The exception that caused the failure.
        error: Error text to record; defaults to ``"ExcType: message"``.
    """
    result.error = error if error is not None else f"{type(exc).__name__}: {exc}"
    setattr(result, _FAILURE_CAUSE_ATTR, exc)


def failure_cause(result: NodeResult) -> BaseException | None:
    """The exception behind ``result.error``, or ``None`` when the failure was
    recorded without one (a node that set ``error`` itself)."""
    cause = getattr(result, _FAILURE_CAUSE_ATTR, None)
    return cause if isinstance(cause, BaseException) else None


def token_usage(message: Any) -> tuple[int, int]:
    """``(input_tokens, output_tokens)`` from a message's ``usage_metadata``.

    LangChain's ``usage_metadata`` is a ``TypedDict`` (a plain dict at
    runtime); attribute-style objects are accepted too. ``(0, 0)`` when the
    provider reported no usage.
    """
    usage = getattr(message, "usage_metadata", None)
    if not usage:
        return 0, 0
    if isinstance(usage, dict):
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    else:
        input_tokens = getattr(usage, "input_tokens", 0)
        output_tokens = getattr(usage, "output_tokens", 0)
    return int(input_tokens or 0), int(output_tokens or 0)


def _is_raw_structured(response: Any) -> bool:
    """Whether *response* is ``with_structured_output(include_raw=True)``'s
    ``{"raw": AIMessage, "parsed": ..., "parsing_error": ...}`` envelope."""
    return (
        isinstance(response, dict)
        and set(response) <= {"raw", "parsed", "parsing_error"}
        and "raw" in response
        and "parsed" in response
        and isinstance(response["raw"], BaseMessage)
    )


# ---------------------------------------------------------------------------
# PromptNode — LLM reasoning step (the core node type)
# ---------------------------------------------------------------------------


class PromptNode(BaseNode):
    """A complete reasoning unit in the graph.

    Each PromptNode is a self-contained processing pipeline:

    **Input** → **Preprocess** → **Context Assembly** → **LLM Call** →
    **Tool Execution** → **Postprocess** → **Guards** → **Output**

    Every aspect is configurable:
    - What the LLM sees (blocks, strategy, perspective, context layers)
    - What the LLM can do (tools, tool_choice)
    - What the LLM must produce (output_schema, guards)
    - How data flows in (input_keys read from state.context)
    - How data flows out (output_key writes to state.context)
    - Pre/post processing (preprocessor, postprocessor callables)
    - Context from previous node (inherit_context_from)

    Args:
        name: Unique node identifier.

        blocks: PromptBlocks to assemble for this node's system prompt.
        strategy: Reasoning strategy (ChainOfThought, SelfCritique, etc.).
        perspective: Cognitive perspective (Analyst, Critic, etc.).

        tools: Tools available at THIS node.  ``None`` disables tools.
        tool_choice: ``"auto"`` (LLM decides), ``"required"`` (must call
            a tool), or ``"none"`` (tools shown but not callable).
        inject_tools: When ``True``, the engine auto-injects all
            discovered MCP tools into this node at runtime.  Use this
            when defining custom graphs where nodes should receive
            tools from ``build_agent(servers=...)`` without hardcoding
            them.  The injected tools merge with any tools already
            set on the node (no duplicates).

        output_schema: Pydantic model for structured output.
        guards: Guards that validate the output before proceeding.

        context_layers: Extra named context layers with priorities.
            Merged into the system prompt during assembly.
        max_tokens: Max response tokens for this node.
        temperature: LLM temperature for this node.

        input_keys: Keys to read from ``state.context`` and inject
            into the system prompt.  E.g. ``["search_results", "user_query"]``
            makes those values available in the prompt.
        output_key: Key to write this node's output to in ``state.context``.
            The next node can read it via ``input_keys``.
        inherit_context_from: Name of a previous node whose output
            should be injected as context.  Reads from
            ``state.context["{node_name}_output"]``.

        preprocessor: Async callable ``(state, config) → state`` that
            runs before the LLM call.  Use for data transformation,
            enrichment, or filtering.
        postprocessor: Async callable ``(output, state, config) → output``
            that runs after the LLM call.  Use for output transformation,
            validation, or enrichment.

        include_observations: Auto-inject recent tool results from state.
        include_plan: Auto-inject current plan/subgoals from state.
        include_reflections: Auto-inject past learnings from state.

        context_scope: How much history the model sees: ``"full"``,
            ``"auto"`` (full, compacting once the tool loop gets long),
            ``"ledger"`` (always compacted) or ``"scoped"``.  See
            :mod:`promptise.engine.compaction`.
        auto_ledger_after: Tool results before an ``"auto"`` node compacts
            (default 6).  Shorthand for
            ``compaction=ContextCompaction(after_tool_results=...)``.
        compaction: :class:`~promptise.engine.compaction.ContextCompaction`
            settings for this node (or ``True`` / ``False`` / an ``int``).
            Without it the node uses the settings ``build_agent`` passed,
            else the defaults.
    """

    def __init__(
        self,
        name: str,
        *,
        # Prompt composition
        blocks: list[Any] | None = None,
        strategy: Any | None = None,
        perspective: Any | None = None,
        # Tools
        tools: list[BaseTool] | None = None,
        tool_choice: str = "auto",
        inject_tools: bool = False,
        # Output
        output_schema: type | None = None,
        guards: list[Any] | None = None,
        # Model
        model_override: Any | None = None,
        # Context
        context_layers: dict[str, int] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        # Data flow
        input_keys: list[str] | None = None,
        output_key: str | None = None,
        inherit_context_from: str | None = None,
        context_scope: str = "full",
        auto_ledger_after: int | None = None,
        compaction: Any | None = None,
        # Processing pipeline
        preprocessor: Callable | None = None,
        postprocessor: Callable | None = None,
        # Auto-inject state data
        include_observations: bool = True,
        include_plan: bool = True,
        include_reflections: bool = True,
        # BaseNode params
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.blocks = list(blocks) if blocks else []
        self.strategy = strategy
        self.perspective = perspective
        from .state import NodeFlag

        self.tools = list(tools) if tools else []
        self.tool_choice = tool_choice
        # inject_tools maps to NodeFlag.INJECT_TOOLS
        if inject_tools:
            self.flags.add(NodeFlag.INJECT_TOOLS)
        self.model_override = model_override
        self.output_schema = output_schema
        self.guards = list(guards) if guards else []
        self.context_layers = dict(context_layers) if context_layers else {}
        self.max_tokens = max_tokens
        self.temperature = temperature
        # Data flow
        self.input_keys = list(input_keys) if input_keys else []
        self.output_key = output_key
        self.inherit_context_from = inherit_context_from
        # Context lifecycle management — how much history this node sees:
        #   "auto": behaves like "full" for short tool loops, then switches to
        #     "ledger" once the loop grows past ``auto_ledger_after`` tool
        #     results. The default for the built-in ReAct pattern: simple tasks
        #     are unchanged, deep tool loops stay bounded — context handling and
        #     token efficiency without choosing a pattern.
        #   "full": the whole accumulated transcript.
        #   "scoped": only the system prompt (with any inherited/​injected
        #     distilled state), the original task, and the node's own
        #     in-progress tool loop — NOT other stages' verbose messages.
        #     Bounds token growth across a multi-stage reasoning graph.
        #   "ledger": for long single-node tool loops — replaces the growing
        #     tool-call/result transcript with a compact, DEDUPLICATED "facts
        #     gathered" ledger (from state.observations), so the model stops
        #     re-querying facts it already has.
        self.context_scope = context_scope
        # "auto" flips to ledger once this many tool results have accumulated.
        self._auto_ledger_explicit = auto_ledger_after is not None
        self.auto_ledger_after = auto_ledger_after if auto_ledger_after is not None else 6
        # Node-level compaction settings; None defers to the engine's.
        from .compaction import ContextCompaction

        self.compaction: ContextCompaction | None = (
            ContextCompaction.coerce(compaction) if compaction is not None else None
        )
        # Pipeline
        self.preprocessor = preprocessor
        self.postprocessor = postprocessor
        # Auto-inject
        self.include_observations = include_observations
        self.include_plan = include_plan
        self.include_reflections = include_reflections

    @property
    def inject_tools(self) -> bool:
        """Whether this node receives MCP tools at runtime."""
        from .state import NodeFlag

        return NodeFlag.INJECT_TOOLS in self.flags

    def _bind_model(
        self, model: Any, active_tools: list[BaseTool], config: dict[str, Any]
    ) -> tuple[Any, bool]:
        """Bind tools and structured output to *model*, reusing the binding
        from an earlier execution of this node in the same run while the
        model and the tool set are unchanged.

        Returns ``(model_to_use, structured_raw)`` —
        ``structured_raw`` is ``True`` when structured output was requested
        with ``include_raw=True`` (so token usage survives parsing).
        """
        cache_key = f"_node_cache_{self.name}"
        tool_names = tuple(t.name for t in active_tools)
        # A string/Model override resolves to a new object every execution;
        # key the binding on the override itself.
        source = self.model_override if self.model_override is not None else model
        cached = config.get(cache_key)
        if (
            cached is not None
            and cached["source"] is source
            and cached["tool_names"] == tool_names
            and cached["output_schema"] is self.output_schema
        ):
            return cached["model"], cached["structured_raw"]

        model_to_use = model.bind_tools(active_tools) if active_tools else model
        structured_raw = False
        if self.output_schema and hasattr(model_to_use, "with_structured_output"):
            try:
                model_to_use = model_to_use.with_structured_output(
                    self.output_schema, include_raw=True
                )
                structured_raw = True
            except Exception:
                try:
                    model_to_use = model_to_use.with_structured_output(self.output_schema)
                except Exception as exc:
                    logger.warning(
                        "Node %r: model does not support structured output (%s) — "
                        "output_schema ignored",
                        self.name,
                        exc,
                    )

        config[cache_key] = {
            "source": source,
            "tool_names": tool_names,
            "output_schema": self.output_schema,
            "model": model_to_use,
            "structured_raw": structured_raw,
        }
        return model_to_use, structured_raw

    def _compaction_settings(self, config: dict[str, Any] | None = None) -> Any:
        """The :class:`ContextCompaction` settings this node runs with.

        The node's own ``compaction`` wins, then the engine's (from
        ``build_agent(context_compaction=...)``), then the defaults.  An
        explicit ``auto_ledger_after`` overrides the threshold of the
        engine's settings.
        """
        from dataclasses import replace

        from .compaction import ContextCompaction

        if self.compaction is not None:
            return self.compaction
        settings = (config or {}).get("_engine_compaction") or ContextCompaction()
        if self._auto_ledger_explicit:
            settings = replace(settings, after_tool_results=self.auto_ledger_after)
        return settings

    def _candidate_tools(self, config: dict[str, Any]) -> list[BaseTool]:
        """The node's own tools plus, when flagged, the engine-injected ones."""
        tools = list(self.tools)
        if self.inject_tools:
            names = {t.name for t in tools}
            for et in config.get("_engine_tools", []):
                if et.name not in names:
                    tools.append(et)
                    names.add(et.name)
        return tools

    def _offered_tools(
        self, candidates: list[BaseTool], state: GraphState, config: dict[str, Any]
    ) -> list[BaseTool]:
        """Narrow *candidates* with the run's tool selector, if one is set."""
        selector = config.get(TOOL_SELECTOR_KEY)
        if selector is None or not candidates:
            return candidates
        try:
            return list(selector(candidates, state))
        except Exception as exc:
            logger.warning(
                "Tool selector failed in node %r, offering all tools: %s", self.name, exc
            )
            return candidates

    def _effective_context_scope(
        self, state: GraphState, config: dict[str, Any] | None = None
    ) -> str:
        """Resolve ``context_scope``, expanding ``"auto"`` for the current state.

        ``"auto"`` stays ``"full"`` while the tool loop is short (no change to
        simple tasks) and switches to ``"ledger"`` once enough tool results have
        accumulated, or once the transcript passes the token budget, so deep
        tool loops stay bounded automatically.  With compaction disabled it
        stays ``"full"``.
        """
        if self.context_scope != "auto":
            return self.context_scope
        settings = self._compaction_settings(config)
        if not settings.enabled:
            return "full"
        if len(state.observations) >= settings.after_tool_results:
            return "ledger"
        if (
            settings.max_tokens is not None
            and settings.tokens(state.messages) > settings.max_tokens
        ):
            return "ledger"
        return "full"

    def _earlier_results(
        self, tool_calls: list[Any], state: GraphState, config: dict[str, Any]
    ) -> list[str | None]:
        """For each tool call, the earlier result that answers it, or ``None``
        when the tool must run (the default for every call).

        A call is answered from an earlier result only when the node's
        compaction settings opt its tool in (``reuse_results``), the node is
        compacting (``"ledger"``), and an earlier successful call in this run
        had the same tool and arguments; its latest result is used, as in
        the ledger.
        """
        settings = self._compaction_settings(config)
        if not settings.reuse_results or self._effective_context_scope(state, config) != "ledger":
            return [None] * len(tool_calls)
        found: list[str | None] = []
        for tc in tool_calls:
            name, args = tc.get("name", ""), tc.get("args", {})
            result: str | None = None
            if settings.reuses(name):
                for obs in reversed(state.observations):
                    if obs.get("tool") == name and obs.get("args") == args and obs.get("success"):
                        result = str(obs.get("result", ""))
                        break
            found.append(result)
        return found

    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        """Execute the full node pipeline:

        1. Preprocessor (custom data transformation)
        2. Context assembly (blocks + input_keys + inherited context + state)
        3. LLM call (with tools if configured)
        4. Tool execution (auto-loop if tools called)
        5. Postprocessor (custom output transformation)
        6. Guards (validate output)
        7. Write output to state.context[output_key]
        """
        result = NodeResult(node_name=self.name, node_type="prompt", iteration=state.iteration)
        async for _event in self._pipeline(state, config, result, stream=False):
            pass
        return result

    async def stream(self, state: GraphState, config: dict[str, Any]) -> AsyncIterator[NodeEvent]:
        """Run the :meth:`execute` pipeline, streaming as it goes.

        One model call per step, exactly as :meth:`execute`: the call is
        streamed and its chunks are yielded as ``on_chat_model_stream``
        events (``data["chunk"]``; all chunks of one call share
        ``data["run_id"]``).  Each tool call yields ``on_tool_start`` and
        then ``on_tool_end`` (``data["status"]`` is ``"error"`` when the
        tool reported an error) or ``on_tool_error`` (the tool raised);
        tool calls run in parallel, as in :meth:`execute`.  The last event
        is ``on_node_end`` with the :class:`NodeResult` under
        ``data["result"]``.

        Nodes with ``output_schema`` call the model without streaming
        (there are no text tokens to stream).  A subclass that overrides
        :meth:`execute` but not this method streams through
        :meth:`BaseNode.stream`, so its own logic is never bypassed.
        """
        if type(self).execute is not PromptNode.execute:
            async for event in super().stream(state, config):
                yield event
            return

        yield NodeEvent(event="on_node_start", node_name=self.name)
        result = NodeResult(node_name=self.name, node_type="prompt", iteration=state.iteration)
        async for event in self._pipeline(state, config, result, stream=True):
            yield event
        yield NodeEvent(event="on_node_end", node_name=self.name, data={"result": result})

    async def _pipeline(
        self,
        state: GraphState,
        config: dict[str, Any],
        result: NodeResult,
        *,
        stream: bool,
    ) -> AsyncIterator[NodeEvent]:
        """The node pipeline behind :meth:`execute` and :meth:`stream`.

        Fills *result* in place.  Yields tool events always, and model
        chunks when *stream* is true (the model is then called with
        ``astream()`` instead of ``ainvoke()``).
        """
        start = time.perf_counter()

        # Resolve model — per-node override takes priority
        if self.model_override is not None:
            model = self.model_override
            # A string ("azure:chat-prod") or a Model resolves like build_agent(model=...)
            if isinstance(model, str) or type(model).__name__ == "Model":
                try:
                    from promptise.models import Model, resolve_model

                    model = model.resolve() if isinstance(model, Model) else resolve_model(model)
                except Exception as exc:
                    # Name the model by its spec, never by the object: a Model
                    # carries credentials and this string lands in logs, the
                    # graph history and the on_node_error event payload.
                    label = model.spec if isinstance(model, Model) else repr(model)
                    record_failure(result, exc, f"Failed to initialize model {label}: {exc}")
                    return
        else:
            model = config.get("_engine_model")
        if model is None:
            result.error = "No model available in config"
            return

        # ── 0. Run preprocessor ──
        if self.preprocessor:
            try:
                preprocess_result = self.preprocessor(state, config)
                if asyncio.iscoroutine(preprocess_result):
                    await preprocess_result
            except Exception as exc:
                logger.warning("Preprocessor failed in node %r: %s", self.name, exc)

        # ── 1. Assemble system prompt from blocks ──
        system_parts: list[str] = []
        if self.instructions:
            system_parts.append(self.instructions)

        # Auto-inject tool schemas into the prompt so the LLM knows exact
        # parameter names, types, and descriptions — without this, the LLM
        # has to guess from tool names alone, which causes wrong parameters.
        # This is a major tool-accuracy improvement.
        _has_tools = bool(self.tools or self.inject_tools)
        candidate_tools = self._candidate_tools(config) if _has_tools else []
        offered_tools = self._offered_tools(candidate_tools, state, config)
        if _has_tools:
            if offered_tools:
                schema_lines = ["Available tools:"]
                for t in offered_tools:
                    desc = getattr(t, "description", "") or ""
                    # Extract parameter info from schema
                    params = ""
                    args_schema = getattr(t, "args_schema", None)
                    if args_schema:
                        try:
                            schema = (
                                args_schema.model_json_schema()
                                if hasattr(args_schema, "model_json_schema")
                                else {}
                            )
                            props = schema.get("properties", {})
                            required = set(schema.get("required", []))
                            parts = []
                            for pname, pinfo in props.items():
                                ptype = pinfo.get("type", "any")
                                req = "*" if pname in required else ""
                                default = pinfo.get("default")
                                d = f"={default}" if default is not None else ""
                                parts.append(f"{pname}: {ptype}{d}{req}")
                            if parts:
                                params = f"({', '.join(parts)})"
                        except Exception as _schema_exc:
                            logger.debug(
                                "Schema extraction failed for tool %s: %s", t.name, _schema_exc
                            )
                            params = "(...)"  # Show generic hint
                    schema_lines.append(f"  - {t.name}{params}: {desc[:120]}")
                system_parts.append("\n".join(schema_lines))

        # Inject input_keys from state.context
        if self.input_keys:
            input_parts = []
            for key in self.input_keys:
                val = state.context.get(key)
                if val is not None:
                    input_parts.append(f"{key}: {val}")
            if input_parts:
                system_parts.append("Input data:\n" + "\n".join(input_parts))

        # Inherit output from a previous node
        if self.inherit_context_from:
            prev_output = state.context.get(f"{self.inherit_context_from}_output")
            if prev_output is not None:
                system_parts.append(f"Output from '{self.inherit_context_from}':\n{prev_output}")

        # Inject custom context layers
        for layer_name, _priority in self.context_layers.items():
            layer_val = state.context.get(layer_name)
            if layer_val is not None:
                system_parts.append(f"{layer_name}:\n{layer_val}")

        for block in self.blocks:
            try:
                rendered = block.render(None)
                if rendered:
                    system_parts.append(rendered)
                    result.blocks_used.append(getattr(block, "name", type(block).__name__))
            except Exception as exc:
                logger.debug("Block render failed in %s: %s", self.name, exc)
                result.blocks_dropped.append(getattr(block, "name", type(block).__name__))

        # Inject observations from state (controlled by include_observations flag)
        # Skip injection if this node has tools — the tool results are already
        # in the conversation as ToolMessages and injecting them again into the
        # system prompt causes duplication, token waste, and confuses the LLM.
        if self.include_observations and state.observations and not _has_tools:
            obs_lines = []
            for obs in state.observations[-5:]:
                tool_name = obs.get("tool", "unknown")
                tool_result = str(obs.get("result", ""))
                obs_lines.append(f"[{tool_name}] → {tool_result}")
            if obs_lines:
                system_parts.append("Recent tool results:\n" + "\n".join(obs_lines))

        # Inject plan from state (controlled by include_plan flag)
        if self.include_plan and state.plan:
            plan_lines = []
            for sg in state.plan:
                status = (
                    "✓" if sg in state.completed else ("→" if sg == state.active_subgoal else " ")
                )
                plan_lines.append(f"[{status}] {sg}")
            system_parts.append("Plan:\n" + "\n".join(plan_lines))

        # Inject reflections from state (controlled by include_reflections flag)
        if self.include_reflections and state.reflections:
            ref_lines = [
                f"Iter {r['iteration']}: {r['mistake']} → {r['correction']}"
                for r in state.reflections[-3:]
            ]
            system_parts.append("Past learnings:\n" + "\n".join(ref_lines))

        # Tell the LLM which routes it can pick — only when there is a genuine
        # CHOICE (two or more transition keys). A node with a single linear
        # default_next has nothing to route, and "choose the next step" is
        # noise a weaker model can latch onto and emit as its answer. The
        # "error" key is the engine's recovery route for an exhausted node,
        # never a choice. Tool nodes route automatically on tool_calls.
        route_choices = [k for k in self.transitions if k != "error"]
        if len(route_choices) > 1 and not candidate_tools:
            options = ", ".join(
                f"{k} (finish)"
                if self.transitions[k] == "__end__"
                else f"{k} (→ {self.transitions[k]})"
                for k in route_choices
            )
            system_parts.append(
                f"Available next steps: {options}\n"
                "Set the 'route' field in your response to one of: "
                + ", ".join(route_choices)
                + "."
            )

        # Apply strategy wrapping
        if self.strategy and hasattr(self.strategy, "wrap"):
            full_prompt = "\n\n".join(system_parts)
            wrapped = self.strategy.wrap(full_prompt, None)
            if wrapped is not None:
                system_parts = [wrapped]
                result.strategy_applied = type(self.strategy).__name__
            else:
                logger.warning(
                    "Strategy %r returned None — using unwrapped prompt",
                    type(self.strategy).__name__,
                )

        # Apply perspective: the Perspective protocol's apply(prompt, ctx)
        # (analyst, critic, advisor, creative, perspective(...)), or an
        # object with a ``framing`` string.
        if self.perspective is not None:
            if hasattr(self.perspective, "apply"):
                framed = self.perspective.apply("\n\n".join(system_parts), None)
                if framed is not None:
                    system_parts = [framed]
                    result.perspective_applied = type(self.perspective).__name__
            elif hasattr(self.perspective, "framing"):
                system_parts.insert(0, self.perspective.framing)
                result.perspective_applied = type(self.perspective).__name__

        # Build messages with this node's system prompt. The prompt is
        # rebuilt on every execution: on re-entry (a tool loop, or a loop back
        # through the graph) its input_keys, plan, reflections, observations
        # and offered tools may have changed.
        system_text = "\n\n".join(system_parts)
        messages = list(state.messages)
        if messages and _is_agent_prompt(messages[0]):
            # One system message: the agent's prompt, then this node's.
            agent_prompt = str(messages[0].content)
            system_text = f"{agent_prompt}\n\n{system_text}" if system_text else agent_prompt
            node_sys_msg = SystemMessage(content=system_text)
            _place_node_prompt(messages, node_sys_msg)
        else:
            node_sys_msg = SystemMessage(content=system_text)
            if messages and isinstance(messages[0], SystemMessage):
                messages.insert(1, node_sys_msg)
            else:
                messages.insert(0, node_sys_msg)

        # ── 2. Bind the offered tools / structured output ──
        # Binding is the costly part of a re-entry, so it is reused while the
        # model and the offered tool set are unchanged (a LIGHTWEIGHT swap
        # changes the model; the tool selector can change the tools).
        active_tools = offered_tools
        model_to_use, structured_raw = self._bind_model(model, active_tools, config)

        # ── 2b. Context scoping ──
        # When the node is context-scoped, replace the full transcript with a
        # bounded working set: this node's system prompt (already carrying any
        # inherited/​injected distilled state), the original task, and this
        # node's own in-progress tool loop. The verbose intermediate messages
        # produced by *other* stages are dropped, so token usage does not grow
        # super-linearly across a multi-stage reasoning graph. ``"auto"`` resolves
        # to "full" (short loops) or "ledger" (deep loops) based on the state.
        _scope = self._effective_context_scope(state, config)
        if _scope in ("scoped", "ledger"):
            # Compaction (see promptise.engine.compaction): pinned input system
            # messages, a note on earlier conversation, the CURRENT question,
            # then for "ledger" the latest tool exchange verbatim plus a
            # deduplicated ledger of older results (long ones cut to an
            # excerpt); for "scoped" this node's own trailing tool loop.
            from .compaction import build_compacted_view

            messages = build_compacted_view(
                state.messages,
                question=state.turn_question,
                head=state.turn_input,
                settings=self._compaction_settings(config),
                scoped=_scope == "scoped",
            )
            if _place_node_prompt(messages, node_sys_msg):
                pass
            elif messages and isinstance(messages[0], SystemMessage):
                messages.insert(1, node_sys_msg)
            else:
                messages.insert(0, node_sys_msg)

        # ── 3. Call LLM ──
        llm_start = time.perf_counter()
        try:
            if stream and self.output_schema is None:
                # Stream the one model call of this step; the aggregated
                # chunks are the same AIMessage ainvoke() would return.
                llm_run_id = str(uuid4())
                aggregate: Any = None
                async for chunk in model_to_use.astream(messages, config=config):
                    if getattr(chunk, "content", None):
                        yield NodeEvent(
                            event="on_chat_model_stream",
                            node_name=self.name,
                            data={"chunk": chunk, "run_id": llm_run_id},
                        )
                    aggregate = chunk if aggregate is None else aggregate + chunk
                if isinstance(aggregate, AIMessageChunk):
                    response = message_chunk_to_message(aggregate)
                else:
                    response = aggregate if aggregate is not None else AIMessage(content="")
            else:
                response = await model_to_use.ainvoke(messages, config=config)
            result.llm_duration_ms = (time.perf_counter() - llm_start) * 1000
        except Exception as exc:
            # Recorded on the result for hooks/history; the engine raises
            # GraphExecutionError (chained to this exception) if the run
            # ends here, so a provider failure never becomes a silent "answer".
            record_failure(result, exc)
            result.duration_ms = (time.perf_counter() - start) * 1000
            return

        # ── 4. Process response ──
        # A message, or (with an output_schema) a dict / Pydantic model / the
        # include_raw envelope.
        response = cast(Any, response)
        if structured_raw and _is_raw_structured(response):
            # with_structured_output(include_raw=True): keep the raw message's
            # token usage, then continue with the parsed value.
            prompt_tokens, completion_tokens = token_usage(response["raw"])
            result.prompt_tokens = prompt_tokens
            result.completion_tokens = completion_tokens
            result.total_tokens = prompt_tokens + completion_tokens
            if response.get("parsed") is None and response.get("parsing_error") is not None:
                parsing_error = response["parsing_error"]
                parse_exc = (
                    parsing_error
                    if isinstance(parsing_error, BaseException)
                    else ValueError(str(parsing_error))
                )
                record_failure(result, parse_exc)
                result.duration_ms = (time.perf_counter() - start) * 1000
                return
            response = response["parsed"]

        if isinstance(response, AIMessage):
            # AIMessage.content is str | list[...] in LangChain's typing; coerce to str.
            _content = response.content
            result.raw_output = _content if isinstance(_content, str) else str(_content or "")
            result.messages_added.append(response)
            state.messages.append(response)

            # Token usage (LangChain's usage_metadata is a dict)
            result.prompt_tokens, result.completion_tokens = token_usage(response)
            result.total_tokens = result.prompt_tokens + result.completion_tokens

            # Handle tool calls
            tool_calls = getattr(response, "tool_calls", None) or []
            if tool_calls:
                # Execute against every candidate, not just the offered ones:
                # a tool the selector left out this step is still callable.
                tool_map = {t.name: t for t in (candidate_tools or active_tools)}
                tool_start = time.perf_counter()
                # A repeated call runs the tool again: its result may have
                # changed (a job's status, a health check).  Only tools opted
                # in with ContextCompaction(reuse_results=...) are answered
                # from an earlier result, and that is reported, never silent.
                earlier = self._earlier_results(tool_calls, state, config)
                call_ids = [tc.get("id") or str(uuid4()) for tc in tool_calls]

                async def _timed_tool(index: int) -> tuple[dict[str, Any], str, float]:
                    """Run one tool call; never raises. Returns (record, content, ms).

                    ``earlier[index]`` is the earlier result that answers the
                    call instead of the tool, or ``None`` to run the tool.
                    """
                    tc = tool_calls[index]
                    tool_name = tc.get("name", "")
                    tool_args = tc.get("args", {})
                    reuse = earlier[index]
                    if reuse is None:
                        return await _run_tool_call(
                            tool_map.get(tool_name),
                            tool_name,
                            tool_args,
                            call_ids[index],
                            state,
                            config,
                        )
                    started = time.perf_counter()
                    await _report_reused_call(
                        tool_map.get(tool_name),
                        tool_name,
                        tool_args,
                        reuse,
                        call_ids[index],
                        state,
                        config,
                    )
                    tc_record = {
                        "name": tool_name,
                        "args": tool_args,
                        "result": reuse,
                        "success": True,
                        "reused": True,
                    }
                    return tc_record, reuse, (time.perf_counter() - started) * 1000

                tool_run_ids = [str(uuid4()) for _ in tool_calls]
                for index, tc in enumerate(tool_calls):
                    yield _tool_start_event(
                        tc.get("name", ""),
                        tc.get("args", {}),
                        tool_run_ids[index],
                        reused=earlier[index] is not None,
                    )

                # Execute tools — parallel if 2+ calls (each end event is
                # yielded as soon as that tool finishes), direct if only 1.
                outcomes: list[tuple[dict[str, Any], str, float] | None] = [None] * len(tool_calls)
                if len(tool_calls) == 1:
                    outcome = await _timed_tool(0)
                    outcomes[0] = outcome
                    yield _tool_end_event(*outcome, tool_run_ids[0])
                else:
                    tasks = {
                        asyncio.ensure_future(_timed_tool(index)): index
                        for index in range(len(tool_calls))
                    }
                    try:
                        pending = set(tasks)
                        while pending:
                            done, pending = await asyncio.wait(
                                pending, return_when=asyncio.FIRST_COMPLETED
                            )
                            for task in sorted(done, key=tasks.__getitem__):
                                index = tasks[task]
                                outcome = task.result()
                                outcomes[index] = outcome
                                yield _tool_end_event(*outcome, tool_run_ids[index])
                    finally:
                        # A consumer that stops reading must not leave tools running.
                        for task in tasks:
                            task.cancel()

                # Append results in original order (preserves tool_call_id alignment)
                for i, maybe_outcome in enumerate(outcomes):
                    assert maybe_outcome is not None
                    tc_record, content, _ms = maybe_outcome
                    tc_record.pop("raised", None)

                    if not tc_record.get("success", True):
                        result.tool_calls_failed += 1

                    tool_msg = _tool_message(tc_record, content, call_ids[i])
                    state.messages.append(tool_msg)
                    result.messages_added.append(tool_msg)
                    result.tool_calls.append(tc_record)

                    state.add_observation(
                        tool_name=tc_record["name"],
                        result=content,
                        args=tc_record.get("args", {}),
                        success=tc_record.get("success", False),
                    )

                result.tool_duration_ms = (time.perf_counter() - tool_start) * 1000

                # Signal that tools were called — engine will re-enter this node
                result.transition_reason = "tool_calls_present"

        elif isinstance(response, dict):
            # Structured output (from with_structured_output)
            result.output = response
            result.raw_output = json.dumps(response, default=str)
        elif isinstance(response, PydanticBaseModel):
            # A Pydantic output_schema: stored as a plain dict, like a
            # TypedDict/JSON-schema output, so transitions, edge conditions,
            # input_keys and the reasoning nodes' routing read it the same way.
            result.output = response.model_dump()
            result.raw_output = response.model_dump_json()
        else:
            # Pydantic model or other
            result.output = response
            result.raw_output = str(response)

        # ── 5. Parse structured output for transition ──
        if result.output and isinstance(result.output, dict):
            # Check for transition keys in the output
            for key, target in self.transitions.items():
                if key in result.output:
                    val = result.output[key]
                    if isinstance(val, bool) and val:
                        result.next_node = target
                        result.transition_reason = f"output.{key}={val}"
                        break
                    elif isinstance(val, str) and val == key:
                        result.next_node = target
                        result.transition_reason = f"output.{key}={val!r}"
                        break

        # ── 6. Run guards ──
        for guard in self.guards:
            try:
                if hasattr(guard, "check_output"):
                    check = guard.check_output(result.raw_output)
                    if asyncio.iscoroutine(check):
                        await check
                result.guards_passed.append(type(guard).__name__)
            except Exception as exc:
                result.guards_failed.append(f"{type(guard).__name__}: {exc}")

        # ── 7. Strategy parsing ──
        if self.strategy and hasattr(self.strategy, "parse") and result.raw_output:
            try:
                parsed = self.strategy.parse(result.raw_output, None)
                if parsed != result.raw_output:
                    result.output = parsed
            except Exception:
                pass

        # ── 8. Run postprocessor ──
        if self.postprocessor:
            try:
                post_result = self.postprocessor(result.output, state, config)
                if asyncio.iscoroutine(post_result):
                    post_result = await post_result
                if post_result is not None:
                    result.output = post_result
            except Exception as exc:
                logger.warning("Postprocessor failed in node %r: %s", self.name, exc)

        # ── 9. Write output to state.context ──
        if self.output_key and result.output is not None:
            state.context[self.output_key] = result.output
        # Always write to {name}_output for context inheritance
        if result.raw_output:
            state.context[f"{self.name}_output"] = result.raw_output
        elif result.output is not None:
            state.context[f"{self.name}_output"] = result.output

        result.duration_ms = (time.perf_counter() - start) * 1000

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> PromptNode:
        """Create a PromptNode from a configuration dict.

        Handles all PromptNode parameters including data flow,
        preprocessing, and context control.
        """
        return cls(
            name=config.get("name", "unnamed"),
            instructions=config.get("instructions", ""),
            description=config.get("description", ""),
            blocks=config.get("blocks"),
            strategy=config.get("strategy"),
            perspective=config.get("perspective"),
            tools=config.get("tools"),
            tool_choice=config.get("tool_choice", "auto"),
            inject_tools=config.get("inject_tools", False),
            model_override=config.get("model_override"),
            output_schema=config.get("output_schema"),
            guards=config.get("guards"),
            context_layers=config.get("context_layers"),
            max_tokens=config.get("max_tokens", 4096),
            temperature=config.get("temperature", 0.0),
            input_keys=config.get("input_keys"),
            output_key=config.get("output_key"),
            inherit_context_from=config.get("inherit_context_from"),
            preprocessor=config.get("preprocessor"),
            postprocessor=config.get("postprocessor"),
            include_observations=config.get("include_observations", True),
            include_plan=config.get("include_plan", True),
            include_reflections=config.get("include_reflections", True),
            transitions=config.get("transitions"),
            default_next=config.get("default_next"),
            max_iterations=config.get("max_iterations", 10),
            metadata=config.get("metadata"),
        )


# ---------------------------------------------------------------------------
# ToolNode — explicit tool execution
# ---------------------------------------------------------------------------


def _call_fingerprint(tool_name: str, args: dict[str, Any]) -> str:
    """A key equal for calls with the same tool and arguments."""
    return f"{tool_name}::{json.dumps(args, sort_keys=True, default=str)}"


class ToolNode(BaseNode):
    """Execute a specific tool or let the engine pick from available tools.

    Unlike PromptNode (where tools are called by the LLM), ToolNode
    executes tools directly based on state context.  Useful when tool
    selection is deterministic rather than LLM-driven.

    A call runs as it would in a :class:`PromptNode`: the engine hooks'
    ``pre_tool`` (may change the arguments, or block the call by raising)
    and ``post_tool`` (may change the result) run around it, the tool is
    invoked as a tool call (an MCP tool error reaches the model as the
    server's error envelope, flagged ``status="error"``), and
    :meth:`stream` yields ``on_tool_start`` / ``on_tool_end`` (or
    ``on_tool_error``) for each call, so ``astream_events()`` and
    ``astream_with_tools()`` show it.  The node runs one turn's calls one
    after another, in order.

    Args:
        tools: Available tools for this node.
        validate_inputs: Check each call's arguments against the tool's
            parameter schema before calling it (after ``pre_tool`` hooks).
            Invalid arguments do not reach the tool: the call's
            ``ToolMessage`` is an error (``status="error"``) listing each
            problem by argument path (``promptise.tools.ToolArgumentError``'s
            message) so the model can correct the call, and callbacks get
            ``on_tool_error``.  As for MCP tools, an explicit ``null`` for an
            optional parameter that cannot be null counts as not given.  An
            MCP tool checks its own arguments the same way, so the node
            leaves the check to it.  With ``False``, the arguments go to the
            tool as they are.
        deduplicate: Run identical (tool, args) calls of one model turn
            once.  Each repeat in that turn still gets its own
            ``ToolMessage`` with the first call's result, and is reported
            as reused (``trace_tools``, ``observer``, callbacks, engine
            hooks' ``on_tool_reused`` instead of ``pre_tool`` /
            ``post_tool``, ``"reused": True`` in ``NodeResult.tool_calls``
            and stream events).  A call in a later model turn, a later pass
            of a ``tool_selector``, or another run runs the tool again, so a
            status check gets the current value.  A failed call is never
            reused.  The record lives in the run, never on the node, so
            sequential and concurrent runs of a graph do not affect each
            other.
        max_result_chars: Cap the size of a successful tool result.
        tool_selector: Optional callable that selects which tool
            to call based on state.  If ``None``, runs the tool calls
            of the most recent AI message that has tool calls.
    """

    def __init__(
        self,
        name: str,
        *,
        tools: list[BaseTool] | None = None,
        validate_inputs: bool = True,
        deduplicate: bool = True,
        max_result_chars: int = 4000,
        tool_selector: Callable[[GraphState], tuple[str, dict]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.tools = list(tools) if tools else []
        self.validate_inputs = validate_inputs
        self.deduplicate = deduplicate
        self.max_result_chars = max_result_chars
        self.tool_selector = tool_selector

    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        """Execute tools from state or selector."""
        result = NodeResult(node_name=self.name, node_type="tool", iteration=state.iteration)
        async for _event in self._pipeline(state, config, result):
            pass
        return result

    async def stream(self, state: GraphState, config: dict[str, Any]) -> AsyncIterator[NodeEvent]:
        """Run the :meth:`execute` pipeline, yielding each call's events.

        Each call yields ``on_tool_start`` and then ``on_tool_end``
        (``data["status"]`` is ``"error"`` when the tool reported an error,
        ``data["reused"]`` is ``True`` for a repeat answered from the turn's
        earlier result) or ``on_tool_error`` (the tool or a hook raised),
        with the call's ``run_id`` and ``duration_ms`` -- the events a
        :class:`PromptNode` yields for its tool calls.  The last event is
        ``on_node_end`` with the :class:`NodeResult` under
        ``data["result"]``.  A subclass that overrides :meth:`execute` but
        not this method streams through :meth:`BaseNode.stream`.
        """
        if type(self).execute is not ToolNode.execute:
            async for event in super().stream(state, config):
                yield event
            return

        yield NodeEvent(event="on_node_start", node_name=self.name)
        result = NodeResult(node_name=self.name, node_type="tool", iteration=state.iteration)
        async for event in self._pipeline(state, config, result):
            yield event
        yield NodeEvent(event="on_node_end", node_name=self.name, data={"result": result})

    def _calls(self, state: GraphState) -> list[tuple[str, dict[str, Any], str]]:
        """``(tool_name, args, tool_call_id)`` of each call to run."""
        if self.tool_selector:
            tool_name, tool_args = self.tool_selector(state)
            return [(tool_name, tool_args, str(uuid4()))]
        # The tool calls of the last AI message that has some
        for msg in reversed(state.messages):
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                return [
                    (tc["name"], tc.get("args", {}) or {}, tc.get("id") or str(uuid4()))
                    for tc in msg.tool_calls
                ]
        return []

    async def _pipeline(
        self, state: GraphState, config: dict[str, Any], result: NodeResult
    ) -> AsyncIterator[NodeEvent]:
        """The node behind :meth:`execute` and :meth:`stream`.

        Fills *result* in place and yields each call's tool events.
        """
        start = time.perf_counter()
        tool_map = {t.name: t for t in self.tools}
        calls = self._calls(state)

        # Deduplication is per model turn of this run.  Nodes are shared by
        # every run of the graph (sequential and concurrent), so nothing about
        # a run is kept on the node: the run's own messages say which calls
        # already have a result, and this turn's results are kept here.
        answered = (
            {m.tool_call_id for m in state.messages if isinstance(m, ToolMessage)}
            if self.deduplicate
            else set()
        )
        turn_results: dict[str, str] = {}  # (tool, args) -> first successful result

        tool_start = time.perf_counter()
        for tool_name, tool_args, tool_id in calls:
            if tool_id in answered:
                # The graph reached this node again before the model's next
                # turn: the call already ran in this run and has its result.
                # Running it again would repeat its effects and give the
                # tool_call_id a second result.
                logger.debug(
                    "ToolNode %r: call %s to %r already has a result in this run; not run again",
                    self.name,
                    tool_id,
                    tool_name,
                )
                result.tool_calls_deduplicated += 1
                continue

            fingerprint = _call_fingerprint(tool_name, tool_args) if self.deduplicate else ""
            earlier = turn_results.get(fingerprint) if self.deduplicate else None
            run_id = str(uuid4())
            yield _tool_start_event(tool_name, tool_args, run_id, reused=earlier is not None)

            tc_record: dict[str, Any]
            if earlier is not None:
                # The same call earlier in this turn: answer it with that
                # result, so its tool_call_id has one, and report it as reused
                # (on_tool_reused instead of pre_tool / post_tool).
                started = time.perf_counter()
                await _report_reused_call(
                    tool_map.get(tool_name), tool_name, tool_args, earlier, tool_id, state, config
                )
                content = earlier
                tc_record = {
                    "name": tool_name,
                    "args": tool_args,
                    "result": content,
                    "success": True,
                    "reused": True,
                }
                ms = (time.perf_counter() - started) * 1000
                result.tool_calls_deduplicated += 1
            else:
                tc_record, content, ms = await _run_tool_call(
                    tool_map.get(tool_name),
                    tool_name,
                    tool_args,
                    tool_id,
                    state,
                    config,
                    validate=self.validate_inputs,
                    max_result_chars=self.max_result_chars,
                )
                if self.deduplicate and tc_record["success"]:
                    turn_results.setdefault(fingerprint, content)

            end_event = _tool_end_event(tc_record, content, ms, run_id)
            tc_record.pop("raised", None)
            if not tc_record.get("success", True):
                result.tool_calls_failed += 1

            tool_msg = _tool_message(tc_record, content, tool_id)
            state.messages.append(tool_msg)
            result.messages_added.append(tool_msg)
            result.tool_calls.append(tc_record)

            state.add_observation(
                tool_name=tool_name,
                result=content[:500],
                args=tc_record.get("args", {}),
                success=tc_record.get("success", False),
            )
            yield end_event

        if calls:
            result.tool_duration_ms = (time.perf_counter() - tool_start) * 1000
        result.duration_ms = (time.perf_counter() - start) * 1000


# ---------------------------------------------------------------------------
# RouterNode — LLM-based routing
# ---------------------------------------------------------------------------


class RouterNode(BaseNode):
    """Lightweight LLM call that decides which path to take.

    No tool calling.  The LLM sees the current state (via context
    blocks) and picks from a list of named routes.

    Args:
        routes: Mapping of route names to next-node names.
        context_blocks: Blocks to render for the LLM's context.
        model_override: Optional different model for routing (e.g.,
            a smaller/faster model for routing decisions).
    """

    def __init__(
        self,
        name: str,
        *,
        routes: dict[str, str] | None = None,
        context_blocks: list[Any] | None = None,
        model_override: BaseChatModel | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.routes = dict(routes) if routes else {}
        self.context_blocks = list(context_blocks) if context_blocks else []
        self.model_override = model_override

    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        """Ask the LLM to choose a route."""
        start = time.perf_counter()
        result = NodeResult(node_name=self.name, node_type="router", iteration=state.iteration)

        model = self.model_override or config.get("_engine_model")
        if model is None:
            result.error = "No model available"
            return result

        # Build routing prompt
        route_names = list(self.routes.keys())
        context_parts = [self.instructions] if self.instructions else []
        for block in self.context_blocks:
            try:
                rendered = block.render(None)
                if rendered:
                    context_parts.append(rendered)
            except Exception:
                pass

        context_parts.append(
            f"Choose ONE of these routes: {route_names}\n"
            f"Respond with ONLY the route name, nothing else."
        )

        messages = list(state.messages)
        messages.append(SystemMessage(content="\n\n".join(context_parts)))

        try:
            response = await model.ainvoke(messages, config=cast("RunnableConfig | None", config))
            result.llm_duration_ms = (time.perf_counter() - start) * 1000

            _resp_content = getattr(response, "content", None)
            _resp_str = _resp_content if isinstance(_resp_content, str) else str(response)
            raw = (
                _resp_str.strip().lower()
                if hasattr(response, "content")
                else str(response).strip().lower()
            )
            result.raw_output = raw

            # Find matching route
            for route_name in route_names:
                if route_name.lower() in raw:
                    result.next_node = self.routes[route_name]
                    result.transition_reason = f"LLM chose route '{route_name}'"
                    break

            if result.next_node is None:
                if self.default_next:
                    result.next_node = self.default_next
                    result.transition_reason = "No route matched, using default"
                else:
                    result.error = (
                        f"No route matched from {list(self.routes.keys())} "
                        f"(LLM output: {raw[:100]!r}) and no default_next configured"
                    )

        except Exception as exc:
            record_failure(result, exc)

        result.duration_ms = (time.perf_counter() - start) * 1000
        return result


# ---------------------------------------------------------------------------
# GuardNode — validate and gate
# ---------------------------------------------------------------------------


class GuardNode(BaseNode):
    """Validate state against guards and route based on pass/fail.

    No LLM call.  Runs guards against the current state and routes
    to ``on_pass`` or ``on_fail`` nodes.

    Args:
        guards: List of guard instances to run.
        target_key: Key in ``state.context`` to validate.  If ``None``,
            validates the last message content.
        on_pass: Node name if all guards pass.
        on_fail: Node name if any guard fails.
    """

    def __init__(
        self,
        name: str,
        *,
        guards: list[Any] | None = None,
        target_key: str | None = None,
        on_pass: str = "__end__",
        on_fail: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.guards = list(guards) if guards else []
        self.target_key = target_key
        self.on_pass = on_pass
        self.on_fail = on_fail

    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        """Run guards and route accordingly."""
        start = time.perf_counter()
        result = NodeResult(node_name=self.name, node_type="guard", iteration=state.iteration)

        # Get the value to validate
        if self.target_key:
            target = state.context.get(self.target_key, "")
        else:
            # Last message content
            target = ""
            for msg in reversed(state.messages):
                if hasattr(msg, "content") and msg.content:
                    target = msg.content
                    break

        all_passed = True
        for guard in self.guards:
            try:
                if hasattr(guard, "check_output"):
                    await guard.check_output(target)
                elif callable(guard):
                    guard_result = guard(target)
                    if asyncio.iscoroutine(guard_result):
                        guard_result = await guard_result
                    if not guard_result:
                        raise ValueError("Guard returned False")
                result.guards_passed.append(
                    type(guard).__name__ if not callable(guard) else str(guard)
                )
            except Exception as exc:
                all_passed = False
                result.guards_failed.append(f"{type(guard).__name__}: {exc}")

        if all_passed:
            result.next_node = self.on_pass
            result.transition_reason = f"All {len(self.guards)} guards passed"
        else:
            result.next_node = self.on_fail or self.default_next
            result.transition_reason = f"{len(result.guards_failed)} guards failed"

        result.duration_ms = (time.perf_counter() - start) * 1000
        return result


# ---------------------------------------------------------------------------
# ParallelNode — concurrent execution
# ---------------------------------------------------------------------------


class ParallelNode(BaseNode):
    """Run multiple child nodes concurrently and merge results.

    Args:
        nodes: Child nodes to execute in parallel.
        merge_strategy: How to merge results.
            ``"concatenate"`` — append all observations.
            ``"dict"`` — return ``{node_name: output}`` dict.
            ``"custom"`` — use ``merge_fn``.
        merge_fn: Custom merge function (when merge_strategy is
            ``"custom"``).
    """

    def __init__(
        self,
        name: str,
        *,
        nodes: list[BaseNode] | None = None,
        merge_strategy: str = "concatenate",
        merge_fn: Callable | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.child_nodes = list(nodes) if nodes else []
        self.merge_strategy = merge_strategy
        self.merge_fn = merge_fn

    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        """Execute all child nodes concurrently."""
        start = time.perf_counter()
        result = NodeResult(node_name=self.name, node_type="parallel", iteration=state.iteration)

        async def run_child(child_node: BaseNode) -> NodeResult:
            # Each child gets a shallow copy of state context
            child_state = GraphState(
                messages=list(state.messages),
                context=dict(state.context),
                current_node=child_node.name,
                observations=list(state.observations),
            )
            return await child_node.execute(child_state, config)

        child_results = await asyncio.gather(
            *[run_child(n) for n in self.child_nodes],
            return_exceptions=True,
        )

        # Merge results
        outputs: dict[str, Any] = {}
        errors: list[str] = []
        for child, child_result in zip(self.child_nodes, child_results, strict=False):
            if isinstance(child_result, BaseException):
                errors.append(f"{child.name}: {child_result}")
                continue

            outputs[child.name] = child_result.output
            result.tool_calls.extend(child_result.tool_calls)
            result.total_tokens += child_result.total_tokens

            # Merge observations into parent state
            for tc in child_result.tool_calls:
                if tc.get("success"):
                    state.add_observation(
                        tool_name=tc.get("name", ""),
                        result=str(tc.get("result", "")),
                        args=tc.get("args", {}),
                        success=True,
                    )

        if errors:
            result.error = "; ".join(errors)

        if self.merge_strategy == "dict":
            result.output = outputs
        elif self.merge_strategy == "custom" and self.merge_fn:
            result.output = self.merge_fn(outputs)
        else:
            result.output = outputs

        result.duration_ms = (time.perf_counter() - start) * 1000
        return result


# ---------------------------------------------------------------------------
# LoopNode — repeat a subgraph
# ---------------------------------------------------------------------------


class LoopNode(BaseNode):
    """Repeat a subgraph until a condition is met.

    Args:
        body_node: The node to repeat (can be a SubgraphNode).
        condition: Callable that receives state and returns ``True``
            when the loop should stop.
        max_loop_iterations: Maximum iterations before forcing exit.
    """

    def __init__(
        self,
        name: str,
        *,
        body_node: BaseNode | None = None,
        condition: Callable[[GraphState], bool] | None = None,
        max_loop_iterations: int = 5,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.body_node = body_node
        self.condition = condition
        self.max_loop_iterations = max_loop_iterations

    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        """Execute body node in a loop until condition met."""
        start = time.perf_counter()
        result = NodeResult(node_name=self.name, node_type="loop", iteration=state.iteration)

        if self.body_node is None:
            result.error = "No body_node configured"
            result.duration_ms = (time.perf_counter() - start) * 1000
            return result

        for i in range(self.max_loop_iterations):
            # Check exit condition
            if self.condition and self.condition(state):
                result.transition_reason = f"Condition met after {i} iterations"
                break

            child_result = await self.body_node.execute(state, config)
            result.tool_calls.extend(child_result.tool_calls)
            result.total_tokens += child_result.total_tokens

            if child_result.error:
                result.error = f"Loop iteration {i}: {child_result.error}"
                child_cause = failure_cause(child_result)
                if child_cause is not None:
                    setattr(result, _FAILURE_CAUSE_ATTR, child_cause)
                break
        else:
            result.transition_reason = f"Max loop iterations ({self.max_loop_iterations}) reached"

        result.output = state.context.get("loop_result")
        result.duration_ms = (time.perf_counter() - start) * 1000
        return result


# ---------------------------------------------------------------------------
# HumanNode — pause for human input
# ---------------------------------------------------------------------------


class HumanNode(BaseNode):
    """Pause execution and wait for human input.

    Integrates with the existing ``ApprovalPolicy`` system.

    Args:
        prompt_template: Template shown to the human.
        timeout: Seconds to wait before applying ``on_timeout``.
        on_approve: Next node if human approves.
        on_deny: Next node if human denies.
        on_timeout: Next node if timeout expires.
    """

    def __init__(
        self,
        name: str,
        *,
        prompt_template: str = "Approve this action?",
        timeout: float = 300.0,
        on_approve: str = "__end__",
        on_deny: str | None = None,
        on_timeout: str = "__end__",
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.prompt_template = prompt_template
        self.timeout = timeout
        self.on_approve = on_approve
        self.on_deny = on_deny
        self.on_timeout = on_timeout

    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        """Pause for human input."""
        start = time.perf_counter()
        result = NodeResult(node_name=self.name, node_type="human", iteration=state.iteration)

        # Check if there's a human handler in config
        handler = config.get("_human_handler")
        if handler is None:
            # No handler — auto-approve
            result.next_node = self.on_approve
            result.transition_reason = "No human handler configured, auto-approved"
            result.duration_ms = (time.perf_counter() - start) * 1000
            return result

        try:
            prompt = self.prompt_template.format(**state.context)
            decision = await asyncio.wait_for(handler(prompt, state), timeout=self.timeout)

            if decision:
                result.next_node = self.on_approve
                result.transition_reason = "Human approved"
            else:
                result.next_node = self.on_deny or self.default_next
                result.transition_reason = "Human denied"

        except asyncio.TimeoutError:
            result.next_node = self.on_timeout
            result.transition_reason = f"Timeout after {self.timeout}s"

        except Exception as exc:
            record_failure(result, exc)

        result.duration_ms = (time.perf_counter() - start) * 1000
        return result


# ---------------------------------------------------------------------------
# TransformNode — pure data transformation
# ---------------------------------------------------------------------------


class TransformNode(BaseNode):
    """Transform state data without calling the LLM.

    Useful for formatting, aggregation, data extraction, or
    preparing state for the next node.

    Args:
        transform: Callable that receives state and returns a value
            to store in ``state.context[output_key]``.
        output_key: Key in ``state.context`` to store the result.
    """

    def __init__(
        self,
        name: str,
        *,
        transform: Callable[[GraphState], Any] | None = None,
        output_key: str = "transform_result",
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.transform = transform
        self.output_key = output_key

    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        """Execute the transform function."""
        start = time.perf_counter()
        result = NodeResult(node_name=self.name, node_type="transform", iteration=state.iteration)

        if self.transform is None:
            result.error = "No transform function configured"
            result.duration_ms = (time.perf_counter() - start) * 1000
            return result

        try:
            output = self.transform(state)
            if asyncio.iscoroutine(output):
                output = await output
            state.context[self.output_key] = output
            result.output = output
        except Exception as exc:
            record_failure(result, exc)

        result.duration_ms = (time.perf_counter() - start) * 1000
        return result


# ---------------------------------------------------------------------------
# SubgraphNode — embed another graph
# ---------------------------------------------------------------------------


class SubgraphNode(BaseNode):
    """Embed a complete sub-graph as a single node.

    The subgraph runs to completion, then the parent graph continues.
    State can be shared (``inherit_state=True``) or isolated.

    Args:
        subgraph: A ``PromptGraph`` instance to run.
        inherit_state: If ``True``, the subgraph shares the parent's
            state (messages, context, observations).  If ``False``,
            the subgraph gets a fresh state and only its final
            messages are appended to the parent.
    """

    def __init__(
        self,
        name: str,
        *,
        subgraph: Any = None,  # PromptGraph — forward ref
        inherit_state: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.subgraph = subgraph
        self.inherit_state = inherit_state

    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        """Run the subgraph to completion."""
        start = time.perf_counter()
        result = NodeResult(node_name=self.name, node_type="subgraph", iteration=state.iteration)

        if self.subgraph is None:
            result.error = "No subgraph configured"
            result.duration_ms = (time.perf_counter() - start) * 1000
            return result

        # Import here to avoid circular imports
        from .execution import PromptGraphEngine

        model = config.get("_engine_model")
        if model is None:
            result.error = "No _engine_model in config"
            result.duration_ms = (time.perf_counter() - start) * 1000
            return result
        engine = PromptGraphEngine(
            graph=self.subgraph,
            model=model,
            max_iterations=config.get("_max_iterations", 25),
        )

        if self.inherit_state:
            sub_input = {"messages": state.messages}
        else:
            sub_input = {"messages": []}

        try:
            sub_output = await engine.ainvoke(sub_input, config=config)

            if not self.inherit_state:
                # Append subgraph's messages to parent state
                for msg in sub_output.get("messages", []):
                    state.messages.append(msg)
                    result.messages_added.append(msg)

            result.output = sub_output
            _last_report = getattr(engine, "_last_report", None)
            result.total_tokens = _last_report.total_tokens if _last_report is not None else 0

        except Exception as exc:
            record_failure(result, exc, f"Subgraph error: {type(exc).__name__}: {exc}")

        result.duration_ms = (time.perf_counter() - start) * 1000
        return result


# ---------------------------------------------------------------------------
# AutonomousNode — the agent builds its own reasoning path at runtime
# ---------------------------------------------------------------------------


class AutonomousNode(BaseNode):
    """Meta-node: the LLM receives a pool of configured nodes and
    dynamically composes its own reasoning path at runtime.

    Instead of following a pre-defined graph, the agent:

    1. Sees all available nodes as a menu of capabilities
    2. Decides which node to execute next based on the task
    3. Executes that node (with its tools, blocks, guards)
    4. Evaluates the result
    5. Decides the next node or finishes

    The developer provides the LEGO blocks.
    The agent builds the model.

    Usage::

        autonomous = AutonomousNode(
            "agent",
            node_pool=[
                web_researcher("search", tools=search_tools),
                data_analyst("analyze", tools=db_tools),
                fact_checker("verify"),
                summarizer("conclude"),
                planner("plan"),
            ],
            planner_instructions="You are a research assistant. "
                "Use the available steps to answer the user's question thoroughly.",
        )

        graph = PromptGraph("autonomous")
        graph.add_node(autonomous)
        graph.set_entry("agent")

    Args:
        node_pool: Available nodes the agent can choose from.
            Each node is fully configured with its own tools,
            blocks, guards, and instructions.
        planner_instructions: How the agent should approach
            selecting and sequencing nodes.
        allow_repeat: Whether the agent can re-use the same
            node multiple times.
        max_steps: Maximum nodes the agent can execute per run.
        entry_node: Pool node to run first, before the agent chooses.
        terminal_nodes: Pool nodes that end the run once they succeed.
        answer_node: Pool node that writes the final answer when the agent
            finishes without a terminal node having run last.  Defaults to
            the first terminal node; with no terminal node, the node asks
            the model for the final answer itself.

    A chosen step runs its whole tool loop — the node is re-run while its
    model keeps calling tools, up to the node's ``max_iterations`` — before
    the agent chooses again.  The routing exchange (the step menu and the
    model's JSON choice) is never added to the conversation, and the run
    always ends with a written answer.  A tool name given as the next step
    runs the pool node that has that tool.
    """

    #: Choices that finish the run.
    _DONE = frozenset({"__done__", "__end__", "done", "finish"})

    def __init__(
        self,
        name: str = "autonomous",
        *,
        node_pool: list[BaseNode] | None = None,
        planner_instructions: str = "",
        allow_repeat: bool = True,
        max_steps: int = 15,
        entry_node: str | None = None,
        terminal_nodes: list[str] | None = None,
        answer_node: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.node_pool: dict[str, BaseNode] = {n.name: n for n in (node_pool or [])}
        self.planner_instructions = planner_instructions
        self.allow_repeat = allow_repeat
        self.max_steps = max_steps
        self.entry_node = entry_node
        self.terminal_nodes = list(terminal_nodes) if terminal_nodes else []
        self.answer_node = answer_node

    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        """Autonomously select and execute nodes from the pool."""
        result = NodeResult(node_name=self.name, node_type="autonomous", iteration=state.iteration)
        async for _event in self._pipeline(state, config, result, stream=False):
            pass
        return result

    async def stream(self, state: GraphState, config: dict[str, Any]) -> AsyncIterator[NodeEvent]:
        """Run the :meth:`execute` pipeline, relaying the chosen steps' events
        (model chunks and tool events).  The routing calls are not streamed."""
        if type(self).execute is not AutonomousNode.execute:
            async for event in super().stream(state, config):
                yield event
            return

        yield NodeEvent(event="on_node_start", node_name=self.name)
        result = NodeResult(node_name=self.name, node_type="autonomous", iteration=state.iteration)
        async for event in self._pipeline(state, config, result, stream=True):
            yield event
        yield NodeEvent(event="on_node_end", node_name=self.name, data={"result": result})

    def _step_tools(self, node: BaseNode, config: dict[str, Any]) -> list[str]:
        """Names of the tools *node* can call in this run."""
        if isinstance(node, PromptNode):
            return [t.name for t in node._candidate_tools(config)]
        return [t.name for t in getattr(node, "tools", None) or []]

    def _catalog(self, config: dict[str, Any]) -> str:
        """The step menu shown to the routing call."""
        lines = ["Available reasoning steps:"]
        for nname, nobj in self.node_pool.items():
            desc = nobj.description or (nobj.instructions or "").strip() or "(no description)"
            if len(desc) > 200:
                desc = desc[:197] + "..."
            tool_names = self._step_tools(nobj, config)
            tools_info = f" [calls tools: {', '.join(tool_names)}]" if tool_names else ""
            terminal_mark = (
                " [TERMINAL — writes the final answer and finishes]"
                if nname in self.terminal_nodes
                else ""
            )
            lines.append(f"  - {nname}: {desc}{tools_info}{terminal_mark}")
        return "\n".join(lines)

    def _resolve_choice(self, chosen: str, config: dict[str, Any]) -> str | None:
        """The pool node to run for *chosen*: the node of that name, or the
        node that has a tool of that name (models sometimes name the tool
        they want instead of the step); ``None`` when neither exists."""
        if chosen in self.node_pool:
            return chosen
        for nname, nobj in self.node_pool.items():
            if chosen in self._step_tools(nobj, config):
                logger.info(
                    "Autonomous: %r is a tool of step %r — running that step", chosen, nname
                )
                return nname
        return None

    def _final_answer_node(self) -> BaseNode:
        """The node that writes the final answer when the agent finishes
        without a terminal node having run last."""
        name = self.answer_node or (self.terminal_nodes[0] if self.terminal_nodes else None)
        if name and name in self.node_pool:
            return self.node_pool[name]
        return PromptNode(
            f"{self.name}_answer",
            instructions=(
                "Write the final answer to the user's question from the work so far. "
                "Use only facts from the tool results; say what could not be determined."
            ),
            default_next="__end__",
        )

    async def _run_step(
        self,
        node: BaseNode,
        state: GraphState,
        config: dict[str, Any],
        result: NodeResult,
        outcome: list[NodeResult],
        *,
        stream: bool,
    ) -> AsyncIterator[NodeEvent]:
        """Run *node* to the end of its tool loop.

        Re-runs the node while its model keeps calling tools (up to the
        node's ``max_iterations``), so a tool step can make several calls
        and its model sees their results.  Adds each run's tool calls and
        tokens to *result* and appends the last run's result to *outcome*.
        """
        limit = max(1, int(getattr(node, "max_iterations", 10) or 1))
        for round_index in range(limit):
            child: NodeResult | None = None
            if stream:
                async for event in node.stream(state, config):
                    if event.event == "on_node_end" and isinstance(
                        event.data.get("result"), NodeResult
                    ):
                        child = event.data["result"]
                    elif event.event not in ("on_node_start", "on_node_end"):
                        yield event
            else:
                child = await node.execute(state, config)
            if child is None:
                child = NodeResult(node_name=node.name, error="step ended without a result")
            result.tool_calls.extend(child.tool_calls)
            result.total_tokens += child.total_tokens
            if (
                child.error
                or not (child.tool_calls and child.transition_reason == "tool_calls_present")
                or round_index == limit - 1
            ):
                outcome.append(child)
                return

    async def _pipeline(
        self,
        state: GraphState,
        config: dict[str, Any],
        result: NodeResult,
        *,
        stream: bool,
    ) -> AsyncIterator[NodeEvent]:
        """The node pipeline behind :meth:`execute` and :meth:`stream`;
        fills *result* in place."""
        start = time.perf_counter()

        model: BaseChatModel | None = config.get("_engine_model")
        if model is None:
            result.error = "No model available"
            return

        if not self.node_pool:
            result.error = "No nodes in the pool"
            return

        catalog = self._catalog(config)
        names = ", ".join(json.dumps(n) for n in self.node_pool)
        used_nodes: list[str] = []
        # Feedback for the next routing call only (never the conversation).
        notes: list[str] = []
        # Whether the last step was a terminal node that succeeded.
        finished = False

        async def run(chosen_name: str) -> AsyncIterator[NodeEvent]:
            nonlocal finished
            outcome: list[NodeResult] = []
            used_nodes.append(chosen_name)
            try:
                async for event in self._run_step(
                    self.node_pool[chosen_name], state, config, result, outcome, stream=stream
                ):
                    yield event
            except Exception as exc:
                logger.warning("Autonomous step %r failed: %s", chosen_name, exc)
                notes.append(f"Step '{chosen_name}' failed: {exc}")
                return
            child = outcome[-1] if outcome else None
            if child is not None and child.error:
                notes.append(f"Step '{chosen_name}' failed: {child.error}")
            finished = chosen_name in self.terminal_nodes and child is not None and not child.error

        # If entry_node is set, execute it first
        if self.entry_node and self.entry_node in self.node_pool:
            logger.info("Autonomous: starting with entry node %r", self.entry_node)
            async for event in run(self.entry_node):
                yield event

        step_idx = 0
        while not finished and step_idx < self.max_steps:
            # Ask the LLM which node to execute next.  The prompt and the
            # model's JSON choice stay out of state.messages: they are
            # routing, not conversation, and must never become the reply.
            routing_prompt = (
                f"{self.planner_instructions}\n\n"
                f"{catalog}\n\n"
                f"Steps completed: {used_nodes if used_nodes else '(none)'}\n"
                f"Step {step_idx + 1}/{self.max_steps}\n"
                + ("".join(f"\nNote: {note}" for note in notes) + "\n" if notes else "")
                + "\nChoose the next step. next_node must be one of "
                f"{names} — a step name, not a tool name: a step calls its own "
                'tools. Choose "__done__" when the work is complete; the final '
                "answer is written after you finish.\n"
                "Respond with ONLY a JSON object:\n"
                '{"next_node": "<step name>", "reason": "<brief reason>"}'
            )
            notes = []
            step_idx += 1

            try:
                response = await model.ainvoke(
                    [*state.messages, SystemMessage(content=routing_prompt)],
                    config=cast("RunnableConfig | None", config),
                )
            except Exception as exc:
                record_failure(result, exc, f"Routing failed at step {step_idx - 1}: {exc}")
                break
            prompt_tokens, completion_tokens = token_usage(response)
            result.total_tokens += prompt_tokens + completion_tokens
            _raw_content = response.content if hasattr(response, "content") else str(response)
            raw = _raw_content if isinstance(_raw_content, str) else str(_raw_content or "")

            # Extract JSON from response (handles nested braces)
            choice = self._extract_json(raw)
            if choice is None:
                result.transition_reason = "Agent finished: unparseable routing reply"
                break

            chosen = str(choice.get("next_node", "__done__"))
            reason = choice.get("reason", "")

            # Finished?
            if chosen in self._DONE:
                result.transition_reason = f"Agent finished: {reason}"
                break

            chosen_name = self._resolve_choice(chosen, config)
            if chosen_name is None:
                notes.append(f"'{chosen}' is not a step. Choose one of: {names}.")
                continue

            if not self.allow_repeat and chosen_name in used_nodes:
                notes.append(f"'{chosen_name}' was already used. Choose another step.")
                continue

            logger.info("Autonomous step %d: executing %r (%s)", step_idx, chosen_name, reason)
            async for event in run(chosen_name):
                yield event
            if finished:
                result.transition_reason = f"Terminal node '{chosen_name}' completed"

        # The run ends with a written answer: when no terminal node ran last,
        # the answer node writes it (unless it just ran).
        if not result.error and not finished:
            answer = self._final_answer_node()
            if not used_nodes or used_nodes[-1] != answer.name:
                outcome: list[NodeResult] = []
                used_nodes.append(answer.name)
                async for event in self._run_step(
                    answer, state, config, result, outcome, stream=stream
                ):
                    yield event
                if outcome and outcome[-1].error:
                    result.error = outcome[-1].error
                    cause = failure_cause(outcome[-1])
                    if cause is not None:
                        setattr(result, _FAILURE_CAUSE_ATTR, cause)

        result.output = {"steps": used_nodes, "total": len(used_nodes)}
        result.duration_ms = (time.perf_counter() - start) * 1000

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """Extract the first valid JSON object from text, handling nested braces."""
        depth = 0
        start = None
        for i, char in enumerate(text):
            if char == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        return json.loads(text[start : i + 1])
                    except (json.JSONDecodeError, ValueError):
                        start = None  # Try next JSON object
        return None
