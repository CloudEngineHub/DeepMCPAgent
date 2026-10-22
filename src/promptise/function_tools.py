"""Native tools: give an agent a Python function as a tool.

Decorate a function with :func:`tool` (or pass it to
``build_agent(extra_tools=[...])`` as it is) and the agent can call it::

    from typing import Annotated

    from pydantic import Field

    from promptise import CallerContext, ToolError, build_agent, tool


    @tool
    async def get_order(order_id: str) -> dict:
        \"\"\"Look up an order by its ID.

        Args:
            order_id: The order ID, for example "A-1001".
        \"\"\"
        order = await db.orders.get(order_id)
        if order is None:
            raise ToolError(f"No order {order_id}.")
        return order


    @tool(name="refund_order", timeout=10)
    def refund(
        order_id: str,
        amount: Annotated[float, Field(gt=0, description="Amount to refund.")],
        caller: CallerContext,
    ) -> str:
        \"\"\"Refund part of an order.\"\"\"
        ...


    agent = await build_agent(model="openai:gpt-5-mini", extra_tools=[get_order, refund])

The tool's name is the function's name, its description the docstring
without the parameter section, and its parameters come from the type hints:
the same schema ``@server.tool()`` on a Promptise MCP server builds from the
same function, made by the same code.  Arguments are validated before the
function runs; invalid ones go back to the model as a tool error that says
what to correct.

A decorated function is a LangChain ``BaseTool`` (:class:`FunctionTool`),
so approval policies, guardrails, tracing, events, tool optimization and
cross-agent setups treat it like any other tool, and it stays callable like
the function it was made from.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import types
from collections.abc import Callable, Iterable, Mapping
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints, overload

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolArg, ToolException
from langchain_core.tools.base import ArgsSchema
from pydantic import BaseModel, PrivateAttr, ValidationError
from pydantic_core import to_jsonable_python

from .agent import CallerContext, get_current_caller
from .mcp.server._decorators import _docstring_without_params, _parse_param_docs
from .mcp.server._errors import MCPError, ToolError
from .mcp.server._middleware import tool_timeout_error
from .mcp.server._validation import _preparse_json_strings, build_input_model
from .tools import ToolArgumentError, drop_disallowed_nulls

__all__ = ["FunctionTool", "FunctionToolError", "ToolError", "tool"]

# Names the providers accept for a function (OpenAI: letters, digits, ``_``
# and ``-``, at most 64; others allow a little more).
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class FunctionToolError(ToolException):
    """A native tool reported a failure the model should see.

    Raised by a :class:`FunctionTool` when its function raises
    :class:`~promptise.ToolError` (or another
    :class:`~promptise.mcp.server.MCPError`) or runs past the tool's
    ``timeout``.  The agent shows the model :attr:`text`, the same error
    envelope a Promptise MCP server returns for that error
    (``{"error": {"code": ..., "message": ..., "retryable": ...}}``), and
    callbacks receive ``on_tool_error``, so the call counts as failed in
    observability, events and adaptive strategy.

    Attributes:
        tool_name: The tool that failed.
        code: The error's code (``"TOOL_ERROR"``, ``"TIMEOUT"`` ...).
        message: The error's message.
        retryable: Whether retrying the same call might succeed.
        text: What the model sees: the JSON error envelope.
        error: The :class:`~promptise.mcp.server.MCPError` behind it.
    """

    def __init__(self, tool_name: str, error: MCPError) -> None:
        self.tool_name = tool_name
        self.error = error
        self.code = error.code
        self.message = str(error)
        self.retryable = error.retryable
        self.text = error.to_text()
        super().__init__(f"{self.code}: {self.message}")


class _JSONText(str):
    """A tool result serialised to JSON text (the function returned data).

    The model reads the text; a code-action program decodes it back into the
    data, as it gets the value a LangChain tool returns.
    """

    __slots__ = ()


def _serialize_result(value: Any) -> Any:
    """What the model reads for a function's return value.

    A string as it is, ``None`` as an empty result, LangChain tool output
    (a ``ToolMessage``) unchanged, and anything else as JSON: dicts, lists,
    numbers, Pydantic models, dataclasses, enums, dates (non-ASCII text is
    kept as is).  A value JSON cannot hold is sent as its ``str()``.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, ToolMessage):
        return value
    try:
        data = to_jsonable_python(value, fallback=str)
        return _JSONText(json.dumps(data, ensure_ascii=False))
    except (TypeError, ValueError):
        return str(value)


# ------------------------------------------------------------------
# Parameters the agent fills in (never in the schema)
# ------------------------------------------------------------------


def _union_members(annotation: Any) -> tuple[Any, ...]:
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if get_origin(annotation) in (Union, types.UnionType):
        return get_args(annotation)
    return (annotation,)


def _caller_annotation(annotation: Any) -> tuple[bool, bool]:
    """``(is_caller, accepts_none)`` for a parameter annotated *annotation*.

    ``CallerContext``, ``CallerContext | None`` and ``Optional[CallerContext]``
    are the caller; so is an annotation string naming it, for a module with
    ``from __future__ import annotations`` whose hints can't be resolved.
    """
    if isinstance(annotation, str):
        text = annotation.replace(" ", "")
        if re.fullmatch(r"(?:\w+\.)*CallerContext", text):
            return True, False
        if re.fullmatch(
            r"(?:Optional\[(?:\w+\.)*CallerContext\]|(?:\w+\.)*CallerContext\|None"
            r"|None\|(?:\w+\.)*CallerContext)",
            text,
        ):
            return True, True
        return False, False
    members = _union_members(annotation)
    if CallerContext not in members:
        return False, False
    others = [m for m in members if m is not CallerContext]
    return others in ([], [type(None)]), bool(others)


def _is_langchain_injected(annotation: Any) -> bool:
    """Whether *annotation* asks LangChain to inject the argument
    (``RunnableConfig``, ``Annotated[..., InjectedToolArg]`` and its kinds)."""
    if isinstance(annotation, str):
        return bool(re.search(r"\b(RunnableConfig|Injected\w*)\b", annotation))
    for member in _union_members(annotation):
        if member is RunnableConfig:
            return True
    if get_origin(annotation) is Annotated:
        for meta in annotation.__metadata__:
            if isinstance(meta, InjectedToolArg) or (
                isinstance(meta, type) and issubclass(meta, InjectedToolArg)
            ):
                return True
    return False


def _resolved_hints(func: Callable[..., Any]) -> dict[str, Any]:
    try:
        return get_type_hints(func, include_extras=True)
    except Exception:
        return {}  # unresolvable hints: the signature's raw annotations are used


# ------------------------------------------------------------------
# The tool
# ------------------------------------------------------------------


class FunctionTool(BaseTool):
    """A tool made from a Python function by :func:`tool`.

    A LangChain ``BaseTool``: pass it to ``build_agent(extra_tools=[...])``
    and approval policies, guardrails (``scan_tool_results``), tracing,
    events and tool optimization treat it like any other tool.

    **Calling the tool directly** runs the function as it was written:
    ``await get_order("A-1001")`` for an ``async def``, ``refund(...)`` for a
    plain ``def``, with no validation and no injected caller.  Use it in
    unit tests.  ``await get_order.ainvoke({"order_id": "A-1001"})`` runs it
    the way the agent does: validated, with the caller injected, and returns
    what the model reads.

    **When the agent calls it**, the arguments are validated against
    :attr:`args_schema` first (with defaults filled in, and nested objects
    the model sent as JSON text decoded); invalid ones are answered with a
    :class:`~promptise.tools.ToolArgumentError` the model can correct.  A
    synchronous function runs in a worker thread, so it never blocks the
    event loop.  A ``CallerContext`` parameter receives the caller of the
    current ``agent.ainvoke(..., caller=...)`` (an empty ``CallerContext()``
    when there is none, or ``None`` when the parameter is
    ``CallerContext | None``).

    **Results** go to the model as text: a string as it is, ``None`` as an
    empty result, anything else as JSON (dicts, lists, numbers, Pydantic
    models, dataclasses ...).

    **Errors:** raise :class:`~promptise.ToolError` to give the model an
    error it can act on: it sees the same ``{"error": {"code", "message",
    ...}}`` envelope a Promptise MCP server sends (raised to callbacks as
    :class:`FunctionToolError`).  Any other exception is a tool that failed:
    the model sees ``Error: <type>: <message>``, as for every tool.  With
    :attr:`timeout`, a call that runs longer is answered with a retryable
    ``TIMEOUT`` error; a synchronous function's thread can't be stopped, so
    it finishes in the background and its result is dropped.

    Attributes:
        timeout: Seconds a call may run, or ``None`` for no limit.
    """

    args_schema: type[BaseModel]  # type: ignore[assignment]
    timeout: float | None = None

    _func: Callable[..., Any] = PrivateAttr()
    _is_async: bool = PrivateAttr(default=False)
    _signature: inspect.Signature = PrivateAttr()
    _caller_params: dict[str, bool] = PrivateAttr(default_factory=dict)

    @classmethod
    def from_function(
        cls,
        func: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        timeout: float | None = None,
        read_only: bool = False,
        idempotent: bool = False,
    ) -> FunctionTool:
        """Make a tool from *func* (what :func:`tool` does).

        Args:
            func: A function, ``async`` or not, or another callable.
            name: The tool's name; defaults to the function's ``__name__``.
            description: What the model is told about the tool; defaults to
                the docstring without its parameter section (``Args:``,
                NumPy ``Parameters``, Sphinx ``:param:``), or the name when
                there is no docstring.
            timeout: Seconds a call may run before it is answered with a
                ``TIMEOUT`` error; ``None`` for no limit.
            read_only: The tool only reads (MCP ``readOnlyHint``).
            idempotent: Calling the tool again with the same arguments has
                no further effect (MCP ``idempotentHint``).

        Raises:
            TypeError: *func* is not a callable that can be a tool (a class,
                a generator function), takes an argument LangChain injects
                (``RunnableConfig``, ``InjectedToolArg``), or has a parameter
                type Pydantic can't build a schema for.
            ValueError: An invalid name or timeout.
        """
        if isinstance(func, BaseTool):
            raise TypeError(f"{func.name!r} is already a tool")
        if not callable(func) or isinstance(func, type):
            raise TypeError(f"tool() needs a function, got {type(func).__name__}")
        if inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func):
            raise TypeError(
                f"{getattr(func, '__name__', func)!r} is a generator function; a tool "
                "returns its result"
            )
        tool_name = name or getattr(func, "__name__", None)
        if not tool_name:
            raise ValueError(f"{func!r} has no __name__: pass tool(name=...)")
        if not _TOOL_NAME.match(tool_name):
            raise ValueError(
                f"Invalid tool name {tool_name!r}: use letters, digits, '_' and '-' "
                "(pass tool(name=...))"
            )
        if timeout is not None and not timeout > 0:
            raise ValueError(f"timeout must be a positive number of seconds, got {timeout!r}")

        signature = inspect.signature(func)
        hints = _resolved_hints(func)
        caller_params: dict[str, bool] = {}
        excluded: set[str] = set()
        for pname, param in signature.parameters.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                excluded.add(pname)
                continue
            annotation = hints.get(pname, param.annotation)
            is_caller, accepts_none = _caller_annotation(annotation)
            if is_caller:
                caller_params[pname] = accepts_none
                excluded.add(pname)
            elif _is_langchain_injected(annotation):
                raise TypeError(
                    f"Parameter {pname!r} of tool {tool_name!r} is an argument LangChain "
                    "injects, which promptise.tool does not support. Take a "
                    "CallerContext parameter for the caller, or build this tool with "
                    "langchain_core.tools.tool."
                )

        doc = inspect.getdoc(func) or ""
        try:
            args_schema, _ = build_input_model(
                func, exclude=excluded, param_docs=_parse_param_docs(doc)
            )
        except Exception as exc:
            raise TypeError(
                f"Can't build the parameter schema of tool {tool_name!r}: {exc}"
            ) from exc

        if description is None:
            description = _docstring_without_params(doc) or tool_name
        # An object whose __call__ is async counts as an async function.
        is_async = inspect.iscoroutinefunction(func) or (
            not inspect.isroutine(func) and inspect.iscoroutinefunction(type(func).__call__)
        )

        # Flat MCP annotation keys, where the agent reads them for MCP tools
        # (semantic cache write detection, approval, timeout errors).
        hints = {"readOnlyHint": True} if read_only else {}
        if idempotent or read_only:
            hints["idempotentHint"] = True
        made = cls(
            name=tool_name,
            description=description,
            args_schema=args_schema,
            timeout=timeout,
            metadata=hints or None,
        )
        made._func = func
        made._is_async = is_async
        made._signature = signature
        made._caller_params = caller_params
        return made

    # -- the function ------------------------------------------------

    @property
    def func(self) -> Callable[..., Any]:
        """The function the tool was made from."""
        return self._func

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Call the function as written (no validation, no injected caller)."""
        return self._func(*args, **kwargs)

    @property
    def tool_call_schema(self) -> ArgsSchema:
        """The schema the model is offered: :attr:`args_schema` itself.

        It has no injected fields to hide (the caller is not a field), and
        LangChain's default rebuild would drop field settings.
        """
        return self.args_schema

    # -- arguments -----------------------------------------------------

    def _validated(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """*arguments* validated, with defaults: what the function gets.

        Nested objects sent as JSON text are decoded, and an explicit
        ``null`` for an optional parameter that can't be null counts as not
        given (as for MCP tools).

        Raises:
            ToolArgumentError: The arguments don't match the schema.
        """
        schema = self.args_schema
        cleaned = drop_disallowed_nulls(
            schema, _preparse_json_strings(dict(arguments), schema), tool_name=self.name
        )
        try:
            instance = schema.model_validate(cleaned)
        except ValidationError as exc:
            raise ToolArgumentError.from_validation_error(self.name, exc) from exc
        # getattr keeps nested models, enums and dates as the function's types.
        return {field: getattr(instance, field) for field in type(instance).model_fields}

    def _to_args_and_kwargs(
        self, tool_input: str | dict[str, Any], tool_call_id: str | None
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Check a call's arguments (raising :class:`ToolArgumentError`) and
        pass them on as given; :meth:`_arun` validates them into the
        function's types once."""
        if isinstance(tool_input, str):
            # LangChain's single-input form: the text is the first parameter.
            fields = list(self.args_schema.model_fields)
            tool_input = {fields[0]: tool_input} if fields else {}
        self._validated(tool_input)
        return (), dict(tool_input)

    def _call_arguments(self, values: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        """Positional and keyword arguments for the function, caller injected."""
        caller = get_current_caller() if self._caller_params else None
        args: list[Any] = []
        kwargs: dict[str, Any] = {}
        for pname, param in self._signature.parameters.items():
            if pname in self._caller_params:
                accepts_none = self._caller_params[pname]
                value: Any = caller if caller is not None or accepts_none else CallerContext()
            elif pname in values:
                value = values[pname]
            else:
                continue
            if param.kind is param.POSITIONAL_ONLY:
                args.append(value)
            else:
                kwargs[pname] = value
        return args, kwargs

    # -- running -------------------------------------------------------

    async def _call(self, values: dict[str, Any]) -> Any:
        args, kwargs = self._call_arguments(values)
        if self._is_async:
            result = await self._func(*args, **kwargs)
        else:
            # A thread copies the context, so get_current_caller() works in it.
            result = await asyncio.to_thread(self._func, *args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _call_bounded(self, values: dict[str, Any]) -> Any:
        if self.timeout is None:
            return await self._call(values)
        task = asyncio.ensure_future(self._call(values))
        try:
            done, _ = await asyncio.wait({task}, timeout=self.timeout)
        except BaseException:
            task.cancel()
            raise
        if not done:
            task.cancel()
            # A result or error that arrives after the timeout is dropped.
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            hints = self.metadata or {}
            raise tool_timeout_error(
                self.name,
                self.timeout,
                read_only=bool(hints.get("readOnlyHint")),
                idempotent=bool(hints.get("idempotentHint")),
                # A thread can't be stopped: a sync function keeps running.
                still_running=not self._is_async,
            )
        return task.result()

    async def _arun(self, **kwargs: Any) -> Any:
        values = self._validated(kwargs)
        try:
            result = await self._call_bounded(values)
        except MCPError as exc:
            raise FunctionToolError(self.name, exc) from exc
        return _serialize_result(result)

    def _run(self, **kwargs: Any) -> Any:
        if self._is_async or self.timeout is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(self._arun(**kwargs))
            raise RuntimeError(
                f"Tool {self.name!r} is async or has a timeout: call it with "
                "`await tool.ainvoke(...)` inside a running event loop"
            )
        args, call_kwargs = self._call_arguments(self._validated(kwargs))
        try:
            result = self._func(*args, **call_kwargs)
        except MCPError as exc:
            raise FunctionToolError(self.name, exc) from exc
        return _serialize_result(result)


# ------------------------------------------------------------------
# The decorator
# ------------------------------------------------------------------


@overload
def tool(func: Callable[..., Any], /) -> FunctionTool: ...


@overload
def tool(
    name: str | None = None,
    /,
    *,
    description: str | None = None,
    timeout: float | None = None,
    read_only: bool = False,
    idempotent: bool = False,
) -> Callable[[Callable[..., Any]], FunctionTool]: ...


@overload
def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    timeout: float | None = None,
    read_only: bool = False,
    idempotent: bool = False,
) -> Callable[[Callable[..., Any]], FunctionTool]: ...


def tool(
    func: Any = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    timeout: float | None = None,
    read_only: bool = False,
    idempotent: bool = False,
) -> Any:
    """Make a Python function a tool an agent can call.

    Works bare (``@tool``), with options (``@tool(name=..., description=...,
    timeout=...)``) and with the name as the only argument
    (``@tool("get_order")``), on ``async def`` and plain ``def`` functions.
    The result is a :class:`FunctionTool`; see it for how calls, results
    and errors work.

    Example::

        from promptise import ToolError, tool

        @tool
        async def get_order(order_id: str) -> dict:
            \"\"\"Look up an order by its ID.

            Args:
                order_id: The order ID, for example "A-1001".
            \"\"\"
            order = await db.orders.get(order_id)
            if order is None:
                raise ToolError(f"No order {order_id}.")
            return order

    The tool's name is the function's name.  Its description is the
    docstring without the parameter section, which describes the
    parameters instead (Google ``Args:``, NumPy ``Parameters`` or Sphinx
    ``:param:``).  ``Annotated[T, Field(description=...)]`` or
    ``Annotated[T, "text"]`` describe a parameter too, and win over the
    docstring.  Type hints, defaults and ``Field`` constraints make the
    parameter schema, validated before every call.

    Args:
        func: The function (when used bare), or the tool's name
            (``@tool("get_order")``).
        name: The tool's name; defaults to the function's name.
        description: The tool's description; defaults to the docstring
            without its parameter section.
        timeout: Seconds a call may run before the model gets a ``TIMEOUT``
            error; ``None`` (default) for no limit.  The error says the
            call's outcome is unknown, and is retryable only for a tool
            marked ``read_only`` or ``idempotent``.
        read_only: The tool only reads data (MCP ``readOnlyHint``): safe to
            call again, and its answers may be cached.
        idempotent: Calling it again with the same arguments has no further
            effect (MCP ``idempotentHint``): safe to call again.

    Returns:
        A :class:`FunctionTool`, or a decorator that makes one.
    """
    if isinstance(func, str):
        if name is not None:
            raise TypeError("tool() got the name twice")
        name, func = func, None
    if func is None:

        def decorate(f: Callable[..., Any]) -> FunctionTool:
            return FunctionTool.from_function(
                f,
                name=name,
                description=description,
                timeout=timeout,
                read_only=read_only,
                idempotent=idempotent,
            )

        return decorate
    return FunctionTool.from_function(
        func,
        name=name,
        description=description,
        timeout=timeout,
        read_only=read_only,
        idempotent=idempotent,
    )


def as_tools(items: Iterable[Any] | None, *, argument: str = "extra_tools") -> list[BaseTool]:
    """*items* as tools: LangChain tools as they are, functions made tools with :func:`tool`.

    Raises:
        TypeError: *items* is not a list, or holds something that is neither
            a ``BaseTool`` nor a function.
    """
    if items is None:
        return []
    if isinstance(items, (BaseTool, str, bytes, Mapping)) or not isinstance(items, Iterable):
        raise TypeError(
            f"{argument} must be a list of tools or functions, got {type(items).__name__}"
            + (f": pass {argument}=[{items.name}]" if isinstance(items, BaseTool) else "")
        )
    tools: list[BaseTool] = []
    for index, item in enumerate(items):
        if isinstance(item, BaseTool):
            tools.append(item)
        elif inspect.isfunction(item) or inspect.ismethod(item):
            tools.append(FunctionTool.from_function(item))
        else:
            raise TypeError(
                f"{argument}[{index}] must be a function or a LangChain BaseTool "
                f"(for example one made with promptise.tool), got {type(item).__name__}"
            )
    return tools
