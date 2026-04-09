"""Elicitation support for MCP servers.

Allows tools to request structured input from the user during
execution via the MCP elicitation protocol.

Example::

    from promptise.mcp.server import MCPServer, Elicitor, Depends

    server = MCPServer(name="deploy")

    @server.tool()
    async def deploy(
        env: str,
        elicit: Elicitor = Depends(Elicitor),
    ) -> str:
        answer = await elicit.ask(
            message=f"Deploy to {env}?",
            schema={"type": "object", "properties": {"confirm": {"type": "boolean"}}},
        )
        if not answer or not answer.get("confirm"):
            return "Cancelled"
        return f"Deployed to {env}"
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("promptise.server")


class Elicitor:
    """Request structured input from the user mid-execution.

    Bound to the MCP session by the framework's DI wiring.
    If the client does not support elicitation, ``ask()`` returns
    ``None`` silently.

    Args:
        timeout: Default timeout in seconds for elicitation requests.
    """

    def __init__(self, timeout: float = 60.0) -> None:
        self._session: Any = None
        self._request_id: str | int | None = None
        self._timeout = timeout

    def _bind(self, session: Any, request_id: str | int | None = None) -> None:
        """Bind to the current MCP session (called by framework)."""
        self._session = session
        self._request_id = request_id

    async def ask(
        self,
        message: str,
        schema: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        """Ask the user for structured input.

        Args:
            message: Human-readable prompt.
            schema: JSON Schema for the expected response.
            timeout: Override default timeout.

        Returns:
            Parsed response dict, or ``None`` if unavailable/declined.
        """
        if self._session is None:
            return None

        try:
            result = await self._session.send_elicitation_request(
                message=message,
                requested_schema=schema or {"type": "object", "properties": {}},
            )
            if result is None:
                return None
            if hasattr(result, "content"):
                return result.content if isinstance(result.content, dict) else None
            return result if isinstance(result, dict) else None
        except AttributeError:
            # Client/session doesn't support elicitation
            logger.debug("Elicitation not supported by client session")
            return None
        except Exception as exc:
            logger.debug("Elicitation failed: %s", exc)
            return None
