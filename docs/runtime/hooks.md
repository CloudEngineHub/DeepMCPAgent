# Runtime Lifecycle Hooks

Runtime hooks let you react to what happens *to* an agent process -- a session starting, a prompt about to reach the model, a tool approval being denied, a trigger event entering or leaving the queue -- without changing the agent. Use them to audit prompts, add standing context, notify a chat channel, or refuse an action by policy. Every `AgentProcess` dispatches the 13 lifecycle events itself; you only register the hooks.

```python
from promptise.runtime import (
    AgentProcess, HookBlocked, HookConfig, HookContext, HookEvent, ProcessConfig,
)

async def no_passwords(ctx: HookContext) -> None:
    if "password" in ctx.data["prompt"].lower():
        raise HookBlocked("prompts may not ask for passwords")

async def add_house_rules(ctx: HookContext) -> None:
    ctx.set("prompt", ctx.data["prompt"] + "\n\nAnswer in one sentence.")

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="You are a helpful assistant.",
    hooks=[
        HookConfig(event="user_prompt_submit", callback=no_passwords, priority=10),
        HookConfig(event="user_prompt_submit", callback=add_house_rules),
        HookConfig(event="task_completed", command=["./hooks/log-task.sh"], timeout=5),
    ],
)

process = AgentProcess("helper", config)

@process.hooks.on(HookEvent.SESSION_START)
async def started(ctx: HookContext) -> None:
    print("session started", ctx.data)   # {'restart': False, 'recovered': None}

await process.start()
```

A trigger event whose prompt mentions a password is refused before any budget is counted: it lands in `process.dead_letters` with reason `"blocked by hook"` and error `"prompts may not ask for passwords"`. Every other prompt gets the extra sentence. After each event, `./hooks/log-task.sh` receives the `task_completed` event as JSON on stdin.

Runtime hooks are not the [reasoning graph hooks](../core/engine-hooks.md) (`Hook`, `LoggingHook`, `TimingHook`): those run for every node and tool call *inside* one agent run. Runtime hooks fire at process-level moments, around runs.

---

## Registering hooks

| Where | How | Accepts |
|---|---|---|
| `ProcessConfig.hooks` | A list of `HookConfig` | Async callbacks and external commands |
| Code | `process.hooks.on(event)` decorator or `process.hooks.register(event, fn)` | Async callbacks (or `ShellHook(...).callback`) |
| Manifest | The `hooks:` section of a `.agent` file | External commands only |
| Shared manager | `AgentRuntime(hooks=manager)` or `AgentProcess(..., hooks=manager)` | One `HookManager` whose hooks fire for every process |

`process.hooks` is the process's own `HookManager`. It holds the hooks from `config.hooks` and whatever you register in code. A shared manager runs after the process's own hooks; `ctx.process_id` tells you which process dispatched the event.

A process spawned by an open-mode agent (`spawn_process`) inherits its parent's `hooks` manager (and whatever the parent inherited): they run for the child after the child's own hooks and before the shared manager, with `ctx.process_id` naming the child. So a `permission_request` or `user_prompt_submit` hook on the parent governs every process it spawns.

```python
from promptise.runtime import AgentRuntime, HookEvent, HookManager

shared = HookManager()

@shared.on(HookEvent.SESSION_START)
async def announce(ctx):
    print(f"{ctx.process_id} is running")

runtime = AgentRuntime(hooks=shared)   # fires for every process the runtime starts
```

A callback must be an async function, or an object with an async `__call__`, that takes one `HookContext`. A sync function raises `TypeError` at registration. The event can be a `HookEvent` member or its value (`"session_start"`); an unknown value raises `ValueError` listing the valid ones.

### HookConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `event` | `str` | required | A `HookEvent` value, for example `"user_prompt_submit"` |
| `command` | `str \| list[str] \| None` | `None` | External program and arguments, run as a [`ShellHook`](#shellhook-external-commands). A string is split like a terminal would (`shlex.split`); no shell runs it |
| `callback` | async callable | `None` | Python hook (code only; not serialized) |
| `timeout` | `float \| None` | `None` | Seconds the hook may run (`> 0`, at most 3600). Unset: 5 for a command, 30 for a callback |
| `priority` | `int` | `0` | Higher runs first |
| `once` | `bool` | `False` | Run on the first matching event only, then deregister |
| `tag` | `str \| None` | `None` | Group name for `remove_by_tag()` |
| `fail_closed` | `bool` | `False` | An error or timeout of this hook blocks the action (blockable events only) |
| `cwd` | `str \| None` | `None` | Working directory of the command |
| `env` | `dict[str, SecretStr]` | `{}` | Extra environment variables for the command. `${VAR}` references are resolved from the host environment each time the hook runs; literal values are masked in JSON dumps |
| `env_passthrough` | `list[str]` | `[]` | Host environment variables passed through to the command |
| `max_output_bytes` | `int` | `65536` | Largest stdout accepted from the command (at most 10 MiB) |

Set exactly one of `command` or `callback`; both or neither raises a validation error. Unknown keys are rejected.

### In a manifest

```yaml
hooks:
  - event: user_prompt_submit
    command: ["./hooks/audit.sh"]
    timeout: 5
    fail_closed: true
  - event: task_completed
    command: "./hooks/notify.sh --channel ops"
    env_passthrough: [SLACK_TOKEN]
```

Each entry is a `HookConfig` with a `command`. A manifest resolves `${VAR}` references when it is loaded, except in the `secrets:` section and in hook `env:` values: those stay `${VAR}` references in the loaded config and are resolved each time the hook runs. Loading still checks that every variable they name is set, so `env: {SLACK_TOKEN: ${SLACK_TOKEN}}` needs the variable at load time but never puts its value into the loaded config. `env_passthrough` hands a host variable to the program as it is. A `callback` key in a manifest is an error (`hooks[0]: a manifest hook runs a 'command'; Python callbacks are registered in code (process.hooks)`). The old top-level `entrypoint` key is not read by the runtime; a manifest that sets it loads with a `FutureWarning`.

### Deploying command hooks over the control API

A command hook runs a program on the runtime's host. The [Orchestration API](api.md) therefore refuses a `POST /api/v1/processes` whose config has a command hook (or a stdio MCP server, or open-mode tool creation) with `403 COMMANDS_NOT_ALLOWED`, unless the API was created with `OrchestrationAPI(..., allow_command_deploys=True)` (`promptise runtime start --allow-command-deploys`). Anyone with the API token could otherwise run any command as the runtime's user. Callback hooks can't be sent over HTTP at all, and processes you load from manifests on the host are not checked.

---

## The 13 lifecycle events

`ctx.data` holds the keys listed for each event. Secret values (from [secret scoping](governance/secrets.md)) are redacted before any hook sees them.

| Event | Fires when | `ctx.data` | Blockable: what a block does |
|---|---|---|---|
| `SESSION_START` | The process entered `RUNNING` (every `start()`, including automatic restarts) | `restart` (bool), `recovered` (what journal recovery restored, or `None`) | No |
| `SESSION_END` | The process is about to stop, before teardown: an explicit `stop()` or a restart | `reason` (`"stop"` or `"restart"`), `invocation_count` | No (a stop always proceeds) |
| `USER_PROMPT_SUBMIT` | A trigger event's prompt is about to be sent to the model | `prompt`, `event_id`, `trigger_id`, `trigger_type` | Yes: the run is skipped and the event dead-lettered (`"blocked by hook"`), before any budget is counted. A hook may also replace `prompt` |
| `PERMISSION_REQUEST` | A tool that needs approval (`ProcessConfig.approval`) is about to be sent to the approval handler | `tool_name`, `arguments` (the reviewer's redacted copy), `request_id` | Yes: the call is denied without asking the handler |
| `PERMISSION_DENIED` | Something the agent was about to do was refused | `kind`, `tool_name`, `request_id`, `reason`, `decided_by`, plus per-kind keys (below) | No |
| `SUBAGENT_START` | An open-mode agent is about to spawn a process (`spawn_process` meta-tool) | `process_name`, `execution_mode`, `trigger_count` | Yes: the spawn is refused |
| `SUBAGENT_STOP` | A process this one spawned stopped or failed | `process_name`, `state` | No |
| `PRE_COMPACT` | The conversation buffer is about to drop its oldest messages (`context.conversation_max_messages`) | `buffer_size`, `max_messages`, `dropping` (the messages that will be dropped) | No |
| `POST_COMPACT` | After that compaction | `buffer_size`, `dropped` | No |
| `FILE_CHANGED` | A file-watch trigger reported a change | The event payload (`path`, `event_type`, ...), `trigger_id`, `event_id` | Yes: the event is dropped and counted as filtered |
| `CONFIG_CHANGE` | The process's effective configuration changed | `source` (`"agent"` or `"operator"`), `change`, plus details | Only for `source="agent"`: the agent's change is not applied. Operator changes always apply |
| `TASK_CREATED` | A trigger event is about to be queued (from a trigger or `inject()`) | `event_id`, `trigger_id`, `trigger_type`, `payload_summary` (the payload as one line of JSON, at most 120 characters, secrets redacted) | Yes: the event is dead-lettered (`"blocked by hook"`) instead of queued |
| `TASK_COMPLETED` | A queued event finished | `event_id`, `trigger_id`, `trigger_type`, `outcome`, `duration_ms`, `error` | No |

`PERMISSION_DENIED` covers three kinds of refusal:

| `kind` | What refused it | Extra data |
|---|---|---|
| `"approval"` | The approval handler, a classifier rule, the gate, or a `permission_request` hook | `decided_by` as in the `ApprovalDecision` |
| `"budget"` | A budget limit refused a tool call (`tool_name` set) or a run (`max_runs_per_day`; `tool_name` is `None`) | `decided_by="budget"`, `limit_name`, `limit_value`, `current_value`; `event_id` for a refused run |
| `"guardrail"` | The payload scan (`decided_by="payload_scan"`) or the guardrail scanner (`decided_by="guardrail"`) refused a trigger event's run | `tool_name` is `None`; `event_id`, `trigger_type` |

`CONFIG_CHANGE` names the change in `change`: `"instructions"`, `"tool"`, `"mcp_server"`, `"trigger_added"`, `"trigger_removed"`, `"budget"`, `"health"`, `"mission"`, `"rollback"` or `"rewind"`.

`TASK_COMPLETED`'s `outcome` is `"success"`, `"retrying"`, `"failed"`, `"blocked"`, `"dead_lettered"` or `"refused"` (the run did not start: `max_runs_per_day` was reached; the event is dead-lettered with reason `"budget: max_runs_per_day"`). The event is no longer the worker's when the hook runs: a `stop()` that arrives while a `task_completed` hook is still running doesn't dead-letter an event whose run has finished.

`HookEvent` values are the lowercase names (`HookEvent.USER_PROMPT_SUBMIT.value == "user_prompt_submit"`). `BLOCKABLE_EVENTS` is the frozenset of the six blockable events.

---

## HookContext

Every callback receives a single `HookContext`:

| Field | Type | Description |
|---|---|---|
| `event` | `HookEvent` | The event that fired |
| `process_id` | `str` | The dispatching process's **name** |
| `timestamp` | `datetime` | UTC time of the dispatch |
| `data` | `dict[str, Any]` | The event payload (a copy; hooks may change it) |
| `metadata` | `dict[str, Any]` | `process_name` and `process_id` (the process's UUID) |

`ctx.get(key, default)` and `ctx.set(key, value)` are shorthands for `ctx.data`.

---

## Blocking, data and errors

**Blocking.** Raise `HookBlocked(reason)` to refuse the action. The remaining hooks for that dispatch don't run. Only the six blockable events can be refused; a block of any other event is logged as a warning and ignored.

**Returned data.** Hooks run one after another on the same `ctx.data`, so a later hook sees what an earlier one changed. The process reads one value back: the `prompt` of `USER_PROMPT_SUBMIT`. The hook sees the prompt with secrets redacted; only a prompt that a hook actually changed replaces the original. Changes to `data` of other events are not used.

**Order.** Hooks run by priority (highest first; ties in registration order). The process's own hooks run first, then those inherited from the process that spawned it, then the shared manager's. A `once=True` hook is removed before it runs, so it runs at most once even when dispatches overlap.

**Timeouts.** A hook that runs longer than its timeout is cancelled and recorded as an error. The manager an `AgentProcess` creates has a default timeout of 30 seconds; a standalone `HookManager()` has none unless you pass `HookManager(default_timeout=...)`. A command hook's program is killed at its own `timeout`; the manager allows 5 seconds more as a backstop.

**Errors.** An exception other than `HookBlocked` never reaches the process: it is logged and recorded in `DispatchResult.errors`, and the next hook runs. A hook with `fail_closed=True` turns its error or timeout into a block (`reason` starts with `"hook failed (fail_closed)"`), which is what you want for a hook that enforces a policy. On a non-blockable event, `fail_closed` has no effect.

`HookManager.dispatch()` returns a `DispatchResult` when you dispatch yourself:

| Field | Description |
|---|---|
| `fired` | Number of hooks that ran |
| `blocked` | `True` if a hook raised `HookBlocked` (or a `fail_closed` hook failed) |
| `reason` | The block reason |
| `errors` | `(hook_id, exception)` pairs for hooks that raised or timed out |
| `data` | The payload after the hooks ran |

### Managing registrations

```python
hook_id = process.hooks.register(HookEvent.SESSION_END, notify, tag="ops")
await process.hooks.remove(hook_id)          # True if it was registered
await process.hooks.remove_by_tag("ops")     # number removed
process.hooks.registered(HookEvent.SESSION_END)  # hook IDs
process.hooks.clear()                         # everything (or clear(event))
```

`process.status()["hook_count"]` reports how many hooks the process's own manager holds.

---

## ShellHook: external commands

A command hook (`HookConfig(command=...)` or a manifest entry) runs as a `ShellHook`. You can also build one in code and register its `callback`:

```python
from promptise.runtime import HookEvent, ShellHook

hook = ShellHook(command=["/opt/hooks/audit_prompt.sh"], timeout=5.0)
process.hooks.register(HookEvent.USER_PROMPT_SUBMIT, hook.callback, fail_closed=True)
```

The program receives the event as JSON on **stdin**:

```json
{
  "event": "user_prompt_submit",
  "process_id": "support-bot",
  "timestamp": "2026-10-19T10:30:00+00:00",
  "data": {"prompt": "...", "event_id": "...", "trigger_id": "...", "trigger_type": "webhook"},
  "metadata": {"process_name": "support-bot", "process_id": "63712925-..."}
}
```

It may write one JSON object to **stdout** (all keys optional):

```json
{
  "block": false,
  "reason": "",
  "data": {"prompt": "REWRITTEN"},
  "log": "anything"
}
```

| Result | Meaning |
|---|---|
| Exit 0, empty stdout | Success, nothing changed |
| Exit 0, a JSON object | `data` (an object) replaces the event data; `"block": true` refuses the action with `reason`; `log` is logged at debug level |
| Non-zero exit | Hook error (the message includes the exit code and up to 500 characters of stderr) |
| Timeout | The program is killed; hook error |
| More than `max_output_bytes` on stdout | The program is killed; hook error |
| stdout that isn't a JSON object, or `data` that isn't an object | Hook error |

A hook error is logged and ignored, or blocks the action when the hook is `fail_closed`. Blocking is done with `{"block": true}`, not with an exit code.

**No shell.** The command runs directly: a list is used as is, a string is split with `shlex.split` (quotes work as in a terminal), and the executable is looked up on `PATH`. The event never appears on the command line. Only a `ShellHook(shell=True)` built in code runs its command string through `/bin/sh -c`; a manifest or `HookConfig` can't.

**Minimal environment.** The program does not inherit the host environment, so API keys and tokens in it don't reach hook programs. It gets:

- these host variables, when they are set: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LANGUAGE`, `LC_ALL`, `LC_CTYPE`, `LC_MESSAGES`, `TZ`, `TMPDIR`, `TEMP`, `TMP`, `TERM`, and on Windows `SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `COMSPEC`, `PATHEXT`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `PROGRAMDATA`, `PROGRAMFILES` (`SAFE_ENV_VARS`);
- the host variables named in `env_passthrough`;
- the values in `env` (`${VAR}` and `${VAR:-default}` resolved when the hook runs);
- `PROMPTISE_HOOK_EVENT` (the event value) and `PROMPTISE_PROCESS` (the process name).

`ShellHook(inherit_env=True)` (code only) passes the whole host environment instead.

| `ShellHook` attribute | Default | Description |
|---|---|---|
| `command` | required | List of program and arguments, or a string split with `shlex.split` |
| `timeout` | `5.0` | Seconds before the program is killed |
| `cwd` | `None` | Working directory |
| `env` | `None` | Extra variables (values may be `${VAR}` references) |
| `shell` | `False` | Run a command string through the shell (code only) |
| `encoding` | `"utf-8"` | stdin/stdout encoding |
| `inherit_env` | `False` | Pass the whole host environment |
| `env_passthrough` | `[]` | Host variables to pass through |
| `max_output_bytes` | `65536` | Largest stdout accepted |

`ShellHook.run(ctx)` runs the program and returns a `ShellHookResult` (`block`, `reason`, `data`, `log`, `raw`) without applying it, which is handy in tests.

Unless `cwd` is set, the program starts without `fork()` (CPython's `posix_spawn` path). That matters on macOS: a forked child of a multithreaded process that has used system frameworks -- any `httpx` client does -- can die with SIGSEGV before `exec` (`exited -11`). Prefer an absolute script path over `cwd` when you can.

---

## Common pitfalls

- **Awaiting the process from its own hook.** A hook must not `await process.stop()`, `start()` or `rewind()` of the process that dispatched it; that waits on itself. Schedule it with `asyncio.create_task(process.stop())`.
- **Expecting a block on an informational event.** `HookBlocked` from `SESSION_END`, `TASK_COMPLETED` or `PERMISSION_DENIED` is ignored (with a warning). Refuse earlier: `TASK_CREATED`, `USER_PROMPT_SUBMIT` or `PERMISSION_REQUEST`.
- **A policy hook that fails open.** Without `fail_closed=True`, a crashed or slow policy hook lets the action through. Set it on every hook that enforces something.
- **A hook script that needs a variable it doesn't get.** Hook programs get a minimal environment. Name what they need in `env_passthrough`, or pass it through `env` with a `${VAR}` reference.
- **A sync callback.** `def hook(ctx)` raises `TypeError` at registration; use `async def`.
- **Deploying a command hook over the API.** It answers `403 COMMANDS_NOT_ALLOWED` unless the API allows command deploys; load the process from a manifest on the host, or use a callback hook in code.
- **A slow hook on a hot path.** `USER_PROMPT_SUBMIT`, `TASK_CREATED` and `TASK_COMPLETED` run for every event, and the event waits for them. Keep them fast, or hand slow work to a background task.

---

## Related

- [Agent Processes](processes.md) -- the lifecycle that dispatches these events
- [Agent Manifests](manifests.md) -- the `hooks:` section
- [Orchestration API](api.md) -- `allow_command_deploys` and `COMMANDS_NOT_ALLOWED`
- [Secret Scoping](governance/secrets.md) -- what is redacted from hook data
- [Reasoning Graph Hooks](../core/engine-hooks.md) -- per-node and per-tool hooks inside a run
- [Approval](../core/approval.md) -- the approval flow behind `PERMISSION_REQUEST`
- [Events](../core/events.md) -- structured event notifications

!!! tip "Learn the concept"

    - [Autonomous AI Agent Runtime: Budgets, Crash Recovery, API](https://www.promptise.com/guides/view/autonomous-ai-agent-runtime) -- How a runtime supervises long-running agents and where hooks fit in.
    - [AI Agent Control API: Manage Running Agents Over HTTP](https://www.promptise.com/guides/view/ai-agent-control-api) -- Deploy and manage processes over HTTP, and what the API refuses.
