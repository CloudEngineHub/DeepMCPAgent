"""Base node types and the ``@node`` decorator for the PromptGraph engine.

Every node in a ``PromptGraph`` implements the ``BaseNode`` interface.
Developers can either:

1. Use built-in node types (``PromptNode``, ``ToolNode``, etc.) — no
   subclassing needed, just configuration.
2. Subclass ``BaseNode`` for full custom behaviour.
3. Use the ``@node`` decorator to turn any async function into a node.

Example — functional node::

    @node("fetch_weather")
    async def fetch_weather(state: GraphState) -> NodeResult:
        city = state.context.get("city", "Berlin")
        weather = await weather_api.get(city)
        state.context["weather"] = weather
        return NodeResult(node_name="fetch_weather", output=weather)

    graph.add_node(fetch_weather)
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from .state import GraphState, NodeEvent, NodeResult

# ---------------------------------------------------------------------------
# BaseNode — the interface every node must implement
# ---------------------------------------------------------------------------


@runtime_checkable
class NodeProtocol(Protocol):
    """Minimal protocol that every graph node must satisfy."""

    name: str

    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        """Execute this node. Return a ``NodeResult`` describing what happened."""
        ...

    async def stream(self, state: GraphState, config: dict[str, Any]) -> AsyncIterator[NodeEvent]:
        """Stream execution events. Yields ``NodeEvent`` objects."""
        ...  # pragma: no cover


class BaseNode:
    """Concrete base class for graph nodes.

    Provides default implementations and common configuration.
    Subclass this for custom node behaviour, or use one of the
    built-in types (``PromptNode``, ``ToolNode``, etc.).

    Args:
        name: Unique node identifier within the graph.
        instructions: Natural-language description of what this
            node does.  Injected into the LLM prompt for
            ``PromptNode``; used for documentation and
            visualization for other node types.
        description: Short description shown in graph
            visualization and ``describe()`` output.
        transitions: Mapping of output keys to next-node names.
            E.g. ``{"proceed": "act", "replan": "plan"}``.
            The engine resolves transitions after execution.
        default_next: Fallback node name if no transition matches.
        max_iterations: Maximum times this node can execute in
            a single graph run (prevents infinite loops).
        metadata: Arbitrary metadata accessible by hooks and
            observability.
    """

    def __init__(
        self,
        name: str,
        *,
        instructions: str = "",
        description: str = "",
        transitions: dict[str, str] | None = None,
        default_next: str | None = None,
        max_iterations: int = 10,
        metadata: dict[str, Any] | None = None,
        is_entry: bool = False,
        is_terminal: bool = False,
        flags: set[Any] | None = None,
    ) -> None:
        from .state import NodeFlag

        self.name = name
        self.instructions = instructions
        self.description = description or instructions[:80]
        self.transitions: dict[str, str] = dict(transitions) if transitions else {}
        self.default_next = default_next
        self.max_iterations = max_iterations
        self.metadata: dict[str, Any] = dict(metadata) if metadata else {}

        # Flag system — unified typed flags
        self.flags: set[Any] = set(flags) if flags else set()

        # Backward compat: is_entry/is_terminal map to flags
        if is_entry:
            self.flags.add(NodeFlag.ENTRY)
        if is_terminal:
            self.flags.add(NodeFlag.TERMINAL)

    @property
    def is_entry(self) -> bool:
        """Whether this node starts the graph."""
        from .state import NodeFlag

        return NodeFlag.ENTRY in self.flags

    @property
    def is_terminal(self) -> bool:
        """Whether this node can end the graph."""
        from .state import NodeFlag

        return NodeFlag.TERMINAL in self.flags

    def has_flag(self, flag: Any) -> bool:
        """Check if this node has a specific flag."""
        return flag in self.flags

    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        """Execute this node.  Subclasses must override."""
        raise NotImplementedError(
            f"Node {self.name!r} ({type(self).__name__}) must implement execute()"
        )

    async def stream(self, state: GraphState, config: dict[str, Any]) -> AsyncIterator[NodeEvent]:
        """Stream execution events.

        Default implementation executes the node and yields a single
        ``on_node_end`` event.  Override for fine-grained streaming.
        """
        result = await self.execute(state, config)
        yield NodeEvent(
            event="on_node_end",
            node_name=self.name,
            data={"result": result},
        )

    def __repr__(self) -> str:
        cls = type(self).__name__
        desc = self.description[:40] + "..." if len(self.description) > 40 else self.description
        return f"{cls}({self.name!r}, {desc!r})"


# ---------------------------------------------------------------------------
# @node decorator — turn an async function into a BaseNode
# ---------------------------------------------------------------------------


class _FunctionalNode(BaseNode):
    """A node backed by a plain async function."""

    def __init__(
        self,
        name: str,
        func: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self._func = func
        # Inherit docstring as description
        if not self.description and func.__doc__:
            self.description = func.__doc__.strip().split("\n")[0]

    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        """Call the wrapped function."""
        sig = inspect.signature(self._func)
        params = list(sig.parameters.keys())

        # Support (state), (state, config), or just ()
        if len(params) >= 2:
            result = await self._func(state, config)
        elif len(params) == 1:
            result = await self._func(state)
        else:
            result = await self._func()

        # If the function returns a NodeResult, use it directly
        if isinstance(result, NodeResult):
            if not result.node_name:
                result.node_name = self.name
            return result

        # Otherwise, wrap the return value
        return NodeResult(
            node_name=self.name,
            output=result,
        )


def node(
    name: str,
    *,
    instructions: str = "",
    transitions: dict[str, str] | None = None,
    default_next: str | None = None,
    max_iterations: int = 10,
    metadata: dict[str, Any] | None = None,
    flags: set[Any] | None = None,
    is_entry: bool = False,
    is_terminal: bool = False,
) -> Any:
    """Decorator that turns an async function into a graph node.

    Usage::

        @node("fetch_data", default_next="process")
        async def fetch_data(state: GraphState) -> NodeResult:
            data = await api.get(state.context["url"])
            state.context["data"] = data
            return NodeResult(node_name="fetch_data", output=data)

        graph.add_node(fetch_data)

    The decorated function can accept:
    - ``(state: GraphState)``
    - ``(state: GraphState, config: dict)``
    - ``()`` (for side-effect-only nodes)

    Returns:
        A ``_FunctionalNode`` instance that can be added to a graph.
    """

    def decorator(func: Any) -> _FunctionalNode:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f"@node requires an async function, got {type(func).__name__}")

        fn_node = _FunctionalNode(
            name=name,
            func=func,
            instructions=instructions,
            transitions=transitions,
            default_next=default_next,
            max_iterations=max_iterations,
            metadata=metadata,
            flags=flags,
            is_entry=is_entry,
            is_terminal=is_terminal,
        )
        # Preserve function metadata. `fn_node` is a Node instance, not a
        # callable, but update_wrapper only copies __doc__/__name__/__module__
        # attributes which Node supports. Safe despite the Callable signature.
        functools.update_wrapper(fn_node, func)  # type: ignore[arg-type]
        return fn_node

    return decorator
