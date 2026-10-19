"""Runtime hardening, phase 3: features that existed but weren't connected.

Lifecycle hooks (HookManager / ShellHook dispatched by AgentProcess),
rewind (RewindEngine wired into AgentProcess, the API and the client),
distributed mode (DistributedConfig read by AgentRuntime), and the
leftovers: meta-tools through the process's public methods, Mem0's
close() no longer wiping memories, budget counters restored by journal
recovery, and the API's public shutdown route.

Every test here failed before the change it covers.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import warnings
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from promptise.runtime import (
    DEFAULT_PRODUCTION_CONFIG,
    AgentRuntime,
    BudgetConfig,
    ContextConfig,
    DistributedConfig,
    ExecutionMode,
    HookBlocked,
    HookConfig,
    HookContext,
    HookEvent,
    HookManager,
    JournalConfig,
    OpenModeConfig,
    ProcessConfig,
    RuntimeConfig,
    SecretScopeConfig,
    ShellHook,
    TriggerConfig,
)
from promptise.runtime.api import OrchestrationAPI
from promptise.runtime.api_client import OrchestrationAPIError, OrchestrationClient
from promptise.runtime.budget import BudgetState
from promptise.runtime.journal import (
    FileJournal,
    InMemoryJournal,
    JournalEntry,
    RewindEngine,
    RewindMode,
)
from promptise.runtime.lifecycle import ProcessState
from promptise.runtime.manifest import AgentManifestSchema, manifest_to_process_config
from promptise.runtime.process import AgentProcess
from promptise.runtime.triggers import register_trigger_type, unregister_trigger_type
from promptise.runtime.triggers.base import TriggerEvent

BUILD_TARGET = "promptise.agent.build_agent"
TOKEN = "wiring-cluster-token"
MEMORY_JOURNAL = JournalConfig(backend="memory")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell scripts")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _event(n: int = 0, trigger_type: str = "manual", **payload: Any) -> TriggerEvent:
    return TriggerEvent(trigger_id="t", trigger_type=trigger_type, payload={"n": n, **payload})


def _agent(reply: str = "done", seen: list[str] | None = None, write: Any = None) -> AsyncMock:
    """A mock agent; records each run's user message in *seen*."""

    async def ainvoke(inp: dict[str, Any], config: Any = None) -> dict[str, Any]:
        if seen is not None:
            seen.append(inp["messages"][-1]["content"])
        if write is not None:
            write()
        return {"messages": [*inp["messages"], AIMessage(content=reply)]}

    agent = AsyncMock()
    agent.ainvoke = AsyncMock(side_effect=ainvoke)
    agent.shutdown = AsyncMock()
    return agent


def _patch_build(agent: Any) -> Any:
    return patch(BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=agent))


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return str(path)


class _Recorder:
    """Async hook that records (event, data) and can block or fail."""

    def __init__(self, *, block: str | None = None, fail: bool = False) -> None:
        self.calls: list[tuple[HookEvent, dict[str, Any], str]] = []
        self.block = block
        self.fail = fail

    async def __call__(self, ctx: HookContext) -> None:
        self.calls.append((ctx.event, dict(ctx.data), ctx.process_id))
        if self.fail:
            raise RuntimeError("hook broke")
        if self.block is not None:
            raise HookBlocked(self.block)

    def of(self, event: HookEvent) -> list[dict[str, Any]]:
        return [d for e, d, _ in self.calls if e == event]


# ===========================================================================
# 1. HookManager
# ===========================================================================


class TestHookManager:
    async def test_dispatch_result_carries_mutated_data(self) -> None:
        hooks = HookManager()

        @hooks.on(HookEvent.USER_PROMPT_SUBMIT)
        async def rewrite(ctx: HookContext) -> None:
            ctx.set("prompt", ctx.get("prompt") + " [ctx]")

        result = await hooks.dispatch(HookEvent.USER_PROMPT_SUBMIT, data={"prompt": "hi"})
        assert result.data == {"prompt": "hi [ctx]"}

    async def test_once_runs_once_under_concurrent_dispatch(self) -> None:
        hooks = HookManager()
        runs: list[int] = []

        async def slow(ctx: HookContext) -> None:
            runs.append(1)
            await asyncio.sleep(0.05)

        hooks.register(HookEvent.TASK_CREATED, slow, once=True)
        await asyncio.gather(*(hooks.dispatch(HookEvent.TASK_CREATED) for _ in range(5)))
        assert runs == [1]
        assert hooks.registered(HookEvent.TASK_CREATED) == []

    async def test_timeout_cancels_and_records_an_error(self) -> None:
        hooks = HookManager()
        later: list[str] = []

        async def hang(ctx: HookContext) -> None:
            await asyncio.sleep(10)

        async def after(ctx: HookContext) -> None:
            later.append("ran")

        hooks.register(HookEvent.SESSION_START, hang, timeout=0.05, priority=1)
        hooks.register(HookEvent.SESSION_START, after)
        result = await hooks.dispatch(HookEvent.SESSION_START)
        assert len(result.errors) == 1 and isinstance(result.errors[0][1], TimeoutError)
        assert later == ["ran"] and not result.blocked

    async def test_default_timeout_applies(self) -> None:
        hooks = HookManager(default_timeout=0.05)

        async def hang(ctx: HookContext) -> None:
            await asyncio.sleep(10)

        hooks.register(HookEvent.SESSION_END, hang)
        result = await asyncio.wait_for(hooks.dispatch(HookEvent.SESSION_END), 2)
        assert result.errors

    async def test_fail_closed_blocks_on_error(self) -> None:
        hooks = HookManager()
        hooks.register(HookEvent.TASK_CREATED, _Recorder(fail=True), fail_closed=True)
        result = await hooks.dispatch(HookEvent.TASK_CREATED)
        assert result.blocked and "fail_closed" in result.reason

    async def test_errors_never_block_without_fail_closed(self) -> None:
        hooks = HookManager()
        hooks.register(HookEvent.TASK_CREATED, _Recorder(fail=True))
        result = await hooks.dispatch(HookEvent.TASK_CREATED)
        assert not result.blocked and len(result.errors) == 1

    def test_register_by_event_name_and_async_callable_object(self) -> None:
        hooks = HookManager()
        hook_id = hooks.register("session_start", _Recorder())
        assert hooks.registered(HookEvent.SESSION_START) == [hook_id]
        with pytest.raises(ValueError, match="Unknown hook event"):
            hooks.register("session_begin", _Recorder())
        with pytest.raises(ValueError, match="positive"):
            hooks.register(HookEvent.SESSION_START, _Recorder(), timeout=0)

    def test_event_list_is_thirteen(self) -> None:
        assert len(HookEvent) == 13


# ===========================================================================
# 2. ShellHook safety
# ===========================================================================


@posix_only
class TestShellHookSafety:
    async def test_environment_is_minimal(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
        monkeypatch.setenv("WIRING_PASS", "passed")
        cmd = _script(
            tmp_path,
            "env.sh",
            'cat >/dev/null\nprintf \'{"data": {"key": "%s", "pass": "%s", "event": "%s", '
            '"proc": "%s", "path": "%s"}}\' "${OPENAI_API_KEY:-}" "${WIRING_PASS:-}" '
            '"$PROMPTISE_HOOK_EVENT" "$PROMPTISE_PROCESS" "${PATH:+set}"\n',
        )
        hook = ShellHook(command=cmd, env_passthrough=["WIRING_PASS"])
        result = await hook.run(HookContext(event=HookEvent.SESSION_START, process_id="p1"))
        assert result.data == {
            "key": "",
            "pass": "passed",
            "event": "session_start",
            "proc": "p1",
            "path": "set",
        }

    async def test_inherit_env_opt_in(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WIRING_SECRET", "x1")
        cmd = _script(
            tmp_path,
            "env.sh",
            'cat >/dev/null\nprintf \'{"data": {"v": "%s"}}\' "$WIRING_SECRET"\n',
        )
        result = await ShellHook(command=cmd, inherit_env=True).run(
            HookContext(event=HookEvent.SESSION_START)
        )
        assert result.data == {"v": "x1"}

    async def test_env_reference_is_resolved_when_run(self, tmp_path, monkeypatch) -> None:
        cmd = _script(
            tmp_path, "env.sh", 'cat >/dev/null\nprintf \'{"data": {"v": "%s"}}\' "$HOOK_TOKEN"\n'
        )
        hook = ShellHook(command=cmd, env={"HOOK_TOKEN": "${WIRING_REF}"})
        monkeypatch.setenv("WIRING_REF", "resolved")
        result = await hook.run(HookContext(event=HookEvent.SESSION_START))
        assert result.data == {"v": "resolved"}

    async def test_payload_is_never_interpolated(self, tmp_path) -> None:
        marker = tmp_path / "pwned"
        cmd = _script(tmp_path, "read.sh", 'payload=$(cat)\necho "{}"\n')
        hook = ShellHook(command=[cmd])
        ctx = HookContext(
            event=HookEvent.USER_PROMPT_SUBMIT,
            data={"prompt": f"$(touch {marker}); `touch {marker}`"},
        )
        await hook.callback(ctx)
        assert not marker.exists()

    async def test_output_is_limited(self, tmp_path) -> None:
        cmd = _script(tmp_path, "big.sh", "cat >/dev/null\nhead -c 200000 /dev/zero | tr '\\0' a\n")
        with pytest.raises(ValueError, match="more than 1000 bytes"):
            await ShellHook(command=cmd, max_output_bytes=1000).run(
                HookContext(event=HookEvent.SESSION_START)
            )

    async def test_script_that_ignores_stdin_and_large_payload(self, tmp_path) -> None:
        cmd = _script(tmp_path, "quiet.sh", "echo '{}'\n")
        ctx = HookContext(event=HookEvent.USER_PROMPT_SUBMIT, data={"prompt": "x" * 500_000})
        result = await ShellHook(command=cmd).run(ctx)
        assert result.block is False

    async def test_data_must_be_an_object(self, tmp_path) -> None:
        cmd = _script(tmp_path, "bad.sh", "cat >/dev/null\necho '{\"data\": [1]}'\n")
        with pytest.raises(ValueError, match="'data' must be a JSON object"):
            await ShellHook(command=cmd).run(HookContext(event=HookEvent.SESSION_START))

    def test_invalid_construction(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            ShellHook(command="  ")
        with pytest.raises(ValueError, match="shell=True"):
            ShellHook(command=["a"], shell=True)
        with pytest.raises(ValueError, match="positive"):
            ShellHook(command="a", timeout=0)


# ===========================================================================
# 3. HookConfig / manifest
# ===========================================================================


class TestHookConfig:
    def test_exactly_one_kind(self) -> None:
        with pytest.raises(ValidationError, match="exactly one"):
            HookConfig(event="session_start")

        async def cb(ctx: HookContext) -> None: ...

        with pytest.raises(ValidationError, match="exactly one"):
            HookConfig(event="session_start", command="x", callback=cb)

    def test_sync_callback_and_unknown_event_rejected(self) -> None:
        with pytest.raises(ValidationError, match="async function"):
            HookConfig(event="session_start", callback=lambda ctx: None)
        with pytest.raises(ValidationError, match="unknown hook event"):
            HookConfig(event="on_start", command="x")

    def test_env_masked_in_json_dump(self) -> None:
        cfg = HookConfig(event="session_start", command="x", env={"A": "secret", "B": "${VAR}"})
        dumped = cfg.model_dump(mode="json")
        assert dumped["env"] == {"A": "**********", "B": "${VAR}"}
        assert "secret" not in repr(cfg)

    def test_manifest_hooks_section(self) -> None:
        manifest = AgentManifestSchema(
            name="m",
            hooks=[{"event": "task_created", "command": ["./audit.sh"], "fail_closed": True}],
        )
        config = manifest_to_process_config(manifest)
        assert config.hooks[0].event == "task_created"
        assert config.hooks[0].fail_closed is True
        with pytest.raises(ValidationError, match="Python callbacks"):
            AgentManifestSchema(name="m", hooks=[{"event": "task_created", "callback": "x"}])
        with pytest.raises(ValidationError, match="hooks\\[0\\]"):
            AgentManifestSchema(name="m", hooks=[{"event": "nope", "command": "x"}])

    def test_manifest_entrypoint_warns(self) -> None:
        with pytest.warns(FutureWarning, match="entrypoint"):
            AgentManifestSchema(name="m", entrypoint="hooks.py")


# ===========================================================================
# 4. AgentProcess dispatches the hooks
# ===========================================================================


class TestProcessHooks:
    async def test_session_start_and_end(self) -> None:
        rec = _Recorder()
        process = AgentProcess(
            "p",
            ProcessConfig(
                hooks=[
                    HookConfig(event="session_start", callback=rec),
                    HookConfig(event="session_end", callback=rec),
                ]
            ),
        )
        with _patch_build(_agent()):
            await process.start()
            await process.stop()
            await process.stop()  # no second session_end
        assert [e for e, _, _ in rec.calls] == [HookEvent.SESSION_START, HookEvent.SESSION_END]
        assert rec.of(HookEvent.SESSION_START)[0]["restart"] is False
        assert rec.of(HookEvent.SESSION_END)[0]["reason"] == "stop"
        assert rec.calls[0][2] == "p"

    async def test_prompt_rewrite_reaches_the_agent(self) -> None:
        seen: list[str] = []
        process = AgentProcess("p", ProcessConfig())

        @process.hooks.on(HookEvent.USER_PROMPT_SUBMIT)
        async def rewrite(ctx: HookContext) -> None:
            ctx.set("prompt", "REWRITTEN " + ctx.get("event_id"))

        with _patch_build(_agent(seen=seen)):
            await process.start()
            event = _event(1)
            await process.inject(event)
            await _wait_for(lambda: len(seen) == 1)
            await process.stop()
        assert seen == [f"REWRITTEN {event.event_id}"]

    async def test_blocked_prompt_dead_letters_without_failure_or_budget(self) -> None:
        seen: list[str] = []
        process = AgentProcess(
            "p",
            ProcessConfig(budget=BudgetConfig(enabled=True, max_runs_per_day=5)),
        )
        process.hooks.register(HookEvent.USER_PROMPT_SUBMIT, _Recorder(block="no thanks"))
        with _patch_build(_agent(seen=seen)):
            await process.start()
            await process.inject(_event(1))
            await _wait_for(lambda: len(process.dead_letters) == 1)
            assert process._consecutive_failures == 0
            assert process._budget.daily_runs == 0
            await process.stop()
        assert seen == []
        letter = process.dead_letters[0]
        assert letter["reason"] == "blocked by hook" and letter["error"] == "no thanks"

    async def test_task_created_block_and_task_completed(self) -> None:
        rec = _Recorder()
        process = AgentProcess("p", ProcessConfig())
        process.hooks.register(HookEvent.TASK_COMPLETED, rec)
        with _patch_build(_agent()):
            await process.start()
            await process.inject(_event(1))
            await _wait_for(lambda: len(rec.of(HookEvent.TASK_COMPLETED)) == 1)
            assert rec.of(HookEvent.TASK_COMPLETED)[0]["outcome"] == "success"

            process.hooks.register(HookEvent.TASK_CREATED, _Recorder(block="queue closed"))
            await process.inject(_event(2))
            await process.stop()
        assert [d["reason"] for d in process.dead_letters] == ["blocked by hook"]

    async def test_file_changed_block_drops_the_event(self) -> None:
        seen: list[str] = []
        rec = _Recorder(block="ignore tmp files")
        process = AgentProcess("p", ProcessConfig())
        process.hooks.register(HookEvent.FILE_CHANGED, rec)
        with _patch_build(_agent(seen=seen)):
            await process.start()
            await process.inject(_event(1, trigger_type="file_watch", path="/x.tmp"))
            await asyncio.sleep(0.05)
            await process.stop()
        assert seen == [] and process.status()["filtered_count"] == 1
        assert rec.of(HookEvent.FILE_CHANGED)[0]["path"] == "/x.tmp"

    async def test_non_blockable_block_is_ignored(self) -> None:
        process = AgentProcess("p", ProcessConfig())
        process.hooks.register(HookEvent.SESSION_START, _Recorder(block="nope"))
        with _patch_build(_agent()):
            await process.start()
            assert process.state == ProcessState.RUNNING
            await process.stop()

    async def test_compaction_hooks(self) -> None:
        rec = _Recorder()
        process = AgentProcess(
            "p", ProcessConfig(context=ContextConfig(conversation_max_messages=2))
        )
        process.hooks.register(HookEvent.PRE_COMPACT, rec)
        process.hooks.register(HookEvent.POST_COMPACT, rec)
        seen: list[str] = []
        with _patch_build(_agent(seen=seen)):
            await process.start()
            await process.inject(_event(1))
            await _wait_for(lambda: len(seen) == 1)
            assert rec.calls == []  # 2 messages fit
            await process.inject(_event(2))
            await _wait_for(lambda: len(rec.calls) == 2)
            await process.stop()
        pre, post = rec.of(HookEvent.PRE_COMPACT)[0], rec.of(HookEvent.POST_COMPACT)[0]
        assert pre["max_messages"] == 2 and len(pre["dropping"]) == 2
        assert pre["dropping"][0]["role"] == "user"
        assert post == {"buffer_size": 2, "dropped": 2}

    async def test_hook_data_has_secrets_redacted(self) -> None:
        rec = _Recorder()
        process = AgentProcess(
            "p",
            ProcessConfig(
                secrets=SecretScopeConfig(enabled=True, secrets={"API": "sk-supersecret-123"})
            ),
        )
        process.hooks.register(HookEvent.USER_PROMPT_SUBMIT, rec)
        seen: list[str] = []
        with _patch_build(_agent(seen=seen)):
            await process.start()
            await process.inject(_event(1, note="key sk-supersecret-123"))
            await _wait_for(lambda: len(seen) == 1)
            await process.stop()
        prompt = rec.of(HookEvent.USER_PROMPT_SUBMIT)[0]["prompt"]
        assert "sk-supersecret-123" not in prompt and "[REDACTED]" in prompt
        # An unchanged (redacted) prompt doesn't replace the real one.
        assert "sk-supersecret-123" in seen[0]

    async def test_config_change_operator_and_agent(self) -> None:
        rec = _Recorder()
        process = AgentProcess("p", ProcessConfig(execution_mode=ExecutionMode.OPEN))
        process.hooks.register(HookEvent.CONFIG_CHANGE, rec)
        with _patch_build(_agent()):
            await process.start()
            process.update_instructions("new ones")
            await _wait_for(lambda: len(rec.calls) == 1)
            assert rec.calls[0][1]["source"] == "operator"
            assert rec.calls[0][1]["change"] == "instructions"

            rec.block = "agents may not rewrite themselves"
            result = await process.set_dynamic_instructions("I am free now")
            assert result.startswith("Error") and "may not" in result
            assert process._dynamic_instructions is None
            assert process._rebuild_count == 0
            await process.stop()

    async def test_shell_hook_from_config_runs(self, tmp_path) -> None:
        if sys.platform == "win32":
            pytest.skip("POSIX shell")
        out = tmp_path / "events.log"
        cmd = _script(tmp_path, "log.sh", f'cat >> "{out}"\necho >> "{out}"\necho "{{}}"\n')
        process = AgentProcess(
            "p", ProcessConfig(hooks=[HookConfig(event="session_start", command=[cmd])])
        )
        with _patch_build(_agent()):
            await process.start()
            await process.stop()
        payload = json.loads(out.read_text().strip())
        assert payload["event"] == "session_start" and payload["process_id"] == "p"

    async def test_runtime_shared_hooks(self) -> None:
        shared = HookManager()
        rec = _Recorder()
        shared.register(HookEvent.SESSION_START, rec)
        runtime = AgentRuntime(hooks=shared)
        await runtime.add_process("a", ProcessConfig())
        await runtime.add_process("b", ProcessConfig())
        with _patch_build(_agent()):
            await runtime.start_all()
            await runtime.shutdown()
        assert sorted(pid for _, _, pid in rec.calls) == ["a", "b"]

    async def test_permission_hooks(self) -> None:
        from promptise.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

        asked: list[str] = []

        async def handler(request: ApprovalRequest) -> ApprovalDecision:
            asked.append(request.tool_name)
            return ApprovalDecision(approved=True)

        policy = ApprovalPolicy(tools=["send_*"], handler=handler)
        rec = _Recorder()
        process = AgentProcess("p", ProcessConfig(approval=policy))
        process.hooks.register(HookEvent.PERMISSION_REQUEST, _Recorder(block="weekend"))
        process.hooks.register(HookEvent.PERMISSION_DENIED, rec)
        hooked = process._hooked_approval(policy)
        assert hooked is not policy and policy.on_decision is None
        request = ApprovalRequest(request_id="r1", tool_name="send_email", arguments={})
        decision = await hooked.handler.request_approval(request)
        assert decision.approved is False and "weekend" in decision.reason
        assert asked == []
        await hooked.on_decision(request, decision)
        assert rec.of(HookEvent.PERMISSION_DENIED)[0]["tool_name"] == "send_email"

    async def test_permission_hooks_wired_into_the_agent_build(self) -> None:
        from promptise.approval import ApprovalDecision, ApprovalPolicy

        async def handler(request: Any) -> ApprovalDecision:
            return ApprovalDecision(approved=True)

        policy = ApprovalPolicy(tools=["x"], handler=handler)
        process = AgentProcess("p", ProcessConfig(approval=policy))
        process.hooks.register(HookEvent.PERMISSION_REQUEST, _Recorder())
        build = AsyncMock(return_value=_agent())
        with patch(BUILD_TARGET, build):
            await process.start()
            await process.stop()
        passed = build.call_args.kwargs["approval"]
        assert isinstance(passed, ApprovalPolicy) and passed is not policy


# ===========================================================================
# 5. Rewind
# ===========================================================================


async def _checkpointed_journal() -> tuple[InMemoryJournal, str]:
    """A journal shaped like a process's: checkpoints after each run."""
    journal = InMemoryJournal()
    await journal.checkpoint(
        "p",
        {
            "context_state": {"step": 1},
            "conversation": [{"role": "user", "content": "one"}],
        },
    )
    target = JournalEntry(process_id="p", entry_type="invocation_result", data={})
    await journal.append(target)
    await journal.append(
        JournalEntry(process_id="p", entry_type="context_update", data={"key": "step", "value": 2})
    )
    await journal.checkpoint(
        "p",
        {
            "context_state": {"step": 2, "extra": True},
            "conversation": [
                {"role": "user", "content": "one"},
                {"role": "user", "content": "two"},
            ],
        },
    )
    return journal, target.entry_id


class TestRewindEngine:
    async def test_checkpoints_are_read(self) -> None:
        journal, target = await _checkpointed_journal()
        result = await RewindEngine(journal).apply(
            process_id="p", target_entry_id=target, mode=RewindMode.BOTH, record=False
        )
        assert result.context_state == {"step": 1}
        assert result.conversation == [{"role": "user", "content": "one"}]

    async def test_conversation_only_keeps_current_state(self) -> None:
        journal, target = await _checkpointed_journal()
        result = await RewindEngine(journal).apply(
            process_id="p", target_entry_id=target, mode="conversation_only", record=False
        )
        assert result.conversation == [{"role": "user", "content": "one"}]
        assert result.context_state == {"step": 2, "extra": True}

    async def test_code_only_keeps_current_conversation(self) -> None:
        journal, target = await _checkpointed_journal()
        result = await RewindEngine(journal).apply(
            process_id="p", target_entry_id=target, mode=RewindMode.CODE_ONLY, record=False
        )
        assert result.context_state == {"step": 1}
        assert len(result.conversation) == 2

    async def test_rewind_ids_are_unique_and_plan_serialises(self) -> None:
        journal, target = await _checkpointed_journal()
        engine = RewindEngine(journal)
        a = await engine.apply(process_id="p", target_entry_id=target, mode=RewindMode.BOTH)
        b = await engine.apply(process_id="p", target_entry_id=target, mode=RewindMode.BOTH)
        assert a.rewind_entry_id != b.rewind_entry_id
        plan = (await engine.plan(process_id="p", target_entry_id=target)).to_dict()
        assert plan["target_entry_type"] == "invocation_result"
        assert plan["conversation_entries_affected"] >= 1
        json.dumps(plan)


def _counter_agent(process_ref: list[AgentProcess], seen: list[str]) -> AsyncMock:
    """An agent whose every run writes ``runs`` into the context state."""

    def write() -> None:
        process = process_ref[0]
        process.context.put("runs", (process.context.get("runs") or 0) + 1)

    return _agent(seen=seen, write=write)


class TestProcessRewind:
    async def _two_runs(self, journal: JournalConfig, **config: Any) -> tuple[AgentProcess, list]:
        ref: list[AgentProcess] = []
        seen: list[str] = []
        process = AgentProcess("rw", ProcessConfig(journal=journal, **config))
        ref.append(process)
        agent = _counter_agent(ref, seen)
        self._patch = _patch_build(agent)
        self._patch.start()
        await process.start()
        await process.inject(_event(1))
        await _wait_for(lambda: len(seen) == 1 and process._inflight == 0)
        await process.context.flush_update_hooks()
        await asyncio.sleep(0.02)
        await process.inject(_event(2))
        await _wait_for(lambda: len(seen) == 2 and process._inflight == 0)
        await process.context.flush_update_hooks()
        await asyncio.sleep(0.02)
        return process, seen

    async def _second_run_start(self, process: AgentProcess) -> str:
        entries = await process._journal.read("rw", entry_type="state_transition")
        del entries
        results = await process._journal.read("rw")
        # The first context_update written by the second run.
        updates = [e for e in results if e.entry_type == "context_update"]
        return updates[1].entry_id

    async def test_rewind_both_while_running(self) -> None:
        process, _ = await self._two_runs(MEMORY_JOURNAL)
        try:
            assert process.context.get("runs") == 2
            target = await self._second_run_start(process)
            states: list[str] = []
            process.lifecycle.add_listener(lambda t: states.append(t.to_state.value))
            result = await process.rewind(target, "both", actor="tester")
            assert process.context.get("runs") == 1
            assert len(process._conversation_buffer) == 2  # first run only
            assert process.state == ProcessState.RUNNING
            assert states == ["suspended", "running"]
            entries = await process._journal.read("rw", entry_type="rewind")
            assert entries[-1].data["actor"] == "tester"
            assert result.rewind_entry_id == entries[-1].entry_id
            # Non-destructive: the original entries are still there.
            assert any(e.entry_id == target for e in await process._journal.read("rw"))
        finally:
            await process.stop()
            self._patch.stop()

    async def test_rewound_state_survives_recovery(self, tmp_path) -> None:
        journal = JournalConfig(path=str(tmp_path / "j"))
        process, _ = await self._two_runs(journal)
        try:
            target = await self._second_run_start(process)
            await process.rewind(target, RewindMode.CODE_ONLY)
            await process.stop()
        finally:
            self._patch.stop()
        fresh = AgentProcess("rw", ProcessConfig(journal=journal))
        with _patch_build(_agent()):
            await fresh.start()
            await fresh.stop()
        assert fresh.context.get("runs") == 1
        assert fresh.status()["conversation_messages"] == 4  # code_only kept it

    async def test_rewind_never_refunds_budget(self) -> None:
        process, _ = await self._two_runs(
            MEMORY_JOURNAL, budget=BudgetConfig(enabled=True, max_runs_per_day=10)
        )
        try:
            target = await self._second_run_start(process)
            await process.rewind(target)
            assert process._budget.daily_runs == 2
        finally:
            await process.stop()
            self._patch.stop()

    async def test_summarize_and_cancel(self) -> None:
        process, _ = await self._two_runs(MEMORY_JOURNAL)
        try:
            target = await self._second_run_start(process)
            before = len(process._conversation_buffer)
            preview = await process.rewind(target, "cancel")
            assert preview.plan is not None and process.context.get("runs") == 2
            await process.rewind(target, "summarize")
            assert process.context.get("runs") == 2
            history = await process._conversation_buffer.async_snapshot()
            assert len(history) == before + 1
            assert history[-1]["content"].startswith("[rewind summary]")
        finally:
            await process.stop()
            self._patch.stop()

    async def test_rewind_errors(self) -> None:
        with pytest.raises(RuntimeError, match="no journal"):
            await AgentProcess("x", ProcessConfig()).rewind("e1")
        process = AgentProcess("x", ProcessConfig(journal=MEMORY_JOURNAL))
        with pytest.raises(ValueError, match="not found"):
            await process.rewind("missing")
        with pytest.raises(ValueError):
            await process.rewind_plan("missing", "sideways")


class TestRewindAPI:
    async def test_routes_and_client(self) -> None:
        runtime = AgentRuntime()
        ref: list[AgentProcess] = []
        seen: list[str] = []
        process = await runtime.add_process("rw", ProcessConfig(journal=MEMORY_JOURNAL))
        await runtime.add_process("plain", ProcessConfig())
        ref.append(process)
        api = OrchestrationAPI(runtime, host="127.0.0.1", port=0)
        await api.start()
        base = f"http://127.0.0.1:{api.port}"
        try:
            with _patch_build(_counter_agent(ref, seen)):
                await process.start()
                await process.inject(_event(1))
                await _wait_for(lambda: len(seen) == 1 and process._inflight == 0)
                await process.context.flush_update_hooks()
                async with OrchestrationClient(base) as client:
                    entries = await client.get_journal("rw", limit=500)
                    assert all(e["entry_id"] for e in entries)
                    target = next(
                        e["entry_id"] for e in entries if e["entry_type"] == "context_update"
                    )
                    plan = await client.rewind_plan("rw", target, mode="both")
                    assert plan["entries_affected"] >= 1
                    done = await client.rewind("rw", target, actor="ops@example.com")
                    assert done["state"] == "running" and done["rewind_entry_id"]
                    assert process.context.get("runs") is None

                    with pytest.raises(OrchestrationAPIError) as exc:
                        await client.rewind("rw", "nope")
                    assert exc.value.code == "ENTRY_NOT_FOUND"
                    with pytest.raises(OrchestrationAPIError) as exc:
                        await client.rewind("plain", "x")
                    assert exc.value.code == "JOURNAL_DISABLED"
                    with pytest.raises(OrchestrationAPIError) as exc:
                        await client.rewind("rw", target, mode="sideways")
                    assert exc.value.code == "INVALID_FIELD"
                    with pytest.raises(OrchestrationAPIError) as exc:
                        await client.rewind_plan("missing", target)
                    assert exc.value.code == "PROCESS_NOT_FOUND"
                await process.stop()
        finally:
            await api.stop()


class TestShutdownRoute:
    async def test_on_shutdown_route(self) -> None:
        called = asyncio.Event()
        api = OrchestrationAPI(AgentRuntime(), host="127.0.0.1", port=0, on_shutdown=called.set)
        await api.start()
        try:
            async with OrchestrationClient(f"http://127.0.0.1:{api.port}") as client:
                assert await client.shutdown() == {"status": "shutting_down"}
            await asyncio.wait_for(called.wait(), 2)
        finally:
            await api.stop()

    async def test_async_callback_and_auth(self) -> None:
        done: list[bool] = []

        async def shutdown() -> None:
            done.append(True)

        api = OrchestrationAPI(
            AgentRuntime(), host="127.0.0.1", port=0, auth_token="t0k", on_shutdown=shutdown
        )
        await api.start()
        try:
            base = f"http://127.0.0.1:{api.port}"
            async with OrchestrationClient(base) as client:
                with pytest.raises(OrchestrationAPIError) as exc:
                    await client.shutdown()
                assert exc.value.status_code == 401
            async with OrchestrationClient(base, token="t0k") as client:
                await client.shutdown()
            await _wait_for(lambda: done == [True])
        finally:
            await api.stop()

    async def test_route_absent_without_callback(self) -> None:
        api = OrchestrationAPI(AgentRuntime(), host="127.0.0.1", port=0)
        await api.start()
        try:
            async with OrchestrationClient(f"http://127.0.0.1:{api.port}") as client:
                with pytest.raises(OrchestrationAPIError) as exc:
                    await client.shutdown()
                assert exc.value.status_code == 404
        finally:
            await api.stop()
        with pytest.raises(TypeError):
            OrchestrationAPI(AgentRuntime(), on_shutdown="nope")  # type: ignore[arg-type]


# ===========================================================================
# 6. Distributed mode
# ===========================================================================


class TestDistributedConfig:
    def test_multicast_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="multicast.*not implemented"):
            DistributedConfig(enabled=True, discovery_method="multicast")

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"coordinator": True, "coordinator_url": "http://c:9100"}, "not both"),
            ({"static_nodes": {"n": "http://n:1"}}, "static_nodes is read by a coordinator"),
            (
                {"discovery_method": "static", "coordinator_url": "http://127.0.0.1:1"},
                "don't register",
            ),
            ({"transport_host": "0.0.0.0"}, "auth_token"),
            ({"coordinator_url": "http://coord.internal:9100"}, "auth_token"),
            (
                {"transport_host": "0.0.0.0", "auth_token": "t", "coordinator": True},
                "advertise_url",
            ),
            ({"transport_port": 80}, "privileged"),
            ({"coordinator_url": "ftp://x"}, "http"),
            ({"auth_token": "**********"}, "masked"),
        ],
    )
    def test_inconsistent_combinations_fail_loudly(self, kwargs, match) -> None:
        with pytest.raises(ValidationError, match=match):
            DistributedConfig(enabled=True, **kwargs)

    def test_token_masked_and_presets(self) -> None:
        cfg = DistributedConfig(auth_token="literal")
        assert cfg.model_dump(mode="json")["auth_token"] == "**********"
        assert DistributedConfig(auth_token="${T}").model_dump(mode="json")["auth_token"] == "${T}"
        assert DEFAULT_PRODUCTION_CONFIG.distributed.enabled is False

    def test_unknown_keys_warn(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            DistributedConfig(coordinater_url="x")
        assert any("coordinater_url" in str(w.message) for w in caught)


async def _cluster_runtime(**distributed: Any) -> AgentRuntime:
    return AgentRuntime(
        RuntimeConfig(
            processes={"worker": ProcessConfig()},
            distributed=DistributedConfig(enabled=True, transport_port=0, **distributed),
        )
    )


class TestDistributedRuntime:
    async def test_standalone_node_starts_and_stops_its_transport(self) -> None:
        runtime = await _cluster_runtime(auth_token=TOKEN, node_id="solo")
        with _patch_build(_agent()):
            await runtime.start_all()
            node = runtime.cluster
            assert node is not None and node.role == "standalone"
            url = node.url
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{url}/status") as r:
                    assert r.status == 401
                async with s.get(
                    f"{url}/status", headers={"Authorization": f"Bearer {TOKEN}"}
                ) as r:
                    assert r.status == 200 and (await r.json())["node_id"] == "solo"
            assert runtime.status()["cluster"]["role"] == "standalone"
            await runtime.shutdown()
        assert runtime.cluster is None
        async with aiohttp.ClientSession() as s:
            with pytest.raises(aiohttp.ClientError):
                async with s.get(f"{url}/health") as r:
                    pass

    async def test_node_registers_with_coordinator(self, monkeypatch) -> None:
        monkeypatch.setenv("WIRING_CLUSTER_TOKEN", TOKEN)
        coord = await _cluster_runtime(
            coordinator=True, auth_token="${WIRING_CLUSTER_TOKEN}", node_id="coord"
        )
        with _patch_build(_agent()):
            await coord.start_all()
            try:
                node = await _cluster_runtime(
                    coordinator_url=coord.cluster.url,
                    auth_token=TOKEN,
                    node_id="n1",
                    heartbeat_interval=0.1,
                )
                await node.start_all()
                assert node.cluster.registered is True
                coordinator = coord.cluster.coordinator
                assert set(coordinator.nodes) == {"coord", "n1"}
                health = await coordinator.check_health()
                assert health["n1"]["status"] == "healthy"
                # The coordinator drives the node with the cluster token.
                await coordinator.stop_process_on_node("n1", "worker")
                assert node.get_process("worker").state == ProcessState.STOPPED

                await node.shutdown()  # unregisters
                assert "n1" not in coordinator.nodes
            finally:
                await coord.shutdown()

    async def test_wrong_token_is_not_registered(self) -> None:
        coord = await _cluster_runtime(coordinator=True, auth_token=TOKEN, node_id="coord")
        with _patch_build(_agent()):
            await coord.start_all()
            try:
                node = await _cluster_runtime(
                    coordinator_url=coord.cluster.url, auth_token="wrong", node_id="evil"
                )
                await node.start_all()
                assert node.cluster.registered is False
                assert "401" in node.cluster.last_registration_error
                assert "evil" not in coord.cluster.coordinator.nodes
                await node.shutdown()
            finally:
                await coord.shutdown()

    async def test_static_discovery_and_registry_expiry(self) -> None:
        static = await _cluster_runtime(
            coordinator=True,
            discovery_method="static",
            static_nodes={"far": "http://127.0.0.1:1"},
            auth_token=TOKEN,
            node_id="coord",
        )
        await static.start_distributed()
        try:
            assert set(static.cluster.coordinator.nodes) == {"coord", "far"}
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{static.cluster.url}/cluster/nodes",
                    json={"node_id": "n", "url": "http://127.0.0.1:2"},
                    headers={"Authorization": f"Bearer {TOKEN}"},
                ) as r:
                    assert r.status == 409
        finally:
            await static.shutdown()

        registry = await _cluster_runtime(
            coordinator=True, auth_token=TOKEN, node_id="coord", node_timeout=0.05
        )
        await registry.start_distributed()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{registry.cluster.url}/cluster/nodes",
                    json={"node_id": "gone", "url": "http://127.0.0.1:2"},
                    headers={"Authorization": f"Bearer {TOKEN}"},
                ) as r:
                    assert r.status == 200
                async with s.post(
                    f"{registry.cluster.url}/cluster/nodes",
                    json={"node_id": "bad", "url": "file:///etc/passwd"},
                    headers={"Authorization": f"Bearer {TOKEN}"},
                ) as r:
                    assert r.status == 422
            assert "gone" in registry.cluster.coordinator.nodes
            await asyncio.sleep(0.1)
            assert await registry.cluster.prune() == ["gone"]
        finally:
            await registry.shutdown()

    async def test_unset_token_variable_fails_start(self, monkeypatch) -> None:
        monkeypatch.delenv("WIRING_UNSET_TOKEN", raising=False)
        runtime = await _cluster_runtime(auth_token="${WIRING_UNSET_TOKEN}")
        with pytest.raises(ValueError, match="auth_token"):
            await runtime.start_all()
        assert runtime.cluster is None


# ===========================================================================
# 7. Leftovers
# ===========================================================================


class _QueueTrigger:
    def __init__(self) -> None:
        self.trigger_id = "dyn"
        self.trigger_type = "wiring_queue"
        self.queue: asyncio.Queue[TriggerEvent] = asyncio.Queue()
        self.stopped = 0

    async def start(self) -> None: ...

    async def stop(self) -> None:
        self.stopped += 1

    async def wait_for_next(self) -> TriggerEvent:
        return await self.queue.get()


@pytest.fixture
def wiring_triggers():
    made: list[_QueueTrigger] = []

    def factory(config, *, event_bus=None, broker=None):
        made.append(_QueueTrigger())
        made[-1].trigger_id = f"dyn-{len(made)}"
        return made[-1]

    register_trigger_type("wiring_queue", factory, overwrite=True)
    yield made
    unregister_trigger_type("wiring_queue")


class TestMetaToolsUsePublicMethods:
    async def test_remove_trigger_cancels_the_listener(self, wiring_triggers) -> None:
        from promptise.runtime.meta_tools import _add_trigger, _remove_trigger

        process = AgentProcess(
            "p",
            ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                triggers=[TriggerConfig(type="wiring_queue")],
            ),
        )
        with _patch_build(_agent()):
            await process.start()
            reply = await _add_trigger(process, trigger_type="wiring_queue")
            assert "added" in reply
            trigger = process._dynamic_triggers[0]
            listener = process._listener_by_trigger[id(trigger)]
            assert "removed" in await _remove_trigger(process, trigger.trigger_id)
            await asyncio.sleep(0)
            assert listener.done() and trigger.stopped == 1
            # The agent can't remove the operator's config triggers.
            config_id = process._triggers[0].trigger_id
            assert "not found" in await _remove_trigger(process, config_id)
            assert len(process.config.triggers) == 1
            await process.stop()

    async def test_rollback_cancels_dynamic_listeners(self, wiring_triggers) -> None:
        process = AgentProcess("p", ProcessConfig(execution_mode=ExecutionMode.OPEN))
        with _patch_build(_agent()):
            await process.start()
            await process.add_dynamic_trigger({"type": "wiring_queue"})
            listener = next(iter(process._listener_by_trigger.values()))
            await process.rollback()
            await asyncio.sleep(0)
            assert listener.done() and process._dynamic_triggers == []
            await process.stop()

    async def test_spawned_process_inherits_guardrails(self) -> None:
        from promptise.runtime.meta_tools import _spawn_process

        runtime = AgentRuntime()
        parent = await runtime.add_process(
            "parent",
            ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(allow_process_spawn=True),
                budget=BudgetConfig(enabled=True, max_runs_per_day=3),
            ),
        )
        rec = _Recorder()
        parent.hooks.register(HookEvent.SUBAGENT_START, rec)
        parent.hooks.register(HookEvent.SUBAGENT_STOP, rec)
        with _patch_build(_agent()):
            reply = await _spawn_process(parent, runtime, "kid", "help out", execution_mode="open")
            assert "spawned" in reply
            kid = runtime.get_process("kid")
            assert kid.config.budget.max_runs_per_day == 3
            assert kid.config.open_mode.allow_process_spawn is False
            assert parent.spawned_processes == ["kid"]
            await runtime.stop_process("kid")
            await _wait_for(lambda: len(rec.of(HookEvent.SUBAGENT_STOP)) == 1)

            rec.block = "no more"
            reply = await _spawn_process(parent, runtime, "kid2", "x")
            assert "blocked" in reply and "kid2" not in runtime.processes
            await runtime.shutdown()


class TestMem0CloseKeepsMemories:
    async def test_close_does_not_reset(self) -> None:
        from promptise.memory import Mem0Provider, MemoryScope

        client = MagicMock()
        provider = object.__new__(Mem0Provider)
        provider._client = client
        provider._user_id = "u"
        provider._agent_id = None
        provider._closed = False
        provider._filters_api = False
        provider.scope = MemoryScope.SHARED
        await provider.close()
        client.reset.assert_not_called()
        client.close.assert_called_once()
        assert provider._closed is True

    async def test_close_without_client_close(self) -> None:
        from promptise.memory import Mem0Provider, MemoryScope

        class Client:
            def __init__(self) -> None:
                self.reset_called = False

            def reset(self) -> None:
                self.reset_called = True

        client = Client()
        provider = object.__new__(Mem0Provider)
        provider._client = client
        provider._closed = False
        provider.scope = MemoryScope.SHARED
        await provider.close()
        assert client.reset_called is False


class TestBudgetSurvivesRestart:
    def test_snapshot_restore_round_trip_and_validation(self) -> None:
        cfg = BudgetConfig(enabled=True, max_runs_per_day=5)
        a = BudgetState(cfg)
        a.daily_runs, a.daily_cost, a.daily_tool_calls, a.run_tool_calls = 4, 2.5, 7, 3
        b = BudgetState(cfg)
        b.restore(a.snapshot())
        assert (b.daily_runs, b.daily_cost, b.daily_tool_calls) == (4, 2.5, 7)
        assert b.run_tool_calls == 0
        assert b.last_daily_reset == a.last_daily_reset
        with pytest.raises(ValueError):
            b.restore({"daily_runs": -1})
        with pytest.raises(ValueError):
            b.restore({"daily_runs": "many"})
        assert b.daily_runs == 4  # unchanged

    def test_daily_reset_honours_the_reset_hour(self) -> None:
        state = BudgetState(BudgetConfig(enabled=True, daily_reset_hour_utc=6))
        now = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)
        state.last_daily_reset = now - timedelta(hours=5)  # 22:00 the day before
        assert state._reset_due(now) is False  # before 06:00: same budget day
        assert state._reset_due(now.replace(hour=7)) is True
        state.last_daily_reset = now.replace(hour=6, minute=30)
        assert state._reset_due(now.replace(hour=23)) is False

    async def test_restart_does_not_reset_daily_limits(self, tmp_path) -> None:
        journal = JournalConfig(path=str(tmp_path / "j"))
        config: dict[str, Any] = {
            "journal": journal,
            "budget": BudgetConfig(enabled=True, max_runs_per_day=2, on_exceeded="pause"),
        }
        seen: list[str] = []
        first = AgentProcess("b", ProcessConfig(**config))
        with _patch_build(_agent(seen=seen)):
            await first.start()
            await first.inject(_event(1))
            await first.inject(_event(2))
            await _wait_for(lambda: len(seen) == 2)
            await first.stop()

        second = AgentProcess("b", ProcessConfig(**config))
        with _patch_build(_agent(seen=seen)):
            await second.start()
            assert second.recovered["budget_restored"] is True
            assert second._budget.daily_runs == 2
            await second.inject(_event(3))
            await _wait_for(lambda: second.state == ProcessState.SUSPENDED)
            await second.stop()
        assert len(seen) == 2  # the third run was refused by the daily limit

    async def test_failed_runs_are_checkpointed(self, tmp_path) -> None:
        journal = JournalConfig(path=str(tmp_path / "j"))

        async def boom(inp: Any, config: Any = None) -> Any:
            raise RuntimeError("model down")

        agent = _agent()
        agent.ainvoke = AsyncMock(side_effect=boom)
        process = AgentProcess(
            "f",
            ProcessConfig(
                journal=journal,
                budget=BudgetConfig(enabled=True, max_runs_per_day=9),
                max_consecutive_failures=5,
            ),
        )
        with _patch_build(agent):
            await process.start()
            await process.inject(_event(1))
            await _wait_for(lambda: len(process.dead_letters) == 1)
        # No clean stop: the checkpoint after the failure holds the run.
        checkpoint = await FileJournal(str(tmp_path / "j")).last_checkpoint("f")
        assert checkpoint["budget"]["daily_runs"] == 1
        with _patch_build(agent):
            await process.stop()


def test_env_is_clean() -> None:
    # Guard: the tests above must not leave cluster tokens in the environment.
    assert "WIRING_CLUSTER_TOKEN" not in os.environ
