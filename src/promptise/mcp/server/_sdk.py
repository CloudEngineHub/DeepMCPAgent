"""Registering ``MCPServer``'s handlers on the ``mcp`` SDK's lowlevel server.

``mcp`` 1.x registers handlers with decorators (``@server.list_tools()``)
that wrap the return value into the result type, turn a raised exception
into an ``isError`` tool result, and expose the request being handled as
``server.request_context``.  The 2.x lowlevel server takes ``(ctx, params)``
handlers that return the full result and passes the request context in.

:func:`handler` registers the same Promptise handler function on either:
with 1.x it applies the SDK decorator, with 2.x it registers an adapter
that does what the 1.x decorator did and makes the context available to
:func:`request_context`.  Everything else (the transports, the
initialisation options) is the same on both SDKs.
"""

from __future__ import annotations

import base64
import contextvars
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from mcp.server.lowlevel import Server as LowLevelServer

from .._compat import MCP_V2

__all__ = [
    "CALL_TOOL_OPTIONS",
    "ModernCall",
    "connection_of",
    "handler",
    "modern_call",
    "progress_token",
    "request_context",
]

_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])

# The 2.x request context of the request being handled (1.x keeps its own).
_request_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar("promptise_mcp_request_ctx")
# The 2.x ``tools/call`` params being handled (for the multi round-trip fields).
_call_params: contextvars.ContextVar[Any] = contextvars.ContextVar("promptise_mcp_call_params")


class ModernCall:
    """A ``tools/call`` on a 2026-07-28 (or later) connection.

    That protocol revision has no session and no server-to-client
    requests: a server that needs input from the client (elicitation)
    answers with an ``InputRequiredResult``, and the client sends the same
    call again with the answers (``input_responses``) and the server's
    ``request_state``.

    Attributes:
        protocol_version: The negotiated protocol version.
        input_responses: The client's answers to the previous round's
            input requests, keyed as the server asked (``{}`` on the first
            round).
        request_state: The ``request_state`` of the previous round, as the
            server minted it (``None`` on the first round).
        client_capabilities: The capabilities the request declared.
    """

    __slots__ = ("client_capabilities", "input_responses", "protocol_version", "request_state")

    def __init__(
        self,
        protocol_version: str,
        input_responses: dict[str, Any],
        request_state: str | None,
        client_capabilities: Any,
    ) -> None:
        self.protocol_version = protocol_version
        self.input_responses = input_responses
        self.request_state = request_state
        self.client_capabilities = client_capabilities


def modern_call(ll: Any) -> ModernCall | None:
    """The :class:`ModernCall` being handled, or ``None`` on a handshake connection.

    Always ``None`` with the 1.x SDK, which speaks only the handshake
    protocol revisions.
    """
    if not MCP_V2:
        return None
    try:
        ctx = _request_ctx.get()
    except LookupError:
        return None
    session = getattr(ctx, "session", None)
    version = getattr(session, "protocol_version", None)
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS

    if version not in MODERN_PROTOCOL_VERSIONS:
        return None
    params = _call_params.get(None)
    return ModernCall(
        version,
        dict(getattr(params, "input_responses", None) or {}),
        getattr(params, "request_state", None),
        getattr(session, "client_capabilities", None),
    )


def _call_tool_options() -> dict[str, Any]:
    """Keywords for the 1.x ``call_tool`` decorator.

    The SDK (1.10+) validates tool arguments against ``inputSchema`` with
    jsonschema before calling the handler.  A call rejected there never
    reaches Promptise's pipeline (no auth, rate limit or audit) and gets a
    plain-text error that quotes the offending value in full.  Promptise
    validates every call itself, inside the pipeline, so the SDK's check is
    turned off where the SDK offers the switch (1.9.x has neither the check
    nor the switch, and the 2.x lowlevel server doesn't validate arguments).
    """
    call_tool = getattr(LowLevelServer, "call_tool", None)
    if call_tool is None:
        return {}
    try:
        parameters = inspect.signature(call_tool).parameters
    except (TypeError, ValueError):  # pragma: no cover - unexpected SDK layout
        return {}
    return {"validate_input": False} if "validate_input" in parameters else {}


CALL_TOOL_OPTIONS: dict[str, Any] = _call_tool_options()


def request_context(ll: Any) -> Any:
    """The SDK's context of the request being handled.

    Raises:
        LookupError: Outside a request handler (as ``request_context`` does
            on the 1.x server).
    """
    if not MCP_V2:
        return ll.request_context
    return _request_ctx.get()


def progress_token(meta: Any) -> Any:
    """The progress token in a request's ``_meta`` (a model in 1.x, a dict in 2.x)."""
    if meta is None:
        return None
    if isinstance(meta, dict):
        return meta.get("progress_token", meta.get("progressToken"))
    return getattr(meta, "progressToken", getattr(meta, "progress_token", None))


def connection_of(session: Any) -> Any:
    """The object that stands for one client connection.

    The 1.x ``ServerSession`` lives as long as the connection.  The 2.x
    one is a proxy built for every message, over the connection's
    ``Connection`` object, which is what per-connection state (and the
    sessions ``list_changed`` notifications go to) must be keyed on.
    """
    if not MCP_V2:
        return session
    return getattr(session, "_connection", None) or session


def handler(ll: Any, kind: str, **options: Any) -> Callable[[_F], _F]:
    """A decorator registering *kind* (``"list_tools"``, ``"call_tool"``...) on *ll*.

    The decorated function has the 1.x decorator's signature: no arguments
    for the ``list_*`` handlers, ``(name, arguments)`` for ``call_tool``,
    ``(uri)`` for ``read_resource`` and ``(name, arguments)`` for
    ``get_prompt``.
    """
    if not MCP_V2:
        decorator: Callable[[_F], _F] = getattr(ll, kind)(**options)
        return decorator

    def register(func: _F) -> _F:
        _V2_ADAPTERS[kind](ll, func)
        return func

    return register


# ---------------------------------------------------------------------------
# 2.x adapters (each does what the 1.x decorator of the same name did)
# ---------------------------------------------------------------------------


async def _in_context(ctx: Any, call: Awaitable[Any]) -> Any:
    if ctx is None:  # called directly (tests): no request, as outside one in 1.x
        return await call
    token = _request_ctx.set(ctx)
    try:
        return await call
    finally:
        _request_ctx.reset(token)


def _list_adapter(method: str, result_type: str, items_field: str) -> Callable[[Any, Any], None]:
    def adapt(ll: Any, func: Callable[[], Awaitable[list[Any]]]) -> None:
        from mcp import types

        result_cls = getattr(types, result_type)

        async def handle(ctx: Any, params: Any) -> Any:
            items = await _in_context(ctx, func())
            return result_cls(**{items_field: list(items)})

        ll.add_request_handler(method, types.PaginatedRequestParams, handle)

    return adapt


def _call_tool_adapter(ll: Any, func: Callable[..., Awaitable[Any]]) -> None:
    from mcp import types

    async def handle(ctx: Any, params: Any) -> Any:
        token = _call_params.set(params)
        try:
            result = await _in_context(ctx, func(params.name, params.arguments or {}))
        except Exception as exc:
            # The 1.x decorator's behaviour: a failed call is a tool result
            # with isError (the model sees the error text), not a JSON-RPC error.
            return types.CallToolResult.model_validate(
                {"content": [types.TextContent(type="text", text=str(exc))], "isError": True}
            )
        finally:
            _call_params.reset(token)
        if isinstance(result, (types.CallToolResult, types.InputRequiredResult)):  # type: ignore[attr-defined,unused-ignore]
            return result
        return types.CallToolResult.model_validate({"content": list(result), "isError": False})

    ll.add_request_handler("tools/call", types.CallToolRequestParams, handle)


def _read_resource_adapter(ll: Any, func: Callable[..., Awaitable[Any]]) -> None:
    from mcp import types

    async def handle(ctx: Any, params: Any) -> Any:
        contents = await _in_context(ctx, func(params.uri))
        items: list[Any] = []
        for item in contents:
            # Wire (camelCase) names: model_validate takes them on either SDK
            data: dict[str, Any] = {"uri": params.uri}
            if getattr(item, "meta", None) is not None:
                data["_meta"] = item.meta
            if isinstance(item.content, bytes):
                data["blob"] = base64.b64encode(item.content).decode()
                data["mimeType"] = item.mime_type or "application/octet-stream"
                items.append(types.BlobResourceContents.model_validate(data))
            else:
                data["text"] = item.content
                data["mimeType"] = item.mime_type or "text/plain"
                items.append(types.TextResourceContents.model_validate(data))
        return types.ReadResourceResult(contents=items)

    ll.add_request_handler("resources/read", types.ReadResourceRequestParams, handle)


def _get_prompt_adapter(ll: Any, func: Callable[..., Awaitable[Any]]) -> None:
    from mcp import types

    async def handle(ctx: Any, params: Any) -> Any:
        return await _in_context(ctx, func(params.name, params.arguments))

    ll.add_request_handler("prompts/get", types.GetPromptRequestParams, handle)


_V2_ADAPTERS: dict[str, Callable[[Any, Any], None]] = {
    "list_tools": _list_adapter("tools/list", "ListToolsResult", "tools"),
    "list_resources": _list_adapter("resources/list", "ListResourcesResult", "resources"),
    "list_resource_templates": _list_adapter(
        "resources/templates/list", "ListResourceTemplatesResult", "resource_templates"
    ),
    "list_prompts": _list_adapter("prompts/list", "ListPromptsResult", "prompts"),
    "call_tool": _call_tool_adapter,
    "read_resource": _read_resource_adapter,
    "get_prompt": _get_prompt_adapter,
}
