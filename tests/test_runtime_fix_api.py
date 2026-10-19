"""Runtime hardening, phase 1: OrchestrationAPI, OrchestrationClient,
the loopback guard and AgentContext.

Every test here failed before the fix it covers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from aiohttp import ClientSession

from promptise.runtime import AgentRuntime, InboxConfig, ProcessConfig
from promptise.runtime.api import ERROR_CODES, OrchestrationAPI, config_trigger_ids
from promptise.runtime.api_client import OrchestrationAPIError, OrchestrationClient
from promptise.runtime.config import (
    BudgetConfig,
    HealthConfig,
    JournalConfig,
    MissionConfig,
    SecretScopeConfig,
    TriggerConfig,
)
from promptise.runtime.context import (
    CONTEXT_UPDATE_ENTRY_TYPE,
    AgentContext,
    create_state_tools,
)
from promptise.runtime.inbox import InboxResponse

TOKEN = "fix-api-token"
MEMORY_JOURNAL = JournalConfig(backend="memory")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Agent:
    """Stands in for build_agent(): never called by these tests."""

    async def ainvoke(self, input: dict, config: object = None) -> dict:
        return {"messages": [{"role": "assistant", "content": "ok"}]}

    async def shutdown(self) -> None:
        return None


def _no_model(process: Any) -> None:
    async def build() -> None:
        process._agent = _Agent()

    process._build_agent = build


async def _add(runtime: AgentRuntime, name: str = "worker", **config: Any) -> Any:
    config.setdefault("journal", MEMORY_JOURNAL)
    process = await runtime.add_process(name, ProcessConfig(**config))
    _no_model(process)
    return process


async def _serve(runtime: AgentRuntime, **kw: Any) -> tuple[OrchestrationAPI, str]:
    api = OrchestrationAPI(runtime, host="127.0.0.1", port=0, **kw)
    await api.start()
    return api, f"http://127.0.0.1:{api.port}"


async def _call(
    method: str, url: str, body: Any = None, *, headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    async with ClientSession() as session:
        data = json.dumps(body).encode() if body is not None else None
        async with session.request(method, url, data=data, headers=headers or {}) as resp:
            text = await resp.text()
            return resp.status, (json.loads(text) if text else None)


@pytest.fixture
async def served():
    runtime = AgentRuntime()
    api, base = await _serve(runtime)
    try:
        yield runtime, api, base
    finally:
        await api.stop()
        await runtime.stop_all()


# ---------------------------------------------------------------------------
# OrchestrationClient
# ---------------------------------------------------------------------------


class TestClient:
    async def test_204_routes_return_empty_dict(self, served) -> None:
        runtime, _, base = served
        await _add(runtime, inbox=InboxConfig(enabled=True))
        async with OrchestrationClient(base) as client:
            await client.send_message("worker", "hello")
            assert await client.clear_inbox("worker") == {}
            assert await client.remove_process("worker") == {}
        assert "worker" not in runtime.processes

    async def test_ask_returns_the_answer_text(self, served) -> None:
        runtime, _, base = served
        process = await _add(runtime, inbox=InboxConfig(enabled=True))

        async def answer() -> None:
            for _ in range(200):
                questions = await process._inbox.get_questions()
                if questions:
                    q = questions[0]
                    await process._inbox.submit_response(
                        q.message_id, InboxResponse(question_id=q.message_id, content="All good.")
                    )
                    return
                await asyncio.sleep(0.01)

        async with OrchestrationClient(base) as client:
            answered, _ = await asyncio.gather(client.ask("worker", "Status?", timeout=5), answer())
            assert answered == "All good."
            # No run answers it: None, not an exception and not a dict
            assert await client.ask("worker", "Still there?", timeout=0.2) is None

    async def test_auth_token_is_an_alias_of_token(self) -> None:
        runtime = AgentRuntime()
        api, base = await _serve(runtime, auth_token=TOKEN)
        try:
            async with OrchestrationClient(base, auth_token=TOKEN) as client:
                assert await client.list_processes() == []
            async with OrchestrationClient(base, token=TOKEN) as client:
                assert await client.list_processes() == []
            async with OrchestrationClient(base) as client:
                with pytest.raises(OrchestrationAPIError) as info:
                    await client.list_processes()
                assert info.value.status_code == 401
                assert info.value.code == "UNAUTHORIZED"
        finally:
            await api.stop()
        with pytest.raises(ValueError, match="not both"):
            OrchestrationClient(base, token="a", auth_token="b")

    async def test_errors_carry_the_api_code(self, served) -> None:
        import httpx

        _, _, base = served
        async with OrchestrationClient(base) as client:
            with pytest.raises(httpx.HTTPStatusError) as info:  # still catchable as before
                await client.get_process("nope")
        assert isinstance(info.value, OrchestrationAPIError)
        assert info.value.code == "PROCESS_NOT_FOUND"
        assert "nope" in str(info.value)

    async def test_works_without_async_with(self, served) -> None:
        _, _, base = served
        client = OrchestrationClient(base)
        try:
            assert (await client.health())["status"] == "healthy"
        finally:
            await client.aclose()

    async def test_every_route_has_a_client_method(self, served) -> None:
        """Calling every client method hits every server route exactly."""
        runtime, api, base = served
        await _add(runtime, inbox=InboxConfig(enabled=True))
        hit: set[tuple[str, str]] = set()
        dispatch = api._dispatch

        async def record(request, handler):
            resource = request.match_info.route.resource
            if resource is not None:
                hit.add((request.method, resource.canonical))
            return await dispatch(request, handler)

        api._dispatch = record  # type: ignore[method-assign]
        n = "worker"
        calls = [
            lambda c: c.health(),
            lambda c: c.runtime_status(),
            lambda c: c.start_all(),
            lambda c: c.stop_all(),
            lambda c: c.list_processes(),
            lambda c: c.get_process(n),
            lambda c: c.deploy("other", {}),
            lambda c: c.remove_process("other"),
            lambda c: c.start_process(n),
            lambda c: c.suspend_process(n),
            lambda c: c.resume_process(n),
            lambda c: c.restart_process(n),
            lambda c: c.update_instructions(n, "Be brief."),
            lambda c: c.update_budget(n, max_tool_calls_per_run=3),
            lambda c: c.update_health(n, cooldown=1.0),
            lambda c: c.update_mission(n, objective="x"),
            lambda c: c.send_message(n, "hi", metadata={"k": 1}),
            lambda c: c.ask(n, "q?", timeout=0.05),
            lambda c: c.get_inbox(n),
            lambda c: c.clear_inbox(n),
            lambda c: c.list_triggers(n),
            lambda c: c.add_trigger(n, {"type": "event", "event_type": "x"}),
            lambda c: c.remove_trigger(n, "nope"),
            lambda c: c.list_secrets(n),
            lambda c: c.rotate_secret(n, "s", "v"),
            lambda c: c.revoke_secrets(n),
            lambda c: c.get_journal(n),
            lambda c: c.get_mission(n),
            lambda c: c.fail_mission(n),
            lambda c: c.pause_mission(n),
            lambda c: c.resume_mission(n),
            lambda c: c.get_anomalies(n),
            lambda c: c.clear_anomalies(n),
            lambda c: c.get_metrics(n),
            lambda c: c.get_context(n),
            lambda c: c.update_context(n, {"k": 1}),
            lambda c: c.stop_process(n),
        ]
        async with OrchestrationClient(base) as client:
            for call in calls:
                try:
                    await call(client)
                except OrchestrationAPIError:
                    pass  # e.g. SECRETS_DISABLED: the route was still reached
        assert api._app is not None
        routes = {
            (route.method, route.resource.canonical)
            for route in api._app.router.routes()
            if route.method != "HEAD" and route.resource is not None
        }
        assert routes - hit == set()


# ---------------------------------------------------------------------------
# Error codes and audit
# ---------------------------------------------------------------------------


class TestErrorsAndAudit:
    async def test_duplicate_deploy_is_409_process_exists(self, served) -> None:
        _, _, base = served
        status, _ = await _call("POST", base + "/api/v1/processes", {"name": "a"})
        assert status == 201
        status, body = await _call("POST", base + "/api/v1/processes", {"name": "a"})
        assert status == 409
        assert body["error"]["code"] == "PROCESS_EXISTS"

    async def test_non_string_name_is_422_not_500(self, served) -> None:
        _, _, base = served
        status, body = await _call("POST", base + "/api/v1/processes", {"name": 42})
        assert status == 422
        assert body["error"]["code"] == "INVALID_NAME"

    async def test_unexpected_error_is_internal_error_without_details(
        self, served, monkeypatch
    ) -> None:
        runtime, _, base = served

        def boom() -> dict:
            raise RuntimeError("db password is hunter2")

        monkeypatch.setattr(runtime, "status", boom)
        status, body = await _call("GET", base + "/api/v1/runtime/status")
        assert status == 500
        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert "hunter2" not in json.dumps(body)

    async def test_unknown_route_is_json_404(self, served) -> None:
        _, _, base = served
        status, body = await _call("GET", base + "/api/v1/nope")
        assert status == 404
        assert body["error"]["code"] == "NOT_FOUND"
        status, body = await _call("PUT", base + "/api/v1/processes")
        assert status == 405
        assert body["error"]["code"] == "METHOD_NOT_ALLOWED"

    async def test_oversized_body_is_json_413(self, served) -> None:
        runtime, _, base = served
        await _add(runtime, inbox=InboxConfig(enabled=True))
        status, body = await _call(
            "POST", base + "/api/v1/processes/worker/messages", {"content": "x" * (2 << 20)}
        )
        assert status == 413
        assert body["error"]["code"] == "PAYLOAD_TOO_LARGE"

    async def test_emitted_codes_are_documented(self) -> None:
        import re
        from pathlib import Path

        import promptise.runtime.api as api_module

        source = Path(api_module.__file__).read_text()
        emitted = set(re.findall(r'_error_response\(\s*"([A-Z_]+)"', source))
        emitted |= {"MISDIRECTED_REQUEST", "CROSS_ORIGIN_REFUSED", "NOT_FOUND"}
        assert emitted - set(ERROR_CODES) == set()

    async def test_mutations_are_logged_and_journaled_without_bodies(self, served, caplog) -> None:
        runtime, _, base = served
        process = await _add(
            runtime,
            inbox=InboxConfig(enabled=True),
            secrets=SecretScopeConfig(enabled=True, secrets={"api_key": "sk-original-1234"}),
        )
        await process._secrets.resolve_initial()
        caplog.set_level(logging.INFO, logger="promptise.runtime.api")
        status, _ = await _call(
            "POST", base + "/api/v1/processes/worker/messages", {"content": "private note"}
        )
        assert status == 201
        status, _ = await _call(
            "PATCH",
            base + "/api/v1/processes/worker/secrets/api_key",
            {"value": "sk-rotated-9876"},
        )
        assert status == 200
        # GETs are not audited
        await _call("GET", base + "/api/v1/processes/worker")

        log = "\n".join(r.getMessage() for r in caplog.records)
        assert "POST /api/v1/processes/{name}/messages" in log
        assert "PATCH /api/v1/processes/{name}/secrets/{secret_name}" in log
        assert "private note" not in log and "sk-rotated-9876" not in log

        entries = await process._journal.read("worker", entry_type="api_request")
        routes = [(e.data["method"], e.data["route"]) for e in entries]
        assert routes == [
            ("POST", "/api/v1/processes/{name}/messages"),
            ("PATCH", "/api/v1/processes/{name}/secrets/{secret_name}"),
        ]
        assert entries[1].data["params"]["secret_name"] == "api_key"
        dumped = json.dumps([e.data for e in entries])
        assert "sk-rotated-9876" not in dumped and "private note" not in dumped

    async def test_refused_requests_are_logged(self, caplog) -> None:
        runtime = AgentRuntime()
        api, base = await _serve(runtime, auth_token=TOKEN)
        caplog.set_level(logging.WARNING, logger="promptise.runtime.api")
        try:
            status, _ = await _call("POST", base + "/api/v1/runtime/stop-all")
        finally:
            await api.stop()
        assert status == 401
        assert any("401 UNAUTHORIZED" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


class _RecordingScope:
    """A SecretScope stand-in that records rotate() calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._secrets: dict[str, Any] = {}

    def list_names(self) -> list[str]:
        return ["api_key"]

    async def rotate(self, name: str, value: str, **kwargs: Any) -> None:
        self.calls.append((name, value, kwargs))


class TestSecretRoutes:
    async def test_unknown_secret_is_404_secret_not_found(self, served) -> None:
        runtime, _, base = served
        process = await _add(
            runtime, secrets=SecretScopeConfig(enabled=True, secrets={"api_key": "sk-1"})
        )
        await process._secrets.resolve_initial()
        status, body = await _call(
            "PATCH", base + "/api/v1/processes/worker/secrets/nope", {"value": "x"}
        )
        assert status == 404
        assert body["error"]["code"] == "SECRET_NOT_FOUND"
        assert "nope" in body["error"]["message"]
        # The process itself is still reported missing as before
        status, body = await _call(
            "PATCH", base + "/api/v1/processes/ghost/secrets/api_key", {"value": "x"}
        )
        assert body["error"]["code"] == "PROCESS_NOT_FOUND"

    async def test_ttl_is_passed_to_rotate(self, served) -> None:
        runtime, _, base = served
        process = await _add(runtime)
        scope = _RecordingScope()
        process._secrets = scope
        status, _ = await _call(
            "PATCH", base + "/api/v1/processes/worker/secrets/api_key", {"value": "v2", "ttl": 60}
        )
        assert status == 200
        status, _ = await _call(
            "PATCH", base + "/api/v1/processes/worker/secrets/api_key", {"value": "v3"}
        )
        assert status == 200
        assert scope.calls == [("api_key", "v2", {"ttl": 60}), ("api_key", "v3", {})]
        for bad in ("60", 0, -5, True):
            status, body = await _call(
                "PATCH",
                base + "/api/v1/processes/worker/secrets/api_key",
                {"value": "v", "ttl": bad},
            )
            assert status == 422, bad
            assert body["error"]["code"] == "INVALID_FIELD"
        status, body = await _call(
            "PATCH",
            base + "/api/v1/processes/worker/secrets/api_key",
            {"value": "v", "ttll": 5},
        )
        assert body["error"]["code"] == "UNKNOWN_FIELD"
        assert len(scope.calls) == 2

    async def test_listing_has_expiry_metadata_and_no_values(self, served) -> None:
        runtime, _, base = served
        process = await _add(
            runtime,
            secrets=SecretScopeConfig(
                enabled=True,
                secrets={"api_key": "sk-listing-secret", "db": "pg-listing-secret"},
                default_ttl=None,
                ttls={"api_key": 600},
            ),
        )
        await process._secrets.resolve_initial()
        status, body = await _call("GET", base + "/api/v1/processes/worker/secrets")
        assert status == 200
        assert "listing-secret" not in json.dumps(body)
        by_name = {s["name"]: s for s in body["secrets"]}
        assert by_name["api_key"]["active"] is True
        assert 590 < by_name["api_key"]["expires_in"] <= 600
        assert by_name["api_key"]["expires_at"] > 0
        assert by_name["db"]["expires_at"] is None
        assert by_name["db"]["expires_in"] is None


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class TestContextRoutes:
    async def test_patch_is_atomic(self, served) -> None:
        runtime, _, base = served
        from promptise.runtime.config import ContextConfig

        process = await _add(
            runtime,
            context=ContextConfig(writable_keys=["status"], initial_state={"status": "idle"}),
        )
        status, body = await _call(
            "PATCH",
            base + "/api/v1/processes/worker/context",
            {"state": {"status": "busy", "owner": "eve", "secret_flag": 1}},
        )
        assert status == 403
        assert body["error"]["code"] == "WRITE_DENIED"
        assert "owner" in body["error"]["message"] and "secret_flag" in body["error"]["message"]
        assert process.context.get("status") == "idle"  # nothing applied
        assert process.context.get("owner") is None

        status, body = await _call(
            "PATCH", base + "/api/v1/processes/worker/context", {"state": {"status": "busy"}}
        )
        assert status == 200
        assert body["updated_keys"] == ["status"]
        assert process.context.state_history("status")[-1].source == "api"

        for bad in ({"state": ["status"]}, {}, {"status": "x"}):
            status, body = await _call("PATCH", base + "/api/v1/processes/worker/context", bad)
            assert status == 422, bad

    async def test_get_reports_all_keys_writable(self, served) -> None:
        runtime, _, base = served
        await _add(runtime)
        status, body = await _call("GET", base + "/api/v1/processes/worker/context")
        assert status == 200
        assert body["writable_keys"] == []
        assert body["all_keys_writable"] is True


class TestAgentContext:
    def test_update_is_all_or_nothing(self) -> None:
        ctx = AgentContext(writable_keys=["a"], initial_state={"a": 0})
        with pytest.raises(KeyError, match="'b', 'c'"):
            ctx.update({"a": 1, "b": 2, "c": 3})
        assert ctx.get("a") == 0
        assert ctx.update({"a": 1}, source="api") == ["a"]
        assert ctx.state_history("a")[-1].source == "api"
        assert ctx.writable_keys == ["a"]
        assert AgentContext().writable_keys is None

    async def test_hooks_journal_context_updates_for_replay(self) -> None:
        from promptise.runtime.journal import JournalEntry, ReplayEngine
        from promptise.runtime.journal.memory import InMemoryJournal

        journal = InMemoryJournal()
        ctx = AgentContext(initial_state={"count": 0})
        seen: list[str] = []

        def failing(entry):
            raise RuntimeError("hook broke")

        async def write(entry):
            await journal.append(
                JournalEntry(
                    process_id="p", entry_type=CONTEXT_UPDATE_ENTRY_TYPE, data=entry.to_dict()
                )
            )

        ctx.add_update_hook(failing)  # never breaks the write
        remove = ctx.add_update_hook(lambda e: seen.append(e.key))
        ctx.add_update_hook(write)
        ctx.put("count", 1)
        ctx.update({"count": 2, "phase": "done"}, source="api")
        await ctx.flush_update_hooks()
        remove()
        ctx.put("count", 3)
        await ctx.flush_update_hooks()
        assert seen == ["count", "count", "phase"]

        # Restored state is not journaled again
        ctx.restore_state({"count": 99})
        await ctx.flush_update_hooks()
        recovered = await ReplayEngine(journal).recover("p")
        assert recovered["context_state"] == {"count": 3, "phase": "done"}
        assert ctx.get("count") == 99
        assert ctx.state_history("count")[-1].source == "recovery"

    def test_async_hook_without_loop_is_skipped(self) -> None:
        ctx = AgentContext()

        async def hook(entry):
            raise AssertionError("must not run")

        ctx.add_update_hook(hook)
        ctx.put("k", 1)  # no event loop: logged, coroutine closed, write kept
        assert ctx.get("k") == 1

    def test_restore_state_bypasses_writable_keys(self) -> None:
        ctx = AgentContext(writable_keys=["a"])
        ctx.restore_state({"b": 1})
        assert ctx.get("b") == 1


class TestStateTool:
    async def test_writes_only_allowed_keys(self) -> None:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        ctx = AgentContext(writable_keys=["progress", "notes"])
        [tool] = create_state_tools(ctx)
        schema = convert_to_openai_tool(tool)["function"]["parameters"]
        assert schema["properties"]["key"]["enum"] == ["notes", "progress"]

        assert "Stored" in await tool.ainvoke({"key": "progress", "value": {"done": 3}})
        assert ctx.get("progress") == {"done": 3}
        assert ctx.state_history("progress")[-1].source == "agent"
        reply = await tool.ainvoke({"key": "admin", "value": True})
        assert reply.startswith("Error") and "progress" in reply
        assert ctx.get("admin") is None

    async def test_limits(self) -> None:
        ctx = AgentContext()
        [tool] = create_state_tools(ctx, max_value_chars=20, max_keys=2)
        assert (await tool.ainvoke({"key": "a", "value": "x" * 50})).startswith("Error")
        assert "Stored" in await tool.ainvoke({"key": "a", "value": 1})
        assert "Stored" in await tool.ainvoke({"key": "b", "value": 2})
        assert (await tool.ainvoke({"key": "c", "value": 3})).startswith("Error")
        assert "Stored" in await tool.ainvoke({"key": "a", "value": 5})  # existing key ok
        with pytest.raises(ValueError):
            create_state_tools(ctx, max_keys=0)

    async def test_writes_fire_hooks(self) -> None:
        ctx = AgentContext()
        seen = []
        ctx.add_update_hook(seen.append)
        [tool] = create_state_tools(ctx, source="agent:worker")
        await tool.ainvoke({"key": "k", "value": [1, 2]})
        assert [(e.key, e.value, e.source) for e in seen] == [("k", [1, 2], "agent:worker")]


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class TestMessageRoutes:
    async def test_stopped_process_is_409(self, served) -> None:
        runtime, _, base = served
        process = await _add(runtime, inbox=InboxConfig(enabled=True))
        await process.start()
        await process.stop()
        for path, body in (
            ("messages", {"content": "hello"}),
            ("ask", {"content": "status?", "timeout": 1}),
        ):
            status, resp = await _call("POST", f"{base}/api/v1/processes/worker/{path}", body)
            assert status == 409, path
            assert resp["error"]["code"] == "PROCESS_NOT_RUNNING"
        assert await process._inbox.get_pending() == []

    async def test_created_process_still_takes_messages(self, served) -> None:
        runtime, _, base = served
        await _add(runtime, inbox=InboxConfig(enabled=True))
        status, _ = await _call(
            "POST", base + "/api/v1/processes/worker/messages", {"content": "x"}
        )
        assert status == 201

    @pytest.mark.parametrize(
        "body",
        [
            {"content": "x", "ttl": "60"},
            {"content": "x", "priority": "urgent"},
            {"content": ["x"]},
            {"content": "x", "metadata": "m"},
            {"content": "x", "sender_id": 7},
        ],
    )
    async def test_bad_fields_are_422_not_500(self, served, body) -> None:
        runtime, _, base = served
        await _add(runtime, inbox=InboxConfig(enabled=True))
        status, resp = await _call("POST", base + "/api/v1/processes/worker/messages", body)
        assert status == 422, resp
        assert resp["error"]["code"] in ("INVALID_FIELD", "INVALID_TYPE")

    @pytest.mark.parametrize("timeout", ["5", 0, -1, 100000])
    async def test_bad_ask_timeout_is_422(self, served, timeout) -> None:
        runtime, _, base = served
        await _add(runtime, inbox=InboxConfig(enabled=True))
        status, resp = await _call(
            "POST", base + "/api/v1/processes/worker/ask", {"content": "q", "timeout": timeout}
        )
        assert status == 422
        assert resp["error"]["code"] == "INVALID_FIELD"


# ---------------------------------------------------------------------------
# PATCH budget / health / mission
# ---------------------------------------------------------------------------


class TestGovernancePatches:
    async def test_enabling_an_unbuilt_subsystem_is_not_reported_live(self, served) -> None:
        # A process without apply_governance_config (AgentProcess has it
        # since phase 2; see test_runtime_fix_process.py for the live path).
        runtime, _, base = served
        process = await _add(runtime)
        process.apply_governance_config = None
        for section in ("budget", "health", "mission"):
            status, body = await _call(
                "PATCH", f"{base}/api/v1/processes/worker/{section}", {"enabled": True}
            )
            assert status == 200, body
            assert body["live"] is False
            assert body["effective_from"] == "redeploy"
            assert body["pending_fields"] == ["enabled"]
            assert "redeploy" in body["note"]
            assert getattr(process.config, section).enabled is True
        assert process._budget is None  # really not built

    async def test_disabling_a_built_subsystem_is_not_reported_live(self, served) -> None:
        runtime, _, base = served
        process = await _add(runtime, budget=BudgetConfig(enabled=True, max_tool_calls_per_run=5))
        process.apply_governance_config = None  # the fallback path, as above
        status, body = await _call(
            "PATCH", base + "/api/v1/processes/worker/budget", {"enabled": False}
        )
        assert status == 200
        assert body["live"] is False
        assert process._budget is not None

    async def test_limits_on_a_built_budget_apply_at_the_next_check(self, served) -> None:
        runtime, _, base = served
        process = await _add(runtime, budget=BudgetConfig(enabled=True, max_tool_calls_per_run=5))
        status, body = await _call(
            "PATCH", base + "/api/v1/processes/worker/budget", {"max_tool_calls_per_run": 2}
        )
        assert status == 200
        assert body["live"] is True
        assert body["effective_from"] == "next_budget_check"
        assert process._budget.remaining()["tool_calls_run"] == 2

    async def test_health_patch_on_a_built_monitor_is_live(self, served) -> None:
        runtime, _, base = served
        process = await _add(runtime, health=HealthConfig(enabled=True))
        status, body = await _call(
            "PATCH",
            base + "/api/v1/processes/worker/health",
            {"error_window": 50, "cooldown": 5.0},
        )
        assert status == 200
        assert body == {
            "name": "worker",
            "health_updated": True,
            "effective_from": "immediate",
            "live": True,
        }
        assert process._health.config.error_window == 50  # the monitor reads it

    async def test_process_hook_makes_changes_live(self, served) -> None:
        runtime, _, base = served
        process = await _add(runtime)
        applied: list[str] = []
        process.apply_governance_config = applied.append
        status, body = await _call(
            "PATCH", base + "/api/v1/processes/worker/mission", {"enabled": True, "objective": "x"}
        )
        assert status == 200
        assert applied == ["mission"]
        assert body["live"] is True and body["effective_from"] == "immediate"

    async def test_empty_patch_is_422(self, served) -> None:
        runtime, _, base = served
        await _add(runtime)
        status, body = await _call("PATCH", base + "/api/v1/processes/worker/budget", {})
        assert status == 422
        assert body["error"]["code"] == "MISSING_FIELD"


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

CRON_A = {"type": "cron", "cron_expression": "*/5 * * * *"}
CRON_B = {"type": "cron", "cron_expression": "0 9 * * 1-5"}
EVENT_C = {"type": "event", "event_type": "deploy.done"}


class TestTriggerRoutes:
    async def test_ids_are_stable_across_removals(self, served) -> None:
        runtime, _, base = served
        process = await _add(runtime)
        url = base + "/api/v1/processes/worker/triggers"
        ids = []
        for trigger in (CRON_A, CRON_B, EVENT_C):
            status, body = await _call("POST", url, trigger)
            assert status == 201, body
            assert body["effective_from"] == "next_start"
            ids.append(body["trigger_id"])
        status, body = await _call("GET", url)
        assert [t["id"] for t in body["triggers"]] == ids

        status, body = await _call("DELETE", f"{url}/{ids[0]}")
        assert status == 200
        assert body["trigger_removed"] == ids[0]
        status, body = await _call("GET", url)
        assert [t["id"] for t in body["triggers"]] == ids[1:]  # unchanged, not shifted
        assert [tc.cron_expression for tc in process.config.triggers][:1] == ["0 9 * * 1-5"]

        status, body = await _call("DELETE", f"{url}/{ids[0]}")
        assert status == 404
        assert body["error"]["code"] == "TRIGGER_NOT_FOUND"

    async def test_identical_trigger_is_409(self, served) -> None:
        runtime, _, base = served
        await _add(runtime)
        url = base + "/api/v1/processes/worker/triggers"
        assert (await _call("POST", url, CRON_A))[0] == 201
        status, body = await _call("POST", url, CRON_A)
        assert status == 409
        assert body["error"]["code"] == "TRIGGER_EXISTS"

    async def test_running_process_reports_restart(self, served) -> None:
        runtime, _, base = served
        process = await _add(runtime, triggers=[TriggerConfig(**CRON_A)])
        # The fallback for a process without add_trigger / remove_trigger
        # (AgentProcess has both since phase 2).
        process.add_trigger = None
        process.remove_trigger = None
        await process.start()
        url = base + "/api/v1/processes/worker/triggers"
        status, body = await _call("POST", url, CRON_B)
        assert status == 201
        assert body["live"] is False
        assert body["effective_from"] == "restart"

        [running_id] = config_trigger_ids([TriggerConfig(**CRON_A)])
        status, body = await _call("DELETE", f"{url}/{running_id}")
        assert status == 200
        assert body["live"] is False
        assert body["effective_from"] == "restart"
        assert "keeps firing" in body["note"]

    async def test_process_methods_make_changes_live(self, served) -> None:
        runtime, _, base = served
        process = await _add(runtime)
        await process.start()
        added: list[TriggerConfig] = []
        removed: list[str] = []

        async def add_trigger(tc):
            added.append(tc)
            process.config.triggers.append(tc)
            return config_trigger_ids(process.config.triggers)[-1]

        async def remove_trigger(trigger_id):
            removed.append(trigger_id)

        process.add_trigger = add_trigger
        process.remove_trigger = remove_trigger
        url = base + "/api/v1/processes/worker/triggers"
        status, body = await _call("POST", url, CRON_A)
        assert status == 201
        assert body["live"] is True and body["effective_from"] == "immediate"
        assert [tc.cron_expression for tc in added] == ["*/5 * * * *"]
        status, body = await _call("DELETE", f"{url}/{body['trigger_id']}")
        assert body["live"] is True and body["effective_from"] == "immediate"
        assert removed == [config_trigger_ids(added)[0]]

    async def test_legacy_position_ids_still_work(self, served) -> None:
        runtime, _, base = served
        process = await _add(runtime, triggers=[TriggerConfig(**CRON_A), TriggerConfig(**CRON_B)])
        status, body = await _call("DELETE", base + "/api/v1/processes/worker/triggers/static_1")
        assert status == 200
        assert "deprecated" in body["warning"]
        assert body["trigger_removed"] == config_trigger_ids([TriggerConfig(**CRON_B)])[0]
        assert len(process.config.triggers) == 1

    async def test_invalid_trigger_is_422_with_field_errors(self, served) -> None:
        runtime, _, base = served
        await _add(runtime)
        status, body = await _call(
            "POST", base + "/api/v1/processes/worker/triggers", {"type": "cron", "bogus": 1}
        )
        assert status == 422
        assert "bogus" in body["error"]["message"]

    def test_ids_ignore_secret_values_and_disambiguate(self) -> None:
        a = TriggerConfig(type="webhook", hmac_secret="one-secret-value-123456")
        b = TriggerConfig(type="webhook", hmac_secret="two-secret-value-123456")
        ids = config_trigger_ids([a, b, a])
        assert len(set(ids)) == 3
        assert ids[1] == ids[0] + "-2"  # masked secrets: same content
        assert ids[2] == ids[0] + "-3"


# ---------------------------------------------------------------------------
# Mission, anomalies, journal
# ---------------------------------------------------------------------------


class TestMissionHealthJournal:
    async def test_mission_actions_check_the_state(self, served) -> None:
        runtime, _, base = served
        process = await _add(
            runtime, mission=MissionConfig(enabled=True, objective="ship", success_criteria="done")
        )
        url = base + "/api/v1/processes/worker/mission"
        status, body = await _call("POST", url + "/resume")
        assert status == 409
        assert body["error"]["code"] == "MISSION_STATE"
        assert (await _call("POST", url + "/pause"))[0] == 200
        assert (await _call("POST", url + "/resume"))[0] == 200
        process._mission.complete()
        for action in ("pause", "resume", "fail"):
            status, body = await _call("POST", f"{url}/{action}")
            assert status == 409, action
        assert process._mission.state.value == "completed"  # not reactivated

    async def test_fail_stores_the_reason_and_emits_mission_failed(
        self, served, monkeypatch
    ) -> None:
        import promptise.events

        runtime, _, base = served
        process = await _add(
            runtime, mission=MissionConfig(enabled=True, objective="ship", success_criteria="done")
        )
        process._event_notifier = object()  # any notifier: emit_event is recorded below
        emitted: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            promptise.events,
            "emit_event",
            lambda notifier, event_type, severity="info", data=None, **kw: emitted.append(
                (event_type, data or {})
            ),
        )
        status, body = await _call(
            "POST", base + "/api/v1/processes/worker/mission/fail", {"reason": "wrong target"}
        )
        assert status == 200
        assert process._mission.state.value == "failed"
        assert process._mission.failure_reason == "wrong target"
        [(event_type, data)] = emitted
        assert event_type == "mission.failed"
        assert {k: data[k] for k in ("process_name", "reason", "message")} == {
            "process_name": "worker",
            "reason": "manual",
            "message": "wrong target",
        }

    async def test_clear_anomalies_resets_cooldowns(self, served) -> None:
        runtime, _, base = served
        process = await _add(runtime, health=HealthConfig(enabled=True, cooldown=3600))
        health = process._health
        for _ in range(4):
            await health.record_tool_call("search", {"q": "same"})
        assert health.anomalies
        assert health._last_anomaly_time
        status, body = await _call("DELETE", base + "/api/v1/processes/worker/health/anomalies")
        assert status == 200
        assert body["anomalies_cleared"] >= 1
        assert health.anomalies == []
        assert not health._last_anomaly_time
        health.begin_invocation()
        for _ in range(4):
            await health.record_tool_call("search", {"q": "same"})
        assert health.anomalies  # reported again, not held back by the cooldown

    @pytest.mark.parametrize("limit", ["0", "abc", "501", "-3"])
    async def test_bad_journal_limit_is_422(self, served, limit) -> None:
        runtime, _, base = served
        await _add(runtime)
        status, body = await _call("GET", f"{base}/api/v1/processes/worker/journal?limit={limit}")
        assert status == 422
        assert body["error"]["code"] == "INVALID_FIELD"


# ---------------------------------------------------------------------------
# Loopback guard
# ---------------------------------------------------------------------------


class TestHttpGuard:
    @pytest.mark.parametrize(
        ("host", "origin", "expected"),
        [
            ("127.0.0.1:9100", None, None),
            ("localhost", None, None),
            ("LOCALHOST:9100", None, None),
            ("[::1]:9100", None, None),
            ("127.0.0.2", "http://127.0.0.1:3000", None),
            ("localhost", "HTTP://LOCALHOST", None),
            ("", None, 421),
            ("attacker.example", None, 421),
            ("127.0.0.1.attacker.example", None, 421),
            ("localhost.attacker.example:9100", None, 421),
            ("0.0.0.0:9100", None, 421),
            ("localhost", "http://localhost.attacker.example", 403),
            ("localhost", "http://127.0.0.1.attacker.example", 403),
            ("localhost", "null", 403),
            ("localhost", "file://", 403),
            ("localhost", "chrome-extension://abc", 403),
        ],
    )
    def test_loopback_request_problem(self, host, origin, expected) -> None:
        from promptise.runtime._http_guard import loopback_request_problem

        problem = loopback_request_problem(host, origin)
        assert (problem[0] if problem else None) == expected

    def test_bearer_scheme_is_case_insensitive(self) -> None:
        from promptise.runtime._http_guard import bearer_token_matches

        assert bearer_token_matches("Bearer tok", "tok")
        assert bearer_token_matches("bearer tok", "tok")
        assert not bearer_token_matches("Basic tok", "tok")
        assert not bearer_token_matches("Bearer tok2", "tok")
        assert not bearer_token_matches("Bearertok", "tok")
        assert not bearer_token_matches("", "tok")

    async def test_guard_runs_before_routing_on_token_bind(self) -> None:
        """A rebinding page is refused even with a valid-looking token header."""
        runtime = AgentRuntime()
        api, base = await _serve(runtime, auth_token=TOKEN)
        try:
            status, body = await _call(
                "GET",
                base + "/api/v1/processes",
                headers={"Host": "attacker.example", "Authorization": f"Bearer {TOKEN}"},
            )
        finally:
            await api.stop()
        assert status == 421
        assert body["error"]["code"] == "MISDIRECTED_REQUEST"
