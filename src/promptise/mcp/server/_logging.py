"""Server-to-client logging for MCP servers.

Provides a ``ServerLogger`` that sends log messages to the connected
MCP client via ``notifications/message``.  Supports all 8 MCP logging
levels.

Example::

    from promptise.mcp.server import MCPServer, Depends, ServerLogger

    server = MCPServer(name="api")

    @server.tool()
    async def ingest(url: str, log: ServerLogger = Depends(ServerLogger)) -> str:
        await log.info("Starting ingestion", logger="ingest")
        data = await fetch(url)
        await log.info(f"Fetched {len(data)} bytes", logger="ingest")
        return "done"
"""

from __future__ import annotations

from typing import Any, Literal

#: MCP logging levels (syslog-style).
LoggingLevel = Literal[
    "debug",
    "info",
    "notice",
    "warning",
    "error",
    "critical",
    "alert",
    "emergency",
]


class ServerLogger:
    """Send log messages from the server to the connected MCP client.

    Messages appear on the client side as ``notifications/message``
    events.  If no session is available (e.g. in tests), calls are
    silently ignored.

    Use via dependency injection::

        @server.tool()
        async def run(log: ServerLogger = Depends(ServerLogger)) -> str:
            await log.info("starting")
            ...
    """

    def __init__(self) -> None:
        self._session: Any = None
        self._request_id: str | int | None = None

    def _bind(self, session: Any, request_id: str | int | None = None) -> None:
        """Bind to an MCP session (called by the framework)."""
        self._session = session
        self._request_id = request_id

    async def _send(
        self,
        level: LoggingLevel,
        data: Any,
        *,
        logger: str | None = None,
    ) -> None:
        """Send a log message to the client."""
        if self._session is None:
            return
        try:
            await self._session.send_log_message(
                level=level,
                data=data,
                logger=logger,
                related_request_id=self._request_id,
            )
        except Exception:
            # Never let logging fail the tool call
            pass

    async def debug(self, data: Any, *, logger: str | None = None) -> None:
        """Send a debug-level log message."""
        await self._send("debug", data, logger=logger)

    async def info(self, data: Any, *, logger: str | None = None) -> None:
        """Send an info-level log message."""
        await self._send("info", data, logger=logger)

    async def notice(self, data: Any, *, logger: str | None = None) -> None:
        """Send a notice-level log message."""
        await self._send("notice", data, logger=logger)

    async def warning(self, data: Any, *, logger: str | None = None) -> None:
        """Send a warning-level log message."""
        await self._send("warning", data, logger=logger)

    async def error(self, data: Any, *, logger: str | None = None) -> None:
        """Send an error-level log message."""
        await self._send("error", data, logger=logger)

    async def critical(self, data: Any, *, logger: str | None = None) -> None:
        """Send a critical-level log message."""
        await self._send("critical", data, logger=logger)

    async def alert(self, data: Any, *, logger: str | None = None) -> None:
        """Send an alert-level log message."""
        await self._send("alert", data, logger=logger)

    async def emergency(self, data: Any, *, logger: str | None = None) -> None:
        """Send an emergency-level log message."""
        await self._send("emergency", data, logger=logger)
