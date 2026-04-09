# Meta-Tools

When an `AgentProcess` runs in `ExecutionMode.OPEN`, it receives a set of meta-tools -- LangChain `BaseTool` subclasses that allow the agent to modify its own configuration at runtime. The agent can change its identity, create new tools, connect to MCP servers, manage triggers, and manipulate long-term memory.

```python
from promptise.runtime import ProcessConfig, ExecutionMode, OpenModeConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="You are an adaptive monitoring agent.",
    execution_mode=ExecutionMode.OPEN,
    open_mode=OpenModeConfig(
        allow_identity_change=True,
        allow_tool_creation=True,
        allow_mcp_connect=False,
        allow_trigger_management=True,
        allow_memory_management=True,
        allow_process_spawn=True,
        max_custom_tools=10,
        max_spawned_processes=3,
        sandbox_custom_tools=True,
    ),
)
```

---

## Concepts

Meta-tools give the agent **self-modification capabilities**. Unlike regular tools that interact with external systems, meta-tools mutate the owning `AgentProcess` itself. When a meta-tool changes the agent's instructions, tools, or MCP servers, the process performs a **hot-reload** -- rebuilding the agent graph without losing conversation state.

Each meta-tool is gated by a permission flag in `OpenModeConfig`. This allows operators to grant selective capabilities:

- An agent that can learn from experience (memory) but not change its identity
- An agent that can add triggers but not create arbitrary code
- A fully autonomous agent with all capabilities unlocked

---

## Available Meta-Tools

| Meta-Tool | Permission | Description |
|---|---|---|
| `modify_instructions` | `allow_identity_change` | Change the system prompt at runtime |
| `create_tool` | `allow_tool_creation` | Define a new Python function tool |
| `connect_mcp_server` | `allow_mcp_connect` | Connect to an additional MCP server |
| `add_trigger` | `allow_trigger_management` | Add a cron, event, message, webhook, file_watch, or custom trigger |
| `remove_trigger` | `allow_trigger_management` | Remove a dynamically added trigger |
| `spawn_process` | `allow_process_spawn` | Create and start a new agent process within the runtime |
| `list_processes` | `allow_process_spawn` | List all processes in the runtime with state info |
| `store_memory` | `allow_memory_management` | Store content in long-term memory |
| `search_memory` | `allow_memory_management` | Search long-term memory |
| `forget_memory` | `allow_memory_management` | Delete a memory by ID |
| `list_capabilities` | Always available | Introspect current tools, triggers, and identity |

---

## modify_instructions

Changes the agent's system prompt. This triggers a hot-reload of the agent graph.

**Input schema:**

| Parameter | Type | Description |
|---|---|---|
| `new_instructions` | `str` | The complete new system prompt |

The instruction length is capped by `OpenModeConfig.max_instruction_length` (default: 10,000 characters). Exceeding the limit returns an error without modifying the agent.

---

## create_tool

Defines a new Python function tool. The code must define a `run(**kwargs) -> str` function.

**Input schema:**

| Parameter | Type | Description |
|---|---|---|
| `tool_name` | `str` | Unique name for the new tool |
| `tool_description` | `str` | Description of what the tool does |
| `parameters` | `dict` | JSON schema for tool parameters |
| `python_code` | `str` | Python code defining `run(**kwargs) -> str` |

The number of custom tools is capped by `OpenModeConfig.max_custom_tools` (default: 20). Duplicate names are rejected. After creation, a hot-reload is triggered.

### Sandbox execution

When `sandbox_custom_tools=True` (the default), agent-written code runs with restricted builtins:

- **Allowed**: `len`, `range`, `str`, `int`, `float`, `list`, `dict`, `set`, `sorted`, `enumerate`, `zip`, `map`, `filter`, `max`, `min`, `sum`, `print`, `round`, `abs`, `bool`, `type`, `isinstance`, common exceptions, and other safe builtins.
- **Blocked**: `open`, `exec`, `eval`, `__import__`, file I/O, network access, and all module imports.

When `sandbox_custom_tools=False`, the code runs with full Python builtins. This is only recommended for trusted environments.

---

## connect_mcp_server

Connects to an additional MCP server at runtime to gain access to its tools.

**Input schema:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `server_name` | `str` | required | Unique name for the server |
| `url` | `str` | required | URL of the MCP server |
| `transport` | `str` | `"streamable-http"` | Transport type |

If `OpenModeConfig.allowed_mcp_urls` is set (non-empty list), only URLs in that whitelist are accepted. After connecting, a hot-reload rebuilds the agent with the new tools.

---

## add_trigger

Adds a new trigger at runtime. The agent can schedule cron jobs, listen for events, subscribe to message topics, set up webhooks, watch files, or use custom trigger types.

**Input schema:**

| Parameter | Type | Description |
|---|---|---|
| `trigger_type` | `str` | Any registered trigger type: `"cron"`, `"event"`, `"message"`, `"webhook"`, `"file_watch"`, or a custom type |
| `cron_expression` | `str \| None` | Cron expression (for `cron` triggers) |
| `event_type` | `str \| None` | Event type to listen for (for `event` triggers) |
| `topic` | `str \| None` | Message topic (for `message` triggers) |
| `webhook_path` | `str \| None` | URL path (for `webhook` triggers) |
| `webhook_port` | `int \| None` | Listen port (for `webhook` triggers) |
| `watch_path` | `str \| None` | Directory to watch (for `file_watch` triggers) |
| `watch_patterns` | `list[str] \| None` | Glob patterns (for `file_watch` triggers) |
| `custom_config` | `dict \| None` | Arbitrary config for custom trigger types |

The number of dynamic triggers is capped by `OpenModeConfig.max_dynamic_triggers` (default: 10). The trigger starts immediately after creation. Custom trigger types must be registered via `register_trigger_type()` before use.

---

## remove_trigger

Removes a dynamically added trigger by its ID. Config-defined (static) triggers cannot be removed.

**Input schema:**

| Parameter | Type | Description |
|---|---|---|
| `trigger_id` | `str` | ID of the trigger to remove |

---

## spawn_process

Creates and starts a new agent process within the runtime. This allows agents to delegate work by spawning sub-processes with their own instructions, triggers, and execution modes.

**Input schema:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `process_name` | `str` | required | Unique name for the new process |
| `instructions` | `str` | required | System prompt for the spawned agent |
| `model` | `str` | `"openai:gpt-5-mini"` | LLM model identifier |
| `triggers` | `list[dict]` | `[]` | Trigger configs (each dict has `type` + type-specific fields) |
| `execution_mode` | `str` | `"strict"` | `"strict"` or `"open"` |

The number of spawned processes is capped by `OpenModeConfig.max_spawned_processes` (default: 3). Duplicate process names are rejected. The spawned process starts immediately and runs independently within the same runtime.

!!! warning "Defaults to disabled"
    Unlike most other permissions, `allow_process_spawn` defaults to `False`. Process spawning is a powerful capability -- enable it only for agents that need to create sub-processes.

---

## list_processes

Lists all processes in the runtime with their current state.

**Input schema:** No parameters.

Returns a formatted list showing each process name, state (RUNNING, STOPPED, etc.), model, and execution mode.

---

## store_memory, search_memory, forget_memory

Explicit memory management tools. These require a `MemoryProvider` to be configured in the process context.

**store_memory:**

| Parameter | Type | Description |
|---|---|---|
| `content` | `str` | Information to store |
| `tags` | `list[str]` | Optional categorization tags |

**search_memory:**

| Parameter | Type | Description |
|---|---|---|
| `query` | `str` | Natural-language search query |
| `limit` | `int` | Max results (1-20, default 5) |

**forget_memory:**

| Parameter | Type | Description |
|---|---|---|
| `memory_id` | `str` | ID of the memory to delete |

---

## list_capabilities

Always available regardless of `OpenModeConfig` settings. Returns a human-readable summary of the agent's current state:

- Process name, mode, and model
- Current instructions (truncated to 200 characters)
- MCP tools and custom tools
- Static and dynamic triggers
- Memory status
- Invocation and rebuild counts

---

## Hot-Reload

When a meta-tool modifies instructions, tools, or MCP servers, the process performs a **hot-reload**:

1. The conversation buffer is preserved.
2. The agent graph is rebuilt with the new configuration.
3. The rebuild count is incremented.
4. A new invocation resumes with the updated agent.

If `OpenModeConfig.max_rebuilds` is set, the process will refuse further modifications once the limit is reached.

---

## Programmatic Creation

The `create_meta_tools` factory builds the tool list based on `OpenModeConfig` permissions:

```python
from promptise.runtime.meta_tools import create_meta_tools

tools = create_meta_tools(process)
# Returns only the tools permitted by process.config.open_mode
```

---

## API Summary

| Function | Description |
|---|---|
| `create_meta_tools(process, *, runtime=None)` | Factory: build meta-tools filtered by `OpenModeConfig` |
| `modify_instructions` | Change agent system prompt |
| `create_tool` | Define new Python tool |
| `connect_mcp_server` | Connect to MCP server |
| `add_trigger` | Add any trigger type (cron, event, message, webhook, file_watch, custom) |
| `remove_trigger` | Remove dynamic trigger |
| `spawn_process` | Create and start a new agent process |
| `list_processes` | List all runtime processes |
| `store_memory` | Store in long-term memory |
| `search_memory` | Search long-term memory |
| `forget_memory` | Delete a memory |
| `list_capabilities` | Introspect current state |

---

## Tips and Gotchas

!!! tip "Start restrictive, then expand"
    Begin with minimal permissions and only enable what the agent actually needs. For example, `allow_mcp_connect=False` prevents the agent from reaching out to arbitrary servers.

!!! tip "Use allowed_mcp_urls in production"
    Whitelisting MCP server URLs prevents the agent from connecting to untrusted endpoints. An empty list means any URL is allowed.

!!! warning "Sandbox limitations"
    Sandboxed tools cannot import any modules, read/write files, or make network calls. If the agent needs these capabilities, either provide them through MCP servers or set `sandbox_custom_tools=False` (with appropriate security review).

!!! warning "Hot-reload cost"
    Each hot-reload rebuilds the agent graph, which involves reconnecting MCP servers and re-initializing tools. Frequent modifications can add latency. Use `max_rebuilds` to cap this.

!!! warning "Dynamic triggers persist until process stop"
    Triggers added via `add_trigger` run until explicitly removed or the process stops. They are not persisted across restarts.

---

## What's Next

- [Configuration](configuration.md) -- `ExecutionMode` and `OpenModeConfig` reference
- [Context](context.md) -- the state and memory layer meta-tools interact with
- [Triggers Overview](triggers/index.md) -- trigger types that can be added dynamically
