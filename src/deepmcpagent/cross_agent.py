# deepmcpagent/cross_agent.py
"""
Cross-agent communication utilities for DeepMCPAgent.

Expose other in-process agents (“peers”) as standard LangChain tools so a
primary (caller) agent can *delegate* to them during planning/execution.

Tools provided
    - Per-peer ask tool  →  ``ask_agent_<name>``
      Forward one message (plus optional caller context) to a single peer and
      return the peer’s final text.

    - Broadcast tool     →  ``broadcast_to_agents``
      Send the same message to multiple peers in parallel and return a mapping
      of peer → final text. Timeouts/errors are captured per peer so one slow
      or failing peer does not fail the whole call.

Notes:
    - No new infrastructure is required. Peers are just in-process LangChain
      ``Runnable`` graphs (e.g., a DeepAgents loop or a LangGraph prebuilt
      executor returned by :func:`deepmcpagent.agent.build_deep_agent`).
    - Both tool classes implement async and sync execution paths (``_arun`` and
      ``_run``) to satisfy ``BaseTool``’s interface.
    - Optional per-call timeouts use ``anyio.move_on_after``.
    - The “final text” is extracted from common agent result shapes. If your
      peer returns a custom structure, adapt upstream or post-process the
      returned string.

Examples:
    Build a peer agent and attach it to a main agent as a tool:

    >>> from deepmcpagent.agent import build_deep_agent
    >>> from deepmcpagent.cross_agent import CrossAgent
    >>>
    >>> peer_graph, _ = await build_deep_agent(servers=..., model="openai:gpt-4o-mini")
    >>> main_graph, _ = await build_deep_agent(
    ...     servers=...,
    ...     model="openai:gpt-4.1",
    ...     cross_agents={"researcher": CrossAgent(agent=peer_graph, description="Web research")}
    ... )
    >>> # Now the main agent can call:
    >>> #   - ask_agent_researcher(message=..., context?=..., timeout_s?=...)
    >>> #   - broadcast_to_agents(message=..., peers?=[...], timeout_s?=...)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

# -----------------------------
# Public API surface
# -----------------------------


@dataclass(frozen=True)
class CrossAgent:
    """Metadata wrapper for a peer agent to be exposed as a tool.

    The wrapper is descriptive only. Behavior is implemented by tools produced
    via :func:`make_cross_agent_tools`.

    Attributes:
        agent: A runnable agent (e.g., LangGraph or DeepAgents) that accepts
            ``{"messages": [...]}`` and returns a result consumable by the
            built-in “best final text” extractor.
        description: One-line human description used in tool docs to help the
            calling agent decide when to delegate.

    Examples:
        >>> cross = CrossAgent(agent=peer_graph, description="Accurate math")
    """

    agent: Runnable[Any, Any]
    description: str = ""


def make_cross_agent_tools(
    peers: Mapping[str, CrossAgent],
    *,
    tool_name_prefix: str = "ask_agent_",
    include_broadcast: bool = True,
) -> list[BaseTool]:
    """Create LangChain tools for cross-agent communication.

    For each peer, a tool named ``f"{tool_name_prefix}{peer_name}"`` is created.
    Optionally, a ``broadcast_to_agents`` tool is added to fan-out questions to
    multiple peers concurrently.

    Args:
        peers: Mapping of peer name → :class:`CrossAgent`. The name becomes part
            of the tool id (e.g., ``ask_agent_mathpeer``).
        tool_name_prefix: Prefix used for each per-peer ask tool. Defaults to
            ``"ask_agent_"``.
        include_broadcast: When ``True`` (default), also include the group
            fan-out tool ``broadcast_to_agents``.

    Returns:
        list[BaseTool]: A list of fully constructed tools ready to be appended
        to the caller agent’s toolset.

    Notes:
        Construction does not contact peers; errors (e.g., network) surface at
        call time during execution of the generated tools.

    Examples:
        >>> tools = make_cross_agent_tools({
        ...     "researcher": CrossAgent(agent=peer_graph, description="Web research")
        ... })
        >>> # Attach `tools` alongside your MCP-discovered tools when building the agent.
    """
    if not peers:
        return []

    def _best_text(result: Any) -> str:
        """Extract a final text answer from common agent result shapes.

        Looks for a LangGraph-like ``{"messages": [...]}`` structure; otherwise
        falls back to ``str(result)``.

        Args:
            result: The raw result returned by a peer agent.

        Returns:
            str: Best-effort final text response.
        """
        try:
            if isinstance(result, dict) and "messages" in result and result["messages"]:
                last = result["messages"][-1]
                content = getattr(last, "content", None)
                if isinstance(content, str) and content:
                    return content
                if isinstance(content, list) and content and isinstance(content[0], dict):
                    return cast(str, content[0].get("text") or str(content))
                return str(last)
            return str(result)
        except Exception:
            return str(result)

    out: list[BaseTool] = []

    # Per-agent ask tools
    for name, spec in peers.items():
        out.append(
            _AskAgentTool(
                name=f"{tool_name_prefix}{name}",
                description=(
                    f"Ask peer agent '{name}' for help. " + (spec.description or "")
                ).strip(),
                target=spec.agent,
                extract=_best_text,
            )
        )

    # Optional broadcast tool
    if include_broadcast:
        out.append(
            _BroadcastTool(
                name="broadcast_to_agents",
                description=(
                    "Ask multiple peer agents the same question in parallel and "
                    "return each peer's final answer."
                ),
                peers=peers,
                extract=_best_text,
            )
        )

    return out


# -----------------------------
# Tool implementations
# -----------------------------


class _AskArgs(BaseModel):
    """Arguments for per-peer ask tools (``ask_agent_<name>``).

    Attributes:
        message: The user-level message to forward to the peer agent.
        context: Optional caller context (constraints, partial results, style
            guide). If provided, it is inserted first as a *system* message to
            bias many executors.
        timeout_s: Optional timeout (seconds). If exceeded, the tool returns
            ``"Timed out waiting for peer agent reply."`` instead of raising.
    """

    message: str = Field(..., description="Message to send to the peer agent.")
    context: str | None = Field(
        None,
        description=(
            "Optional additional context from the caller (e.g., hints, partial "
            "results, or constraints)."
        ),
    )
    timeout_s: float | None = Field(
        None,
        ge=0,
        description="Optional timeout in seconds for the peer agent call.",
    )


class _AskAgentTool(BaseTool):
    """Tool that forwards a question to a specific peer agent.

    This tool wraps a peer :class:`~langchain_core.runnables.Runnable` and
    returns the peer’s *final text* using a best-effort extractor.

    Attributes:
        name: Tool identifier (e.g., ``ask_agent_researcher``).
        description: Human description to guide the caller agent’s planning.
        args_schema: Pydantic model describing accepted keyword args.

    Notes:
        - Async-first: prefer ``_arun``; a sync shim (``_run``) is provided to
          satisfy the abstract base class and support sync-only executors.
        - The peer is invoked with a ChatML-like payload:
          ``{"messages": [{"role": "...", "content": "..."}]}``.

    """

    name: str
    description: str
    # Pydantic v2 requires a type annotation for field overrides.
    args_schema: type[BaseModel] = _AskArgs

    _target: Runnable[Any, Any] = PrivateAttr()
    _extract: Callable[[Any], str] = PrivateAttr()

    def __init__(
        self,
        *,
        name: str,
        description: str,
        target: Runnable[Any, Any],
        extract: Callable[[Any], str],
    ) -> None:
        """Initialize the ask tool.

        Args:
            name: Tool identifier.
            description: Human description for planning.
            target: The peer agent runnable to call.
            extract: Function that extracts the final text from peer results.
        """
        super().__init__(name=name, description=description)
        self._target = target
        self._extract = extract

    async def _arun(
        self,
        *,
        message: str,
        context: str | None = None,
        timeout_s: float | None = None,
    ) -> str:
        """Asynchronously forward a message to the peer agent.

        Args:
            message: The message to forward (becomes a user message).
            context: Optional caller context, sent first as a system message.
            timeout_s: Optional timeout in seconds for the peer call.

        Returns:
            str: The peer agent’s best-effort final text answer, or a timeout
            message if the deadline is exceeded.

        Raises:
            Exception: Propagates exceptions raised by the peer call (network or
            executor failures). On timeout, returns a string instead of raising.
        """
        payload: list[dict[str, Any]] = []
        # Put context first to bias some executors that read system first
        if context:
            payload.append({"role": "system", "content": f"Caller context: {context}"})
        payload.append({"role": "user", "content": message})

        async def _call() -> Any:
            return await self._target.ainvoke({"messages": payload})

        if timeout_s and timeout_s > 0:
            import anyio

            with anyio.move_on_after(timeout_s) as scope:
                res = await _call()
                if scope.cancel_called:  # rare
                    return "Timed out waiting for peer agent reply."
        else:
            res = await _call()

        return self._extract(res)

    def _run(
        self,
        *,
        message: str,
        context: str | None = None,
        timeout_s: float | None = None,
    ) -> str:  # pragma: no cover (usually unused in async apps)
        """Synchronous shim that delegates to :meth:`_arun`.

        Args:
            message: The message to forward (becomes a user message).
            context: Optional caller context, sent first as a system message.
            timeout_s: Optional timeout in seconds for the peer call.

        Returns:
            str: The peer agent’s best-effort final text answer (or timeout text).
        """
        import anyio

        return anyio.run(lambda: self._arun(message=message, context=context, timeout_s=timeout_s))


class _BroadcastArgs(BaseModel):
    """Arguments for the broadcast tool (``broadcast_to_agents``).

    Attributes:
        message: The shared message sent to all (or a subset of) peers.
        peers: Optional subset of peer names to consult. If omitted, all
            registered peers are consulted.
        timeout_s: Optional per-peer timeout in seconds. Affected peers return
            ``"Timed out"`` in the result mapping.
    """

    message: str = Field(..., description="Message to send to all/selected peers.")
    peers: Sequence[str] | None = Field(
        None, description="Optional subset of peer names. If omitted, use all peers."
    )
    timeout_s: float | None = Field(
        None, ge=0, description="Optional timeout per peer call in seconds."
    )


class _BroadcastTool(BaseTool):
    """Ask multiple peer agents in parallel and return a mapping of answers.

    Each selected peer is invoked concurrently. Timeouts and exceptions are
    captured **per peer** so the overall call remains resilient.

    Attributes:
        name: Tool identifier (``broadcast_to_agents``).
        description: Human description for planning.
        args_schema: Pydantic model describing accepted keyword args.

    Notes:
        Uses ``anyio.create_task_group`` for compatibility across anyio versions.
    """

    name: str
    description: str
    # Pydantic v2 requires a type annotation for field overrides.
    args_schema: type[BaseModel] = _BroadcastArgs

    _peers: Mapping[str, CrossAgent] = PrivateAttr()
    _extract: Callable[[Any], str] = PrivateAttr()

    def __init__(
        self,
        *,
        name: str,
        description: str,
        peers: Mapping[str, CrossAgent],
        extract: Callable[[Any], str],
    ) -> None:
        """Initialize the broadcast tool.

        Args:
            name: Tool identifier.
            description: Human description for planning.
            peers: Mapping of peer name → :class:`CrossAgent`.
            extract: Function that extracts the final text from peer results.
        """
        super().__init__(name=name, description=description)
        self._peers = peers
        self._extract = extract

    async def _arun(
        self,
        *,
        message: str,
        peers: Sequence[str] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, str]:
        """Asynchronously consult multiple peers in parallel.

        Args:
            message: The message forwarded to each selected peer.
            peers: Optional subset of peer names to target. If ``None``, uses all.
            timeout_s: Optional timeout in seconds applied per peer call.

        Returns:
            dict[str, str]: Mapping of ``peer_name`` → final text. Peers that
            exceed the timeout return ``"Timed out"``. Peers that raise an
            exception return ``"Error: <message>"``.

        Raises:
            ValueError: If any requested peer name is unknown.
        """
        selected: Iterable[tuple[str, CrossAgent]]
        if peers:
            missing = [p for p in peers if p not in self._peers]
            if missing:
                raise ValueError(f"Unknown peer(s): {', '.join(missing)}")
            selected = [(p, self._peers[p]) for p in peers]
        else:
            selected = list(self._peers.items())

        import anyio

        results: dict[str, str] = {}

        async def _one(name: str, target: Runnable[Any, Any]) -> None:
            async def _call() -> Any:
                return await target.ainvoke({"messages": [{"role": "user", "content": message}]})

            if timeout_s and timeout_s > 0:
                with anyio.move_on_after(timeout_s) as scope:
                    try:
                        res = await _call()
                        if scope.cancel_called:
                            results[name] = "Timed out"
                            return
                        results[name] = self._extract(res)
                    except Exception as exc:  # keep broadcast resilient
                        results[name] = f"Error: {exc}"
            else:
                try:
                    res = await _call()
                    results[name] = self._extract(res)
                except Exception as exc:
                    results[name] = f"Error: {exc}"

        # Using TaskGroup for compatibility across anyio versions
        async with anyio.create_task_group() as tg:
            for n, s in selected:
                tg.start_soon(_one, n, s.agent)

        return results

    def _run(
        self,
        *,
        message: str,
        peers: Sequence[str] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, str]:  # pragma: no cover (usually unused in async apps)
        """Synchronous shim that delegates to :meth:`_arun`.

        Args:
            message: The message forwarded to each selected peer.
            peers: Optional subset of peer names to target. If ``None``, uses all.
            timeout_s: Optional timeout in seconds applied per peer call.

        Returns:
            dict[str, str]: Mapping of ``peer_name`` → final text (or timeout/error text).
        """
        import anyio

        return anyio.run(lambda: self._arun(message=message, peers=peers, timeout_s=timeout_s))
