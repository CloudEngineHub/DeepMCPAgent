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

Each tool captures a reference to the owning ``AgentProcess`` and goes
through its methods (:meth:`~AgentProcess.set_dynamic_instructions`,
:meth:`~AgentProcess.add_dynamic_trigger`,
:meth:`~AgentProcess.remove_trigger`, :meth:`~AgentProcess.spawn_process`),
so the limits, the ``CONFIG_CHANGE`` / ``SUBAGENT_START`` hooks (which
can refuse the change) and the cleanup (trigger listeners) apply.  Tools
that change the graph (instructions, tools, MCP servers) trigger a
hot-reload.

Security: these tools hand the model control over its own setup.
``create_tool`` in particular runs model-written Python in the runtime's
own process with restricted builtins, which are **not a sandbox**: code
can walk Python's object graph back to ``os`` and run commands as the
runtime's user.  Enable ``allow_tool_creation`` only where that is
acceptable (the control API refuses such deploys unless
``allow_command_deploys`` is on); for code from untrusted sources use the
Docker sandbox (:mod:`promptise.sandbox`, ``build_agent(sandbox=...)``)
instead.  ``connect_mcp_server`` only connects HTTP(S) servers that pass
:func:`check_mcp_url`, and never sends them the process identity.
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
        description=(
            "JSON schema of the tool's parameters: an object schema such as "
            '{"type": "object", "properties": {"a": {"type": "integer"}}, '
            '"required": ["a"]}. Each property is passed to run() as a keyword '
            "argument. Leave it empty for a tool that takes one free-form "
            "'input' string."
        ),
    )
    python_code: str = Field(
        ...,
        description=(
            "Python code for the tool function body. "
            "Must define a function called 'run' that takes the declared "
            "parameters as keyword arguments (or 'input' when there are none) "
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


#: Evaluations ``check_mission`` shows (the latest ones).
_MISSION_HISTORY_LIMIT = 10


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
    """Implementation for modify_instructions tool (:meth:`AgentProcess.set_dynamic_instructions`)."""
    result: str = await process.set_dynamic_instructions(new_instructions)
    return result


_NEXT_RUN_NOTE = (
    "You can call it from your next run, not in this one: this run keeps the "
    "tools it started with, so finish the current task without it."
)


async def _create_tool(
    process: Any,
    tool_name: str,
    tool_description: str,
    parameters: dict[str, Any],
    python_code: str,
) -> str:
    """Implementation for create_tool tool.

    The code runs in the runtime's process (see :func:`_build_dynamic_tool`):
    restricted builtins limit mistakes, not a determined model, so this is
    equivalent to letting the model run code as the runtime's user.
    """
    max_tools = process.config.open_mode.max_custom_tools
    if len(process._custom_tools) >= max_tools:
        return f"Error: max custom tools ({max_tools}) reached"

    # Check for duplicate names (the operator's local tools included)
    for existing in [*process._extra_tools, *process._custom_tools]:
        if existing.name == tool_name:
            return f"Error: tool '{tool_name}' already exists"

    try:
        tool = _build_dynamic_tool(
            tool_name,
            tool_description,
            python_code,
            sandbox=process.config.open_mode.sandbox_custom_tools,
            parameters=parameters,
        )
    except Exception as exc:
        return f"Error creating tool: {exc}"

    def stage() -> Any:
        process._custom_tools.append(tool)

        def undo() -> None:
            if tool in process._custom_tools:
                process._custom_tools.remove(tool)

        return undo

    try:
        result = await process._hot_reload(
            reason=f"tool '{tool_name}' created",
            stage=stage,
            change="tool",
            details={"tool_name": tool_name},
        )
    except Exception as exc:
        return f"Error creating tool: {exc}"
    if result.startswith("Error"):
        return result
    # The run that created the tool keeps the agent (and tool list) it
    # started with; the rebuilt agent serves the next invocation.  Say so,
    # or the model keeps looking for the tool and re-creating it.
    return f"Tool '{tool_name}' created. {_NEXT_RUN_NOTE}"


def normalize_mcp_url(url: str) -> str:
    """An MCP URL for comparison: lowercase scheme and host, default port
    dropped, no trailing slash."""
    from urllib.parse import urlparse

    p = urlparse(url.strip())
    scheme = p.scheme.lower()
    try:
        port = p.port
    except ValueError:
        port = None
    if port == {"http": 80, "https": 443}.get(scheme):
        port = None
    host = (p.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}{':%d' % port if port else ''}{p.path.rstrip('/')}"


async def check_mcp_url(process: Any, url: str) -> str | None:
    """Why an agent may not connect the MCP server at *url* (``None``: it may).

    * Only ``http://`` / ``https://`` URLs (an agent can't start a stdio
      server, a program on the host).
    * With ``open_mode.allowed_mcp_urls`` set, the URL must be one of
      them; a listed URL is trusted as the operator wrote it, private
      addresses included.
    * Without an allowlist, a process deployed through the control API
      may connect no server at all, and an operator-configured one only
      servers whose host resolves to public addresses (no loopback,
      private, link-local or cloud-metadata targets), checked like
      escalation webhooks (:func:`promptise._outbound.pin_target`).  The
      check runs when the agent connects and again at every agent build;
      it can't stop a host that changes its DNS answer in between, so set
      ``allowed_mcp_urls`` where that matters.
    """
    if not url.strip().lower().startswith(("http://", "https://")):
        return "the MCP server URL must start with http:// or https://"
    allowed = process.config.open_mode.allowed_mcp_urls
    if allowed:
        if normalize_mcp_url(url) not in {normalize_mcp_url(a) for a in allowed}:
            return "URL not in allowed whitelist (open_mode.allowed_mcp_urls)"
        return None
    policy = getattr(process, "_deploy_policy", None)
    if policy is not None and policy.from_api:
        return (
            "this process was deployed through the control API and has no "
            "open_mode.allowed_mcp_urls, so it may not connect MCP servers"
        )
    from promptise._outbound import BlockedTarget, pin_target

    try:
        await pin_target(url.strip())
    except BlockedTarget as exc:
        return f"refused: {exc} (list the URL in open_mode.allowed_mcp_urls to allow it)"
    except (OSError, ValueError) as exc:
        return f"the MCP server host did not resolve ({exc})"
    return None


async def _connect_mcp_server(
    process: Any,
    server_name: str,
    url: str,
    transport: str = "streamable-http",
) -> str:
    """Implementation for connect_mcp_server tool.

    Only HTTP(S) servers: the agent can't start a stdio server (a program
    on the host), whatever ``allowed_mcp_urls`` says.  The URL is checked
    by :func:`check_mcp_url` (allowlist, private addresses).  The server
    gets neither the process identity's credential nor a caller's token,
    unless its URL is in ``open_mode.identity_mcp_urls``.
    """
    if server_name in process._dynamic_servers:
        return f"Error: server '{server_name}' already connected"
    problem = await check_mcp_url(process, url)
    if problem is not None:
        return f"Error: {problem}"

    try:
        from promptise.config import HTTPServerSpec

        identity_urls = process.config.open_mode.identity_mcp_urls
        trusted = normalize_mcp_url(url) in {normalize_mcp_url(a) for a in identity_urls}
        spec = HTTPServerSpec(
            url=url,
            transport=transport,
            # Never hand a caller's token to a server the agent picked.
            forward_caller_token=None if trusted else False,
        )

        def stage() -> Any:
            process._dynamic_servers[server_name] = spec

            def undo() -> None:
                process._dynamic_servers.pop(server_name, None)

            return undo

        result = await process._hot_reload(
            reason=f"MCP server '{server_name}' connected",
            stage=stage,
            change="mcp_server",
            details={"server_name": server_name, "url": url},
        )
    except Exception as exc:
        return f"Error connecting to MCP server: {exc}"
    if result.startswith("Error"):
        return result
    return (
        f"Connected to MCP server '{server_name}' at {url}. Its tools are available "
        "from your next run, not in this one: this run keeps the tools it started with."
    )


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

    try:
        trigger_id = await process.add_dynamic_trigger(kwargs)
    except Exception as exc:
        return f"Error adding trigger: {exc}"
    return f"Trigger '{trigger_id}' ({trigger_type}) added and listening."


async def _remove_trigger(process: Any, trigger_id: str) -> str:
    """Implementation for remove_trigger tool.

    Uses :meth:`AgentProcess.remove_trigger` (``source="agent"``), which
    also cancels the trigger's listener.  Only triggers the agent added
    can be removed.
    """
    try:
        await process.remove_trigger(trigger_id, source="agent")
    except KeyError:
        return f"Error: trigger '{trigger_id}' not found in dynamic triggers"
    except Exception as exc:
        return f"Error removing trigger: {exc}"
    return f"Trigger '{trigger_id}' removed."


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

    Uses :meth:`AgentProcess.spawn_process`: the new process is created
    in the parent :class:`AgentRuntime` with the parent's guardrails
    (budget, health, approval, ...), started, and tracked in the parent's
    spawned process list.
    """
    if runtime is None:
        return (
            "Error: process is not running inside an AgentRuntime. "
            "spawn_process requires a runtime."
        )
    try:
        await process.spawn_process(
            process_name,
            instructions=instructions,
            model=model,
            triggers=list(triggers or []),
            execution_mode=execution_mode,
            runtime=runtime,
        )
    except Exception as exc:
        return f"Error spawning process: {exc}"
    return (
        f"Process '{process_name}' spawned and started. "
        f"Mode: {execution_mode}. "
        f"Triggers: {len(triggers or [])}."
    )


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
            f"invocations={proc.status().get('invocation_count', 0)}"
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
    """Implementation for check_mission tool: state plus the latest evaluations."""
    if process._mission is None:
        return "No mission configured."
    return process._mission.history_summary(limit=_MISSION_HISTORY_LIMIT)


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
    """Generic args schema for agent-created tools declared without parameters."""

    input: str = Field(
        default="",
        description="Free-form input string passed to the dynamic tool.",
    )


_SCHEMA_KEYS_WITHOUT_PROPERTIES = frozenset(
    {"type", "title", "description", "additionalProperties", "required"}
)


def _dynamic_args_schema(name: str, parameters: dict[str, Any] | None) -> type[BaseModel]:
    """The args schema of an agent-created tool, from its declared ``parameters``.

    ``parameters`` is a JSON object schema (``{"type": "object",
    "properties": {...}, "required": [...]}``); each property becomes a
    keyword argument of ``run``.  A tool declared without properties takes
    the single free-form ``input`` string (:class:`_DynamicToolArgs`).

    Raises:
        ValueError: ``parameters`` is not a usable object schema.
    """
    import keyword

    from promptise.tools import _jsonschema_to_pydantic

    if not parameters:
        return _DynamicToolArgs
    example = '{"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}'
    if not isinstance(parameters, dict) or parameters.get("type", "object") != "object":
        raise ValueError(f"parameters must be a JSON object schema, such as {example}")
    properties = parameters.get("properties")
    if properties is None:
        unknown = sorted(set(parameters) - _SCHEMA_KEYS_WITHOUT_PROPERTIES)
        if unknown:
            raise ValueError(
                f"parameters has no 'properties' but has {unknown}: declare the "
                f"parameters under 'properties', as in {example}"
            )
        return _DynamicToolArgs
    if not isinstance(properties, dict):
        raise ValueError(f"parameters['properties'] must be an object, as in {example}")
    if not properties:
        return _DynamicToolArgs
    for prop in properties:
        if not (isinstance(prop, str) and prop.isidentifier()) or (
            keyword.iskeyword(prop) or prop.startswith("_")
        ):
            raise ValueError(f"parameter name {prop!r} is not usable as a Python keyword argument")
    required = parameters.get("required") or []
    if not isinstance(required, list) or any(r not in properties for r in required):
        raise ValueError("parameters['required'] must list names declared in 'properties'")
    return _jsonschema_to_pydantic(parameters, model_name=f"Args_{name}")


#: The ``StructuredTool`` subclass built (once) by :func:`_dynamic_tool_class`.
_DYNAMIC_TOOL_CLASS: type[Any] | None = None


def _dynamic_tool_class() -> type[Any]:
    """A ``StructuredTool`` that passes ``run`` exactly the arguments given.

    The arguments are validated against the declared schema and handed to
    ``run`` as plain JSON (nested objects as dicts), with the declared
    schema defaults for arguments left out; an optional parameter without
    a default that the caller left out is not passed, so ``run``'s own
    default applies.  Invalid arguments raise
    :class:`~promptise.tools.ToolArgumentError`.
    """
    global _DYNAMIC_TOOL_CLASS
    if _DYNAMIC_TOOL_CLASS is not None:
        return _DYNAMIC_TOOL_CLASS

    from langchain_core.tools import StructuredTool

    from promptise.tools import parse_tool_arguments

    class _DynamicTool(StructuredTool):
        schema_defaults: dict[str, Any] = Field(default_factory=dict)

        @property
        def tool_call_schema(self) -> Any:
            # Offer the declared schema whole (LangChain's rebuild drops
            # field constraints and the model config).
            return self.args_schema

        def _to_args_and_kwargs(
            self, tool_input: str | dict[str, Any], tool_call_id: str | None
        ) -> tuple[tuple[Any, ...], dict[str, Any]]:
            if not isinstance(tool_input, dict) or self.args_schema is _DynamicToolArgs:
                return super()._to_args_and_kwargs(tool_input, tool_call_id)
            given = parse_tool_arguments(self.name, self.args_schema, tool_input)
            return (), {**self.schema_defaults, **given}

    _DYNAMIC_TOOL_CLASS = _DynamicTool
    return _DynamicTool


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
    parameters: dict[str, Any] | None = None,
) -> Any:
    """Build a LangChain tool from agent-provided Python code.

    The code must define a function called ``run`` that accepts
    keyword arguments and returns a string.  The tool's arguments are the
    properties of the *parameters* JSON schema, each passed to ``run`` as a
    keyword argument; without parameters, the tool takes one free-form
    ``input`` string (``run(input=...)``).

    When ``sandbox=True``, the code runs with restricted builtins:
    no file I/O, no imports, no ``exec``/``eval``, no ``open``.
    When ``sandbox=False``, the code runs with the same restricted builtins
    but may import a fixed allowlist of standard-library modules (``json``,
    ``math``, ``re``, ``datetime``, ``collections``, ``itertools``,
    ``functools``, ``string``, ``hashlib``, ``base64``, ``urllib.parse``,
    ``uuid``, ``copy``); it is never given full builtins.

    .. warning::

        This is **not a sandbox**, with ``sandbox=True`` or ``False``.  The
        code runs in the runtime's own process, and restricted builtins
        can be escaped through Python's object model (for example by
        walking ``object.__subclasses__()`` to a class whose globals hold
        ``os``), so agent-written code can run any command as the
        runtime's user.  The restrictions only keep honest code from
        reaching for files and imports by accident.  For untrusted code,
        use the Docker sandbox (:mod:`promptise.sandbox`) instead.

    Args:
        name: Tool name.
        description: Tool description.
        python_code: Python code defining a ``run(**kwargs) -> str``
            function.
        sandbox: Restricted builtins and no imports (default ``True``);
            ``False`` allows the fixed import allowlist.  Neither isolates
            the code.
        parameters: JSON object schema of the tool's parameters.

    Returns:
        A LangChain ``StructuredTool`` instance.

    Raises:
        ValueError: *parameters* is not a usable JSON object schema.
    """
    args_schema = _dynamic_args_schema(name, parameters)
    schema_defaults: dict[str, Any] = {}
    if args_schema is not _DynamicToolArgs and parameters:
        for prop, prop_schema in (parameters.get("properties") or {}).items():
            if isinstance(prop_schema, dict) and "default" in prop_schema:
                schema_defaults[prop] = prop_schema["default"]

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
                    "json",
                    "math",
                    "re",
                    "datetime",
                    "collections",
                    "itertools",
                    "functools",
                    "string",
                    "hashlib",
                    "base64",
                    "urllib.parse",
                    "uuid",
                    "copy",
                }

                def _safe_import(name, *args, **kwargs):
                    if name.split(".")[0] in _safe_modules:
                        return (
                            __builtins__["__import__"](name, *args, **kwargs)
                            if isinstance(__builtins__, dict)
                            else __import__(name, *args, **kwargs)
                        )
                    raise ImportError(
                        f"Import of '{name}' is not allowed. Safe modules: {sorted(_safe_modules)}"
                    )

                ns["__builtins__"]["__import__"] = _safe_import
            # Not isolated (see the docstring): the control API only allows
            # it with allow_command_deploys.
            exec(python_code, ns)  # noqa: S102  # nosec B102 - open-mode create_tool, documented as unsandboxed

            run_fn = ns.get("run")
            if run_fn is None:
                return "Error: code must define a 'run' function"

            result = run_fn(**kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            return str(result)
        except Exception as exc:
            return f"Error executing tool: {exc}"

    return _dynamic_tool_class()(
        name=name,
        description=description,
        args_schema=args_schema,
        coroutine=_execute,
        func=lambda **kw: None,
        schema_defaults=schema_defaults,
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
                    "Define a new Python tool for your future runs. Provide a name, "
                    "a description, parameters (JSON schema) and Python code defining "
                    "a 'run' function that takes the declared parameters as keyword "
                    "arguments and returns a string. The tool can be called from your "
                    "next run on, not in the run that creates it."
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
                    "Connect to an additional MCP server to gain access to its tools. "
                    "Its tools can be called from your next run on, not in the run "
                    "that connects it."
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
                    "Returns the secret value: pass it only as an argument "
                    "to the tool that needs it and never repeat it in a reply. "
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
                    "success criteria, progress, and the latest "
                    f"{_MISSION_HISTORY_LIMIT} evaluations (verdict, "
                    "confidence and reasoning)."
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
