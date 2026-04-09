"""Tool versioning support for MCP servers.

Allows multiple versions of the same tool to coexist.  By default the
latest version is exposed to clients; callers can pin a version via a
``version`` suffix in the tool name (e.g. ``search@2.0``).

Example::

    from promptise.mcp.server import MCPServer

    server = MCPServer(name="api")

    @server.tool(version="1.0")
    async def search(query: str) -> list[dict]:
        return await basic_search(query)

    @server.tool(version="2.0")
    async def search(query: str, filters: dict | None = None) -> list[dict]:
        return await advanced_search(query, filters)

    # Clients see ``search`` (latest) and ``search@1.0`` / ``search@2.0``
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ._types import ToolDef


@dataclass
class VersionedTool:
    """A group of tool versions sharing the same base name."""

    base_name: str
    versions: dict[str, ToolDef] = field(default_factory=dict)

    @property
    def latest_version(self) -> str:
        """Return the highest version string (semantic-aware)."""
        return max(self.versions, key=_version_key)

    @property
    def latest(self) -> ToolDef:
        return self.versions[self.latest_version]


def _version_key(v: str) -> tuple[int, ...]:
    """Convert ``"1.0"`` → ``(1, 0)`` for comparison."""
    parts: list[int] = []
    for seg in v.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return tuple(parts)


class VersionedToolRegistry:
    """Overlay registry that manages versioned tools.

    Sits alongside the standard ``ToolRegistry`` and is consulted at
    ``list_tools`` time to expose versioned aliases.

    Usage::

        vr = VersionedToolRegistry()
        vr.register("search", "1.0", tool_def_v1)
        vr.register("search", "2.0", tool_def_v2)

        # list_all() returns both pinned names and the latest alias
    """

    def __init__(self) -> None:
        self._groups: dict[str, VersionedTool] = {}

    def register(self, base_name: str, version: str, tool_def: ToolDef) -> None:
        """Register a versioned tool definition."""
        if base_name not in self._groups:
            self._groups[base_name] = VersionedTool(base_name=base_name)
        group = self._groups[base_name]
        if version in group.versions:
            raise ValueError(f"Tool '{base_name}' version '{version}' is already registered")
        group.versions[version] = tool_def

    def get(self, name: str) -> ToolDef | None:
        """Look up by ``name`` or ``name@version``."""
        if "@" in name:
            base, version = name.rsplit("@", 1)
            group = self._groups.get(base)
            if group is None:
                return None
            return group.versions.get(version)
        group = self._groups.get(name)
        if group is None:
            return None
        return group.latest

    def list_all(self) -> list[ToolDef]:
        """Return all versioned tool defs (pinned names + latest alias)."""
        result: list[ToolDef] = []
        for group in self._groups.values():
            for version, tdef in group.versions.items():
                result.append(tdef)
        return result

    def has(self, base_name: str) -> bool:
        return base_name in self._groups

    def list_versions(self, base_name: str) -> list[str]:
        """List available versions for a tool."""
        group = self._groups.get(base_name)
        if group is None:
            return []
        return sorted(group.versions.keys(), key=_version_key)
