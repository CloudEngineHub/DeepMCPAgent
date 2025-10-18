# deepmcpagent/cross_agent.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Iterable, Sequence, Callable, cast

from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr


# -----------------------------
# Public API surface
# -----------------------------

@dataclass(frozen=True)
class CrossAgent:
    """Wrap a runnable agent (LangGraph/DeepAgents) to expose it as a tool.

    Attributes:
        agent: A Runnable that accepts {"messages": [...]} and returns a result.
        description: One-line human description used in tool docs.
    """

    agent: Runnable[Any, Any]
    description: str = ""


def make_cross_agent_tools(
    peers: Mapping[str, CrossAgent],
    *,
    tool_name_prefix: str = "ask_agent_",
    include_broadcast: bool = True,
) -> list[BaseTool]:
    """Create LangChain tools that allow an agent to ask its peers questions.

    For each peer, we create a tool `{tool_name_prefix}{peer_name}` that takes a
    text message (plus optional context) and returns the peer's final text.

    Optionally, we also add a `broadcast_to_agents` tool to consult multiple peers
    in parallel and return a dict of peer->answer.
    """
    if not peers:
        return []

    def _best_text(result: Any) -> str:
        """Best-effort extraction of a final text answer from common executors."""
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
                description=(f"Ask peer agent '{name}' for help. " + (spec.description or "")).strip(),
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
    message: str = Field(..., description="Message to send to the peer agent.")
    context: str | None = Field(
        None,
        description=(
            "Optional additional context from the caller (e.g., hints, partial "
            "results, constraints)."
        ),
    )
    timeout_s: float | None = Field(
        None,
        ge=0,
        description="Optional timeout in seconds for the peer agent call.",
    )


class _AskAgentTool(BaseTool):
    """Tool that forwards a question to a specific peer agent."""

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

    # BaseTool is abstract and requires a sync path, even if you mainly use async.
    def _run(
        self,
        *,
        message: str,
        context: str | None = None,
        timeout_s: float | None = None,
    ) -> str:  # pragma: no cover (usually unused in async apps)
        import anyio
        return anyio.run(
            lambda: self._arun(message=message, context=context, timeout_s=timeout_s)
        )


class _BroadcastArgs(BaseModel):
    message: str = Field(..., description="Message to send to all/selected peers.")
    peers: Sequence[str] | None = Field(
        None, description="Optional subset of peer names. If omitted, use all peers."
    )
    timeout_s: float | None = Field(
        None, ge=0, description="Optional timeout per peer call in seconds."
    )


class _BroadcastTool(BaseTool):
    """Ask multiple peer agents in parallel and return a mapping of answers."""

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
                return await target.ainvoke(
                    {"messages": [{"role": "user", "content": message}]}
                )

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
        import anyio
        return anyio.run(
            lambda: self._arun(message=message, peers=peers, timeout_s=timeout_s)
        )
