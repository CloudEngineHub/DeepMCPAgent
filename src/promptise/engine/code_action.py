"""Code-action reasoning node — the model writes ONE program, not a tool chain.

For aggregation / data-traversal tasks, chaining dozens of conversational tool
calls is slow, expensive, and error-prone: the transcript grows, the model
loses the thread, and it aggregates facts unreliably in its head. ``code-action``
changes the *action space* — in a single LLM turn the model writes one Python
program that calls the available tools (via a sandbox bridge) and computes the
answer deterministically. Validated on agentic tasks: large accuracy gain at a
fraction of the tokens and latency, in one turn.

Security: the model-written code runs inside Promptise's hardened Docker sandbox
(read-only rootfs, seccomp, dropped capabilities, no network, resource limits).
It can only reach the outside world through the *bridge*, which invokes the real
:class:`~langchain_core.tools.BaseTool` instances on the host. Those host tools
keep their normal protections — if ``build_agent`` wrapped them with an
**approval** gate, bridged calls trigger it too; and when the Agent Runtime has
attached governance **hooks** (budget/health/audit), each bridged call passes
through them. Independently of any hooks, the node enforces a hard
``max_tool_calls`` cap on the calls each program makes (every call counts,
failed ones included) so a program cannot loop a tool unbounded, and
validates every bridge request id to keep file I/O inside the RPC directory.
A program that reaches the cap gets ``ToolBudgetExceeded`` for each further
call, and its output is never taken as the answer.

The model is told the cap exists but not its value, and is told never to skip
or truncate records to stay under it: a job that needs more calls must fail,
not be answered from part of the data. After a program reaches the cap, the
repair asks for a program that does the WHOLE job in fewer calls, or for the
model to say it cannot (``CANNOT_FINISH_WITHIN_CALL_CAP``). A run that cannot
finish within the cap raises
:class:`~promptise.engine.execution.CodeActionBudgetError`.

The bridge is a filesystem rendezvous over the sandbox's writable ``/workspace``
tmpfs: the in-container program writes a request file and blocks; a concurrent
host loop services it by running the real tool and writing the response file.
This avoids granting the sandbox any network access.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from .base import BaseNode
from .nodes import TOOL_SELECTOR_KEY, record_failure, token_usage
from .state import GraphState, NodeResult

logger = logging.getLogger("promptise.engine.code_action")

# Only hex/uuid-style request ids are serviced (defense-in-depth: a request id
# can never contain a path separator or traversal sequence).
_SAFE_RID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Directory inside the sandbox used for the host<->program tool bridge.
_RPC_DIR = "/workspace/_rpc"
_PROGRAM_PATH = "/workspace/program.py"
_TOOLS_PATH = "/workspace/promptise_tools.py"

# The in-sandbox RPC client, prepended to the generated tools module. It writes a
# request atomically (tmp + rename) and waits for the host to write a ``.done``
# marker (written AFTER the response body, so reads never race a partial write).
_RPC_CLIENT = """
import json as _json, os as _os, time as _time, uuid as _uuid
_RPC = "%s"
_os.makedirs(_RPC, exist_ok=True)
_BUDGET_SPENT = []


class ToolBudgetExceeded(RuntimeError):
    \"\"\"The program used up its tool-call budget (max_tool_calls).\"\"\"


def _call(_tool, _args):
    if _BUDGET_SPENT:
        raise ToolBudgetExceeded(_BUDGET_SPENT[0])
    _rid = _uuid.uuid4().hex
    _req, _tmp = _RPC + "/req_" + _rid + ".json", _RPC + "/req_" + _rid + ".json.tmp"
    _resp, _done = _RPC + "/resp_" + _rid + ".json", _RPC + "/resp_" + _rid + ".done"
    with open(_tmp, "w") as _f:
        _json.dump({"tool": _tool, "args": _args}, _f)
    _os.rename(_tmp, _req)
    _deadline = _time.time() + %d
    while not _os.path.exists(_done):
        if _time.time() > _deadline:
            raise TimeoutError("tool call timed out: " + _tool)
        _time.sleep(0.02)
    with open(_resp) as _f:
        _data = _json.load(_f)
    if _data.get("budget_exceeded"):
        _BUDGET_SPENT.append(_data["error"])
        raise ToolBudgetExceeded(_data["error"])
    if "error" in _data:
        raise RuntimeError(_data["error"])
    return _data["result"]
"""


# The line a model answers with, after a program reached the tool-call cap,
# when the whole job cannot be done in fewer calls. The node then fails the run
# (CodeActionBudgetError) instead of accepting a program over part of the data.
BUDGET_GIVE_UP = "CANNOT_FINISH_WITHIN_CALL_CAP"


class CallBudgetFailure(RuntimeError):
    """A code-action node's failure to do the job within ``max_tool_calls``.

    Recorded as the cause of the node's failure; the engine raises it as
    :class:`~promptise.engine.execution.CodeActionBudgetError` (chained to
    this exception).

    Attributes:
        max_tool_calls: The per-program cap.
        attempts: How many programs the model wrote.
    """

    def __init__(self, message: str, *, max_tool_calls: int, attempts: int) -> None:
        super().__init__(message)
        self.max_tool_calls = max_tool_calls
        self.attempts = attempts


def _gives_up(text: str) -> bool:
    """Whether a model reply is the :data:`BUDGET_GIVE_UP` line (alone on a
    line, optionally inside a code fence)."""
    return any(line.strip().strip("`").strip() == BUDGET_GIVE_UP for line in text.splitlines())


@dataclass
class _ToolBudget:
    """The tool calls one program run has made against ``max_tool_calls``.

    Every call the program asks for counts: successful, failed, an unknown
    tool, or a call a ``pre_tool`` hook blocked. Calls beyond the limit are
    refused (``refused``) and do not run.
    """

    limit: int
    used: int = 0
    refused: int = 0

    @property
    def exhausted(self) -> bool:
        return self.refused > 0


class CodeActionConfig(BaseModel):
    """Tuning knobs for the ``code-action`` pattern.

    Pass it (or an equivalent dict) as ``build_agent(code_action=...)``.
    Unknown keys are rejected.

    Attributes:
        exec_timeout: Max seconds the generated program may run in the
            sandbox. When it expires the program's processes are killed.
        max_repairs: How many times a failed program (it crashed, or it
            reached ``max_tool_calls``) is fed back to the model for a fix.
        max_tool_calls: Hard cap on the bridged tool calls of each program
            the model writes. Every call counts, failed ones included. The
            first program and each repaired one get their own budget, so a
            run makes at most ``(max_repairs + 1) * max_tool_calls`` calls.
            A program that reaches the cap gets ``ToolBudgetExceeded`` for
            each further call, and its output is never taken as the answer.
            The model is not told the value, and is told never to skip
            records to stay under it. A run that cannot do the whole job
            within the cap raises ``CodeActionBudgetError`` (a
            ``GraphExecutionError``). ``0`` disables the cap (not advised).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    exec_timeout: int = Field(120, gt=0, le=3600)
    max_repairs: int = Field(1, ge=0, le=10)
    max_tool_calls: int = Field(50, ge=0)


# A factory that produces a ready sandbox session. The node owns the session
# lifecycle (creates one per run, cleans it up afterwards).
SandboxFactory = Callable[[], Awaitable[Any]]


def _tool_signature(tool: Any) -> tuple[str, list[str]]:
    """Return (name, [arg_names]) for a LangChain BaseTool."""
    name = str(getattr(tool, "name", None) or getattr(tool, "__name__", None) or "tool")
    args = getattr(tool, "args", None)
    arg_names: list[str] = [str(k) for k in args] if isinstance(args, dict) else []
    return name, arg_names


def render_api_spec(tools: list[Any]) -> str:
    """Render the available tools as a Python API the model can call."""
    if not tools:
        return "(no data functions available — compute the answer directly)"
    lines: list[str] = []
    for tool in tools:
        name, arg_names = _tool_signature(tool)
        sig = ", ".join(arg_names)
        desc = (getattr(tool, "description", "") or "").strip().splitlines()
        doc = desc[0] if desc else ""
        lines.append(f"def {name}({sig}):  # {doc}".rstrip())
    return "\n".join(lines)


def _render_tools_module(tools: list[Any], call_timeout: int) -> str:
    """Generate the in-sandbox ``promptise_tools.py`` (RPC stubs)."""
    parts = [_RPC_CLIENT % (_RPC_DIR, call_timeout)]
    for tool in tools:
        name, arg_names = _tool_signature(tool)
        sig = ", ".join(arg_names)
        arg_dict = "{" + ", ".join(f"{a!r}: {a}" for a in arg_names) + "}"
        parts.append(f"def {name}({sig}):\n    return _call({name!r}, {arg_dict})\n")
    return "\n".join(parts)


def extract_code(text: str) -> str:
    """Pull a Python program out of an LLM response (```python block or raw)."""
    if not text:
        return ""
    fence = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text.strip()


def parse_result(stdout: str, marker: str) -> str:
    """Extract the final answer from program stdout (last ``marker`` line)."""
    answer = ""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(marker):
            answer = stripped[len(marker) :].strip()
    if answer:
        return answer
    # Fallback: last non-empty line.
    for line in reversed(stdout.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _message_role(msg: Any) -> str | None:
    """Return ``"system"``, ``"user"`` or ``"assistant"`` for a message (object or dict)."""
    if isinstance(msg, SystemMessage):
        return "system"
    if isinstance(msg, HumanMessage):
        return "user"
    if isinstance(msg, AIMessage):
        return "assistant"
    if isinstance(msg, dict):
        role = msg.get("role")
        if role in ("system", "user", "assistant"):
            return str(role)
        if role == "human":
            return "user"
        if role == "ai":
            return "assistant"
    return None


def _message_text(msg: Any) -> str:
    content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", msg)
    return content if isinstance(content, str) else str(content)


def _extract_task(state: GraphState) -> str:
    """Get the user's latest question from the message list (objects or dicts)."""
    for msg in reversed(state.messages):
        if _message_role(msg) == "user":
            return _message_text(msg)
    # Fallback: first message content.
    if state.messages:
        return _message_text(state.messages[0])
    return ""


def _extract_context(state: GraphState) -> tuple[list[str], list[Any]]:
    """Split the conversation before the latest question into context.

    Returns:
        ``(system_texts, history)``: the content of every system message
        (memory context, conversation-flow prompts, ``chat()`` system
        prompts), and the earlier user/assistant turns as text-only
        messages. Tool traffic and empty assistant turns are dropped.
    """
    last_user = None
    for index in range(len(state.messages) - 1, -1, -1):
        if _message_role(state.messages[index]) == "user":
            last_user = index
            break
    system_texts: list[str] = []
    history: list[Any] = []
    for index, msg in enumerate(state.messages):
        role = _message_role(msg)
        text = _message_text(msg).strip() if role else ""
        if not text:
            continue
        if role == "system":
            system_texts.append(text)
        elif last_user is not None and index < last_user:
            history.append(
                HumanMessage(content=text) if role == "user" else AIMessage(content=text)
            )
    return system_texts, history


async def write_text(session: Any, path: str, content: str) -> None:
    """Write a file into the sandbox (read-only-rootfs safe).

    Delegates to :meth:`SandboxSession.write_file`, which writes from
    *inside* the container in base64 slices, so arbitrary content (quotes,
    newlines, shell metachars) is never interpreted by the shell and large
    files are not limited by the kernel's argument-size cap.
    """
    try:
        await session.write_file(path, content)
    except Exception as exc:
        raise RuntimeError(f"sandbox write failed ({path}): {str(exc)[:200]}") from exc


async def _read_text(session: Any, path: str) -> str | None:
    res = await session.execute(f"cat {shlex.quote(path)}")
    return res.stdout if res.success else None


def _is_mcp_tool(tool: Any) -> bool:
    """Whether *tool* is a Promptise MCP tool, bare or behind Promptise's
    wrappers (approval gate, guardrail scanning, tool tracing)."""
    from promptise.mcp.client._tool_adapter import _PromptiseMCPTool
    from promptise.tools import WrappingTool

    inner: Any = tool
    for _ in range(8):  # wrappers nest a few levels deep at most
        if not isinstance(inner, WrappingTool):
            break
        inner = getattr(inner, "_inner", None)
    return isinstance(inner, _PromptiseMCPTool)


def _parse_mcp_text(tool_name: str, text: str) -> Any:
    """The value an MCP tool's text result carries.

    A Promptise MCP server sends a tool's return value as text: JSON for
    lists and dicts, ``str()`` for numbers, the string itself for a string.
    Text that is valid JSON is decoded (so a list arrives as a list and a
    number as a number); any other text is returned as it is. A Promptise
    error envelope (``{"error": {"code": ..., "message": ...}}``) raises
    :class:`~promptise.mcp.client.MCPToolError`, like the MCP tool itself
    does for an error result.
    """
    from promptise.mcp.client._tool_adapter import MCPToolError, _error_envelope

    envelope = _error_envelope(text)
    if envelope is not None:
        code, message, retryable = envelope
        raise MCPToolError(tool_name, message, code=code, retryable=retryable, text=text)
    try:
        return json.loads(text)
    except ValueError:
        return text


class CodeActionNode(BaseNode):
    """A node that solves a task by writing and running one sandboxed program.

    Args:
        name: Node identifier.
        tools: Tools the generated program may call (bridged to the host). If
            empty, falls back to the engine's runtime tools (``_engine_tools``).
        system_prompt: Base system prompt prepended to the code-writing prompt.
        blocks: Accepted for signature parity with other prebuilts (unused in
            the program prompt — ``system_prompt`` carries the instructions).
        sandbox_factory: ``async () -> SandboxSession``. Required at run time;
            the node creates one session per run and cleans it up.
        model_override: Optional per-node model.
        max_repairs: How many times to feed a failed program (a crash, or a
            program that reached ``max_tool_calls``) back for a fix.
        result_marker: stdout prefix that marks the final answer line.
        exec_timeout: Max seconds the program may run inside the sandbox.
        max_tool_calls: Hard upper bound on the bridged tool calls of each
            program (the first and each repaired one get their own budget).
            Every call counts: successful, failed, an unknown tool, or one a
            hook blocked. Once reached, further calls raise
            ``ToolBudgetExceeded`` in the program instead of executing, a
            warning is logged, and the program's output is discarded: the
            model is asked for a program that does the whole job in fewer
            calls (never one over part of the data), or to reply
            ``CANNOT_FINISH_WITHIN_CALL_CAP``. That reply, or a budget hit
            with no repairs left, fails the node, and the engine raises
            ``CodeActionBudgetError`` (a ``GraphExecutionError``). The prompt
            says the cap exists but not its value, so the model cannot fit
            the data to it. A hook-independent safety bound so a generated
            program cannot loop a tool unbounded. Set ``0`` to disable (not
            advised).
    """

    def __init__(
        self,
        name: str,
        *,
        tools: list[Any] | None = None,
        system_prompt: str = "",
        blocks: list[Any] | None = None,
        sandbox_factory: SandboxFactory | None = None,
        model_override: Any | None = None,
        max_repairs: int = 1,
        result_marker: str = "RESULT:",
        exec_timeout: int = 120,
        max_tool_calls: int = 50,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.tools = list(tools) if tools else []
        self.system_prompt = system_prompt
        self.blocks = list(blocks) if blocks else []
        self.sandbox_factory = sandbox_factory
        self.model_override = model_override
        self.max_repairs = max_repairs
        self.result_marker = result_marker
        self.exec_timeout = exec_timeout
        self.max_tool_calls = max_tool_calls

    # ── prompt assembly ──────────────────────────────────────────────────────
    def _build_prompt(
        self,
        task: str,
        tools: list[Any],
        context: tuple[list[str], list[Any]] | None = None,
    ) -> list[Any]:
        api = render_api_spec(tools)
        system_texts, history = context if context is not None else ([], [])
        preamble = "\n\n".join(t for t in [self.system_prompt, *system_texts] if t)
        system = (f"{preamble}\n\n" if preamble else "") + (
            "You answer the user's question by writing ONE Python 3 program.\n"
            "The following functions are already imported and available to call:\n\n"
            f"{api}\n\n"
            "Rules:\n"
            "- Use ONLY those functions to obtain data; never invent data values.\n"
            "- Each function returns a plain Python value (str, int, float, list, "
            "or dict) — use the returned value directly. If a return value is a "
            "string, inspect it before assuming a structure (don't call .get on a "
            "str). When unsure of the shape, handle both dict and string.\n"
            "- Do the work in Python — loops, sums, filtering, etc.\n"
            f"- Print the final answer on the last line as: {self.result_marker} <answer>\n"
            + (
                # The cap's value is deliberately not stated: told "at most N
                # calls", models count their own calls and stop early, or slice
                # the data to fit, and the partial result becomes the answer.
                "- Use ALL the data the question needs. Never skip, sample or "
                "truncate records, and never count your own function calls or stop "
                "early to stay under a limit.\n"
                "- Each program has a safety cap on function calls. If the work needs "
                "more calls than that, the call over the cap raises ToolBudgetExceeded: "
                "do not catch it, let it raise. The run then ends with an error, which "
                "is correct; an answer computed from part of the data is wrong. Avoid "
                "needless calls (prefer a function that returns many records over one "
                "call per record, and never fetch the same data twice).\n"
                if self.max_tool_calls
                else ""
            )
            + "- Output ONLY the program inside a single ```python code block, no prose.\n"
            "- Do not call input(); do not access the network."
            + (
                "\n- The earlier conversation is included for context; answer the latest question."
                if history
                else ""
            )
        )
        return [
            SystemMessage(content=system),
            *history,
            HumanMessage(content=f"Question: {task}"),
        ]

    # ── tool bridge ──────────────────────────────────────────────────────────
    def _budget_message(self, name: str) -> str:
        """The error the program gets for a refused call. It does not state
        the cap's value: a repair told the number fits the data to it."""
        return (
            "tool-call budget exhausted: this program reached the cap on function "
            f"calls per program, so the call to {name!r} was refused and every "
            "further call will be; the program's output will be discarded"
        )

    async def _invoke_tool(
        self,
        tool_map: dict[str, Any],
        state: GraphState,
        config: dict[str, Any],
        name: str,
        args: dict[str, Any],
        result: NodeResult,
        budget: _ToolBudget | None = None,
    ) -> dict[str, Any]:
        """Run one real tool on the host, through the engine's hooks.

        *budget* is the calling program's tool-call budget. Without one (a
        direct call), the calls already recorded on *result* count.
        """
        if budget is None:
            budget = _ToolBudget(
                self.max_tool_calls, used=len(result.tool_calls) + result.tool_calls_failed
            )
        # Hook-independent safety bound: a generated program cannot loop a tool
        # unbounded, regardless of whether budget governance hooks are attached.
        # Every call counts, whether it succeeds or not.
        if budget.limit and budget.used >= budget.limit:
            budget.refused += 1
            if budget.refused == 1:
                logger.warning(
                    "code-action node %r: tool-call budget exhausted: the program "
                    "already made %d tool call(s) (max_tool_calls=%d); the call to %r "
                    "was refused and the program's output will be discarded",
                    self.name,
                    budget.limit,
                    budget.limit,
                    name,
                )
            return {"error": self._budget_message(name), "budget_exceeded": True}
        budget.used += 1

        from promptise.function_tools import _JSONText

        hooks = config.get("_engine_hooks", [])
        tool = tool_map.get(name)
        call_start = time.perf_counter()
        try:
            if tool is None:
                raise LookupError(f"unknown tool: {name}")
            for hook in hooks:
                if hasattr(hook, "pre_tool"):
                    args = await hook.pre_tool(name, args, state)
            output = await tool.ainvoke(args)
            text: str | None = None
            if isinstance(output, str) and _is_mcp_tool(tool):
                # An MCP tool answers with the text of the server's result,
                # which carries JSON data as JSON text: hand the program the
                # data (lists, dicts, numbers) as the docs promise.
                text = output
                output = _parse_mcp_text(name, output)
            elif isinstance(output, _JSONText):
                # A native tool's return value, as JSON for the model: the
                # program gets the value back.
                text = str(output)
                output = json.loads(output)
            # Preserve JSON-serializable structure so the program receives real
            # Python objects (dict/list/number/str), not a stringified blob.
            try:
                json.dumps(output)
                value: Any = output
            except (TypeError, ValueError):
                value = str(output)
            if text is None:
                text = value if isinstance(value, str) else json.dumps(value, default=str)
            # post_tool(tool_name, result, args, state) may rewrite the result
            # (redaction, truncation); the program and the record get that.
            for hook in hooks:
                if hasattr(hook, "post_tool"):
                    rewritten = await hook.post_tool(name, text, args, state)
                    if isinstance(rewritten, str) and rewritten != text:
                        text = rewritten
                        try:
                            value = json.loads(rewritten)
                        except ValueError:
                            value = rewritten
            state.add_observation(
                name,
                text,
                args,
                success=True,
                duration_ms=(time.perf_counter() - call_start) * 1000,
            )
            result.tool_calls.append({"name": name, "args": args, "result": text})
            return {"result": value}
        except Exception as exc:  # noqa: BLE001 — surface error to the program
            result.tool_calls_failed += 1
            message = str(exc) if tool is None else f"{type(exc).__name__}: {exc}"
            state.add_observation(
                name,
                str(exc),
                args,
                success=False,
                duration_ms=(time.perf_counter() - call_start) * 1000,
            )
            return {"error": message}

    async def _bridge_loop(
        self,
        session: Any,
        tool_map: dict[str, Any],
        state: GraphState,
        config: dict[str, Any],
        result: NodeResult,
        stop: asyncio.Event,
        budget: _ToolBudget | None = None,
    ) -> None:
        """Service the program's tool requests until it finishes."""
        if budget is None:
            budget = _ToolBudget(self.max_tool_calls)
        served: set[str] = set()
        while not stop.is_set():
            try:
                files = await session.list_files(_RPC_DIR)
            except Exception:  # noqa: BLE001 — dir may not exist yet
                files = []
            for fn in files:
                if not (fn.startswith("req_") and fn.endswith(".json")):
                    continue
                rid = fn[len("req_") : -len(".json")]
                # Defense-in-depth: only service safe request ids so the id can
                # never steer host file I/O outside the RPC directory.
                if not _SAFE_RID.match(rid):
                    continue
                if rid in served:
                    continue
                served.add(rid)
                raw = await _read_text(session, f"{_RPC_DIR}/{fn}")
                if raw is None:
                    served.discard(rid)
                    continue
                try:
                    req = json.loads(raw)
                except json.JSONDecodeError:
                    served.discard(rid)
                    continue
                resp = await self._invoke_tool(
                    tool_map,
                    state,
                    config,
                    req.get("tool", ""),
                    req.get("args", {}),
                    result,
                    budget,
                )
                await write_text(session, f"{_RPC_DIR}/resp_{rid}.json", json.dumps(resp))
                await write_text(session, f"{_RPC_DIR}/resp_{rid}.done", "")
            await asyncio.sleep(0.03)

    # ── main pipeline ────────────────────────────────────────────────────────
    async def execute(self, state: GraphState, config: dict[str, Any]) -> NodeResult:
        start = time.perf_counter()
        result = NodeResult(node_name=self.name, node_type="CodeActionNode")

        model = self.model_override or config.get("_engine_model")
        if model is None:
            result.error = "No model available (_engine_model missing)"
            result.duration_ms = (time.perf_counter() - start) * 1000
            return result
        if self.sandbox_factory is None:
            result.error = (
                "code-action requires a sandbox. Build the agent with sandbox=True "
                "(Docker must be installed and running)."
            )
            result.duration_ms = (time.perf_counter() - start) * 1000
            return result

        tools = self.tools or config.get("_engine_tools", []) or []
        tool_map = {getattr(t, "name", ""): t for t in tools}
        # The run's tool selector (semantic tool selection) narrows the API the
        # prompt describes; the bridge still serves every tool.
        offered = tools
        selector = config.get(TOOL_SELECTOR_KEY)
        if selector is not None and tools:
            try:
                offered = list(selector(tools, state))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Tool selector failed in node %r: %s", self.name, exc)
        task = _extract_task(state)
        messages = self._build_prompt(task, offered, _extract_context(state))

        # 1. Generate the program (1 LLM turn; +1 per repair).
        code = ""
        failure = ""
        budget_failure = False  # the last attempt failed on the tool-call cap
        budget_hits = 0
        attempts = 0
        for attempt in range(self.max_repairs + 1):
            attempts = attempt + 1
            llm_start = time.perf_counter()
            try:
                response = await model.ainvoke(messages, config=config)
            except Exception as exc:  # noqa: BLE001
                result.error = f"LLM call failed: {type(exc).__name__}: {exc}"
                result.duration_ms = (time.perf_counter() - start) * 1000
                return result
            result.llm_duration_ms += (time.perf_counter() - llm_start) * 1000
            prompt_tokens, completion_tokens = token_usage(response)
            result.prompt_tokens += prompt_tokens
            result.completion_tokens += completion_tokens
            raw = response.content if isinstance(response.content, str) else str(response.content)
            code = extract_code(raw)

            # After a budget hit the model may answer that the whole job cannot
            # be done in fewer calls: that ends the run with an error.
            if budget_failure and _gives_up(raw):
                failure = (
                    "the job needs more tool calls than the per-program budget "
                    f"(max_tool_calls={self.max_tool_calls}) allows: a program reached "
                    "it, and the model reported that the whole job cannot be done in "
                    "fewer calls"
                )
                break

            # 2. Run it in the sandbox (bridging tool calls back to the host).
            # Each program gets its own tool-call budget.
            budget = _ToolBudget(self.max_tool_calls)
            run = await self._run_program(code, tool_map, state, config, result, budget)
            if run.success and not budget.exhausted:
                if budget_hits:
                    logger.warning(
                        "code-action node %r: the answer comes from a program written "
                        "after %d earlier program(s) reached max_tool_calls=%d",
                        self.name,
                        budget_hits,
                        self.max_tool_calls,
                    )
                answer = parse_result(run.stdout, self.result_marker)
                result.total_tokens = result.prompt_tokens + result.completion_tokens
                result.raw_output = answer
                result.output = answer
                ai = AIMessage(content=answer)
                state.messages.append(ai)
                result.messages_added.append(ai)
                result.transition_reason = f"code-action solved in {attempt + 1} attempt(s)"
                result.duration_ms = (time.perf_counter() - start) * 1000
                return result

            # 3. Repair: feed the failure back and try again. A program that
            # reached the tool-call budget is a failure even if it caught the
            # error and printed something: its output is incomplete.
            budget_failure = budget.exhausted
            if budget.exhausted:
                budget_hits += 1
                failure = (
                    "the program reached the tool-call budget (max_tool_calls="
                    f"{self.max_tool_calls}) and its further calls were refused, so its "
                    "output was discarded"
                )
                feedback = (
                    "Your program reached the cap on function calls per program (calls "
                    "over it raise ToolBudgetExceeded), so it could not finish and its "
                    "output was discarded.\n\n"
                    "Write a new program that does the WHOLE job with fewer calls (it "
                    "gets a fresh budget): use a function that returns many records at "
                    "once if there is one, never fetch the same data twice, and reuse "
                    "values you already have. Do NOT skip, sample or truncate records, "
                    "and do not count calls or stop early: a program that answers from "
                    "part of the data gives a wrong answer.\n\n"
                    "If the whole job cannot be done with fewer calls, do not write a "
                    "program; reply with exactly this line and nothing else:\n"
                    f"{BUDGET_GIVE_UP}\n\n"
                    "Otherwise output only the program."
                )
                if run.stderr.strip():
                    feedback += f"\n\nIts error output:\n\n{run.stderr[-800:]}"
            else:
                last_line = next(
                    (ln.strip() for ln in reversed(run.stderr.splitlines()) if ln.strip()),
                    f"exit code {run.exit_code}",
                )
                failure = f"the program crashed: {last_line[:300]}"
                feedback = (
                    "Your program failed with this error — fix it and output "
                    f"only the corrected program:\n\n{run.stderr[-800:]}"
                )
            if attempt < self.max_repairs:
                messages = messages + [AIMessage(content=raw), HumanMessage(content=feedback)]

        # All attempts failed (or the model reported the job cannot fit).
        result.total_tokens = result.prompt_tokens + result.completion_tokens
        error = f"program failed after {attempts} attempt(s); last attempt: {failure}"
        if budget_failure:
            # The engine raises this as CodeActionBudgetError.
            record_failure(
                result,
                CallBudgetFailure(error, max_tool_calls=self.max_tool_calls, attempts=attempts),
                error,
            )
        else:
            result.error = error
        result.raw_output = code
        result.output = ""
        result.duration_ms = (time.perf_counter() - start) * 1000
        return result

    async def _run_program(
        self,
        code: str,
        tool_map: dict[str, Any],
        state: GraphState,
        config: dict[str, Any],
        result: NodeResult,
        budget: _ToolBudget | None = None,
    ) -> Any:
        """Write + execute the program in a fresh sandbox session, bridging tools."""
        if budget is None:
            budget = _ToolBudget(self.max_tool_calls)
        session = await self.sandbox_factory()  # type: ignore[misc]
        try:
            await session.execute(f"mkdir -p {_RPC_DIR}")
            await write_text(
                session,
                _TOOLS_PATH,
                _render_tools_module(list(tool_map.values()), self.exec_timeout),
            )
            program = "from promptise_tools import *  # noqa\n\n" + code
            await write_text(session, _PROGRAM_PATH, program)

            stop = asyncio.Event()
            prog_task = asyncio.create_task(
                session.execute(f"python3 {_PROGRAM_PATH}", timeout=self.exec_timeout)
            )
            bridge_task = asyncio.create_task(
                self._bridge_loop(session, tool_map, state, config, result, stop, budget)
            )
            tool_start = time.perf_counter()
            cmd = await prog_task
            stop.set()
            await bridge_task
            result.tool_duration_ms += (time.perf_counter() - tool_start) * 1000
            logger.debug("code-action program:\n%s", program)
            logger.debug(
                "code-action exit=%s stdout=%r stderr=%r",
                cmd.exit_code,
                cmd.stdout[:600],
                cmd.stderr[:600],
            )
            return cmd
        finally:
            try:
                await session.cleanup()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.warning("sandbox cleanup failed", exc_info=True)
