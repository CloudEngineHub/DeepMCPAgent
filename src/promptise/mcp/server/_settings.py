"""Server settings with environment variable support.

Provides a Pydantic ``BaseSettings`` subclass that reads configuration
from environment variables.  Users subclass for their own settings and
inject via ``Depends()``.

Example::

    from pydantic_settings import BaseSettings
    from promptise.mcp.server import MCPServer, Depends
    from promptise.mcp.server._settings import ServerSettings

    class MySettings(ServerSettings):
        database_url: str = "sqlite:///local.db"
        api_key: str = ""

        model_config = {"env_prefix": "MY_APP_"}

    @server.tool()
    async def info(settings: MySettings = Depends(MySettings)) -> dict:
        return {"db": settings.database_url}
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class ServerSettings(BaseSettings):
    """Base server settings with sensible defaults.

    Reads from environment variables with ``PROMPTISE_`` prefix.
    Subclass to add your own fields.

    Attributes:
        server_name: MCP server name.
        log_level: Logging level (``DEBUG``, ``INFO``, ``WARNING``, etc.).
        timeout_default: Default tool timeout in seconds.
    """

    model_config = {"env_prefix": "PROMPTISE_", "extra": "ignore"}

    server_name: str = "promptise-server"
    log_level: str = "INFO"
    timeout_default: float = 30.0
