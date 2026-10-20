"""Regression tests for the CodeQL and Bandit findings on the runtime
(polynomial ReDoS, log injection, clear-text logging, stack-trace exposure,
overwritten inherited attributes, the distributed import cycle)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import pytest
from aiohttp import ClientSession

from promptise.env_resolver import (
    ENV_VAR_PATTERN,
    has_env_reference,
    iter_env_references,
    resolve_env_var,
    validate_all_env_vars_available,
)
from promptise.exceptions import EnvVarNotFoundError
from promptise.runtime import AgentRuntime, InboxConfig, ProcessConfig
from promptise.runtime._deploy_policy import env_references_in
from promptise.runtime.api import OrchestrationAPI, _inbox_add
from promptise.runtime.api_client import OrchestrationAPIError
from promptise.runtime.exceptions import ProcessStateError, RuntimeBaseError
from promptise.runtime.lifecycle import ProcessState, StateError

# A string full of unterminated references: ENV_VAR_PATTERN retries from
# every "${" and scans to the line break each time (quadratic).
_PATHOLOGICAL = "${A:-" + "${a:-a" * 100_000 + "\n}"


# ---------------------------------------------------------------------------
# py/polynomial-redos: ${VAR} references are found in linear time
# ---------------------------------------------------------------------------


class TestEnvReferenceScanner:
    def test_finds_exactly_what_the_regex_finds(self) -> None:
        rnd = random.Random(1234)
        pieces = ["$", "{", "}", ":", "-", "a", "B", "_", "1", "\n", "${", "${A", "${A:-", " "]
        for _ in range(20_000):
            text = "".join(rnd.choice(pieces) for _ in range(rnd.randint(0, 24)))
            expected = [
                (m.start(), m.end(), m.group(1), m.group(2)) for m in ENV_VAR_PATTERN.finditer(text)
            ]
            assert [tuple(r) for r in iter_env_references(text)] == expected, text

    @pytest.mark.parametrize(
        ("text", "found"),
        [
            ("${A}", [("A", None)]),
            ("x ${A_1:-dflt} y ${B}", [("A_1", "dflt"), ("B", None)]),
            ("${A:-$5}", [("A", "$5")]),
            ("${A:-${B}}", [("A", "${B")]),  # references don't nest
            ("${A:-x\ny}", []),  # a default can't span a line break
            ("${1A}", []),
            ("$A ${ A}", []),
        ],
    )
    def test_syntax(self, text: str, found: list[tuple[str, str | None]]) -> None:
        assert [(r.name, r.default) for r in iter_env_references(text)] == found
        assert has_env_reference(text) is bool(found)

    def test_pathological_input_is_handled(self) -> None:
        assert list(env_references_in({"instructions": _PATHOLOGICAL})) == []
        assert resolve_env_var(_PATHOLOGICAL) == _PATHOLOGICAL
        assert validate_all_env_vars_available({"k": _PATHOLOGICAL}) == []

    def test_pathological_input_scales_linearly(self) -> None:
        # Wall-clock limits are flaky on shared CI runners, so compare growth
        # instead: 4x the input takes ~4x as long when linear, ~16x when
        # quadratic (the regex this replaced needs minutes at this size).
        def best_of_three(repeats: int) -> float:
            text = "${A:-" + "${a:-a" * repeats + "\n}"
            times = []
            for _ in range(3):
                started = time.perf_counter()
                resolve_env_var(text)
                times.append(time.perf_counter() - started)
            return min(times)

        small, large = best_of_three(50_000), best_of_three(200_000)
        assert large < max(small, 1e-3) * 10

    def test_resolution_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTISE_T_SET", "v")
        monkeypatch.delenv("PROMPTISE_T_UNSET", raising=False)
        assert resolve_env_var("a ${PROMPTISE_T_SET} b") == "a v b"
        assert resolve_env_var("${PROMPTISE_T_UNSET:-d}/${PROMPTISE_T_SET}") == "d/v"
        assert resolve_env_var("${PROMPTISE_T_UNSET:-}") == ""
        assert resolve_env_var("${PROMPTISE_T_UNSET}", allow_missing=True) == (
            "${PROMPTISE_T_UNSET}"
        )
        with pytest.raises(EnvVarNotFoundError):
            resolve_env_var("${PROMPTISE_T_UNSET}")
        assert validate_all_env_vars_available(
            {"a": "${PROMPTISE_T_UNSET}", "b": "${PROMPTISE_T_UNSET:-x}"}
        ) == ["PROMPTISE_T_UNSET"]

    async def test_api_deploy_with_pathological_value_answers_quickly(self) -> None:
        api = OrchestrationAPI(AgentRuntime(), host="127.0.0.1", port=0)
        await api.start()
        try:
            body = json.dumps({"name": "p", "config": {"model": _PATHOLOGICAL}}).encode()
            started = time.perf_counter()
            async with ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{api.port}/api/v1/processes", data=body
                ) as resp:
                    assert resp.status < 500
            assert time.perf_counter() - started < 5.0
        finally:
            await api.stop()


# ---------------------------------------------------------------------------
# py/log-injection, py/clear-text-logging-sensitive-data
# ---------------------------------------------------------------------------

_FORGED = "x\nCRITICAL promptise: forged entry"


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


class TestLogInjection:
    async def test_runtime_process_names(self, caplog: pytest.LogCaptureFixture) -> None:
        runtime = AgentRuntime()
        with caplog.at_level(logging.INFO, logger="promptise.runtime"):
            await runtime.add_process(_FORGED, ProcessConfig())
            await runtime.remove_process(_FORGED)
        lines = [m for m in _messages(caplog) if m.startswith("AgentRuntime:")]
        assert len(lines) == 2 and all("forged entry" in m and "\n" not in m for m in lines)

    async def test_coordinator_and_registry(self, caplog: pytest.LogCaptureFixture) -> None:
        from promptise.runtime.distributed import RegistryDiscovery, RuntimeCoordinator

        coordinator = RuntimeCoordinator()
        registry = RegistryDiscovery()
        with caplog.at_level(logging.INFO, logger="promptise.runtime.distributed"):
            coordinator.register_node(_FORGED, "http://h:1/\r\nforged entry")
            coordinator.unregister_node(_FORGED)
            await registry.register(_FORGED, "http://h:1")
            await registry.unregister(_FORGED)
        lines = [m for m in _messages(caplog) if "forged entry" in m]
        assert len(lines) == 4
        assert all("\n" not in m and "\r" not in m for m in lines)

    async def test_failed_rotation_logs_neither_secret_name_nor_value(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        runtime = AgentRuntime()
        process = await runtime.add_process("p", ProcessConfig())

        class _Secrets:
            async def rotate(self, name: str, value: str, **kw: object) -> None:
                raise RuntimeError(f"vault refused {name}={value}")

        process._secrets = _Secrets()
        api = OrchestrationAPI(runtime, host="127.0.0.1", port=0)
        await api.start()
        try:
            with caplog.at_level(logging.WARNING, logger="promptise.runtime.api"):
                async with ClientSession() as session:
                    async with session.patch(
                        f"http://127.0.0.1:{api.port}/api/v1/processes/p/secrets/db_password",
                        json={"value": "hunter2"},
                    ) as resp:
                        status, body = resp.status, await resp.json()
        finally:
            await api.stop()
        assert status == 400 and body["error"]["code"] == "ROTATION_FAILED"
        error_id = body["error"]["error_id"]
        assert "hunter2" not in json.dumps(body) and "vault" not in json.dumps(body)
        [line] = [m for m in _messages(caplog) if "rotating a secret" in m]
        assert error_id in line and "RuntimeError" in line
        assert "db_password" not in line and "hunter2" not in line
        assert all(r.exc_info is None for r in caplog.records if "rotating" in r.getMessage())


# ---------------------------------------------------------------------------
# py/stack-trace-exposure: inbox refusals don't quote the exception
# ---------------------------------------------------------------------------


class _RefusingInbox:
    _max_messages = 3

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def add(self, message: object) -> str:
        raise self._exc


class TestInboxRefusals:
    @pytest.mark.parametrize(
        ("exc_name", "code", "status", "expected"),
        [
            ("InboxMessageRejected", "MESSAGE_REJECTED", 422, "guardrail scanner"),
            ("InboxFullError", "INBOX_FULL", 409, "(3 messages)"),
            ("InvalidInboxMessage", "INVALID_FIELD", 422, "'priority' one of"),
            ("InboxRateLimitError", "RATE_LIMITED", 429, "'ops'"),
        ],
    )
    async def test_answer_is_written_by_the_api(
        self, exc_name: str, code: str, status: int, expected: str
    ) -> None:
        from promptise.runtime import inbox as inbox_module
        from promptise.runtime.inbox import InboxMessage

        cls = getattr(inbox_module, exc_name)
        secret = "internal detail /srv/x"
        exc = cls(secret, "pii") if exc_name == "InboxMessageRejected" else cls(secret)
        message = InboxMessage(content="hi", sender_id="ops")
        _, response = await _inbox_add(_RefusingInbox(exc), message)
        assert response is not None and response.status == status
        error = json.loads(response.text)["error"]
        assert error["code"] == code and error["error_id"]
        assert expected in error["message"] and secret not in error["message"]

    async def test_rate_limit_over_http(self) -> None:
        runtime = AgentRuntime()
        process = await runtime.add_process(
            "p", ProcessConfig(inbox=InboxConfig(enabled=True, rate_limit_per_sender=1))
        )
        await process.start()
        api = OrchestrationAPI(runtime, host="127.0.0.1", port=0)
        await api.start()
        try:
            url = f"http://127.0.0.1:{api.port}/api/v1/processes/p/messages"
            async with ClientSession() as session:
                for _ in range(2):
                    async with session.post(
                        url, json={"content": "hi", "sender_id": "ops"}
                    ) as resp:
                        status, body = resp.status, await resp.json()
        finally:
            await api.stop()
            await process.stop()
        assert status == 429 and body["error"]["code"] == "RATE_LIMITED"
        assert body["error"]["error_id"] and "limit:" not in body["error"]["message"]


class TestClientErrorId:
    def test_error_id_is_parsed(self) -> None:
        import httpx

        request = httpx.Request("POST", "http://h/api/v1/processes/p/start")
        response = httpx.Response(
            500,
            json={"error": {"code": "START_FAILED", "message": "m", "error_id": "abc123"}},
            request=request,
        )
        err = OrchestrationAPIError(response)
        assert err.code == "START_FAILED" and err.error_id == "abc123"
        assert "error_id=abc123" in str(err)
        plain = OrchestrationAPIError(httpx.Response(502, text="bad gateway", request=request))
        assert plain.error_id is None and plain.error_message == "bad gateway"


# ---------------------------------------------------------------------------
# py/overwritten-inherited-attribute: StateError sets its attributes once
# ---------------------------------------------------------------------------


class TestStateError:
    def test_attributes_and_message(self) -> None:
        err = StateError(ProcessState.STOPPED, ProcessState.SUSPENDED, process_id="p")
        assert isinstance(err, ProcessStateError) and isinstance(err, ValueError)
        assert isinstance(err, RuntimeBaseError)
        assert (err.process_id, err.current_state, err.attempted_state) == (
            "p",
            "stopped",
            "suspended",
        )
        assert err.current is ProcessState.STOPPED and err.target is ProcessState.SUSPENDED
        assert str(err).startswith("Process 'p': Cannot transition from 'stopped' to 'suspended'")
        anonymous = StateError(ProcessState.STOPPED, ProcessState.SUSPENDED)
        assert anonymous.process_id is None and str(anonymous).startswith("Cannot transition")

    def test_process_state_error_keeps_its_message(self) -> None:
        err = ProcessStateError("p", "running", "created")
        assert str(err) == "Process 'p': invalid transition 'running' -> 'created'"


# ---------------------------------------------------------------------------
# py/cyclic-import and the cluster node's task shutdown
# ---------------------------------------------------------------------------


class TestDistributed:
    def test_transport_does_not_import_the_runtime_module(self) -> None:
        # runtime -> distributed.node -> transport -> runtime was a cycle;
        # transport only needs AgentRuntime as a type.
        from promptise.runtime.distributed import transport

        assert not hasattr(transport, "AgentRuntime")

    async def test_stop_waits_for_failing_tasks(self) -> None:
        from promptise.runtime.config import DistributedConfig
        from promptise.runtime.distributed import ClusterNode

        node = ClusterNode(AgentRuntime(), DistributedConfig())

        async def forever() -> None:
            await asyncio.sleep(3600)

        async def broken() -> None:
            raise RuntimeError("boom")

        node._tasks = [asyncio.create_task(forever()), asyncio.create_task(broken())]
        await asyncio.sleep(0)
        tasks = list(node._tasks)
        await node.stop()
        assert node._tasks == [] and all(t.done() for t in tasks)

    async def test_stop_does_not_swallow_its_own_cancellation(self) -> None:
        from promptise.runtime.config import DistributedConfig
        from promptise.runtime.distributed import ClusterNode

        node = ClusterNode(AgentRuntime(), DistributedConfig())

        async def stubborn() -> None:
            # Ignores the first cancel, so stop() is still waiting for it.
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(3600)

        node._tasks = [asyncio.create_task(stubborn())]
        await asyncio.sleep(0)
        stopper = asyncio.create_task(node.stop())
        await asyncio.sleep(0.05)
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopper, 2)
