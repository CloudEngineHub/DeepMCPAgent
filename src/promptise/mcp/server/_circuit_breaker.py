"""Circuit breaker middleware for MCP servers.

Protects downstream services from cascading failures by tracking
error rates and short-circuiting tool calls when a threshold is
exceeded.

Example::

    from promptise.mcp.server import MCPServer, CircuitBreakerMiddleware

    server = MCPServer(name="api")
    server.add_middleware(CircuitBreakerMiddleware(
        failure_threshold=5,
        recovery_timeout=60.0,
        excluded_tools={"health_check"},
    ))
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ._context import RequestContext


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing — reject calls
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class _CircuitStats:
    """Per-tool circuit state and counters."""

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    success_count: int = 0


class CircuitOpenError(Exception):
    """Raised when a tool call is rejected by an open circuit."""

    def __init__(self, tool: str, retry_after: float) -> None:
        self.tool = tool
        self.retry_after = retry_after
        super().__init__(f"Circuit open for tool '{tool}'. Retry after {retry_after:.1f}s.")


class CircuitBreakerMiddleware:
    """Circuit breaker middleware.

    Tracks consecutive failures per tool. When ``failure_threshold``
    consecutive failures occur, the circuit opens and subsequent calls
    are rejected immediately. After ``recovery_timeout`` seconds the
    circuit enters half-open state and allows one probe call through.

    Args:
        failure_threshold: Consecutive failures before opening the
            circuit (default ``5``).
        recovery_timeout: Seconds to wait before probing recovery
            (default ``60.0``).
        excluded_tools: Tool names exempt from circuit breaking.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        *,
        excluded_tools: set[str] | None = None,
    ) -> None:
        self._threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._excluded = excluded_tools or set()
        self._circuits: dict[str, _CircuitStats] = {}

    def _get_circuit(self, tool: str) -> _CircuitStats:
        if tool not in self._circuits:
            self._circuits[tool] = _CircuitStats()
        return self._circuits[tool]

    def get_state(self, tool: str) -> CircuitState:
        """Get the current circuit state for a tool."""
        return self._get_circuit(tool).state

    async def __call__(self, ctx: RequestContext, call_next: Callable[..., Any]) -> Any:
        tool = ctx.tool_name
        if tool in self._excluded:
            return await call_next(ctx)

        circuit = self._get_circuit(tool)
        now = time.monotonic()

        # Open circuit — reject unless recovery timeout has elapsed
        if circuit.state == CircuitState.OPEN:
            elapsed = now - circuit.last_failure_time
            if elapsed < self._recovery_timeout:
                raise CircuitOpenError(tool, self._recovery_timeout - elapsed)
            # Transition to half-open for probe
            circuit.state = CircuitState.HALF_OPEN

        try:
            result = await call_next(ctx)
            # Success — reset circuit
            circuit.failure_count = 0
            circuit.success_count += 1
            if circuit.state == CircuitState.HALF_OPEN:
                circuit.state = CircuitState.CLOSED
            return result
        except Exception:
            circuit.failure_count += 1
            circuit.last_failure_time = now
            circuit.success_count = 0
            if circuit.failure_count >= self._threshold or circuit.state == CircuitState.HALF_OPEN:
                circuit.state = CircuitState.OPEN
            raise

    def reset(self, tool: str | None = None) -> None:
        """Reset circuit state for a tool (or all tools)."""
        if tool is None:
            self._circuits.clear()
        else:
            self._circuits.pop(tool, None)
