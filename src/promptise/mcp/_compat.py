"""Compatibility with both major versions of the ``mcp`` SDK.

Promptise supports ``mcp>=1.9,<3``.  The 2.x SDK renamed or moved a few
things Promptise uses; everything that differs goes through this module:

* ``McpError`` is ``MCPError`` in 2.x, built from ``(code, message, data)``
  instead of an ``ErrorData`` (:func:`mcp_error`, :func:`mcp_error_message`).
* Protocol type fields are snake_case attributes in 2.x (``input_schema``,
  ``is_error``, ``next_cursor``, ``mime_type``...); 1.x has the camelCase
  wire names.  Both accept either spelling as constructor keywords, but
  attributes must be read with :func:`field`.
* Objects handed to the SDK's HTTP transports (an ``httpx.Auth``, a
  transport, the client) must be ``httpx2`` types in 2.x and ``httpx``
  types in 1.x (:data:`sdk_httpx`).
* The server's request handlers are registered differently; see
  :mod:`promptise.mcp.server._sdk`.
"""

from __future__ import annotations

import re
from typing import Any

import mcp.shared.exceptions as _exceptions

#: True with the 2.x SDK (``MCPError``, ``MCPServer``, the dispatcher-based
#: lowlevel ``Server``), False with 1.x.
MCP_V2: bool = hasattr(_exceptions, "MCPError") and not hasattr(_exceptions, "McpError")

if MCP_V2:
    McpError: type[Exception] = _exceptions.MCPError  # type: ignore[attr-defined]
else:
    McpError = _exceptions.McpError  # type: ignore[attr-defined]

if MCP_V2:
    import httpx2 as sdk_httpx  # type: ignore[import-not-found,unused-ignore]
else:
    import httpx as sdk_httpx  # type: ignore[no-redef]

__all__ = [
    "MCP_V2",
    "McpError",
    "field",
    "mcp_error",
    "mcp_error_code",
    "mcp_error_message",
    "sdk_httpx",
]


def mcp_error(code: int, message: str, data: Any = None) -> Exception:
    """The SDK's JSON-RPC error exception (``McpError`` / ``MCPError``)."""
    if MCP_V2:
        return McpError(code, message, data)  # type: ignore[call-arg]
    from mcp.types import ErrorData

    return McpError(ErrorData(code=code, message=message, data=data))


def mcp_error_message(exc: BaseException) -> str | None:
    """The JSON-RPC error message of an SDK error, or ``None`` for anything else."""
    if not isinstance(exc, McpError):
        return None
    error = getattr(exc, "error", None)
    message = getattr(error, "message", None)
    if message is None:
        message = getattr(exc, "message", None)
    return message if isinstance(message, str) else None


def mcp_error_code(exc: BaseException) -> int | None:
    """The JSON-RPC error code of an SDK error, or ``None`` for anything else."""
    if not isinstance(exc, McpError):
        return None
    error = getattr(exc, "error", None)
    code = getattr(error, "code", None)
    if code is None:
        code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


_MISSING = object()
_SNAKE_PART = re.compile(r"_([a-z])")


def field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a protocol-type field by its snake_case *name* on either SDK.

    ``field(tool, "input_schema")`` reads ``tool.input_schema`` (2.x) or
    ``tool.inputSchema`` (1.x); the installed SDK's spelling is tried first.
    """
    camel = _SNAKE_PART.sub(lambda m: m.group(1).upper(), name)
    first, second = (name, camel) if MCP_V2 else (camel, name)
    value = getattr(obj, first, _MISSING)
    if value is not _MISSING:
        return value
    return getattr(obj, second, default)
