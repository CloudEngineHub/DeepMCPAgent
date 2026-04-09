"""Meta-tools for open execution mode.

When an :class:`AgentProcess` runs in
:attr:`~promptise.runtime.config.ExecutionMode.OPEN` mode, it receives
a set of *meta-tools* — LangChain ``BaseTool`` subclasses that allow
the agent to modify its own configuration at runtime:

* **modify_instructions** — change the system prompt / identity
* **create_tool** — define a new Python function tool
* **connect_mcp_server** — connect to an additional MCP server
* **add_trigger** — add a trigger (cron, event, message, webhook,
  file_watch, or custom)
* **remove_trigger** — remove a dynamically added trigger
* **spawn_process** — spawn a new agent process in the runtime
* **list_processes** — list all processes in the runtime
* **store_memory** — store in long-term memory
* **search_memory** — search long-term memory
* **forget_memory** — delete a memory
* **list_capabilities** — introspect current tools, triggers, identity

Each tool captures a reference to the owning ``AgentProcess`` and
mutates its dynamic state.  Tools that change the graph (instructions,
tools, MCP servers) trigger a hot-reload via
:meth:`AgentProcess._hot_reload`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Arg schemas
# ---------------------------------------------------------------------------


class ModifyInstructionsArgs(BaseModel):
    """Arguments for modify_instructions tool."""

    new_instructions: str = Field(..., description="The complete new system prompt / instructions.")


class CreateToolArgs(BaseModel):
    """Arguments for create_tool tool."""

    tool_name: str = Field(..., description="Unique name for the new tool.")
    tool_description: str = Field(..., description="Description of what the tool does.")
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON schema for tool parameters.",
    )
    python_code: str = Field(
        ...,
        description=(
            "Python code for the tool function body. "
            "Must define a function called 'run' that takes **kwargs "
            "and returns a string."
        ),
    )


class ConnectMCPServerArgs(BaseModel):
    """Arguments for connect_mcp_server tool."""

    server_name: str = Field(..., description="Unique name for the MCP server.")
    url: str = Field(..., description="URL of the MCP server.")
    transport: str = Field(
        "streamable-http",
        description="Transport type (streamable-http, sse).",
    )


class AddTriggerArgs(BaseModel):
    """Arguments for add_trigger tool."""

    trigger_type: str = Field(
        ...,
        description=(
            "Trigger type: cron, event, message, webhook, file_watch, or a custom-registered type."
        ),
    )
    cron_expression: str | None = Field(None, description="Cron expression (for cron triggers).")
    event_type: str | None = Field(
        None, description="Event type to listen for (for event triggers)."
    )
    topic: str | None = Field(None, description="Message topic (for message triggers).")
    webhook_path: str | None = Field(
        None, description="URL path for webhook endpoint (for webhook triggers)."
    )
    webhook_port: int | None = Field(
        None, description="Port for webhook endpoint (for webhook triggers)."
    )
    watch_path: str | None = Field(
        None, description="Directory to watch (for file_watch triggers)."
    )
    watch_patterns: list[str] | None = Field(
        None, description="Glob patterns for file watching (for file_watch triggers)."
    )
    custom_config: dict[str, Any] | None = Field(
        None, description="Additional config for custom trigger types."
    )


class RemoveTriggerArgs(BaseModel):
    """Arguments for remove_trigger tool."""

    trigger_id: str = Field(..., description="ID of the trigger to remove.")


class SpawnProcessArgs(BaseModel):
    """Arguments for spawn_process tool."""

    process_name: str = Field(..., description="Unique name for the new process.")
    instructions: str = Field(..., description="System prompt / instructions for the new process.")
    model: str | None = Field(
        None,
        description="LLM model ID (defaults to parent's model).",
    )
    triggers: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Trigger configurations for the new process. "
            "Each dict has a 'type' key and type-specific fields."
        ),
    )
    execution_mode: str = Field(
        "strict",
        description="Execution mode: 'strict' or 'open'.",
    )


class ListProcessesArgs(BaseModel):
    """Arguments for list_processes tool (none required)."""

    pass


class StoreMemoryArgs(BaseModel):
    """Arguments for store_memory tool."""

    content: str = Field(..., description="Information to store in long-term memory.")
    tags: list[str] = Field(
        default_factory=list,
        description="Optional tags for categorization.",
    )


class SearchMemoryArgs(BaseModel):
    """Arguments for search_memory tool."""

    query: str = Field(..., description="Natural-language search query.")
    limit: int = Field(5, ge=1, le=20, description="Max results to return.")


class ForgetMemoryArgs(BaseModel):
    """Arguments for forget_memory tool."""

    memory_id: str = Field(..., description="ID of the memory to delete.")


class ListCapabilitiesArgs(BaseModel):
    """Arguments for list_capabilities tool (none required)."""

    pass


class GetSecretArgs(BaseModel):
    """Arguments for get_secret tool."""

    name: str = Field(..., description="Name of the secret to retrieve.")


class CheckBudgetArgs(BaseModel):
    """Arguments for check_budget tool (none required)."""

    pass


class CheckMissionArgs(BaseModel):
    """Arguments for check_mission tool (none required)."""

    pass


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _make_tool(
    name: str,
    description: str,
    args_schema: type[BaseModel],
    process: Any,
    func: Any,
) -> Any:
    """Create a LangChain-compatible tool without importing BaseTool.

    Uses langchain_core.tools.StructuredTool for dynamic tool creation.
    """
    from langchain_core.tools import StructuredTool

    return StructuredTool(
        name=name,
        description=description,
        args_schema=args_schema,
        coroutine=func,
        func=lambda **kw: None,  # sync fallback (not used)
    )


async def _modify_instructions(process: Any, new_instructions: str) -> str:
    """Implementation for modify_instructions tool."""
    max_len = process.config.open_mode.max_instruction_length
    if len(new_instructions) > max_len:
        return f"Error: instructions exceed max length ({len(new_instructions)} > {max_len})"

    process._dynamic_instructions = new_instructions
    result = await process._hot_reload(reason="instructions modified")
    return result


async def _create_tool(
    process: Any,
    tool_name: str,
    tool_description: str,
    parameters: dict[str, Any],
    python_code: str,
) -> str:
    """Implementation for create_tool tool."""
    max_tools = process.config.open_mode.max_custom_tools
    if len(process._custom_tools) >= max_tools:
        return f"Error: max custom tools ({max_tools}) reached"

    # Check for duplicate names
    for existing in process._custom_tools:
        if existing.name == tool_name:
            return f"Error: tool '{tool_name}' already exists"

    try:
        tool = _build_dynamic_tool(
            tool_name,
            tool_description,
            python_code,
            sandbox=process.config.open_mode.sandbox_custom_tools,
        )
        process._custom_tools.append(tool)
        result = await process._hot_reload(reason=f"tool '{tool_name}' created")
        return f"Tool '{tool_name}' created. {result}"
    except Exception as exc:
        return f"Error creating tool: {exc}"


async def _connect_mcp_server(
    process: Any,
    server_name: str,
    url: str,
    transport: str = "streamable-http",
) -> str:
    """Implementation for connect_mcp_server tool."""
    allowed = process.config.open_mode.allowed_mcp_urls
    if allowed:
        # Normalize URLs for comparison: lowercase scheme+host, strip trailing slash
        from urllib.parse import urlparse

        def _normalize_url(u: str) -> str:
            p = urlparse(u.strip())
            return (
                f"{p.scheme.lower()}://{p.hostname or ''}"
                f"{':%d' % p.port if p.port else ''}"
                f"{p.path.rstrip('/')}"
            )

        norm_url = _normalize_url(url)
        norm_allowed = {_normalize_url(a) for a in allowed}
        if norm_url not in norm_allowed:
            return "Error: URL not in allowed whitelist"

    if server_name in process._dynamic_servers:
        return f"Error: server '{server_name}' already connected"

    try:
        from promptise.config import HTTPServerSpec

        process._dynamic_servers[server_name] = HTTPServerSpec(
            url=url,
            transport=transport,
        )
        result = await process._hot_reload(reason=f"MCP server '{server_name}' connected")
        return f"Connected to MCP server '{server_name}' at {url}. {result}"
    except Exception as exc:
        # Remove from dynamic servers on failure
        process._dynamic_servers.pop(server_name, None)
        return f"Error connecting to MCP server: {exc}"


async def _add_trigger(
    process: Any,
    trigger_type: str,
    cron_expression: str | None = None,
    event_type: str | None = None,
    topic: str | None = None,
    webhook_path: str | None = None,
    webhook_port: int | None = None,
    watch_path: str | None = None,
    watch_patterns: list[str] | None = None,
    custom_config: dict[str, Any] | None = None,
) -> str:
    """Implementation for add_trigger tool.

    Supports all built-in trigger types (cron, event, message, webhook,
    file_watch) and any custom-registered trigger types.
    """
    max_triggers = process.config.open_mode.max_dynamic_triggers
    if len(process._dynamic_triggers) >= max_triggers:
        return f"Error: max dynamic triggers ({max_triggers}) reached"

    try:
        from .config import TriggerConfig
        from .triggers import create_trigger

        kwargs: dict[str, Any] = {"type": trigger_type}
        if cron_expression:
            kwargs["cron_expression"] = cron_expression
        if event_type:
            kwargs["event_type"] = event_type
        if topic:
            kwargs["topic"] = topic
        if webhook_path is not None:
            kwargs["webhook_path"] = webhook_path
        if webhook_port is not None:
            kwargs["webhook_port"] = webhook_port
        if watch_path is not None:
            kwargs["watch_path"] = watch_path
        if watch_patterns is not None:
            kwargs["watch_patterns"] = watch_patterns
        if custom_config:
            kwargs["custom_config"] = custom_config

        config = TriggerConfig(**kwargs)
        trigger = create_trigger(
            config,
            event_bus=process._event_bus,
            broker=process._broker,
        )
        await trigger.start()

        # Start listener task
        task = asyncio.create_task(
            process._trigger_listener(trigger),
            name=f"{process.name}-dynamic-{trigger.trigger_id}",
        )
        process._trigger_listener_tasks.append(task)
        process._dynamic_triggers.append(trigger)

        return f"Trigger '{trigger.trigger_id}' ({trigger_type}) added and listening."
    except Exception as exc:
        return f"Error adding trigger: {exc}"


async def _remove_trigger(process: Any, trigger_id: str) -> str:
    """Implementation for remove_trigger tool."""
    for i, trigger in enumerate(process._dynamic_triggers):
        if trigger.trigger_id == trigger_id:
            try:
                await trigger.stop()
            except Exception:
                logger.debug("Trigger stop error during removal", exc_info=True)
            process._dynamic_triggers.pop(i)
            return f"Trigger '{trigger_id}' removed."

    return f"Error: trigger '{trigger_id}' not found in dynamic triggers"


async def _spawn_process(
    process: Any,
    runtime: Any,
    process_name: str,
    instructions: str,
    model: str | None = None,
    triggers: list[dict[str, Any]] | None = None,
    execution_mode: str = "strict",
) -> str:
    """Implementation for spawn_process tool.

    Creates a new :class:`AgentProcess` within the parent
    :class:`AgentRuntime`, starts it, and tracks it in the parent's
    spawned process list.
    """
    if runtime is None:
        return (
            "Error: process is not running inside an AgentRuntime. "
            "spawn_process requires a runtime."
        )

    max_spawned = process.config.open_mode.max_spawned_processes
    if len(process._spawned_processes) >= max_spawned:
        return f"Error: max spawned processes ({max_spawned}) reached"

    # Check for duplicate name
    if process_name in runtime.processes:
        return f"Error: process '{process_name}' already exists in the runtime"

    try:
        from .config import ExecutionMode, ProcessConfig, TriggerConfig

        trigger_configs = []
        for t in triggers or []:
            trigger_configs.append(TriggerConfig(**t))

        config = ProcessConfig(
            model=model or process.config.model,
            instructions=instructions,
            execution_mode=ExecutionMode(execution_mode),
            triggers=trigger_configs,
        )

        new_process = await runtime.add_process(process_name, config)
        await new_process.start()

        process._spawned_processes.append(process_name)

        return (
            f"Process '{process_name}' spawned and started. "
            f"Mode: {execution_mode}. "
            f"Triggers: {len(trigger_configs)}."
        )
    except Exception as exc:
        return f"Error spawning process: {exc}"


async def _list_processes(process: Any, runtime: Any) -> str:
    """Implementation for list_processes tool.

    Lists all processes in the runtime with their state and
    invocation count.
    """
    if runtime is None:
        return (
            "Error: process is not running inside an AgentRuntime. "
            "list_processes requires a runtime."
        )

    lines = ["Processes in runtime:"]
    for name, proc in runtime.processes.items():
        is_self = " (this process)" if name == process.name else ""
        spawned_by = ""
        if name in process._spawned_processes:
            spawned_by = " [spawned by this process]"
        lines.append(
            f"  - {name}: state={proc.state.value}, "
            f"invocations={proc._invocation_count}"
            f"{is_self}{spawned_by}"
        )

    return "\n".join(lines)


async def _store_memory(
    process: Any,
    content: str,
    tags: list[str] | None = None,
) -> str:
    """Implementation for store_memory tool."""
    if process._context.memory is None:
        return "Error: no memory provider configured"

    metadata = {"source": "agent_explicit"}
    if tags:
        metadata["tags"] = ",".join(tags)

    memory_id = await process._context.add_memory(
        content,
        metadata=metadata,
    )
    return f"Stored in memory (id={memory_id})."


async def _search_memory(
    process: Any,
    query: str,
    limit: int = 5,
) -> str:
    """Implementation for search_memory tool."""
    if process._context.memory is None:
        return "Error: no memory provider configured"

    results = await process._context.search_memory(query, limit=limit)
    if not results:
        return "No relevant memories found."

    lines = []
    for r in results:
        lines.append(f"- [{r.memory_id}] (score={r.score:.2f}) {r.content}")
    return "Found memories:\n" + "\n".join(lines)


async def _forget_memory(process: Any, memory_id: str) -> str:
    """Implementation for forget_memory tool."""
    if process._context.memory is None:
        return "Error: no memory provider configured"

    deleted = await process._context.delete_memory(memory_id)
    if deleted:
        return f"Memory '{memory_id}' deleted."
    return f"Memory '{memory_id}' not found."


async def _get_secret(process: Any, name: str) -> str:
    """Implementation for get_secret tool."""
    if process._secrets is None:
        return "Error: secret scoping is not enabled"
    value = process._secrets.get(name)
    if value is None:
        return f"Error: secret '{name}' not found or expired"
    return value


async def _check_budget(process: Any) -> str:
    """Implementation for check_budget tool."""
    if process._budget is None:
        return "Budget tracking is not enabled. No limits."
    remaining = process._budget.remaining()
    lines = ["Budget remaining:"]
    for key, val in remaining.items():
        label = key.replace("_", " ").title()
        if val is None:
            lines.append(f"  {label}: unlimited")
        else:
            lines.append(f"  {label}: {val}")
    return "\n".join(lines)


async def _check_mission(process: Any) -> str:
    """Implementation for check_mission tool."""
    if process._mission is None:
        return "No mission configured."
    return process._mission.context_summary()


async def _list_capabilities(process: Any) -> str:
    """Implementation for list_capabilities tool."""
    lines = [f"Process: {process.name}"]
    lines.append(f"Mode: {process.config.execution_mode.value}")
    lines.append(f"Model: {process.config.model}")

    # Instructions
    instr = process._dynamic_instructions or process.config.instructions
    if instr:
        preview = instr[:200] + "..." if len(instr) > 200 else instr
        lines.append(f"Instructions: {preview}")
    else:
        lines.append("Instructions: (none)")

    # Tools
    if process._custom_tools:
        custom_names = [t.name for t in process._custom_tools]
        lines.append(f"Custom tools: {custom_names}")
    else:
        lines.append("Custom tools: (none)")

    # Triggers
    static_ids = [t.trigger_id for t in process._triggers]
    dynamic_ids = [t.trigger_id for t in process._dynamic_triggers]
    lines.append(f"Static triggers: {static_ids}")
    lines.append(f"Dynamic triggers: {dynamic_ids}")

    # Spawned processes
    if process._spawned_processes:
        lines.append(f"Spawned processes: {process._spawned_processes}")

    # Memory
    lines.append(f"Memory: {'enabled' if process._long_term_memory else 'disabled'}")
    lines.append(f"Conversation buffer: {len(process._conversation_buffer)} messages")

    # Stats
    lines.append(f"Invocations: {process._invocation_count}")
    lines.append(f"Rebuilds: {process._rebuild_count}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dynamic tool builder
# ---------------------------------------------------------------------------


class _DynamicToolArgs(BaseModel):
    """Generic args schema for agent-created dynamic tools."""

    input: str = Field(
        default="",
        description="Free-form input string passed to the dynamic tool.",
    )


def _make_sandbox_globals() -> dict[str, Any]:
    """Create a restricted globals dict for sandboxed code execution.

    Blocks access to dangerous modules and operations while allowing
    safe builtins like ``len``, ``range``, ``str``, ``int``, etc.
    """
    import builtins as _builtins

    # Allowed safe builtins (no file I/O, no exec, no import)
    _safe_names = {
        "abs",
        "all",
        "any",
        "bin",
        "bool",
        "bytes",
        "callable",
        "chr",
        "complex",
        "dict",
        "dir",
        "divmod",
        "enumerate",
        "filter",
        "float",
        "format",
        "frozenset",
        "getattr",
        "hasattr",
        "hash",
        "hex",
        "id",
        "int",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "oct",
        "ord",
        "pow",
        "print",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "type",
        "vars",
        "zip",
        "True",
        "False",
        "None",
        "Exception",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "RuntimeError",
        "StopIteration",
        "AttributeError",
        "ZeroDivisionError",
        "NotImplementedError",
    }
    safe_builtins: dict[str, Any] = {}
    for name in _safe_names:
        val = getattr(_builtins, name, None)
        if val is not None:
            safe_builtins[name] = val

    # Block __import__ to prevent importing dangerous modules
    def _blocked_import(*args: Any, **kwargs: Any) -> None:
        raise ImportError(
            "Imports are not allowed in sandboxed tools. Use only built-in Python operations."
        )

    safe_builtins["__import__"] = _blocked_import

    return {"__builtins__": safe_builtins}


def _build_dynamic_tool(
    name: str,
    description: str,
    python_code: str,
    *,
    sandbox: bool = True,
) -> Any:
    """Build a LangChain tool from agent-provided Python code.

    The code must define a function called ``run`` that accepts
    keyword arguments and returns a string.

    When ``sandbox=True``, the code runs with restricted builtins:
    no file I/O, no imports, no ``exec``/``eval``, no ``open``.
    When ``sandbox=False``, the code runs with full Python builtins.

    .. warning::

        The ``sandbox=True`` restricted-builtins mode is **defense-in-depth
        only** and should NOT be considered a security boundary.  Python's
        object model allows ``getattr``-based traversal that can reach
        dangerous functions.  For untrusted code, use Docker-based execution
        via the sandbox module instead.

    Args:
        name: Tool name.
        description: Tool description.
        python_code: Python code defining a ``run(**kwargs) -> str``
            function.
        sandbox: Restrict builtins for safety (default ``True``).

    Returns:
        A LangChain ``StructuredTool`` instance.
    """
    from langchain_core.tools import StructuredTool

    async def _execute(**kwargs: Any) -> str:
        try:
            # Compile and execute the code.
            # Use a single namespace so imports and function definitions
            # share scope (exec with separate locals causes function
            # lookups to fail for module-level imports).
            if sandbox:
                ns = _make_sandbox_globals()
            else:
                # sandbox=False: use restricted builtins but allow safe imports
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "Agent-created tool %r running with sandbox=False — "
                    "restricted builtins with safe imports allowed.",
                    name,
                )
                ns = _make_sandbox_globals()
                # Allow importing from a safe whitelist
                _safe_modules = {
                    "json", "math", "re", "datetime", "collections",
                    "itertools", "functools", "string", "hashlib",
                    "base64", "urllib.parse", "uuid", "copy",
                }

                def _safe_import(name, *args, **kwargs):
                    if name.split(".")[0] in _safe_modules:
                        return __builtins__["__import__"](name, *args, **kwargs) if isinstance(__builtins__, dict) else __import__(name, *args, **kwargs)
                    raise ImportError(f"Import of '{name}' is not allowed. Safe modules: {sorted(_safe_modules)}")

                ns["__builtins__"]["__import__"] = _safe_import
            exec(python_code, ns)  # noqa: S102

            run_fn = ns.get("run")
            if run_fn is None:
                return "Error: code must define a 'run' function"

            result = run_fn(**kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            return str(result)
        except Exception as exc:
            return f"Error executing tool: {exc}"

    return StructuredTool(
        name=name,
        description=description,
        args_schema=_DynamicToolArgs,
        coroutine=_execute,
        func=lambda **kw: None,
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_meta_tools(
    process: Any,
    *,
    runtime: Any | None = None,
) -> list[Any]:
    """Create the meta-tools for open mode.

    Tools are filtered by :class:`OpenModeConfig` permissions.

    Args:
        process: The owning :class:`AgentProcess`.
        runtime: Optional parent :class:`AgentRuntime` reference
            (enables ``spawn_process`` and ``list_processes`` tools).

    Returns:
        List of LangChain tool instances.
    """
    from langchain_core.tools import StructuredTool

    tools: list[Any] = []
    cfg = process.config.open_mode

    if cfg.allow_identity_change:
        tools.append(
            StructuredTool(
                name="modify_instructions",
                description=(
                    "Modify your own system prompt / identity. "
                    "Provide the complete new instructions text. "
                    "This rebuilds your capabilities with the new identity."
                ),
                args_schema=ModifyInstructionsArgs,
                coroutine=lambda **kw: _modify_instructions(process, **kw),
                func=lambda **kw: None,
            )
        )

    if cfg.allow_tool_creation:
        tools.append(
            StructuredTool(
                name="create_tool",
                description=(
                    "Define a new Python tool. Provide name, description, "
                    "parameters (JSON schema), and Python code defining "
                    "a 'run(**kwargs) -> str' function."
                ),
                args_schema=CreateToolArgs,
                coroutine=lambda **kw: _create_tool(process, **kw),
                func=lambda **kw: None,
            )
        )

    if cfg.allow_mcp_connect:
        tools.append(
            StructuredTool(
                name="connect_mcp_server",
                description=(
                    "Connect to an additional MCP server at runtime to gain access to its tools."
                ),
                args_schema=ConnectMCPServerArgs,
                coroutine=lambda **kw: _connect_mcp_server(process, **kw),
                func=lambda **kw: None,
            )
        )

    if cfg.allow_trigger_management:
        tools.append(
            StructuredTool(
                name="add_trigger",
                description=(
                    "Add a new trigger (cron job, event listener, message "
                    "subscriber, webhook endpoint, file watcher, or custom "
                    "type) to keep yourself active."
                ),
                args_schema=AddTriggerArgs,
                coroutine=lambda **kw: _add_trigger(process, **kw),
                func=lambda **kw: None,
            )
        )
        tools.append(
            StructuredTool(
                name="remove_trigger",
                description=(
                    "Remove a dynamically added trigger by its ID. "
                    "Cannot remove config-defined triggers."
                ),
                args_schema=RemoveTriggerArgs,
                coroutine=lambda **kw: _remove_trigger(process, **kw),
                func=lambda **kw: None,
            )
        )

    if cfg.allow_process_spawn:
        tools.append(
            StructuredTool(
                name="spawn_process",
                description=(
                    "Spawn a new agent process within the runtime. "
                    "The new process runs independently with its own "
                    "triggers and instructions."
                ),
                args_schema=SpawnProcessArgs,
                coroutine=lambda **kw: _spawn_process(process, runtime, **kw),
                func=lambda **kw: None,
            )
        )
        tools.append(
            StructuredTool(
                name="list_processes",
                description=(
                    "List all agent processes currently registered in the runtime with their state."
                ),
                args_schema=ListProcessesArgs,
                coroutine=lambda **kw: _list_processes(process, runtime),
                func=lambda **kw: None,
            )
        )

    if cfg.allow_memory_management:
        tools.append(
            StructuredTool(
                name="store_memory",
                description=(
                    "Store important information in your persistent "
                    "long-term memory for future recall."
                ),
                args_schema=StoreMemoryArgs,
                coroutine=lambda **kw: _store_memory(process, **kw),
                func=lambda **kw: None,
            )
        )
        tools.append(
            StructuredTool(
                name="search_memory",
                description=("Search your persistent long-term memory for relevant information."),
                args_schema=SearchMemoryArgs,
                coroutine=lambda **kw: _search_memory(process, **kw),
                func=lambda **kw: None,
            )
        )
        tools.append(
            StructuredTool(
                name="forget_memory",
                description="Delete a specific memory by its ID.",
                args_schema=ForgetMemoryArgs,
                coroutine=lambda **kw: _forget_memory(process, **kw),
                func=lambda **kw: None,
            )
        )

    # -- Governance meta-tools (conditional on subsystem being enabled) --
    if process._secrets is not None:
        tools.append(
            StructuredTool(
                name="get_secret",
                description=(
                    "Retrieve a scoped secret by name. "
                    "Returns the secret value. "
                    "Access is logged in the audit trail."
                ),
                args_schema=GetSecretArgs,
                coroutine=lambda **kw: _get_secret(process, **kw),
                func=lambda **kw: None,
            )
        )

    if process._budget is not None:
        tools.append(
            StructuredTool(
                name="check_budget",
                description=(
                    "Check your remaining autonomy budget — tool calls, LLM turns, cost units."
                ),
                args_schema=CheckBudgetArgs,
                coroutine=lambda **kw: _check_budget(process),
                func=lambda **kw: None,
            )
        )

    if process._mission is not None:
        tools.append(
            StructuredTool(
                name="check_mission",
                description=(
                    "Check the current mission state: objective, "
                    "success criteria, evaluation history, and progress."
                ),
                args_schema=CheckMissionArgs,
                coroutine=lambda **kw: _check_mission(process),
                func=lambda **kw: None,
            )
        )

    # Always available
    tools.append(
        StructuredTool(
            name="list_capabilities",
            description=("List your current tools, triggers, instructions, and memory status."),
            args_schema=ListCapabilitiesArgs,
            coroutine=lambda **kw: _list_capabilities(process),
            func=lambda **kw: None,
        )
    )

    return tools
