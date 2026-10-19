# Meta-Tools

In open mode (`execution_mode=ExecutionMode.OPEN`) an agent process gets meta-tools: tools that change the process itself instead of the outside world. With them the agent can rewrite its instructions, write new Python tools, connect MCP servers, add and remove its own triggers, manage long-term memory and spawn helper processes. Every meta-tool goes through the same process methods an operator would use, so the limits in `OpenModeConfig` and your [hooks](hooks.md) apply to each change.

```python
import asyncio

from promptise.runtime import AgentRuntime, ExecutionMode, OpenModeConfig, ProcessConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="You are an adaptive monitoring agent. Improve your own tooling over time.",
    execution_mode=ExecutionMode.OPEN,
    open_mode=OpenModeConfig(
        allow_mcp_connect=False,      # no new servers
        allow_process_spawn=True,     # may start helper processes
        max_custom_tools=10,
        max_spawned_processes=2,
        max_rebuilds=20,
    ),
)


async def main():
    async with AgentRuntime() as runtime:  # spawn_process needs a runtime
        await runtime.add_process("monitor", config)
        await runtime.start_all()
        await asyncio.sleep(3600)


asyncio.run(main())
```

---

## Available meta-tools

`create_meta_tools()` builds the list when the agent is built, filtered by `OpenModeConfig`. Strict-mode processes get none of them.

| Meta-tool | Present when | What it does |
|---|---|---|
| `modify_instructions` | `allow_identity_change` | Replace the system prompt (hot reload) |
| `create_tool` | `allow_tool_creation` | Define a Python tool (hot reload) |
| `connect_mcp_server` | `allow_mcp_connect` | Connect an HTTP MCP server (hot reload) |
| `add_trigger` | `allow_trigger_management` | Add a cron, event, message, webhook, file_watch or custom trigger |
| `remove_trigger` | `allow_trigger_management` | Remove a trigger the agent added |
| `spawn_process` | `allow_process_spawn` | Create and start a new process in the runtime |
| `list_processes` | `allow_process_spawn` | List the runtime's processes |
| `store_memory` | `allow_memory_management` | Store text in long-term memory |
| `search_memory` | `allow_memory_management` | Search long-term memory |
| `forget_memory` | `allow_memory_management` | Delete a memory by ID |
| `get_secret` | secret scoping enabled | Read a scoped secret |
| `check_budget` | budget enabled | Show the remaining budget |
| `check_mission` | mission enabled | Show the mission state and latest evaluations |
| `list_capabilities` | always (in open mode) | Show the agent's current instructions, tools, triggers and counters |

The `update_state(key, value)` tool is not a meta-tool: `ContextConfig(state_tool=True)` gives it to a process in either mode (see [Context](context.md)).

### How changes are applied

Each meta-tool calls a process method, and every method enforces the limits and dispatches the `config_change` [hook](hooks.md) (`source="agent"`), which can refuse the change:

| Meta-tool | Calls | Limits checked |
|---|---|---|
| `modify_instructions` | `process.set_dynamic_instructions()` | `max_instruction_length`, `max_rebuilds` |
| `create_tool` | hot reload | `max_custom_tools`, duplicate names, `max_rebuilds` |
| `connect_mcp_server` | hot reload | `http(s)` URL, `allowed_mcp_urls`, duplicate names, `max_rebuilds` |
| `add_trigger` | `process.add_dynamic_trigger()` | `max_dynamic_triggers`, trigger validation, process state |
| `remove_trigger` | `process.remove_trigger(trigger_id, source="agent")` | only triggers the agent added |
| `spawn_process` | `process.spawn_process()` | `max_spawned_processes`, unique name; the `subagent_start` hook can refuse it |

A refused or failed change is not applied, now or after a restart. Tools report problems as text starting with `Error`, so the model can read them and carry on.

---

## modify_instructions

| Parameter | Type | Description |
|---|---|---|
| `new_instructions` | `str` | The complete new system prompt |

The agent is rebuilt with the new instructions. [`rollback()`](#rollback) restores the configured ones, and an operator's `process.update_instructions()` replaces whatever the agent set.

| Result | When |
|---|---|
| `Rebuild successful` | Applied |
| `Error: instructions exceed max length (12000 > 10000)` | Longer than `max_instruction_length` |
| `Error: max rebuilds (N) reached; the change was not applied` | `max_rebuilds` used up |
| `Error: the change was blocked by a hook: <reason>` | A `config_change` hook refused it |

---

## create_tool

| Parameter | Type | Description |
|---|---|---|
| `tool_name` | `str` | Unique name for the new tool |
| `tool_description` | `str` | What the tool does |
| `parameters` | `dict` | JSON object schema of the tool's parameters (empty: one free-form `input` string) |
| `python_code` | `str` | Python code defining `run(...)`, which returns a string |

`parameters` shapes the new tool's inputs. Each property of the object schema is a parameter, offered to the model with its type and constraints, and passed to `run` as a keyword argument:

```python
create_tool(
    tool_name="add",
    tool_description="Add two integers.",
    parameters={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
    python_code="def run(a, b):\n    return str(a + b)",
)
# The new tool is called as add(a=2, b=3) -> "5"
```

- Arguments are validated against the schema before `run` is called. Invalid arguments raise `ToolArgumentError` (the model sees each problem and can correct the call), and `run` is not called.
- `run` gets the arguments the caller gave, as plain JSON (nested objects are dicts), plus each property's schema `default` when the caller left it out. An optional parameter without a schema default that the caller left out is not passed, so `run`'s own default applies.
- Property names must be usable as Python keyword arguments (identifiers, not keywords, not starting with `_`), and `required` may only name declared properties. A schema that is not an object schema, or that lists parameters without `properties`, is refused and no tool is created.
- With empty `parameters` (or an object schema without properties), the tool takes one free-form `input` string: `run(input="...")`.

**The new tool can be called from the agent's next run, not in the run that creates it** (the current run keeps the tools it started with). The tool's description and its result say so, so the model finishes the current task without it instead of looking for it or creating it again.

| Result | When |
|---|---|
| `Tool 'add' created. You can call it from your next run, not in this one: this run keeps the tools it started with, so finish the current task without it.` | Created |
| `Error: max custom tools (20) reached` | `max_custom_tools` reached |
| `Error: tool 'add' already exists` | Duplicate name |
| `Error creating tool: <reason>` | Unusable `parameters` schema, or the rebuild failed |
| `Error: max rebuilds (N) reached; the change was not applied` | `max_rebuilds` used up |
| `Error: the change was blocked by a hook: <reason>` | A `config_change` hook refused it |

### Sandbox execution

With `sandbox_custom_tools=True` (the default), agent-written code runs with restricted builtins:

- **Allowed**: `len`, `range`, `str`, `int`, `float`, `list`, `dict`, `set`, `sorted`, `enumerate`, `zip`, `map`, `filter`, `max`, `min`, `sum`, `print`, `round`, `abs`, `bool`, `type`, `isinstance`, common exceptions and other safe builtins.
- **Blocked**: `open`, `exec`, `eval`, `__import__`, file I/O, network access and all imports.

With `sandbox_custom_tools=False`, the code runs with the **same restricted builtins** (no `open`, `exec`, `eval`, file I/O or network access) but may import a fixed allowlist of standard-library modules: `base64`, `collections`, `copy`, `datetime`, `functools`, `hashlib`, `itertools`, `json`, `math`, `re`, `string`, `urllib.parse`, `uuid`. Any other import (`os`, `decimal`, ...) fails with `Import of '...' is not allowed`, and each call logs a warning. No setting gives agent-written code full Python builtins; give the agent such capabilities as reviewed MCP tools.

Restricted builtins are defense in depth, not a security boundary: Python's object model can reach other functions through attribute access. For untrusted code, use the Docker [sandbox](../core/sandbox.md).

---

## connect_mcp_server

| Parameter | Type | Default | Description |
|---|---|---|---|
| `server_name` | `str` | required | Unique name for the server |
| `url` | `str` | required | `http://` or `https://` URL of the MCP server |
| `transport` | `str` | `"streamable-http"` | `streamable-http` or `sse` |

Only HTTP(S) servers: the agent can't start a stdio server (a program on the host). When `allowed_mcp_urls` is non-empty, only those URLs are accepted (compared with a lowercase scheme and host and without a trailing slash). After connecting, the agent is rebuilt with the server's tools, which are available from the next run.

| Result | When |
|---|---|
| `Connected to MCP server 'x' at <url>. Its tools are available from your next run, not in this one: this run keeps the tools it started with.` | Connected |
| `Error: the MCP server URL must start with http:// or https://` | Not an HTTP(S) URL |
| `Error: URL not in allowed whitelist` | Not in `allowed_mcp_urls` |
| `Error: server 'x' already connected` | Duplicate name |
| `Error connecting to MCP server: <reason>` | The connection or rebuild failed |
| `Error: max rebuilds (N) reached; the change was not applied` | `max_rebuilds` used up |
| `Error: the change was blocked by a hook: <reason>` | A `config_change` hook refused it |

---

## add_trigger

| Parameter | Type | Description |
|---|---|---|
| `trigger_type` | `str` | `"cron"`, `"event"`, `"message"`, `"webhook"`, `"file_watch"` or a registered custom type |
| `cron_expression` | `str \| None` | For `cron` |
| `event_type` | `str \| None` | For `event` |
| `topic` | `str \| None` | For `message` |
| `webhook_path` | `str \| None` | For `webhook` |
| `webhook_port` | `int \| None` | For `webhook` |
| `watch_path` | `str \| None` | For `file_watch` |
| `watch_patterns` | `list[str] \| None` | For `file_watch` |
| `custom_config` | `dict \| None` | For custom trigger types |

The trigger is validated like any `TriggerConfig`, started at once with its listener, and counts toward `max_dynamic_triggers`. It is not added to `config.triggers`: it lives until `remove_trigger`, `rollback()` or the next `stop()` (a restart drops it too).

| Result | When |
|---|---|
| `Trigger 'cron-36042769' (cron) added and listening.` | Added; the ID is what `remove_trigger` takes |
| `Error adding trigger: max dynamic triggers (10) reached` | Limit reached |
| `Error adding trigger: Invalid trigger config: <details>` | Invalid fields |
| `Error adding trigger: Adding the cron trigger was blocked: <reason>` | A `config_change` hook refused it |
| `Error adding trigger: Trigger could not start: <reason>` | The trigger failed to start (for example a port in use) |

---

## remove_trigger

| Parameter | Type | Description |
|---|---|---|
| `trigger_id` | `str` | ID returned by `add_trigger` |

The agent can only remove triggers it added; config triggers stay. The trigger's listener is cancelled with it.

| Result | When |
|---|---|
| `Trigger 'cron-36042769' removed.` | Removed |
| `Error: trigger 'x' not found in dynamic triggers` | Not an agent-added trigger |
| `Error removing trigger: Removing trigger 'x' was blocked: <reason>` | A `config_change` hook refused it |

---

## spawn_process

| Parameter | Type | Default | Description |
|---|---|---|---|
| `process_name` | `str` | required | Unique name in the runtime |
| `instructions` | `str` | required | System prompt for the new process |
| `model` | `str \| None` | `None` | Model ID; `None` uses the parent's model |
| `triggers` | `list[dict]` | `[]` | Trigger configs (each with `type` and its fields) |
| `execution_mode` | `str` | `"strict"` | `"strict"` or `"open"` |

The new process is registered in the parent's `AgentRuntime`, started, and listed in `process.spawned_processes`. It inherits the parent's guardrails:

| Inherited from the parent | Not inherited |
|---|---|
| `budget`, `health`, `trigger_delivery` and `journal` settings (copies: the child has its own counters) | Secrets |
| `approval` policy and `guardrails` scanner | MCP servers |
| `max_invocation_time` | Identity |
| `open_mode` limits, with `allow_process_spawn` switched off | Hooks, mission, inbox, context settings |

So a spawned process can never spawn more processes, even in open mode. The `subagent_start` hook runs before the spawn and can refuse it; `subagent_stop` runs when the child stops or fails. The child runs independently: stopping the parent does not stop it. `allow_process_spawn` defaults to `False`.

| Result | When |
|---|---|
| `Process 'helper' spawned and started. Mode: strict. Triggers: 1.` | Spawned |
| `Error: process is not running inside an AgentRuntime. spawn_process requires a runtime.` | The process has no runtime |
| `Error spawning process: max spawned processes (3) reached` | Limit reached |
| `Error spawning process: process 'helper' already exists in the runtime` | Name taken |
| `Error spawning process: invalid process config: <details>` | Bad trigger or mode |
| `Error spawning process: spawning 'helper' was blocked by a hook: <reason>` | A `subagent_start` hook refused it |

A child that fails to start is removed from the runtime again, and the error is returned.

---

## list_processes

No parameters. Lists every process in the runtime:

```text
Processes in runtime:
  - monitor: state=running, invocations=12 (this process)
  - helper: state=running, invocations=3 [spawned by this process]
```

Without a runtime: `Error: process is not running inside an AgentRuntime. list_processes requires a runtime.`

---

## store_memory, search_memory, forget_memory

These need a long-term memory provider (`ContextConfig.memory_provider`); without one each returns `Error: no memory provider configured`.

| Tool | Parameters | Result |
|---|---|---|
| `store_memory` | `content: str`, `tags: list[str] = []` | `Stored in memory (id=<id>).` |
| `search_memory` | `query: str`, `limit: int = 5` (1-20) | `Found memories:` and one `- [<id>] (score=0.87) <content>` line each, or `No relevant memories found.` |
| `forget_memory` | `memory_id: str` | `Memory '<id>' deleted.` or `Memory '<id>' not found.` |

---

## Governance tools

These appear in open mode when the subsystem is enabled.

| Tool | Parameters | Result |
|---|---|---|
| `get_secret` | `name: str` | The secret value, or `Error: secret '<name>' not found or expired`. Each access is logged in the audit trail. |
| `check_budget` | none | `Budget remaining:` and one line per limit (`Tool Calls Run: 5`, `Cost Day: unlimited`, ...) |
| `check_mission` | none | The mission summary and up to the latest 10 evaluations (`- #<run> [<source>] achieved, confidence=0.82: <reasoning>`) |

---

## list_capabilities

No parameters. Returns the process name, mode and model, the current instructions (first 200 characters), custom tool names, config and dynamic trigger IDs, spawned processes, memory status, conversation size, and the invocation and rebuild counts:

```text
Process: monitor
Mode: open
Model: openai:gpt-5-mini
Instructions: You coordinate carefully.
Custom tools: ['add']
Static triggers: []
Dynamic triggers: []
Spawned processes: ['helper']
Memory: disabled
Conversation buffer: 0 messages
Invocations: 0
Rebuilds: 2
```

---

## Hot reload

`modify_instructions`, `create_tool` and `connect_mcp_server` rebuild the agent:

1. The limits are checked and the `config_change` hook runs; a refusal stops here, with nothing changed.
2. The change is staged and the conversation history saved.
3. The agent is rebuilt from the static config plus the dynamic state, with a fresh connection to each MCP server.
4. The previous agent and its MCP connections are closed (stdio servers exit) once the run that asked for the reload has finished; until then that run keeps the tools it started with.
5. The conversation history is restored and `rebuild_count` goes up.

If the rebuild fails, the process keeps the previous agent and the change is undone, so a later start doesn't apply it either.

### max_rebuilds

With `OpenModeConfig.max_rebuilds` set, the process refuses further changes once that many hot reloads have happened. The limit counts over the lifetime of the `AgentProcess` object: `stop()` / `start()`, recovery and automatic restarts don't reset it (`status()["rebuild_count"]`). A refused change is not made at all; the limit is checked before the change is staged, under the same lock as the rebuild.

### rollback

`await process.rollback()` is the operator's undo: it removes the agent's instructions, tools, MCP servers and triggers and rebuilds from the original config. It is not refused by `max_rebuilds` but counts as a rebuild, and it returns `Rebuild successful`. It keeps the context state and conversation; use [`rewind()`](journal/rewind.md) to roll those back. Strict-mode processes raise `RuntimeError`.

### What survives a restart

Agent-written instructions, custom tools and MCP servers stay on the process object, so `stop()` / `start()` and automatic restarts rebuild the agent with them. Agent-added triggers end at `stop()`. A new process object (a redeploy) starts from the config only.

---

## Programmatic use

```python
from promptise.runtime.meta_tools import create_meta_tools

tools = {t.name: t for t in create_meta_tools(process, runtime=runtime)}
print(await tools["add_trigger"].ainvoke({"trigger_type": "cron", "cron_expression": "*/5 * * * *"}))
# Trigger 'cron-36042769' (cron) added and listening.
```

`create_meta_tools(process, *, runtime=None)` returns LangChain `StructuredTool`s filtered by `process.config.open_mode`. Without `runtime`, `spawn_process` and `list_processes` answer with the "not running inside an AgentRuntime" error. Calling the tools directly is useful in tests; the process adds them to its own agent by itself.

---

## Common pitfalls

- **Spawning without a runtime.** A bare `AgentProcess` can't spawn; register it with `AgentRuntime.add_process()` (which passes the runtime) or pass `runtime=`.
- **Expecting the new tool in the same run.** Created tools and connected servers are available from the next run.
- **Leaving `max_rebuilds` unset.** Each hot reload reconnects every MCP server. A cap bounds both the cost and how far the agent can drift.
- **Assuming a child has the parent's secrets or tools.** A spawned process gets the parent's limits, not its secrets, servers or hooks.
- **Treating the sandbox as isolation.** Restricted builtins reduce risk; they don't contain hostile code. Keep `sandbox_custom_tools=True` and use the Docker sandbox or MCP tools for anything sensitive.
- **Empty `allowed_mcp_urls`.** It means any HTTP(S) URL. List the servers the agent may use, or set `allow_mcp_connect=False`.

---

## What's next

- [Configuration](configuration.md#openmodeconfig) -- every `OpenModeConfig` field
- [Runtime Hooks](hooks.md) -- refuse an agent's change with a `config_change` or `subagent_start` hook
- [Runtime Manager](runtime-manager.md) -- the runtime spawned processes join
- [Triggers Overview](triggers/index.md) -- the trigger types `add_trigger` can create
- [Context](context.md) -- the state and memory the memory tools use

!!! tip "Learn the concept"

    - [Self-Modifying AI Agent in Python: Open Mode & Limits](https://www.promptise.com/guides/view/self-modifying-ai-agent-open-mode) -- Meta-tools, hot reload and the limits that keep open mode bounded.
    - [Autonomous AI Agent Runtime: Budgets, Crash Recovery, API](https://www.promptise.com/guides/view/autonomous-ai-agent-runtime) -- The budgets and controls around an agent that changes itself.
