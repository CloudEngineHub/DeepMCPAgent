"""Progress tracking for long-running MCP tool calls.

Allows tool handlers to report progress to clients using MCP
progress notifications.  The ``ProgressReporter`` is injected via
``Depends()`` and provides a simple async API for sending updates.

Example::

    from promptise.mcp.server import MCPServer, Depends, ProgressReporter

    server = MCPServer(name="etl")

    @server.tool()
    async def process(items: list[str], progress: ProgressReporter = Depends(ProgressReporter)) -> str:
        for i, item in enumerate(items):
            await do_work(item)
            await progress.report(i + 1, total=len(items), message=f"Processed {item}")
        return "done"
"""

from __future__ import annotations

from typing import Any


class ProgressReporter:
    """Report progress during long-running tool calls.

    Sends MCP ``notifications/progress`` messages to the connected
    client.  If no progress token was provided by the client or no
    session is available (e.g. in tests), calls are silently ignored.

    Use via dependency injection::

        @server.tool()
        async def etl(progress: ProgressReporter = Depends(ProgressReporter)) -> str:
            await progress.report(1, total=10, message="Step 1")
            ...
    """

    def __init__(self) -> None:
        self._session: Any = None
        self._progress_token: str | int | None = None
        self._request_id: str | int | None = None

    def _bind(
        self,
        session: Any,
        progress_token: str | int | None,
        request_id: str | int | None = None,
    ) -> None:
        """Bind to an MCP session (called by the framework)."""
        self._session = session
        self._progress_token = progress_token
        self._request_id = request_id

    async def report(
        self,
        progress: float,
        *,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        """Send a progress notification to the client.

        Args:
            progress: Current progress value (e.g. items processed).
            total: Total expected value (e.g. total items). If provided,
                clients can calculate a percentage.
            message: Optional human-readable status message.
        """
        if self._session is None or self._progress_token is None:
            return

        try:
            await self._session.send_progress_notification(
                progress_token=self._progress_token,
                progress=progress,
                total=total,
                message=message,
                related_request_id=self._request_id,
            )
        except Exception:
            # Never let progress reporting fail the tool call
            pass
