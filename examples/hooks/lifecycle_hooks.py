"""Runtime lifecycle hooks — HookManager + ShellHook demo.

Demonstrates:
  - Registering hooks with @hooks.on() decorator
  - once=True for one-shot hooks
  - Priority ordering
  - Data mutation via HookContext
  - ShellHook for delegating to external scripts
  - HookBlocked for blocking actions

Run:
    .venv/bin/python examples/hooks/lifecycle_hooks.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from promptise.runtime.hooks import (
    HookBlocked,
    HookContext,
    HookEvent,
    HookManager,
)
from promptise.runtime.shell_hook import ShellHook


async def main() -> None:
    hooks = HookManager()

    # -----------------------------------------------------------------------
    # 1. Basic hook — audit every user prompt
    # -----------------------------------------------------------------------

    prompts_seen: list[str] = []

    @hooks.on(HookEvent.USER_PROMPT_SUBMIT)
    async def audit_prompt(ctx: HookContext) -> None:
        prompt = ctx.get("prompt", "")
        prompts_seen.append(prompt)
        print(f"  [audit] Received prompt: {prompt!r}")

    # -----------------------------------------------------------------------
    # 2. once=True — fires only on the first session start
    # -----------------------------------------------------------------------

    @hooks.on(HookEvent.SESSION_START, once=True)
    async def welcome(ctx: HookContext) -> None:
        print("  [welcome] First session started! (this hook won't fire again)")

    # -----------------------------------------------------------------------
    # 3. Priority ordering — higher runs first
    # -----------------------------------------------------------------------

    @hooks.on(HookEvent.USER_PROMPT_SUBMIT, priority=100)
    async def inject_context(ctx: HookContext) -> None:
        """Runs BEFORE the audit hook (priority 100 > default 0)."""
        original = ctx.get("prompt", "")
        ctx.set("prompt", f"[injected context] {original}")
        print(f"  [inject] Prepended context to prompt")

    # -----------------------------------------------------------------------
    # 4. Blocking — reject dangerous prompts
    # -----------------------------------------------------------------------

    @hooks.on(HookEvent.PERMISSION_REQUEST, priority=50)
    async def block_dangerous(ctx: HookContext) -> None:
        tool = ctx.get("tool_name", "")
        if tool == "delete_everything":
            raise HookBlocked(f"Tool {tool!r} is blocked by policy")

    # -----------------------------------------------------------------------
    # 5. ShellHook — delegate to an external script
    # -----------------------------------------------------------------------

    # Create a temporary script that logs events to a file
    tmp = Path(tempfile.mkdtemp())
    script = tmp / "log_event.sh"
    script.write_text(
        '#!/usr/bin/env bash\n'
        '# Read JSON event from stdin and log it\n'
        'event=$(cat)\n'
        f'echo "$event" >> {tmp}/events.log\n'
        'echo \'{{"log": "event logged"}}\'\n'
    )
    script.chmod(0o755)

    shell_hook = ShellHook(command=str(script), timeout=5.0)
    hooks.register(HookEvent.TASK_COMPLETED, shell_hook.callback, tag="shell")

    # -----------------------------------------------------------------------
    # Dispatch some events
    # -----------------------------------------------------------------------

    print("\n=== SESSION_START ===")
    result = await hooks.dispatch(HookEvent.SESSION_START)
    print(f"  Fired: {result.fired}, Blocked: {result.blocked}")

    print("\n=== SESSION_START (second time — once hook is gone) ===")
    result = await hooks.dispatch(HookEvent.SESSION_START)
    print(f"  Fired: {result.fired}")

    print("\n=== USER_PROMPT_SUBMIT ===")
    result = await hooks.dispatch(
        HookEvent.USER_PROMPT_SUBMIT,
        data={"prompt": "What is our refund policy?"},
    )
    print(f"  Fired: {result.fired}")
    # The inject_context hook ran first (priority 100), then audit_prompt
    # So audit_prompt saw the modified prompt
    print(f"  Prompts seen: {prompts_seen}")

    print("\n=== PERMISSION_REQUEST (safe tool) ===")
    result = await hooks.dispatch(
        HookEvent.PERMISSION_REQUEST,
        data={"tool_name": "search_docs"},
    )
    print(f"  Blocked: {result.blocked}")

    print("\n=== PERMISSION_REQUEST (dangerous tool) ===")
    result = await hooks.dispatch(
        HookEvent.PERMISSION_REQUEST,
        data={"tool_name": "delete_everything"},
    )
    print(f"  Blocked: {result.blocked}, Reason: {result.reason!r}")

    print("\n=== TASK_COMPLETED (triggers ShellHook) ===")
    result = await hooks.dispatch(
        HookEvent.TASK_COMPLETED,
        data={"task_id": "t-1", "status": "success", "duration": 3.2},
    )
    print(f"  Fired: {result.fired}")
    log_file = tmp / "events.log"
    if log_file.exists():
        logged = json.loads(log_file.read_text().strip())
        print(f"  Shell hook logged event: {logged['event']}")

    # -----------------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------------

    removed = await hooks.remove_by_tag("shell")
    print(f"\n  Removed {removed} shell hooks by tag")
    print(f"  Total hooks still registered: {len(hooks.registered())}")


if __name__ == "__main__":
    asyncio.run(main())
