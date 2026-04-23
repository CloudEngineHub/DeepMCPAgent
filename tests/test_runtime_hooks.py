"""Tests for runtime lifecycle hooks (HookManager + ShellHook)."""

from __future__ import annotations

import pytest

from promptise.runtime.hooks import (
    DispatchResult,
    HookBlocked,
    HookContext,
    HookEvent,
    HookManager,
)
from promptise.runtime.shell_hook import ShellHook, ShellHookResult

# ---------------------------------------------------------------------------
# HookManager core
# ---------------------------------------------------------------------------


class TestHookManager:
    @pytest.mark.asyncio
    async def test_register_and_dispatch(self):
        hooks = HookManager()
        fired: list[str] = []

        async def cb(ctx: HookContext) -> None:
            fired.append(ctx.data.get("msg", ""))

        hook_id = hooks.register(HookEvent.SESSION_START, cb)
        assert hook_id.startswith("hook-")

        result = await hooks.dispatch(HookEvent.SESSION_START, data={"msg": "hi"})
        assert result.fired == 1
        assert result.blocked is False
        assert fired == ["hi"]

    @pytest.mark.asyncio
    async def test_multiple_hooks_run_in_priority_order(self):
        hooks = HookManager()
        order: list[int] = []

        async def low(ctx: HookContext) -> None:
            order.append(1)

        async def high(ctx: HookContext) -> None:
            order.append(2)

        async def middle(ctx: HookContext) -> None:
            order.append(3)

        hooks.register(HookEvent.USER_PROMPT_SUBMIT, low, priority=0)
        hooks.register(HookEvent.USER_PROMPT_SUBMIT, high, priority=10)
        hooks.register(HookEvent.USER_PROMPT_SUBMIT, middle, priority=5)

        await hooks.dispatch(HookEvent.USER_PROMPT_SUBMIT)
        assert order == [2, 3, 1]  # 10, 5, 0

    @pytest.mark.asyncio
    async def test_once_hook_auto_removes(self):
        hooks = HookManager()
        fired: list[int] = []

        async def cb(ctx: HookContext) -> None:
            fired.append(1)

        hooks.register(HookEvent.SESSION_START, cb, once=True)
        assert len(hooks.registered(HookEvent.SESSION_START)) == 1

        await hooks.dispatch(HookEvent.SESSION_START)
        await hooks.dispatch(HookEvent.SESSION_START)
        await hooks.dispatch(HookEvent.SESSION_START)

        assert fired == [1]  # only fired once
        assert len(hooks.registered(HookEvent.SESSION_START)) == 0

    @pytest.mark.asyncio
    async def test_hook_blocked_short_circuits(self):
        hooks = HookManager()
        ran: list[str] = []

        async def first(ctx: HookContext) -> None:
            ran.append("first")
            raise HookBlocked("nope")

        async def second(ctx: HookContext) -> None:
            ran.append("second")  # should not be reached

        hooks.register(HookEvent.PERMISSION_REQUEST, first, priority=10)
        hooks.register(HookEvent.PERMISSION_REQUEST, second, priority=5)

        result = await hooks.dispatch(HookEvent.PERMISSION_REQUEST)
        assert result.blocked is True
        assert result.reason == "nope"
        assert ran == ["first"]

    @pytest.mark.asyncio
    async def test_broken_hook_does_not_kill_dispatch(self):
        hooks = HookManager()
        ran: list[str] = []

        async def broken(ctx: HookContext) -> None:
            raise RuntimeError("kaboom")

        async def good(ctx: HookContext) -> None:
            ran.append("ok")

        hooks.register(HookEvent.SESSION_END, broken, priority=10)
        hooks.register(HookEvent.SESSION_END, good, priority=5)

        result = await hooks.dispatch(HookEvent.SESSION_END)
        assert ran == ["ok"]
        assert len(result.errors) == 1
        assert "kaboom" in str(result.errors[0][1])
        assert result.fired == 1

    @pytest.mark.asyncio
    async def test_decorator_form(self):
        hooks = HookManager()
        fired: list[str] = []

        @hooks.on(HookEvent.SESSION_START)
        async def greet(ctx: HookContext) -> None:
            fired.append("hi")

        await hooks.dispatch(HookEvent.SESSION_START)
        assert fired == ["hi"]

    @pytest.mark.asyncio
    async def test_remove_by_tag(self):
        hooks = HookManager()

        async def cb(ctx: HookContext) -> None:
            pass

        hooks.register(HookEvent.SESSION_START, cb, tag="audit")
        hooks.register(HookEvent.SESSION_END, cb, tag="audit")
        hooks.register(HookEvent.SESSION_START, cb, tag="other")

        removed = await hooks.remove_by_tag("audit")
        assert removed == 2
        assert len(hooks.registered(HookEvent.SESSION_START)) == 1
        assert len(hooks.registered(HookEvent.SESSION_END)) == 0

    @pytest.mark.asyncio
    async def test_data_mutation_visible_to_later_hooks(self):
        hooks = HookManager()

        async def first(ctx: HookContext) -> None:
            ctx.set("prompt", ctx.get("prompt", "") + " [audited]")

        async def second(ctx: HookContext) -> None:
            assert ctx.get("prompt") == "hello [audited]"

        hooks.register(HookEvent.USER_PROMPT_SUBMIT, first, priority=10)
        hooks.register(HookEvent.USER_PROMPT_SUBMIT, second, priority=5)

        await hooks.dispatch(HookEvent.USER_PROMPT_SUBMIT, data={"prompt": "hello"})

    def test_register_rejects_sync_callback(self):
        hooks = HookManager()

        def sync_cb(ctx):
            pass

        with pytest.raises(TypeError, match="async function"):
            hooks.register(HookEvent.SESSION_START, sync_cb)

    @pytest.mark.asyncio
    async def test_all_lifecycle_events_exist(self):
        # Sanity check: every promised event in HookEvent dispatches
        # cleanly even with no hooks.
        hooks = HookManager()
        for event in HookEvent:
            result = await hooks.dispatch(event)
            assert isinstance(result, DispatchResult)
            assert result.fired == 0
            assert result.blocked is False


# ---------------------------------------------------------------------------
# ShellHook
# ---------------------------------------------------------------------------


class TestShellHook:
    @pytest.mark.asyncio
    async def test_run_returns_empty_when_script_silent(self, tmp_path):
        script = tmp_path / "silent.sh"
        script.write_text("#!/usr/bin/env bash\nexit 0\n")
        script.chmod(0o755)

        hook = ShellHook(command=str(script))
        ctx = HookContext(event=HookEvent.SESSION_START, data={"foo": "bar"})
        result = await hook.run(ctx)
        assert isinstance(result, ShellHookResult)
        assert result.block is False
        assert result.data is None

    @pytest.mark.asyncio
    async def test_run_parses_json_response(self, tmp_path):
        script = tmp_path / "echo.sh"
        script.write_text(
            '#!/usr/bin/env bash\necho \'{"block": false, "data": {"injected": true}}\'\n'
        )
        script.chmod(0o755)

        hook = ShellHook(command=str(script))
        ctx = HookContext(event=HookEvent.USER_PROMPT_SUBMIT)
        result = await hook.run(ctx)
        assert result.data == {"injected": True}
        assert result.block is False

    @pytest.mark.asyncio
    async def test_callback_mutates_context_data(self, tmp_path):
        script = tmp_path / "rewrite.sh"
        script.write_text('#!/usr/bin/env bash\necho \'{"data": {"prompt": "REWRITTEN"}}\'\n')
        script.chmod(0o755)

        hook = ShellHook(command=str(script))
        ctx = HookContext(event=HookEvent.USER_PROMPT_SUBMIT, data={"prompt": "original"})
        await hook.callback(ctx)
        assert ctx.data == {"prompt": "REWRITTEN"}

    @pytest.mark.asyncio
    async def test_callback_block_raises(self, tmp_path):
        script = tmp_path / "block.sh"
        script.write_text('#!/usr/bin/env bash\necho \'{"block": true, "reason": "bad prompt"}\'\n')
        script.chmod(0o755)

        hook = ShellHook(command=str(script))
        ctx = HookContext(event=HookEvent.USER_PROMPT_SUBMIT)
        with pytest.raises(HookBlocked, match="bad prompt"):
            await hook.callback(ctx)

    @pytest.mark.asyncio
    async def test_run_timeout(self, tmp_path):
        script = tmp_path / "slow.sh"
        script.write_text("#!/usr/bin/env bash\nsleep 5\n")
        script.chmod(0o755)

        hook = ShellHook(command=str(script), timeout=0.2)
        ctx = HookContext(event=HookEvent.SESSION_START)
        with pytest.raises(TimeoutError):
            await hook.run(ctx)

    @pytest.mark.asyncio
    async def test_run_nonzero_exit_raises(self, tmp_path):
        script = tmp_path / "fail.sh"
        script.write_text("#!/usr/bin/env bash\necho 'oops' >&2\nexit 1\n")
        script.chmod(0o755)

        hook = ShellHook(command=str(script))
        ctx = HookContext(event=HookEvent.SESSION_START)
        with pytest.raises(RuntimeError, match="exited 1"):
            await hook.run(ctx)

    @pytest.mark.asyncio
    async def test_full_pipeline_via_hookmanager(self, tmp_path):
        script = tmp_path / "audit.sh"
        # Read stdin (the JSON event), write a fixed response.
        script.write_text(
            '#!/usr/bin/env bash\ncat > /dev/null\necho \'{"data": {"prompt": "hooked"}}\'\n'
        )
        script.chmod(0o755)

        hooks = HookManager()
        hook = ShellHook(command=str(script))
        hooks.register(HookEvent.USER_PROMPT_SUBMIT, hook.callback)

        result = await hooks.dispatch(
            HookEvent.USER_PROMPT_SUBMIT,
            data={"prompt": "original"},
        )
        # Dispatch builds its own ctx; we can't observe data mutation
        # from outside, but we can confirm there were no errors.
        assert result.fired == 1
        assert result.blocked is False
        assert result.errors == []
