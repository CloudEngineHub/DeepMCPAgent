"""Traces keep the tenant and the session, and record tool arguments as data.

* Every entry carries the caller's ``tenant_id`` next to ``user_id``, so two
  tenants' users with the same id stay apart in ``for_user``, ``get_users``
  and ``purge_user``.
* ``chat(session_id=...)`` stamps the session on every entry of the run.
* A ``tool.call`` entry's ``arguments`` are JSON data, not a Python repr.
* ``AuditMiddleware(include_args=True)`` and
  ``StructuredLoggingMiddleware(include_args=True)`` write nested Pydantic
  arguments as JSON objects.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from promptise import build_agent, tool
from promptise.agent import CallerContext, _caller_ctx_var
from promptise.callback_handler import PromptiseCallbackHandler
from promptise.observability import (
    ObservabilityCollector,
    TimelineEntry,
    TimelineEventCategory,
    TimelineEventType,
)
from promptise.testing import ScriptedModel, ToolCall


def _record_as(collector: ObservabilityCollector, caller: CallerContext) -> TimelineEntry:
    token = _caller_ctx_var.set(caller)
    try:
        return collector.record(TimelineEventType.TOOL_CALL, details="x")
    finally:
        _caller_ctx_var.reset(token)


def _two_alices() -> ObservabilityCollector:
    c = ObservabilityCollector()
    _record_as(c, CallerContext(user_id="alice", tenant_id="acme"))
    _record_as(c, CallerContext(user_id="alice", tenant_id="acme"))
    _record_as(c, CallerContext(user_id="alice", tenant_id="globex"))
    _record_as(c, CallerContext(user_id="alice"))
    c.record(TimelineEventType.HEALTH_CHECK)
    return c


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------


class TestTenantIsRecorded:
    def test_tenant_is_stamped_from_caller(self) -> None:
        entry = _record_as(
            ObservabilityCollector(), CallerContext(user_id="alice", tenant_id="acme")
        )
        assert entry.user_id == "alice"
        assert entry.tenant_id == "acme"
        assert entry.isolation_key == "acme::alice"

    def test_untenanted_caller(self) -> None:
        entry = _record_as(ObservabilityCollector(), CallerContext(user_id="alice"))
        assert entry.tenant_id is None
        assert entry.isolation_key == "alice"

    def test_to_dict_has_tenant(self) -> None:
        entry = _record_as(
            ObservabilityCollector(), CallerContext(user_id="alice", tenant_id="acme")
        )
        d = entry.to_dict()
        assert d["user_id"] == "alice"
        assert d["tenant_id"] == "acme"

    def test_explicit_isolation_key_is_split(self) -> None:
        entry = ObservabilityCollector().record(TimelineEventType.TOOL_CALL, user_id="acme::alice")
        assert (entry.user_id, entry.tenant_id) == ("alice", "acme")

    def test_explicit_user_and_tenant(self) -> None:
        entry = ObservabilityCollector().record(
            TimelineEventType.TOOL_CALL, user_id="bob", tenant_id="initech"
        )
        assert (entry.user_id, entry.tenant_id) == ("bob", "initech")

    def test_explicit_user_does_not_take_the_callers_tenant(self) -> None:
        c = ObservabilityCollector()
        token = _caller_ctx_var.set(CallerContext(user_id="alice", tenant_id="acme"))
        try:
            entry = c.record(TimelineEventType.TOOL_CALL, user_id="system")
        finally:
            _caller_ctx_var.reset(token)
        assert (entry.user_id, entry.tenant_id) == ("system", None)

    def test_positional_construction_still_works(self) -> None:
        # tenant_id was added last, so older positional calls keep their meaning.
        entry = TimelineEntry(
            "id-1",
            1.0,
            TimelineEventType.TOOL_CALL,
            TimelineEventCategory.TRANSPARENCY,
            "agent",
            None,
            "details",
            None,
            None,
            "alice",
            "sess",
            {"k": "v"},
        )
        assert entry.metadata == {"k": "v"}
        assert entry.session_id == "sess"
        assert entry.tenant_id is None


class TestTenantAwareQueries:
    def test_two_tenants_users_are_kept_apart(self) -> None:
        c = _two_alices()
        assert len(c.for_user("alice", tenant_id="acme")) == 2
        assert len(c.for_user("acme::alice")) == 2
        assert len(c.for_user("alice", tenant_id="globex")) == 1
        # Without a tenant: only the untenanted Alice.
        [plain] = c.for_user("alice")
        assert plain.tenant_id is None

    def test_get_users_lists_isolation_keys(self) -> None:
        assert _two_alices().get_users() == ["acme::alice", "alice", "globex::alice"]

    def test_query_by_tenant(self) -> None:
        c = _two_alices()
        assert len(c.query(tenant_ids=["acme"])) == 2
        assert len(c.query(user_ids=["alice"], tenant_ids=["globex"])) == 1
        # None selects events recorded without a tenant (here: plain Alice + health check).
        assert len(c.query(tenant_ids=[None])) == 2
        # user_ids alone still matches the user id in any tenant.
        assert len(c.query(user_ids=["alice"])) == 4


class TestPurgeUserIsTenantAware:
    def test_purge_one_tenants_user(self) -> None:
        c = _two_alices()
        assert c.purge_user("alice", tenant_id="acme") == 2
        assert c.get_users() == ["alice", "globex::alice"]

    def test_purge_by_isolation_key(self) -> None:
        c = _two_alices()
        assert c.purge_user("globex::alice") == 1
        assert c.get_users() == ["acme::alice", "alice"]

    def test_positional_call_keeps_working(self, caplog: pytest.LogCaptureFixture) -> None:
        c = _two_alices()
        with caplog.at_level(logging.WARNING, logger="promptise.observability"):
            removed = c.purge_user("alice")
        # Only the untenanted Alice: another tenant's Alice is someone else.
        assert removed == 1
        assert c.get_users() == ["acme::alice", "globex::alice"]
        assert "kept 3 event(s) of a user with that id in a tenant" in caplog.text

    def test_system_events_are_never_purged(self) -> None:
        c = _two_alices()
        for key in ("acme::alice", "globex::alice", "alice"):
            c.purge_user(key)
        [left] = c.get_timeline()
        assert left.event_type == TimelineEventType.HEALTH_CHECK

    def test_empty_user_id_purges_nothing(self) -> None:
        assert _two_alices().purge_user("") == 0


# ---------------------------------------------------------------------------
# Session id and arguments through a real agent
# ---------------------------------------------------------------------------


class Address(BaseModel):
    city: str
    zip_code: str


class Order(BaseModel):
    sku: str
    qty: int
    ship_to: Address


def _order_tool(seen: list[Any]) -> Any:
    @tool
    def place_order(order: Order, note: str = "") -> str:
        """Place an order."""
        seen.append(order)
        return "placed"

    return place_order


ORDER_ARGS = {
    "order": {"sku": "MS-30", "qty": 2, "ship_to": {"city": "Bern", "zip_code": "3011"}},
    "note": "gift",
}


async def _observed_agent(seen: list[Any]) -> Any:
    model = ScriptedModel([ToolCall("place_order", ORDER_ARGS), "Order placed."])
    return await build_agent(model=model, extra_tools=[_order_tool(seen)], observe=True)


class TestChatRecordsSessionAndTenant:
    async def test_chat_session_and_tenant_on_every_entry(self) -> None:
        seen: list[Any] = []
        agent = await _observed_agent(seen)
        try:
            await agent.chat(
                "Order two MS-30",
                session_id="sess-42",
                caller=CallerContext(user_id="alice", tenant_id="acme"),
            )
            entries = agent.collector.get_timeline()
        finally:
            await agent.shutdown()
        assert seen, "the tool ran"
        owned = [e for e in entries if e.user_id is not None]
        assert owned
        assert {e.session_id for e in owned} == {"sess-42"}
        assert {e.tenant_id for e in owned} == {"acme"}
        types = {e.event_type for e in owned}
        assert TimelineEventType.TOOL_CALL in types
        assert TimelineEventType.AGENT_INPUT in types

    async def test_chat_with_user_id_only(self) -> None:
        agent = await _observed_agent([])
        try:
            await agent.chat("Order two MS-30", session_id="s-1", user_id="bob")
            calls = agent.collector.query(event_types=["tool.call"])
        finally:
            await agent.shutdown()
        assert [(e.user_id, e.tenant_id, e.session_id) for e in calls] == [("bob", None, "s-1")]

    async def test_session_does_not_leak_after_chat(self) -> None:
        agent = await _observed_agent([])
        try:
            await agent.chat("Order two MS-30", session_id="s-1", user_id="bob")
            entry = agent.collector.record(TimelineEventType.HEALTH_CHECK)
        finally:
            await agent.shutdown()
        assert entry.session_id is None


class TestToolArgumentsAreData:
    async def test_tool_call_arguments_are_a_dict(self) -> None:
        agent = await _observed_agent([])
        try:
            await agent.ainvoke({"messages": [{"role": "user", "content": "Order"}]})
            [call] = agent.collector.query(event_types=["tool.call"])
        finally:
            await agent.shutdown()
        assert call.metadata["arguments"] == ORDER_ARGS
        # And the whole entry is plain JSON.
        assert json.loads(json.dumps(call.to_dict()))["metadata"]["arguments"] == ORDER_ARGS

    def _handler(self) -> tuple[PromptiseCallbackHandler, ObservabilityCollector]:
        collector = ObservabilityCollector()
        return PromptiseCallbackHandler(collector, agent_id="a"), collector

    def _arguments(self, input_str: str, **kwargs: Any) -> Any:
        handler, collector = self._handler()
        handler.on_tool_start({"name": "t"}, input_str, run_id=uuid4(), **kwargs)
        [call] = collector.query(event_types=["tool.call"])
        return call.metadata["arguments"]

    def test_inputs_dict_wins(self) -> None:
        assert self._arguments("ignored", inputs={"a": 1, "b": [1, 2]}) == {"a": 1, "b": [1, 2]}

    def test_python_repr_is_parsed(self) -> None:
        assert self._arguments("{'sku': 'MS-30', 'gift': True, 'note': None}") == {
            "sku": "MS-30",
            "gift": True,
            "note": None,
        }

    def test_json_text_is_parsed(self) -> None:
        assert self._arguments('{"q": "x"}') == {"q": "x"}

    def test_plain_text_input_stays_text(self) -> None:
        assert self._arguments("weather in Bern") == "weather in Bern"

    def test_unparseable_text_stays_text(self) -> None:
        assert self._arguments("{not: valid") == "{not: valid"

    def test_models_and_odd_values_become_json(self) -> None:
        args = self._arguments(
            "x",
            inputs={
                "order": Order(sku="A", qty=1, ship_to=Address(city="Bern", zip_code="3011")),
                "tags": {"red"},
                "raw": object(),
            },
        )
        assert args["order"] == {
            "sku": "A",
            "qty": 1,
            "ship_to": {"city": "Bern", "zip_code": "3011"},
        }
        assert args["tags"] == ["red"]
        assert isinstance(args["raw"], str)
        json.dumps(args)

    def test_long_strings_are_cut(self) -> None:
        args = self._arguments("x", inputs={"body": "a" * 5000})
        assert args["body"].startswith("a" * 2000)
        assert args["body"].endswith("[truncated, 5000 total chars]")

    def test_large_lists_are_capped(self) -> None:
        args = self._arguments("x", inputs={"ids": list(range(500))})
        assert args["ids"][:200] == list(range(200))
        assert args["ids"][200] == "[300 more items not recorded]"
        assert len(args["ids"]) == 201

    def test_redaction_reaches_nested_arguments(self) -> None:
        from promptise.observability import redact_sensitive

        collector = ObservabilityCollector(sanitizer=redact_sensitive)
        handler = PromptiseCallbackHandler(collector, agent_id="a")
        handler.on_tool_start(
            {"name": "login"},
            "x",
            run_id=uuid4(),
            inputs={"account": {"user": "ada", "password": "hunter2"}},
        )
        [call] = collector.query(event_types=["tool.call"])
        assert call.metadata["arguments"] == {"account": {"user": "ada", "password": "[REDACTED]"}}

    def test_record_tool_io_off_records_only_the_length(self) -> None:
        collector = ObservabilityCollector()
        handler = PromptiseCallbackHandler(collector, agent_id="a", record_tool_io=False)
        handler.on_tool_start({"name": "t"}, "{'a': 1}", run_id=uuid4(), inputs={"a": 1})
        [call] = collector.query(event_types=["tool.call"])
        assert "arguments" not in call.metadata
        assert call.metadata["arguments_length"] == len("{'a': 1}")


# ---------------------------------------------------------------------------
# MCP server: audit and structured logs
# ---------------------------------------------------------------------------

_KEY = "test-audit-secret"


def _order_server(middleware: Any) -> Any:
    from promptise.mcp.server import MCPServer

    server = MCPServer(name="shop")
    server.add_middleware(middleware)

    @server.tool()
    async def place_order(order: Order, tags: list[str] | None = None) -> str:
        return f"placed {order.sku}"

    return server


class TestAuditNestedModels:
    async def test_nested_model_is_a_json_object(self, tmp_path: Any) -> None:
        from promptise.mcp.server import AuditMiddleware, TestClient, verify_audit_log

        path = tmp_path / "audit.jsonl"
        audit = AuditMiddleware(log_path=str(path), hmac_secret=_KEY, include_args=True)
        await TestClient(_order_server(audit)).call_tool(
            "place_order", {"order": ORDER_ARGS["order"], "tags": ["gift"]}
        )
        with open(path) as f:
            [entry] = [json.loads(line) for line in f]
        assert entry["args"]["order"] == ORDER_ARGS["order"]
        assert entry["args"]["tags"] == ["gift"]
        assert entry["status"] == "ok"
        # The signed chain still verifies over the JSON objects.
        assert verify_audit_log(str(path), _KEY).ok
        assert audit.entries[-1]["args"]["order"] == ORDER_ARGS["order"]

    def test_canonical_entry_handles_models_sets_and_dates(self) -> None:
        import datetime

        from promptise.mcp.server._audit import canonical_entry

        out = canonical_entry(
            {
                "args": {
                    "items": [Address(city="Bern", zip_code="3011")],
                    "when": datetime.datetime(2026, 10, 23, 12, 0),
                    "labels": {"a"},
                    1: "int key",
                }
            }
        )
        assert out["args"]["items"] == [{"city": "Bern", "zip_code": "3011"}]
        assert out["args"]["when"] == "2026-10-23T12:00:00"
        assert out["args"]["labels"] == ["a"]
        assert out["args"]["1"] == "int key"


class TestStructuredLoggingNestedModels:
    async def test_nested_model_is_a_json_object(self, caplog: pytest.LogCaptureFixture) -> None:
        from promptise.mcp.server import StructuredLoggingMiddleware, TestClient

        order = {**ORDER_ARGS["order"], "ship_to": {"city": "ada@example.com", "zip_code": "1"}}
        with caplog.at_level(logging.INFO, logger="promptise.server"):
            await TestClient(
                _order_server(StructuredLoggingMiddleware(include_args=True))
            ).call_tool("place_order", {"order": order})
        starts = [
            json.loads(r.getMessage())
            for r in caplog.records
            if r.getMessage().startswith("{") and '"tool_call_start"' in r.getMessage()
        ]
        [start] = starts
        logged = start["args"]["order"]
        assert logged["sku"] == "MS-30"
        assert logged["qty"] == 2
        # A JSON object, so redaction reaches the strings inside the model.
        assert logged["ship_to"]["zip_code"] == "1"
        assert "ada@example.com" not in json.dumps(start)
