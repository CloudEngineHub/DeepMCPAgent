"""Native tools: ``promptise.tool`` makes a Python function a tool.

Covers the decorator forms, the name / description / parameter schema built
from the signature and docstring, argument validation, ``CallerContext``
injection, ``ToolError`` / other errors / timeouts, how results are sent to
the model, calling the tool like the function, and schema parity with
LangChain's ``@tool`` and with ``@server.tool()`` on a Promptise MCP server.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import enum
import json
import threading
import time
from collections.abc import Iterator
from typing import Annotated, Any, Literal, Optional

import pytest
from langchain_core.tools import BaseTool, InjectedToolArg, ToolException
from langchain_core.tools import tool as lc_tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field

import promptise
from promptise import CallerContext, ToolError, tool
from promptise.agent import _caller_ctx_var
from promptise.function_tools import FunctionTool, FunctionToolError, as_tools
from promptise.mcp._compat import field as mcp_field
from promptise.mcp.server import MCPServer, RateLimitError, TestClient
from promptise.tools import ToolArgumentError

# ---------------------------------------------------------------------------
# Helpers and shared types
# ---------------------------------------------------------------------------


class Color(str, enum.Enum):
    RED = "red"
    BLUE = "blue"


class Address(BaseModel):
    """A postal address."""

    city: str
    zip: str | None = None


class Line(BaseModel):
    sku: str
    qty: int = Field(ge=1)


@dataclasses.dataclass
class Receipt:
    order_id: str
    total: float


def _offered(t: BaseTool) -> dict[str, Any]:
    """What a chat model is offered for *t* (the OpenAI function format)."""
    return convert_to_openai_tool(t)["function"]


def _params(t: BaseTool) -> dict[str, Any]:
    return _offered(t)["parameters"]


def _without_titles(node: Any) -> Any:
    """A JSON schema without its ``title`` keywords (models are not shown them)."""
    if isinstance(node, list):
        return [_without_titles(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _without_titles(sub) for name, sub in value.items()}
        elif key != "title":
            out[key] = _without_titles(value)
    return out


def _call(name: str, args: dict[str, Any], call_id: str = "call-1") -> dict[str, Any]:
    return {"type": "tool_call", "name": name, "args": args, "id": call_id}


@contextlib.contextmanager
def _as_caller(caller: CallerContext | None) -> Iterator[None]:
    """Run code as if inside ``agent.ainvoke(..., caller=...)``."""
    token = _caller_ctx_var.set(caller)
    try:
        yield
    finally:
        _caller_ctx_var.reset(token)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


class TestExports:
    def test_tool_and_tool_error_are_top_level(self):
        assert promptise.tool is tool
        assert "tool" in promptise.__all__
        assert "ToolError" in promptise.__all__

    def test_tool_error_is_the_mcp_server_class(self):
        from promptise.mcp.server import ToolError as ServerToolError

        assert ToolError is ServerToolError

    def test_existing_exports_still_work(self):
        from promptise import ToolInfo
        from promptise.tools import ToolInfo as ToolsToolInfo

        assert ToolInfo is ToolsToolInfo


# ---------------------------------------------------------------------------
# Decorator forms
# ---------------------------------------------------------------------------


class TestDecorator:
    def test_bare_on_async_function(self):
        @tool
        async def get_order(order_id: str) -> dict:
            """Look up an order."""
            return {"id": order_id}

        assert isinstance(get_order, FunctionTool)
        assert isinstance(get_order, BaseTool)
        assert get_order.name == "get_order"
        assert get_order.description == "Look up an order."

    def test_bare_on_sync_function(self):
        @tool
        def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        assert isinstance(add, FunctionTool)
        assert add.invoke({"a": 1, "b": 2}) == "3"

    def test_with_options(self):
        @tool(name="refund_order", description="Refund an order.", timeout=10)
        def refund(order_id: str) -> str:
            """Ignored docstring."""
            return order_id

        assert refund.name == "refund_order"
        assert refund.description == "Refund an order."
        assert refund.timeout == 10

    def test_name_as_only_argument(self):
        @tool("list_employees")
        def employees() -> list:
            """All employees."""
            return []

        assert employees.name == "list_employees"

    def test_empty_parentheses(self):
        @tool()
        def ping() -> str:
            """Answer pong."""
            return "pong"

        assert ping.name == "ping"

    def test_called_on_a_function(self):
        def ping() -> str:
            """Answer pong."""
            return "pong"

        assert tool(ping).name == "ping"
        assert FunctionTool.from_function(ping, name="pong").name == "pong"

    def test_name_twice_is_an_error(self):
        with pytest.raises(TypeError, match="name twice"):
            tool("a", name="b")

    @pytest.mark.parametrize("timeout", [0, -1])
    def test_timeout_must_be_positive(self, timeout):
        with pytest.raises(ValueError, match="timeout"):

            @tool(timeout=timeout)
            def f() -> str:
                """F."""
                return ""

    def test_invalid_name(self):
        with pytest.raises(ValueError, match="Invalid tool name"):
            tool(lambda x: x)
        with pytest.raises(ValueError, match="Invalid tool name"):
            tool(name="get order")(lambda: None)

    def test_rejects_non_functions(self):
        class Thing:
            pass

        with pytest.raises(TypeError, match="needs a function"):
            tool(Thing)
        with pytest.raises(TypeError, match="needs a function"):
            tool(42)  # type: ignore[call-overload]

    def test_rejects_a_tool(self):
        @tool
        def ping() -> str:
            """Pong."""
            return "pong"

        with pytest.raises(TypeError, match="already a tool"):
            tool(ping)

    def test_rejects_generator_functions(self):
        def numbers():
            """Numbers."""
            yield 1

        async def stream():
            """Stream."""
            yield 1

        for func in (numbers, stream):
            with pytest.raises(TypeError, match="generator"):
                tool(func)

    def test_rejects_langchain_injected_arguments(self):
        from langchain_core.runnables import RunnableConfig

        def with_config(query: str, config: RunnableConfig) -> str:
            """Search."""
            return query

        def with_injected(query: str, user: Annotated[str, InjectedToolArg]) -> str:
            """Search."""
            return query

        for func in (with_config, with_injected):
            with pytest.raises(TypeError, match="CallerContext"):
                tool(func)

    def test_unsupported_parameter_type_names_the_tool(self):
        class Opaque:
            pass

        def f(x: Opaque) -> str:
            """F."""
            return ""

        with pytest.raises(TypeError, match="parameter schema of tool 'f'"):
            tool(f)


# ---------------------------------------------------------------------------
# Name and description
# ---------------------------------------------------------------------------


class TestDescription:
    def test_google_args_section_is_removed(self):
        @tool
        def get_order(order_id: str) -> dict:
            """Look up an order by its ID.

            Returns the order with its status and lines.

            Args:
                order_id: The order ID, for example "A-1001".

            Returns:
                The order as a dict.
            """
            return {}

        assert get_order.description == (
            "Look up an order by its ID.\n\n"
            "Returns the order with its status and lines.\n\n"
            "Returns:\n    The order as a dict."
        )

    def test_numpy_parameters_section_is_removed(self):
        @tool
        def get_order(order_id: str, verbose: bool = False) -> dict:
            """Look up an order by its ID.

            Parameters
            ----------
            order_id : str
                The order ID.
            verbose : bool, optional
                Include the lines.

            Returns
            -------
            dict
                The order.
            """
            return {}

        assert get_order.description == (
            "Look up an order by its ID.\n\nReturns\n-------\ndict\n    The order."
        )

    def test_sphinx_param_fields_are_removed(self):
        @tool
        def get_order(order_id: str) -> dict:
            """Look up an order by its ID.

            :param order_id: The order ID,
                for example "A-1001".
            :type order_id: str
            :returns: The order.
            """
            return {}

        assert get_order.description == "Look up an order by its ID.\n\n:returns: The order."

    def test_summary_without_blank_line_before_args(self):
        @tool
        def ping(host: str) -> str:
            """Ping a host.
            Args:
                host: The host.
            """
            return host

        assert ping.description == "Ping a host."

    def test_no_docstring_uses_the_name(self):
        def ping(host: str) -> str:
            return host

        assert tool(ping).description == "ping"

    def test_explicit_description_wins(self):
        @tool(description="Check that a host answers.")
        def ping(host: str) -> str:
            """Ping a host."""
            return host

        assert ping.description == "Check that a host answers."


# ---------------------------------------------------------------------------
# Parameter descriptions
# ---------------------------------------------------------------------------


class TestParameterDescriptions:
    def test_google_args(self):
        @tool
        def search(query: str, limit: int = 10) -> list:
            """Search the catalog.

            Args:
                query: What to look for. Product names
                    and SKUs both work.
                limit (int): At most this many results.
            """
            return []

        props = _params(search)["properties"]
        assert (
            props["query"]["description"] == "What to look for. Product names and SKUs both work."
        )
        assert props["limit"]["description"] == "At most this many results."

    def test_numpy_parameters(self):
        @tool
        def search(query: str, limit: int = 10, offset: int = 0) -> list:
            """Search the catalog.

            Parameters
            ----------
            query : str
                What to look for.
            limit, offset : int, optional
                Paging: page size and
                start.
            """
            return []

        props = _params(search)["properties"]
        assert props["query"]["description"] == "What to look for."
        assert props["limit"]["description"] == "Paging: page size and start."
        assert props["offset"]["description"] == "Paging: page size and start."

    def test_sphinx_params(self):
        @tool
        def search(query: str) -> list:
            """Search the catalog.

            :param query: What to look for.
            """
            return []

        assert _params(search)["properties"]["query"]["description"] == "What to look for."

    def test_annotated_field_and_string(self):
        @tool
        def refund(
            order_id: Annotated[str, Field(description="The order to refund.")],
            reason: Annotated[str, "Why the customer wants a refund."] = "",
        ) -> str:
            """Refund an order."""
            return order_id

        props = _params(refund)["properties"]
        assert props["order_id"]["description"] == "The order to refund."
        assert props["reason"]["description"] == "Why the customer wants a refund."

    def test_annotation_wins_over_docstring(self):
        @tool
        def refund(
            order_id: Annotated[str, Field(description="From Field.")],
            reason: Annotated[str, "From Annotated."],
            note: str,
        ) -> str:
            """Refund an order.

            Args:
                order_id: From the docstring.
                reason: From the docstring.
                note: From the docstring.
            """
            return order_id

        props = _params(refund)["properties"]
        assert props["order_id"]["description"] == "From Field."
        assert props["reason"]["description"] == "From Annotated."
        assert props["note"]["description"] == "From the docstring."

    def test_undocumented_parameter_has_no_description(self):
        @tool
        def ping(host: str) -> str:
            """Ping a host."""
            return host

        assert "description" not in _params(ping)["properties"]["host"]


# ---------------------------------------------------------------------------
# Schema from type hints
# ---------------------------------------------------------------------------


@tool
def kitchen_sink(
    text: str,
    count: int,
    ratio: float,
    flag: bool,
    mode: Literal["fast", "slow"],
    color: Color,
    tags: list[str],
    weights: dict[str, float],
    address: Address,
    lines: list[Line],
    note: Optional[str] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 10,
    code: Annotated[str, Field(pattern=r"^[A-Z]{3}$", max_length=3)] = "ABC",
    since: datetime.date | None = None,
    page: int = Field(default=1, ge=1, description="Page number."),
) -> dict:
    """Every kind of parameter."""
    return {
        "text": text,
        "count": count,
        "color": color,
        "address": address,
        "lines": lines,
        "limit": limit,
        "page": page,
        "since": since,
    }


class TestSchema:
    def test_types_required_and_defaults(self):
        params = _params(kitchen_sink)
        props = params["properties"]
        assert params["type"] == "object"
        assert set(params["required"]) == {
            "text",
            "count",
            "ratio",
            "flag",
            "mode",
            "color",
            "tags",
            "weights",
            "address",
            "lines",
        }
        assert props["text"] == {"type": "string"}
        assert props["count"] == {"type": "integer"}
        assert props["ratio"] == {"type": "number"}
        assert props["flag"] == {"type": "boolean"}
        assert props["mode"] == {"enum": ["fast", "slow"], "type": "string"}
        assert props["color"] == {"enum": ["red", "blue"], "type": "string"}
        assert props["tags"] == {"items": {"type": "string"}, "type": "array"}
        assert props["weights"] == {"additionalProperties": {"type": "number"}, "type": "object"}
        assert props["note"] == {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
        assert props["since"]["anyOf"][0] == {"format": "date", "type": "string"}

    def test_nested_models_are_inlined(self):
        props = _params(kitchen_sink)["properties"]
        assert props["address"]["type"] == "object"
        assert props["address"]["required"] == ["city"]
        assert props["address"]["properties"]["city"] == {"type": "string"}
        assert props["lines"]["items"]["properties"]["qty"] == {"minimum": 1, "type": "integer"}
        assert "$ref" not in json.dumps(_params(kitchen_sink))
        assert "$defs" not in json.dumps(_params(kitchen_sink))

    def test_constraints(self):
        props = _params(kitchen_sink)["properties"]
        assert props["limit"] == {"default": 10, "maximum": 100, "minimum": 1, "type": "integer"}
        assert props["code"]["pattern"] == "^[A-Z]{3}$"
        assert props["code"]["maxLength"] == 3
        assert props["page"] == {
            "default": 1,
            "description": "Page number.",
            "minimum": 1,
            "type": "integer",
        }

    def test_untyped_parameter_is_a_string(self):
        def echo(text):  # noqa: ANN001
            """Echo."""
            return text

        assert _params(tool(echo))["properties"]["text"] == {"type": "string"}

    def test_var_args_are_not_parameters(self):
        @tool
        def search(query: str, *args: Any, **kwargs: Any) -> str:
            """Search."""
            return query

        assert set(_params(search)["properties"]) == {"query"}
        assert search.invoke({"query": "x"}) == "x"

    def test_tool_call_schema_is_the_args_schema(self):
        assert kitchen_sink.tool_call_schema is kitchen_sink.args_schema


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


GOOD_SINK = {
    "text": "t",
    "count": 1,
    "ratio": 0.5,
    "flag": True,
    "mode": "fast",
    "color": "red",
    "tags": ["a"],
    "weights": {"a": 1.0},
    "address": {"city": "Bern"},
    "lines": [{"sku": "X", "qty": 2}],
}


class TestValidation:
    async def test_values_reach_the_function_in_their_types(self):
        seen: dict[str, Any] = {}

        @tool
        def record(address: Address, lines: list[Line], color: Color, when: datetime.date) -> str:
            """Record."""
            seen.update(address=address, lines=lines, color=color, when=when)
            return "ok"

        await record.ainvoke(
            {
                "address": {"city": "Bern"},
                "lines": [{"sku": "X", "qty": 2}],
                "color": "blue",
                "when": "2026-10-21",
            }
        )
        assert seen["address"] == Address(city="Bern")
        assert seen["lines"] == [Line(sku="X", qty=2)]
        assert seen["color"] is Color.BLUE
        assert seen["when"] == datetime.date(2026, 10, 21)

    async def test_defaults_are_filled_in(self):
        result = json.loads(await kitchen_sink.ainvoke(GOOD_SINK))
        assert result["limit"] == 10
        assert result["page"] == 1  # a Field() default, not the FieldInfo
        assert result["since"] is None

    async def test_lax_coercion(self):
        result = json.loads(await kitchen_sink.ainvoke({**GOOD_SINK, "count": "7"}))
        assert result["count"] == 7

    async def test_invalid_arguments_raise_a_message_for_the_model(self):
        with pytest.raises(ToolArgumentError) as info:
            await kitchen_sink.ainvoke({**GOOD_SINK, "limit": 0, "mode": "medium"})
        message = info.value.text
        assert message.startswith("Invalid arguments for tool 'kitchen_sink':")
        assert "- limit: Input should be greater than or equal to 1 (got 0)" in message
        assert "- mode: Input should be 'fast' or 'slow'" in message
        assert message.endswith("Correct the arguments and call the tool again.")

    async def test_missing_required_argument(self):
        args = dict(GOOD_SINK)
        del args["text"]
        with pytest.raises(ToolArgumentError, match="- text: Field required"):
            await kitchen_sink.ainvoke(args)

    async def test_nested_error_has_a_path(self):
        with pytest.raises(ToolArgumentError, match=r"- lines\.0\.qty: Input should be greater"):
            await kitchen_sink.ainvoke({**GOOD_SINK, "lines": [{"sku": "X", "qty": 0}]})

    async def test_function_does_not_run_on_invalid_arguments(self):
        ran = []

        @tool
        def refund(amount: Annotated[float, Field(gt=0)]) -> str:
            """Refund."""
            ran.append(amount)
            return "ok"

        with pytest.raises(ToolArgumentError):
            await refund.ainvoke({"amount": -5})
        assert ran == []

    async def test_null_for_an_optional_non_nullable_parameter_counts_as_not_given(self):
        result = json.loads(await kitchen_sink.ainvoke({**GOOD_SINK, "limit": None}))
        assert result["limit"] == 10

    async def test_nested_object_sent_as_json_text_is_decoded(self):
        result = json.loads(
            await kitchen_sink.ainvoke({**GOOD_SINK, "address": json.dumps({"city": "Basel"})})
        )
        assert result["address"] == {"city": "Basel", "zip": None}

    async def test_validation_error_raised_as_tool_call_is_a_tool_exception(self):
        @tool
        def refund(amount: Annotated[float, Field(gt=0)]) -> str:
            """Refund."""
            return "ok"

        with pytest.raises(ToolException):
            await refund.ainvoke(_call("refund", {"amount": 0}))

    def test_single_string_input_goes_to_the_first_parameter(self):
        @tool
        def shout(text: str, times: int = 1) -> str:
            """Shout."""
            return text.upper() * times

        assert shout.invoke("hey") == "HEY"


# ---------------------------------------------------------------------------
# CallerContext injection
# ---------------------------------------------------------------------------


@tool
async def whoami_async(greeting: str, caller: CallerContext) -> str:
    """Say who is calling."""
    return f"{greeting} {caller.user_id}"


@tool
def whoami_sync(greeting: str, caller: CallerContext) -> str:
    """Say who is calling, from a worker thread."""
    return f"{greeting} {caller.user_id} {threading.current_thread() is threading.main_thread()}"


@tool
def whoami_optional(caller: CallerContext | None = None) -> str:
    """Say whether there is a caller."""
    return "anonymous" if caller is None else str(caller.user_id)


class TestCallerInjection:
    def test_hidden_from_the_schema(self):
        for t in (whoami_async, whoami_sync):
            assert set(_params(t)["properties"]) == {"greeting"}
            assert set(t.args_schema.model_fields) == {"greeting"}
        assert _params(whoami_optional)["properties"] == {}

    async def test_injected_into_an_async_tool(self):
        with _as_caller(CallerContext(user_id="alice")):
            assert await whoami_async.ainvoke({"greeting": "hi"}) == "hi alice"

    async def test_injected_into_a_sync_tool_in_its_thread(self):
        with _as_caller(CallerContext(user_id="bob")):
            assert await whoami_sync.ainvoke({"greeting": "hi"}) == "hi bob False"

    async def test_no_caller_gives_an_empty_context(self):
        assert await whoami_async.ainvoke({"greeting": "hi"}) == "hi None"

    async def test_optional_parameter_gets_none_without_a_caller(self):
        assert await whoami_optional.ainvoke({}) == "anonymous"
        with _as_caller(CallerContext(user_id="carol")):
            assert await whoami_optional.ainvoke({}) == "carol"

    async def test_a_caller_argument_from_the_model_is_ignored(self):
        with _as_caller(CallerContext(user_id="alice")):
            result = await whoami_async.ainvoke(
                {"greeting": "hi", "caller": {"user_id": "mallory"}}
            )
        assert result == "hi alice"

    async def test_unresolvable_string_annotation(self):
        namespace: dict[str, Any] = {}
        exec(
            "from __future__ import annotations\n"
            "def whoami(name: str, caller: CallerContext) -> str:\n"
            "    '''Who.'''\n"
            "    return f'{name}:{caller.user_id}'\n",
            namespace,
        )
        t = tool(namespace["whoami"])
        assert set(_params(t)["properties"]) == {"name"}
        with _as_caller(CallerContext(user_id="dave")):
            assert await t.ainvoke({"name": "x"}) == "x:dave"

    async def test_positional_only_parameters(self):
        @tool
        def tag(order_id: str, /, label: str, caller: CallerContext) -> str:
            """Tag an order."""
            return f"{order_id}:{label}:{caller.user_id}"

        with _as_caller(CallerContext(user_id="erin")):
            assert await tag.ainvoke({"order_id": "A", "label": "vip"}) == "A:vip:erin"

    async def test_bound_method(self):
        class Shop:
            prefix = "shop"

            def order(self, order_id: str, caller: CallerContext) -> str:
                """Look up an order."""
                return f"{self.prefix}:{order_id}:{caller.user_id}"

        t = tool(Shop().order)
        assert set(_params(t)["properties"]) == {"order_id"}
        with _as_caller(CallerContext(user_id="fay")):
            assert await t.ainvoke({"order_id": "A"}) == "shop:A:fay"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@tool
async def find_order(order_id: str) -> dict:
    """Find an order."""
    if order_id == "missing":
        raise ToolError(f"No order {order_id}.")
    if order_id == "busy":
        raise ToolError(
            "The order service is busy.",
            code="UPSTREAM_BUSY",
            retryable=True,
            suggestion="Retry in a minute.",
        )
    if order_id == "limited":
        raise RateLimitError(retry_after=3)
    if order_id == "boom":
        raise ValueError("database exploded")
    if order_id == "lc":
        raise ToolException("plain LangChain tool error")
    return {"id": order_id}


class TestErrors:
    async def test_tool_error_becomes_the_mcp_error_envelope(self):
        with pytest.raises(FunctionToolError) as info:
            await find_order.ainvoke({"order_id": "missing"})
        err = info.value
        assert isinstance(err, ToolException)
        assert err.tool_name == "find_order"
        assert err.code == "TOOL_ERROR"
        assert err.message == "No order missing."
        assert err.retryable is False
        assert json.loads(err.text) == {
            "error": {"code": "TOOL_ERROR", "message": "No order missing.", "retryable": False}
        }
        assert isinstance(err.__cause__, ToolError)

    async def test_tool_error_text_matches_what_an_mcp_server_sends(self):
        server = MCPServer(name="t")

        @server.tool()
        async def find_order(order_id: str) -> dict:
            """Find an order."""
            raise ToolError(f"No order {order_id}.")

        [content] = await TestClient(server).call_tool("find_order", {"order_id": "missing"})
        with pytest.raises(FunctionToolError) as info:
            await globals()["find_order"].ainvoke({"order_id": "missing"})
        assert info.value.text == content.text

    async def test_tool_error_options_are_kept(self):
        with pytest.raises(FunctionToolError) as info:
            await find_order.ainvoke({"order_id": "busy"})
        payload = json.loads(info.value.text)["error"]
        assert payload == {
            "code": "UPSTREAM_BUSY",
            "message": "The order service is busy.",
            "retryable": True,
            "suggestion": "Retry in a minute.",
        }
        assert info.value.retryable is True

    async def test_other_mcp_errors_are_reported_the_same_way(self):
        with pytest.raises(FunctionToolError) as info:
            await find_order.ainvoke({"order_id": "limited"})
        assert info.value.code == "RATE_LIMIT_EXCEEDED"
        assert info.value.retryable is True

    async def test_tool_call_raises_so_callbacks_see_the_error(self):
        with pytest.raises(FunctionToolError):
            await find_order.ainvoke(_call("find_order", {"order_id": "missing"}))

    async def test_other_exceptions_propagate_unchanged(self):
        with pytest.raises(ValueError, match="database exploded"):
            await find_order.ainvoke({"order_id": "boom"})

    async def test_langchain_tool_exception_passes_through(self):
        with pytest.raises(ToolException, match="plain LangChain tool error") as info:
            await find_order.ainvoke({"order_id": "lc"})
        assert not isinstance(info.value, FunctionToolError)

    def test_sync_invoke_reports_tool_error(self):
        @tool
        def find(order_id: str) -> str:
            """Find."""
            raise ToolError("nope")

        with pytest.raises(FunctionToolError, match="TOOL_ERROR: nope"):
            find.invoke({"order_id": "A"})


class TestTimeout:
    async def test_async_tool_times_out(self):
        cancelled = asyncio.Event()

        @tool(timeout=0.1)
        async def slow() -> str:
            """Slow."""
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "late"

        started = time.perf_counter()
        with pytest.raises(FunctionToolError) as info:
            await slow.ainvoke({})
        assert time.perf_counter() - started < 1.5
        assert info.value.code == "TIMEOUT"
        # The call may have done its work before it was stopped.
        assert info.value.retryable is False
        assert (
            info.value.message
            == "Tool 'slow' did not finish within 0.1s, so its outcome is unknown."
        )
        await asyncio.wait_for(cancelled.wait(), 1)  # the function was cancelled

    async def test_sync_tool_times_out_without_blocking(self):
        release = threading.Event()

        @tool(timeout=0.1)
        def stuck() -> str:
            """Stuck."""
            release.wait(5)
            return "late"

        started = time.perf_counter()
        try:
            with pytest.raises(FunctionToolError, match="TIMEOUT") as info:
                await stuck.ainvoke({})
            assert time.perf_counter() - started < 1.5
        finally:
            release.set()
        # A thread can't be stopped: the call keeps running and may complete.
        assert "still running and may complete" in info.value.message
        assert info.value.retryable is False
        assert "Check whether it did" in info.value.error.suggestion

    @pytest.mark.parametrize(
        ("hint", "kind"), [("read_only", "read-only"), ("idempotent", "idempotent")]
    )
    async def test_timeout_of_a_tool_safe_to_repeat_is_retryable(self, hint, kind):
        @tool(timeout=0.05, **{hint: True})
        async def lookup() -> str:
            """Look something up."""
            await asyncio.sleep(5)
            return "late"

        with pytest.raises(FunctionToolError) as info:
            await lookup.ainvoke({})
        assert info.value.retryable is True
        assert f"marked {kind}" in info.value.error.suggestion

    def test_hints_are_mcp_annotations_in_metadata(self):
        @tool(read_only=True)
        def find(q: str) -> str:
            """Find."""
            return q

        @tool(idempotent=True)
        def put(key: str) -> str:
            """Put."""
            return key

        @tool
        def send(to: str) -> str:
            """Send."""
            return to

        assert find.metadata == {"readOnlyHint": True, "idempotentHint": True}
        assert put.metadata == {"idempotentHint": True}
        assert not send.metadata

    async def test_fast_tool_is_not_affected(self):
        @tool(timeout=5)
        async def quick(x: int) -> int:
            """Quick."""
            return x * 2

        assert await quick.ainvoke({"x": 21}) == "42"

    async def test_a_timeout_raised_by_the_function_is_its_own_error(self):
        @tool(timeout=5)
        async def upstream() -> str:
            """Upstream."""
            raise TimeoutError("upstream did not answer")

        with pytest.raises(TimeoutError, match="upstream did not answer"):
            await upstream.ainvoke({})

    def test_sync_invoke_honors_the_timeout(self):
        @tool(timeout=0.1)
        async def slow() -> str:
            """Slow."""
            await asyncio.sleep(5)
            return "late"

        with pytest.raises(FunctionToolError, match="TIMEOUT"):
            slow.invoke({})


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class _Status(enum.Enum):
    OPEN = "open"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain text", "plain text"),
        (None, ""),
        ({"name": "Zürich", "n": 1}, '{"name": "Zürich", "n": 1}'),
        (["a", "b"], '["a", "b"]'),
        ([], "[]"),
        ([{"id": 1}], '[{"id": 1}]'),
        ((1, 2), "[1, 2]"),
        (42, "42"),
        (1.5, "1.5"),
        (True, "true"),
        (Address(city="Bern"), '{"city": "Bern", "zip": null}'),
        (Receipt(order_id="A", total=9.5), '{"order_id": "A", "total": 9.5}'),
        (
            {"when": datetime.date(2026, 10, 21), "status": _Status.OPEN},
            '{"when": "2026-10-21", "status": "open"}',
        ),
        ({"tags": {"x"}}, '{"tags": ["x"]}'),
    ],
)
async def test_results_reach_the_model_as_text(value: Any, expected: str) -> None:
    @tool
    def produce() -> Any:
        """Produce a value."""
        return value

    assert await produce.ainvoke({}) == expected
    message = await produce.ainvoke(_call("produce", {}))
    assert message.content == expected
    assert message.status == "success"


async def test_unserializable_value_is_sent_as_its_str() -> None:
    class Opaque:
        def __str__(self) -> str:
            return "<opaque>"

    @tool
    def produce() -> Any:
        """Produce."""
        return {"thing": Opaque()}

    assert await produce.ainvoke({}) == '{"thing": "<opaque>"}'


# ---------------------------------------------------------------------------
# Calling the tool like the function
# ---------------------------------------------------------------------------


class TestDirectCall:
    async def test_async_tool_is_awaitable_like_the_function(self):
        assert await find_order("A-1001") == {"id": "A-1001"}
        assert find_order.func.__name__ == "find_order"

    def test_sync_tool_returns_the_raw_value(self):
        @tool
        def receipt(order_id: str) -> Receipt:
            """Receipt."""
            return Receipt(order_id=order_id, total=1.0)

        assert receipt("A") == Receipt(order_id="A", total=1.0)
        assert receipt(order_id="B").order_id == "B"

    def test_no_validation_and_no_injection(self):
        @tool
        def echo(value: int, caller: CallerContext) -> tuple:
            """Echo."""
            return value, caller

        marker = CallerContext(user_id="explicit")
        assert echo("not an int", marker) == ("not an int", marker)

    async def test_direct_call_raises_the_function_errors(self):
        with pytest.raises(ToolError, match="No order missing"):
            await find_order("missing")


# ---------------------------------------------------------------------------
# Running sync and async tools
# ---------------------------------------------------------------------------


class TestExecution:
    async def test_sync_tool_does_not_block_the_event_loop(self):
        @tool
        def slow(x: int) -> int:
            """Slow."""
            time.sleep(0.3)
            return x

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        try:
            assert await slow.ainvoke({"x": 1}) == "1"
        finally:
            task.cancel()
        assert ticks >= 10

    async def test_sync_tools_run_concurrently(self):
        @tool
        def slow(x: int) -> int:
            """Slow."""
            time.sleep(0.3)
            return x

        started = time.perf_counter()
        results = await asyncio.gather(*(slow.ainvoke({"x": i}) for i in range(4)))
        assert results == ["0", "1", "2", "3"]
        assert time.perf_counter() - started < 1.0

    def test_sync_invoke_of_an_async_tool_outside_a_loop(self):
        assert find_order.invoke({"order_id": "A"}) == '{"id": "A"}'

    async def test_sync_invoke_of_an_async_tool_inside_a_loop_is_refused(self):
        with pytest.raises(RuntimeError, match="ainvoke"):
            find_order.invoke({"order_id": "A"})


# ---------------------------------------------------------------------------
# extra_tools normalisation
# ---------------------------------------------------------------------------


class TestAsTools:
    def test_functions_become_tools_and_tools_stay(self):
        def plain(a: int) -> int:
            """Plain function."""
            return a

        @lc_tool
        def langchain(a: int) -> int:
            """LangChain tool."""
            return a

        tools = as_tools([plain, find_order, langchain])
        assert isinstance(tools[0], FunctionTool) and tools[0].name == "plain"
        assert tools[1] is find_order
        assert tools[2] is langchain
        assert as_tools(None) == []
        assert as_tools(()) == []

    @pytest.mark.parametrize("bad", [42, "get_order", {"name": "x"}, object()])
    def test_rejects_other_entries(self, bad):
        with pytest.raises(TypeError, match=r"extra_tools\[1\] must be a function or a LangChain"):
            as_tools([find_order, bad])

    def test_rejects_a_class(self):
        with pytest.raises(TypeError, match=r"extra_tools\[0\]"):
            as_tools([Address])

    def test_rejects_a_single_tool(self):
        with pytest.raises(TypeError, match=r"pass extra_tools=\[find_order\]"):
            as_tools(find_order)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Parity with LangChain's @tool
# ---------------------------------------------------------------------------


def lc_simple(order_id: str) -> str:
    """Look up an order."""
    return order_id


def lc_defaults(query: str, limit: int = 10, exact: bool = False, ratio: float = 0.5) -> str:
    """Search orders."""
    return query


def lc_optional(customer: Optional[str] = None, tags: list[str] | None = None) -> str:
    """List customers."""
    return ""


def lc_containers(ids: list[int], meta: dict[str, str], mode: Literal["a", "b"]) -> str:
    """Containers."""
    return ""


def lc_models(address: Address, lines: list[Line], color: Color) -> str:
    """Nested models and enums."""
    return ""


def lc_annotated(
    order_id: Annotated[str, "The order ID."],
    amount: Annotated[float, Field(gt=0, description="Amount.")],
) -> str:
    """Annotated descriptions."""
    return ""


def lc_no_args() -> str:
    """No arguments."""
    return ""


async def lc_async(order_id: str, verbose: bool = False) -> str:
    """An async tool."""
    return order_id


LC_SIMPLE = [lc_simple, lc_defaults, lc_optional, lc_containers, lc_models, lc_annotated]
LC_SIMPLE += [lc_no_args, lc_async]


class TestLangChainParity:
    @pytest.mark.parametrize("func", LC_SIMPLE, ids=lambda f: f.__name__)
    def test_same_name_description_and_parameters(self, func):
        assert _offered(tool(func)) == _offered(lc_tool(func))

    @pytest.mark.parametrize("func", LC_SIMPLE, ids=lambda f: f.__name__)
    def test_same_argument_names(self, func):
        assert tool(func).args.keys() == lc_tool(func).args.keys()

    def test_named_form_matches(self):
        assert _offered(tool("lookup")(lc_simple)) == _offered(lc_tool("lookup")(lc_simple))

    def test_google_docstring_matches_parse_docstring(self):
        def search(query: str, limit: int = 10) -> str:
            """Search the catalog.

            Args:
                query: What to look for.
                limit: At most this many results.
            """
            return query

        assert _offered(tool(search)) == _offered(lc_tool(search, parse_docstring=True))

    async def test_same_results_for_simple_values(self):
        def answer(kind: str) -> Any:
            """Answer."""
            return {"text": "x", "dict": {"a": 1}, "num": 3}[kind]

        native, langchain = tool(answer), lc_tool(answer)
        for kind in ("text", "dict", "num"):
            a = await native.ainvoke(_call("answer", {"kind": kind}))
            b = await langchain.ainvoke(_call("answer", {"kind": kind}))
            assert a.content == b.content


# ---------------------------------------------------------------------------
# Parity with @server.tool() on a Promptise MCP server
# ---------------------------------------------------------------------------


def srv_search(
    query: Annotated[str, Field(min_length=2, description="What to look for.")],
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    color: Color = Color.RED,
    since: datetime.date | None = None,
) -> list:
    """Search the catalog.

    Args:
        query: Ignored, the Field description wins.
        color: Which color.
    """
    return []


def srv_create(address: Address, lines: list[Line], note: Annotated[str, "A note."] = "") -> dict:
    """Create an order.

    Parameters
    ----------
    address : Address
        Where to ship.
    """
    return {}


def srv_simple(order_id: str, verbose: bool = False) -> dict:
    """Look up an order.

    :param order_id: The order ID.
    """
    return {}


SERVER_FUNCS = [srv_search, srv_create, srv_simple, lc_containers, lc_optional, lc_no_args]


async def _server_tool(func: Any) -> Any:
    server = MCPServer(name="parity")
    server.tool()(func)
    [listed] = await TestClient(server).list_tools()
    return server, listed


class TestMCPServerParity:
    @pytest.mark.parametrize("func", SERVER_FUNCS, ids=lambda f: f.__name__)
    async def test_same_parameter_schema(self, func):
        _, listed = await _server_tool(func)
        server_schema = _without_titles(mcp_field(listed, "input_schema"))
        assert _params(tool(func)) == server_schema

    @pytest.mark.parametrize("func", SERVER_FUNCS, ids=lambda f: f.__name__)
    async def test_same_name_and_summary(self, func):
        _, listed = await _server_tool(func)
        native = tool(func)
        assert native.name == listed.name
        # The server sends the summary paragraph; the native tool sends the
        # docstring without its parameter section, which starts with it.
        assert native.description.startswith(listed.description)

    async def test_same_arguments_accepted_and_rejected(self):
        server, _ = await _server_tool(srv_search)
        client = TestClient(server)
        native = tool(srv_search)
        cases = [
            ({"query": "lamp"}, True),
            ({"query": "lamp", "limit": "5"}, True),
            ({"query": "lamp", "color": "blue", "since": "2026-01-01"}, True),
            ({"query": "x"}, False),
            ({"query": "lamp", "limit": 0}, False),
            ({"query": "lamp", "color": "green"}, False),
            ({"limit": 5}, False),
        ]
        for args, ok in cases:
            [content] = await client.call_tool("srv_search", args)
            server_ok = '"VALIDATION_ERROR"' not in content.text
            try:
                await native.ainvoke(args)
                native_ok = True
            except ToolArgumentError:
                native_ok = False
            assert server_ok is native_ok is ok, args
