"""Agent Orchestration API — REST API for managing AgentRuntime.

Exposes lifecycle management, configuration updates, human communication,
observability, and batch operations as HTTP endpoints.  Sits on top of
an existing :class:`AgentRuntime` and translates HTTP requests to
Python method calls.

Example::

    from promptise.runtime import AgentRuntime
    from promptise.runtime.api import OrchestrationAPI

    runtime = AgentRuntime()
    api = OrchestrationAPI(
        runtime,
        host="0.0.0.0",
        port=9100,
        auth_token="${ORCHESTRATION_API_TOKEN}",
    )
    await api.start()

Security model:

* Every route except ``GET /api/v1/health`` requires
  ``Authorization: Bearer <auth_token>`` when ``auth_token`` is set.  A
  non-loopback ``host`` without ``auth_token`` is refused at construction.
* On a loopback bind, requests whose ``Host`` header is not a loopback
  name are refused with ``421`` (DNS rebinding), and requests carrying a
  cross-site ``Origin`` with ``403``, so a web page open in a browser on
  the same machine can't drive the runtime.
* Request bodies must be JSON objects (``400`` otherwise).
* Deploys can't run code on the host unless that is enabled: a
  ``POST /api/v1/processes`` whose config has a stdio MCP server (a
  ``command``), a command hook, or open mode with
  ``open_mode.allow_tool_creation`` (the agent's ``create_tool`` runs
  model-written Python in the runtime's process, and restricted builtins
  are not a sandbox) is refused with ``403 COMMANDS_NOT_ALLOWED`` unless
  the API was created with ``allow_command_deploys=True`` (``promptise
  runtime start --allow-command-deploys``).  The other routes that change
  a process (triggers, ``PATCH`` sections, context, instructions) have no
  field that runs a program, and an open-mode agent's
  ``connect_mcp_server`` only connects HTTP servers and ``spawn_process``
  children get no servers, so a deployed agent can't start one either.
  Processes the operator loads from manifests on the host are not
  affected.
* Deploys can't read the host's environment: a config sent to
  ``POST /api/v1/processes``, ``POST .../triggers`` or a ``PATCH`` route
  with a ``${VAR}`` reference anywhere (``instructions`` aside, which are
  never resolved) is refused with ``403 ENV_REFERENCES_NOT_ALLOWED``,
  unless ``allow_env_references=True`` or every variable is in
  ``allowed_env_vars``.  As a backstop the deployed process's secret
  scope resolves only those variables, and a failed resolution gives the
  same generic error whether a variable is refused or not set.
* Deploys can't pick file system paths outside ``data_dir`` (default
  ``./.promptise``): ``journal.path`` (file journal),
  ``context.memory_persist_directory`` and file-watch ``watch_path``
  values outside it, or with ``..``, are refused with ``403
  PATH_NOT_ALLOWED`` (the agent's own ``add_trigger`` is held to it too).
* An API-deployed open-mode process with an empty
  ``open_mode.allowed_mcp_urls`` may connect no MCP server (for an
  operator's process empty still means any public URL).
* Answers and inbox listings are redacted: secret values in an ``/ask``
  answer or ``GET .../inbox`` are ``[REDACTED]``, as in the journal; and
  ``GET .../triggers`` masks ``hmac_secret`` and the values of credential
  keys in ``custom_config``.
* ``PATCH`` on ``budget``, ``health`` and ``mission`` validates the patched
  section against its config model (limits, strict JSON types, no unknown
  fields) and answers ``422`` without changing anything when it fails.

Errors are JSON objects ``{"error": {"code": ..., "message": ...}}``; the
codes are listed in :data:`ERROR_CODES`.  An unexpected server error is
``500 INTERNAL_ERROR`` with a generic message (the details go to the log).

Audit: every mutating request (``POST``/``PATCH``/``PUT``/``DELETE``) is
logged to the ``promptise.runtime.api`` logger with its method, route,
path parameters, status and client address.  Successful ones are also
written to the process's journal (when it has one) as an ``api_request``
entry.  Request bodies are never logged (they can hold secret values and
message text).

Changes that the running process can't apply yet say so: config updates
answer with ``effective_from`` and ``live`` (``False`` when the change
only applies after a restart or a redeploy) instead of pretending to be
live.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import re
import time
import types
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from aiohttp import web
from pydantic import BaseModel, ValidationError

from .._logsafe import log_safe
from ._deploy_policy import DeployPolicy, default_data_dir, env_references_in, path_problem
from ._http_guard import bearer_token_matches, is_loopback, loopback_request_problem
from .triggers.base import config_trigger_ids

logger = logging.getLogger("promptise.runtime.api")

__all__ = ["ERROR_CODES", "OrchestrationAPI", "command_features", "config_trigger_ids"]

# Process name validation: alphanumeric + hyphens + underscores, max 64 chars
_PROCESS_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")

#: Routes that answer without a bearer token (load-balancer probes).
_PUBLIC_PATHS = frozenset({"/api/v1/health"})

#: Methods whose request body is checked to be a JSON object.
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Methods that change something (audited).
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Request storage key: the process a deploy created (for the audit log).
_AUDIT_PROCESS: Any = (
    web.RequestKey("promptise_audit_process", str)
    if hasattr(web, "RequestKey")
    else "promptise_audit_process"
)

#: Every error code the API answers with: ``code -> (status, meaning)``.
#: The status is ``None`` for ``HTTP_ERROR``, which keeps the status of the
#: underlying HTTP error.
ERROR_CODES: dict[str, tuple[int | None, str]] = {
    "INVALID_JSON": (400, "Request body is not a JSON object"),
    "INBOX_DISABLED": (400, "Inbox operation on a process without an inbox"),
    "SECRETS_DISABLED": (400, "Secret operation on a process without secret scoping"),
    "MISSION_DISABLED": (400, "Mission operation on a process without a mission"),
    "HEALTH_DISABLED": (400, "Health operation on a process without health monitoring"),
    "JOURNAL_DISABLED": (400, "Rewind on a process without a journal"),
    "SUSPEND_FAILED": (400, "The process can't be suspended in its current state"),
    "RESUME_FAILED": (400, "The process can't be resumed in its current state"),
    "ROTATION_FAILED": (400, "The secret could not be rotated"),
    "UNAUTHORIZED": (401, "Missing or wrong bearer token"),
    "WRITE_DENIED": (403, "Context key(s) not writable (nothing was written)"),
    "CROSS_ORIGIN_REFUSED": (403, "Cross-site Origin on a loopback bind"),
    "COMMANDS_NOT_ALLOWED": (
        403,
        "Deploy with a stdio MCP server, a command hook or open-mode tool creation while "
        "allow_command_deploys is off",
    ),
    "ENV_REFERENCES_NOT_ALLOWED": (
        403,
        "Config with a ${VAR} reference the API doesn't allow (allow_env_references / "
        "allowed_env_vars)",
    ),
    "PATH_NOT_ALLOWED": (
        403,
        "Journal, memory or watch path outside the API's data_dir (or with '..')",
    ),
    "NOT_FOUND": (404, "No such route"),
    "PROCESS_NOT_FOUND": (404, "Process name doesn't exist"),
    "TRIGGER_NOT_FOUND": (404, "Trigger ID doesn't exist"),
    "SECRET_NOT_FOUND": (404, "The process has no secret of that name"),
    "ENTRY_NOT_FOUND": (404, "Journal entry ID doesn't exist (rewind target)"),
    "METHOD_NOT_ALLOWED": (405, "The route doesn't accept this method"),
    "QUESTION_REMOVED": (
        410,
        "/ask: the question was cleared or evicted from the inbox before it was answered",
    ),
    "PAYLOAD_TOO_LARGE": (413, "Request body over the server's size limit (1 MiB)"),
    # No fixed status: the HTTP error's own (400, 408, 411, 414, 431, ...).
    "HTTP_ERROR": (None, "Another HTTP error from the server; the response status says which"),
    "PROCESS_EXISTS": (409, "Deploy with a name that's already registered"),
    "PROCESS_NOT_RUNNING": (409, "Message or question to a stopped or failed process"),
    "TRIGGER_EXISTS": (409, "Adding a trigger identical to an existing one"),
    "MISSION_STATE": (409, "Mission fail/pause/resume not valid in the mission's state"),
    "REWIND_BUSY": (409, "A run was still in flight when the rewind timed out"),
    "MISDIRECTED_REQUEST": (421, "Non-loopback Host on a loopback bind (DNS rebinding)"),
    "MISSING_FIELD": (422, "Required field missing from the request"),
    "INVALID_NAME": (422, "Process name doesn't match ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$"),
    "INVALID_FIELD": (422, "A field has the wrong type or an out-of-range value"),
    "INVALID_TYPE": (422, "Unknown message_type"),
    "INVALID_CONFIG": (422, "Config fails validation (deploy, trigger, PATCH sections)"),
    "UNKNOWN_FIELD": (422, "PATCH names a field the config model doesn't declare"),
    "DEPLOY_FAILED": (422, "The process could not be created from a valid config"),
    "MESSAGE_REJECTED": (422, "The inbox's guardrail scan flagged the message"),
    "INBOX_FULL": (409, "The inbox is full and nothing can be evicted for the message"),
    "RATE_LIMITED": (429, "Inbox rate limit exceeded"),
    "INTERNAL_ERROR": (500, "Unexpected server error (details in the server log)"),
    "START_FAILED": (500, "The process failed to start"),
    "STOP_FAILED": (500, "The process failed to stop"),
    "RESTART_FAILED": (500, "The process failed to restart"),
}

#: Process states in which triggers run and live changes take effect (the
#: states in which ``AgentProcess.add_trigger`` starts a trigger at once; a
#: starting process is reported by the state it reaches).
_ACTIVE_STATES = frozenset({"running", "suspended", "awaiting"})

#: States in which a process takes no messages (as ``process.send_message``).
_CLOSED_STATES = frozenset({"stopped", "failed"})

#: Inbox message priorities.
_PRIORITIES = ("low", "normal", "high", "critical")

#: Longest ``/ask`` long-poll, in seconds.
_MAX_ASK_TIMEOUT = 3600.0

#: Process attribute holding each governance subsystem (``None`` when the
#: process was built without it).
_SUBSYSTEMS = {"budget": "_budget", "health": "_health", "mission": "_mission"}


def _json_dumps(obj: Any) -> str:
    """JSON serializer that handles non-standard types."""
    return json.dumps(obj, default=str, ensure_ascii=False)


def _json_response(data: Any, status: int = 200) -> web.Response:
    """Create a JSON response."""
    return web.Response(
        text=_json_dumps(data),
        content_type="application/json",
        status=status,
    )


def _error_response(code: str, message: str, status: int = 400) -> web.Response:
    """Create a structured error response."""
    return _json_response(
        {"error": {"code": code, "message": message}},
        status=status,
    )


def _http_error_response(exc: web.HTTPException) -> web.Response:
    """An aiohttp HTTP error (unknown route, body too large...) as a JSON error."""
    code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 413: "PAYLOAD_TOO_LARGE"}.get(
        exc.status, "HTTP_ERROR"
    )
    return _error_response(code, exc.reason, exc.status)


def command_features(config: Any) -> list[str]:
    """What in a process config runs a program (or arbitrary code) on the host.

    Stdio MCP servers (a :class:`~promptise.config.StdioServerSpec`, or a
    dict entry with a ``command`` or ``type``/``transport`` ``"stdio"``),
    command hooks (:class:`~promptise.runtime.config.HookConfig` with
    ``command``) and open mode with ``open_mode.allow_tool_creation`` (the
    agent's ``create_tool`` runs model-written Python in the runtime's
    process; its restricted builtins are not a sandbox).  Returns one
    description per finding, e.g. ``"servers['files'] (stdio MCP
    server)"``; empty when there is none.
    """
    from promptise.config import StdioServerSpec

    found: list[str] = []
    for name, spec in (getattr(config, "servers", None) or {}).items():
        if isinstance(spec, StdioServerSpec):
            stdio = True
        elif isinstance(spec, Mapping):
            transport = str(spec.get("type", spec.get("transport", "")) or "").lower()
            stdio = "command" in spec or transport == "stdio"
        else:
            stdio = False
        if stdio:
            found.append(f"servers[{name!r}] (stdio MCP server)")
    for index, hook in enumerate(getattr(config, "hooks", None) or []):
        command = (
            hook.get("command") if isinstance(hook, Mapping) else getattr(hook, "command", None)
        )
        if command is not None:
            event = hook.get("event") if isinstance(hook, Mapping) else getattr(hook, "event", "?")
            found.append(f"hooks[{index}] (command hook for {event!r})")
    mode = getattr(config, "execution_mode", None)
    open_mode = getattr(config, "open_mode", None)
    if str(getattr(mode, "value", mode)) == "open" and getattr(
        open_mode, "allow_tool_creation", False
    ):
        found.append(
            "open_mode.allow_tool_creation (agent-written Python runs in the runtime's process)"
        )
    return found


#: ``custom_config`` keys whose values ``GET .../triggers`` masks.
_CREDENTIAL_KEY = re.compile(
    r"secret|token|passw|pwd|api[-_]?key|auth|credential|private[-_]?key|signature|cookie",
    re.I,
)


def _mask_custom_config(value: Any) -> Any:
    """*value* with the values of credential-looking keys masked."""
    if isinstance(value, Mapping):
        return {
            k: "**********"
            if isinstance(k, str) and _CREDENTIAL_KEY.search(k) and v not in (None, "")
            else _mask_custom_config(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_custom_config(v) for v in value]
    return value


def _failure_response(
    code: str, message: str, exc: BaseException, status: int = 500
) -> web.Response:
    """An error response for a failure whose details must stay server-side.

    Exception text can carry file paths, internal state or secret values
    (a failed secret rotation, a provider error), so it is never sent to
    the client.  The client gets *message* and an ``error_id``; the
    exception, with its traceback, is logged on ``promptise.runtime.api``
    under the same id so an operator can find it (``ERROR`` for a ``5xx``,
    ``WARNING`` otherwise).
    """
    error_id = uuid.uuid4().hex[:12]
    logger.log(
        logging.ERROR if status >= 500 else logging.WARNING,
        "OrchestrationAPI: %s: %s (error_id=%s)",
        code,
        log_safe(message, 500),
        error_id,
        exc_info=exc,
    )
    return _json_response(
        {"error": {"code": code, "message": message, "error_id": error_id}},
        status=status,
    )


def _secret_unresolved(exc: BaseException) -> bool:
    """Whether *exc*, or an exception it was raised from, is a secret that
    couldn't be resolved."""
    from .secrets import SecretResolutionError

    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, SecretResolutionError):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def _entry_not_found(target: str, exc: BaseException) -> web.Response:
    """``404 ENTRY_NOT_FOUND`` for a rewind target the journal doesn't hold."""
    return _failure_response(
        "ENTRY_NOT_FOUND",
        f"Journal entry {target!r} not found in the process's journal (entry IDs are in "
        "GET .../journal)",
        exc,
        404,
    )


def _not_found(name: str) -> web.Response:
    return _error_response("PROCESS_NOT_FOUND", f"Process '{name}' not found", 404)


def _state_value(process: Any) -> str:
    state = process.state
    return state.value if hasattr(state, "value") else str(state)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _is_number(value: Any) -> bool:
    """A finite JSON number (``bool`` is not one)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _inbox_add(inbox: Any, message: Any) -> tuple[str, web.Response | None]:
    """Add *message* to *inbox*; map the inbox's refusals to API errors.

    The answers are written here from the message and the inbox settings,
    not from the exception (whose text the inbox writes for its own log);
    the exception is logged under the answer's ``error_id``.
    """
    from . import inbox as inbox_module

    try:
        return await inbox.add(message), None
    except ValueError as exc:
        rejected = getattr(inbox_module, "InboxMessageRejected", ())
        full = getattr(inbox_module, "InboxFullError", ())
        invalid = getattr(inbox_module, "InvalidInboxMessage", ())
        if rejected and isinstance(exc, rejected):
            return "", _failure_response("MESSAGE_REJECTED", _MESSAGE_REJECTED_TEXT, exc, 422)
        if full and isinstance(exc, full):
            limit = getattr(inbox, "_max_messages", None)
            size = f" ({limit} messages)" if isinstance(limit, int) else ""
            return "", _failure_response(
                "INBOX_FULL",
                f"The inbox is full{size} and nothing in it can be evicted for a "
                f"{message.priority!r} message; send it with a higher priority or wait "
                "for the agent to consume messages",
                exc,
                409,
            )
        if invalid and isinstance(exc, invalid):
            return "", _failure_response(
                "INVALID_FIELD",
                "Invalid message: 'content' must be a non-empty string, 'message_type' one "
                f"of {_MESSAGE_TYPES} and 'priority' one of {_PRIORITIES}",
                exc,
                422,
            )
        return "", _failure_response(
            "RATE_LIMITED",
            f"Rate limit exceeded: sender {message.sender_id!r} sent more messages in the "
            "last hour than the inbox's rate_limit_per_sender allows",
            exc,
            429,
        )


_MESSAGE_REJECTED_TEXT = (
    "Message refused: the inbox's guardrail scanner flagged its content (the finding "
    "categories are in the server log under the error_id)"
)
_MESSAGE_TYPES = "directive, context, question, correction"
_PRIORITIES = "low, normal, high, critical"


def _validation_detail(exc: BaseException) -> str | None:
    """Field-level messages of a pydantic ``ValidationError``, else ``None``.

    These name the offending field and say what was expected of the
    client's own input, so they are safe and useful to return.  Any other
    exception's text is not (see :func:`_failure_response`).
    """
    if not isinstance(exc, ValidationError):
        return None
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'body'}: {err['msg']}" for err in exc.errors()
    )


def _invalid_config(prefix: str, exc: BaseException) -> web.Response:
    """``422 INVALID_CONFIG`` for a config that failed to build.

    A pydantic error lists each field and what was expected; any other
    error gets a fixed message and an ``error_id`` (its text stays in the
    server log).
    """
    detail = _validation_detail(exc)
    if detail is None:
        return _failure_response(
            "INVALID_CONFIG", f"{prefix} (details are in the server log)", exc, 422
        )
    return _error_response("INVALID_CONFIG", f"{prefix}: {detail}", 422)


def _unknown_fields(
    model_cls: type[BaseModel], data: Mapping[str, Any], path: str = ""
) -> list[str]:
    """Dotted names of keys in *data* that *model_cls* does not declare.

    Recurses into nested models (``escalation``) and dicts of models
    (``tool_costs``), which pydantic would otherwise silently drop.
    """
    unknown: list[str] = []
    for key, value in data.items():
        field = model_cls.model_fields.get(key)
        if field is None:
            unknown.append(f"{path}{key}")
            continue
        if not isinstance(value, Mapping):
            continue
        annotation = field.annotation
        is_union = get_origin(annotation) in (Union, types.UnionType)
        for kind in get_args(annotation) if is_union else (annotation,):
            if isinstance(kind, type) and issubclass(kind, BaseModel):
                unknown += _unknown_fields(kind, value, f"{path}{key}.")
            elif get_origin(kind) is dict:
                item_type = get_args(kind)[1]
                if isinstance(item_type, type) and issubclass(item_type, BaseModel):
                    for name, item in value.items():
                        if isinstance(item, Mapping):
                            unknown += _unknown_fields(item_type, item, f"{path}{key}.{name}.")
    return unknown


def _apply_patch(section: BaseModel, body: dict[str, Any]) -> web.Response | None:
    """Validate *body* against *section*'s model, then apply it in place.

    The patched section is validated as a whole (strict JSON types, the
    model's limits) before anything changes, so a bad value leaves the
    live config untouched.  Fields are set on the existing object, which
    the running process keeps reading.

    Returns:
        A ``422`` error response, or ``None`` once the patch is applied.
    """
    model_cls = type(section)
    unknown = _unknown_fields(model_cls, body)
    if unknown:
        return _error_response(
            "UNKNOWN_FIELD",
            f"Unknown field(s): {', '.join(unknown)}. Allowed: {', '.join(model_cls.model_fields)}",
            422,
        )
    merged = {**section.model_dump(mode="json"), **body}
    try:
        validated = model_cls.model_validate_json(json.dumps(merged), strict=True)
    except ValidationError as exc:
        return _invalid_config("Config validation failed", exc)
    for key in body:
        setattr(section, key, getattr(validated, key))
    return None


class OrchestrationAPI:
    """REST API server for managing an AgentRuntime.

    Args:
        runtime: The :class:`AgentRuntime` to manage.
        host: Bind address (default ``127.0.0.1``).
        port: Port to listen on (default ``9100``).
        auth_token: Bearer token required on every route except
            ``GET /api/v1/health``. **Required** when ``host`` is not a
            loopback address.  ``"${VAR}"`` reads it from the environment.
        on_shutdown: Enables ``POST /api/v1/runtime/shutdown``
            (authenticated like every route): the request is answered
            ``202 {"status": "shutting_down"}`` and *on_shutdown* (a sync
            or async callable without arguments) is called right after,
            e.g. ``runtime.request_shutdown``.  Without it the route
            doesn't exist (``404``).  ``promptise runtime start`` uses it
            for ``promptise runtime stop``.
        allow_command_deploys: Let ``POST /api/v1/processes`` deploy
            processes that run programs on this host: stdio MCP servers
            (``command``) and command hooks.  Off by default, because
            anyone with the token could then run any command as the
            runtime's user; such a deploy answers ``403
            COMMANDS_NOT_ALLOWED``.  Turn it on only when every token
            holder may run commands here.  Processes already in the
            runtime (loaded from manifests by the operator) are not
            checked.  Open mode with ``open_mode.allow_tool_creation``
            counts as running commands (the agent's ``create_tool`` is not
            sandboxed).
        allow_env_references: Let deployed configs use ``${VAR}``
            references to any environment variable of this host (in
            secrets, which the agent can read back).  Off by default:
            such a config answers ``403 ENV_REFERENCES_NOT_ALLOWED``.
        allowed_env_vars: The variables deployed configs may reference
            when ``allow_env_references`` is off, e.g.
            ``{"SEARCH_API_KEY"}``.
        data_dir: The directory deployed configs' file system paths must
            lie in: ``journal.path`` (file journal),
            ``context.memory_persist_directory`` and file-watch
            ``watch_path`` (also those the deployed agent adds later).
            Relative paths are resolved against the working directory,
            as the process uses them.  Default: ``./.promptise`` (where
            the journal goes by default).  Others answer ``403
            PATH_NOT_ALLOWED``.

    Raises:
        ValueError: ``auth_token`` is empty, names an unset environment
            variable, or ``host`` is not a loopback address and there is no
            ``auth_token``.
    """

    def __init__(
        self,
        runtime: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 9100,
        auth_token: str | None = None,
        on_shutdown: Callable[[], Any] | None = None,
        allow_command_deploys: bool = False,
        allow_env_references: bool = False,
        allowed_env_vars: Iterable[str] | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        if on_shutdown is not None and not callable(on_shutdown):
            raise TypeError(f"on_shutdown must be callable, got {type(on_shutdown).__name__}")
        # Resolve env var in auth_token
        if auth_token and auth_token.startswith("${") and auth_token.endswith("}"):
            import os

            var_name = auth_token[2:-1].split(":-")[0]
            auth_token = os.environ.get(var_name, "")
            if not auth_token:
                raise ValueError(f"Environment variable '{var_name}' not set for auth_token")

        if auth_token is not None and not auth_token.strip():
            raise ValueError("OrchestrationAPI: auth_token must be a non-empty string (or None)")
        self._loopback = is_loopback(host)
        if not self._loopback and not auth_token:
            raise ValueError(
                "OrchestrationAPI: auth_token is required when binding to "
                f"non-localhost address '{host}'. Set auth_token to secure "
                "the management API."
            )

        self._runtime = runtime
        self._host = host
        self._port = port
        self._auth_token = auth_token
        self._on_shutdown = on_shutdown
        self._allow_command_deploys = bool(allow_command_deploys)
        if isinstance(allowed_env_vars, str):
            raise TypeError("allowed_env_vars must be a collection of names, not a string")
        self._allowed_env_vars = frozenset(allowed_env_vars or ())
        self._allow_env_references = bool(allow_env_references)
        self._data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self._deploy_policy = DeployPolicy.for_api(
            allow_env_references=self._allow_env_references,
            allowed_env_vars=self._allowed_env_vars,
            data_dir=self._data_dir,
        )
        self._shutdown_tasks: set[asyncio.Future[Any]] = set()

        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None

    @property
    def port(self) -> int:
        """The port the API listens on.

        With ``port=0`` the OS picks a free port; after :meth:`start` this
        returns the port actually bound.
        """
        if self._port == 0 and self._runner is not None:
            for address in self._runner.addresses:
                if isinstance(address, tuple) and len(address) >= 2:
                    return int(address[1])
        return self._port

    def _check_auth(self, request: web.Request) -> bool:
        """Verify Bearer token (timing-safe)."""
        if self._auth_token is None:
            return True  # Loopback bind without a token
        return bearer_token_matches(request.headers.get("Authorization", ""), self._auth_token)

    @web.middleware
    async def _guard(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        """Host/Origin checks (loopback binds), auth, JSON-object bodies,
        JSON errors and the audit log."""
        if self._loopback:
            problem = loopback_request_problem(
                request.headers.get("Host", ""), request.headers.get("Origin")
            )
            if problem is not None:
                status, message = problem
                code = "MISDIRECTED_REQUEST" if status == 421 else "CROSS_ORIGIN_REFUSED"
                logger.warning(
                    "OrchestrationAPI: refused %s %s from %s (%d %s)",
                    log_safe(request.method),
                    log_safe(request.path),
                    log_safe(request.remote),
                    status,
                    code,
                )
                return _error_response(code, message, status)
        if request.path not in _PUBLIC_PATHS and not self._check_auth(request):
            logger.warning(
                "OrchestrationAPI: refused %s %s from %s (401 UNAUTHORIZED)",
                log_safe(request.method),
                log_safe(request.path),
                log_safe(request.remote),
            )
            return _error_response("UNAUTHORIZED", "Invalid or missing auth token", 401)
        if request.method in _BODY_METHODS and request.can_read_body:
            try:
                raw = await request.read()
            except web.HTTPException as exc:  # 413: body over client_max_size
                return _http_error_response(exc)
            if raw.strip():
                try:
                    body = json.loads(raw)
                except ValueError:
                    return _error_response("INVALID_JSON", "Request body must be valid JSON", 400)
                if not isinstance(body, dict):
                    return _error_response(
                        "INVALID_JSON", "Request body must be a JSON object", 400
                    )
        response = await self._dispatch(request, handler)
        if request.method in _MUTATING_METHODS:
            await self._audit(request, response.status)
        return response

    async def _dispatch(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        """Run *handler*; turn HTTP errors and unexpected exceptions into JSON."""
        try:
            return await handler(request)
        except web.HTTPException as exc:
            if exc.status < 400:
                raise
            return _http_error_response(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _failure_response(
                "INTERNAL_ERROR",
                f"Internal server error in {request.method} {request.path} (details are in "
                "the server log)",
                exc,
            )

    async def _audit(self, request: web.Request, status: int) -> None:
        """Log a mutating request; journal it on its process when it succeeded.

        Records the method, route, path parameters (process, trigger and
        secret *names*), status and client address.  Never the body.
        """
        info = request.match_info
        route = getattr(getattr(info, "route", None), "resource", None)
        canonical = getattr(route, "canonical", request.path)
        params = dict(info)
        process_name = params.get("name") or request.get(_AUDIT_PROCESS)
        logger.info(
            "OrchestrationAPI: %s %s %s -> %d (client %s)",
            log_safe(request.method),
            log_safe(canonical),
            log_safe(params or "", 500),
            status,
            log_safe(request.remote),
        )
        if status >= 400 or not process_name:
            return
        process = self._runtime.processes.get(process_name)
        record = getattr(process, "record_operator_action", None) or getattr(
            process, "_journal_record", None
        )
        if record is None:
            return
        try:
            await record(
                "api_request",
                {
                    "method": request.method,
                    "route": canonical,
                    "params": params,
                    "status": status,
                    "client": request.remote,
                },
            )
        except Exception:
            logger.warning("OrchestrationAPI: could not journal an API request", exc_info=True)

    async def _json_body(self, request: web.Request) -> dict[str, Any]:
        """The request's JSON object body (``{}`` when the body is empty).

        :meth:`_guard` has already refused bodies that aren't JSON objects.
        """
        raw = await request.read()
        if not raw.strip():
            return {}
        body = json.loads(raw)
        return body if isinstance(body, dict) else {}

    def _require_auth(self, request: web.Request) -> web.Response | None:
        """Return error response if auth fails, None if OK."""
        if not self._check_auth(request):
            return _error_response("UNAUTHORIZED", "Invalid or missing auth token", 401)
        return None

    def _get_process(self, name: str) -> Any:
        """Get a process by name, raise KeyError if not found."""
        return self._runtime.get_process(name)

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the API server."""
        self._app = web.Application(middlewares=[self._guard])
        self._setup_routes()

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info(
            "OrchestrationAPI started on %s:%d (%s)",
            self._host,
            self.port,
            "authenticated" if self._auth_token else "no auth (localhost)",
        )

    async def stop(self) -> None:
        """Stop the API server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        logger.info("OrchestrationAPI stopped")

    def _setup_routes(self) -> None:
        """Register all API routes."""
        assert self._app is not None
        r = self._app.router

        # Health (no auth — for load balancers)
        r.add_get("/api/v1/health", self._handle_health)

        # Runtime management
        r.add_get("/api/v1/runtime/status", self._handle_runtime_status)
        r.add_post("/api/v1/runtime/start-all", self._handle_start_all)
        r.add_post("/api/v1/runtime/stop-all", self._handle_stop_all)
        if self._on_shutdown is not None:
            r.add_post("/api/v1/runtime/shutdown", self._handle_shutdown)

        # Process lifecycle
        r.add_get("/api/v1/processes", self._handle_list_processes)
        r.add_post("/api/v1/processes", self._handle_deploy)
        r.add_get("/api/v1/processes/{name}", self._handle_get_process)
        r.add_delete("/api/v1/processes/{name}", self._handle_remove_process)
        r.add_post("/api/v1/processes/{name}/start", self._handle_start)
        r.add_post("/api/v1/processes/{name}/stop", self._handle_stop)
        r.add_post("/api/v1/processes/{name}/restart", self._handle_restart)

        # Config updates
        r.add_patch("/api/v1/processes/{name}/instructions", self._handle_update_instructions)
        r.add_patch("/api/v1/processes/{name}/budget", self._handle_update_budget)
        r.add_patch("/api/v1/processes/{name}/health", self._handle_update_health)
        r.add_patch("/api/v1/processes/{name}/mission", self._handle_update_mission)

        # Human communication
        r.add_post("/api/v1/processes/{name}/messages", self._handle_send_message)
        r.add_post("/api/v1/processes/{name}/ask", self._handle_ask)
        r.add_get("/api/v1/processes/{name}/inbox", self._handle_get_inbox)
        r.add_delete("/api/v1/processes/{name}/inbox", self._handle_clear_inbox)

        # Trigger management
        r.add_get("/api/v1/processes/{name}/triggers", self._handle_list_triggers)
        r.add_post("/api/v1/processes/{name}/triggers", self._handle_add_trigger)
        r.add_delete("/api/v1/processes/{name}/triggers/{trigger_id}", self._handle_remove_trigger)

        # Secret management
        r.add_get("/api/v1/processes/{name}/secrets", self._handle_list_secrets)
        r.add_patch("/api/v1/processes/{name}/secrets/{secret_name}", self._handle_rotate_secret)
        r.add_delete("/api/v1/processes/{name}/secrets", self._handle_revoke_secrets)

        # Journal
        r.add_get("/api/v1/processes/{name}/journal", self._handle_get_journal)
        r.add_get("/api/v1/processes/{name}/rewind/plan", self._handle_rewind_plan)
        r.add_post("/api/v1/processes/{name}/rewind", self._handle_rewind)

        # Mission control
        r.add_get("/api/v1/processes/{name}/mission", self._handle_get_mission)
        r.add_post("/api/v1/processes/{name}/mission/fail", self._handle_fail_mission)
        r.add_post("/api/v1/processes/{name}/mission/pause", self._handle_pause_mission)
        r.add_post("/api/v1/processes/{name}/mission/resume", self._handle_resume_mission)

        # Health details
        r.add_get("/api/v1/processes/{name}/health/anomalies", self._handle_get_anomalies)
        r.add_delete("/api/v1/processes/{name}/health/anomalies", self._handle_clear_anomalies)

        # Process suspend/resume
        r.add_post("/api/v1/processes/{name}/suspend", self._handle_suspend)
        r.add_post("/api/v1/processes/{name}/resume", self._handle_resume)

        # Observability
        r.add_get("/api/v1/processes/{name}/metrics", self._handle_get_metrics)
        r.add_get("/api/v1/processes/{name}/context", self._handle_get_context)
        r.add_patch("/api/v1/processes/{name}/context", self._handle_update_context)

    # ------------------------------------------------------------------
    # Health (no auth)
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health probe for load balancers."""
        return _json_response(
            {
                "status": "healthy",
                "process_count": len(self._runtime.processes),
            }
        )

    # ------------------------------------------------------------------
    # Runtime management
    # ------------------------------------------------------------------

    async def _handle_runtime_status(self, request: web.Request) -> web.Response:
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        return _json_response(self._runtime.status())

    async def _handle_start_all(self, request: web.Request) -> web.Response:
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        await self._runtime.start_all()
        return _json_response({"status": "started"})

    async def _handle_shutdown(self, request: web.Request) -> web.Response:
        """``202``, then the ``on_shutdown`` callback (after the answer is sent)."""
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        callback = self._on_shutdown
        assert callback is not None  # the route exists only with a callback

        def fire() -> None:
            try:
                result = callback()
            except Exception:
                logger.exception("OrchestrationAPI: on_shutdown raised")
                return
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                self._shutdown_tasks.add(task)
                task.add_done_callback(self._shutdown_tasks.discard)

        asyncio.get_running_loop().call_soon(fire)
        return _json_response({"status": "shutting_down"}, status=202)

    async def _handle_stop_all(self, request: web.Request) -> web.Response:
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        await self._runtime.stop_all()
        return _json_response({"status": "stopped"})

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    async def _handle_list_processes(self, request: web.Request) -> web.Response:
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        processes = self._runtime.list_processes()
        return _json_response({"processes": processes, "total": len(processes)})

    async def _handle_get_process(self, request: web.Request) -> web.Response:
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            status = self._runtime.process_status(name)
            return _json_response(status)
        except KeyError:
            return _not_found(name)

    async def _handle_deploy(self, request: web.Request) -> web.Response:
        """Deploy a process: ``201``, ``409 PROCESS_EXISTS``, ``422`` for a
        bad name or config, ``403 COMMANDS_NOT_ALLOWED`` for a config with
        stdio servers, command hooks or open-mode tool creation (unless
        ``allow_command_deploys``), ``403 ENV_REFERENCES_NOT_ALLOWED`` for
        a ``${VAR}`` the API doesn't allow, ``403 PATH_NOT_ALLOWED`` for a
        path outside ``data_dir``, ``500 START_FAILED`` when ``start``
        fails (the process stays deployed).

        The process is created with the API's deploy policy
        (:class:`~promptise.runtime._deploy_policy.DeployPolicy`), which
        it passes on to the processes it spawns."""
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err

        body = await self._json_body(request)

        name = body.get("name")
        if not name:
            return _error_response("MISSING_FIELD", "'name' is required", 422)
        if not isinstance(name, str) or not _PROCESS_NAME_RE.match(name):
            return _error_response(
                "INVALID_NAME",
                "Process name must be alphanumeric with hyphens/underscores, max 64 chars",
                422,
            )
        start = body.get("start", False)
        if not isinstance(start, bool):
            return _error_response("INVALID_FIELD", "'start' must be true or false", 422)
        if name in self._runtime.processes:
            return _error_response("PROCESS_EXISTS", f"Process '{name}' already exists", 409)

        config_data = body.get("config", {})
        if not isinstance(config_data, dict):
            return _error_response("INVALID_CONFIG", "'config' must be a JSON object", 422)
        refused = self._refuse_env_references(
            {k: v for k, v in config_data.items() if k != "instructions"}, "config"
        )
        if refused is not None:
            return refused
        try:
            from .config import ProcessConfig

            config = ProcessConfig(**config_data)
        except (ValidationError, ValueError, TypeError) as exc:
            return _invalid_config("Config validation failed", exc)
        refused = self._refuse_commands(config) or self._refuse_paths(config)
        if refused is not None:
            return refused

        from .exceptions import RuntimeBaseError

        try:
            process = await self._runtime.add_process(
                name, config, deploy_policy=self._deploy_policy
            )
        except RuntimeBaseError as exc:
            if name in self._runtime.processes:  # lost a race with another deploy
                return _error_response("PROCESS_EXISTS", f"Process '{name}' already exists", 409)
            return _failure_response(
                "DEPLOY_FAILED", f"Could not create the process '{name}'", exc, 422
            )
        except (ValueError, TypeError, ImportError) as exc:
            return _failure_response(
                "DEPLOY_FAILED", f"Could not create the process '{name}'", exc, 422
            )
        request[_AUDIT_PROCESS] = name

        if start:
            try:
                await self._runtime.start_process(name)
            except Exception as exc:
                return _failure_response(
                    "START_FAILED",
                    f"Process '{name}' was deployed but failed to start"
                    + ("; a secret could not be resolved" if _secret_unresolved(exc) else ""),
                    exc,
                )

        return _json_response(
            {
                "name": name,
                "process_id": process.process_id,
                "state": _state_value(process),
                "created_at": time.time(),
            },
            status=201,
        )

    def _refuse_env_references(self, data: Any, where: str) -> web.Response | None:
        """``403 ENV_REFERENCES_NOT_ALLOWED`` when *data* has a ``${VAR}``
        reference the API doesn't allow (``None`` otherwise).

        The message names the references found in the request (its own
        text), never whether a variable exists.
        """
        if self._allow_env_references:
            return None
        refused = [
            (path, var)
            for path, var in env_references_in(data, where)
            if var not in self._allowed_env_vars
        ]
        if not refused:
            return None
        found = ", ".join(f"{path} (${{{var}}})" for path, var in refused[:10])
        logger.warning(
            "OrchestrationAPI: refused a request with environment references: %s",
            log_safe(found, 1000),
        )
        return _error_response(
            "ENV_REFERENCES_NOT_ALLOWED",
            "This request references environment variables of the runtime's host ("
            + found
            + "), which the control API doesn't allow by default: the agent could read "
            "them back. Send the values themselves (e.g. as secrets), or have the operator "
            "allow the variables with OrchestrationAPI(allowed_env_vars={...}) "
            "('promptise runtime start --allow-env-var NAME') or all of them with "
            "allow_env_references=True ('--allow-env-references').",
            403,
        )

    def _path_refusal(self, problems: list[str]) -> web.Response | None:
        if not problems:
            return None
        logger.warning("OrchestrationAPI: refused paths: %s", log_safe("; ".join(problems), 1000))
        return _error_response(
            "PATH_NOT_ALLOWED",
            "; ".join(problems)
            + f". Paths in API requests must lie in the data directory {str(self._data_dir)!r} "
            "(OrchestrationAPI(data_dir=...)), without '..'.",
            403,
        )

    def _trigger_path_problems(self, triggers: Any, where: str) -> list[str]:
        problems: list[str] = []
        for index, trigger in enumerate(triggers or []):
            watch_path = getattr(trigger, "watch_path", None)
            if getattr(trigger, "type", None) == "file_watch" and watch_path:
                problem = path_problem(watch_path, self._data_dir)
                if problem is not None:
                    label = f"{where}[{index}].watch_path" if where else "watch_path"
                    problems.append(f"{label}: {problem}")
        return problems

    def _refuse_paths(self, config: Any) -> web.Response | None:
        """``403 PATH_NOT_ALLOWED`` when *config* names a journal, memory or
        watch path outside ``data_dir`` (``None`` otherwise)."""
        problems: list[str] = []
        journal = config.journal
        if journal.level != "none" and journal.backend == "file":
            problem = path_problem(journal.path, self._data_dir)
            if problem is not None:
                problems.append(f"journal.path: {problem}")
        persist = config.context.memory_persist_directory
        if persist:
            problem = path_problem(persist, self._data_dir)
            if problem is not None:
                problems.append(f"context.memory_persist_directory: {problem}")
        problems += self._trigger_path_problems(config.triggers, "triggers")
        return self._path_refusal(problems)

    def _refuse_commands(self, config: Any) -> web.Response | None:
        """``403 COMMANDS_NOT_ALLOWED`` when *config* would run host programs
        and ``allow_command_deploys`` is off (``None`` otherwise)."""
        if self._allow_command_deploys:
            return None
        features = command_features(config)
        if not features:
            return None
        logger.warning(
            "OrchestrationAPI: refused a deploy that runs host commands: %s",
            log_safe(", ".join(features), 1000),
        )
        return _error_response(
            "COMMANDS_NOT_ALLOWED",
            "This config runs programs or code on the runtime's host ("
            + ", ".join(features)
            + "), which the control API doesn't allow by default: anyone with the API "
            "token could run any command. Use HTTP MCP servers instead of stdio ones, "
            "leave out the command hooks (a hook that runs code is registered on the host, "
            "in a manifest or in Python), set open_mode.allow_tool_creation=false, load "
            "the process from a manifest on the host, or, if every token holder may run "
            "commands there, start the API with OrchestrationAPI(allow_command_deploys=True) "
            "('promptise runtime start --allow-command-deploys').",
            403,
        )

    async def _handle_remove_process(self, request: web.Request) -> web.Response:
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            await self._runtime.remove_process(name)
            return web.Response(status=204)
        except KeyError:
            return _not_found(name)

    async def _lifecycle_action(self, request: web.Request, action: str, code: str) -> web.Response:
        """Run ``runtime.<action>_process``; ``404`` only for an unknown name.

        Looked up first, so a ``KeyError`` from the process itself (a
        secret that can't be resolved is one) is ``500 <code>``, not
        ``PROCESS_NOT_FOUND``.
        """
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            self._get_process(name)
        except KeyError:
            return _not_found(name)
        try:
            await getattr(self._runtime, f"{action}_process")(name)
            process = self._get_process(name)
        except Exception as exc:
            reason = "; a secret could not be resolved" if _secret_unresolved(exc) else ""
            return _failure_response(code, f"Process '{name}' failed to {action}{reason}", exc)
        return _json_response({"name": name, "state": _state_value(process)})

    async def _handle_start(self, request: web.Request) -> web.Response:
        return await self._lifecycle_action(request, "start", "START_FAILED")

    async def _handle_stop(self, request: web.Request) -> web.Response:
        return await self._lifecycle_action(request, "stop", "STOP_FAILED")

    async def _handle_restart(self, request: web.Request) -> web.Response:
        return await self._lifecycle_action(request, "restart", "RESTART_FAILED")

    # ------------------------------------------------------------------
    # Config updates
    # ------------------------------------------------------------------

    async def _handle_update_instructions(self, request: web.Request) -> web.Response:
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        body = await self._json_body(request)

        try:
            instructions = body.get("instructions")
            if not instructions:
                return _error_response("MISSING_FIELD", "'instructions' is required", 422)

            if not isinstance(instructions, str):
                return _error_response("INVALID_CONFIG", "'instructions' must be a string", 422)

            process = self._get_process(name)
            # The next invocation rebuilds the agent with them first.
            process.update_instructions(instructions)

            return _json_response(
                {
                    "name": name,
                    "instructions_updated": True,
                    "effective_from": "next_invocation",
                }
            )
        except KeyError:
            return _not_found(name)

    async def _handle_update_budget(self, request: web.Request) -> web.Response:
        """Patch ``BudgetConfig``.

        Limits are read from the config at every budget check, so a new
        limit applies at the next check (the next tool call, LLM turn or
        run start; ``effective_from: "next_budget_check"``), and usage
        counted so far is kept.  See :meth:`_patch_section` for ``live``.
        """
        return await self._patch_section(request, "budget")

    async def _handle_update_health(self, request: web.Request) -> web.Response:
        """Patch ``HealthConfig`` (thresholds apply to the next check)."""
        return await self._patch_section(request, "health")

    async def _handle_update_mission(self, request: web.Request) -> web.Response:
        """Patch ``MissionConfig`` (applies to the next evaluation and prompt)."""
        return await self._patch_section(request, "mission")

    async def _patch_section(self, request: web.Request, section: str) -> web.Response:
        """Validate and apply a ``budget``/``health``/``mission`` patch.

        The response says when the change applies:

        * ``live: true`` with ``effective_from`` ``"next_budget_check"``
          (budget) or ``"immediate"``: the running subsystem reads the new
          values.
        * ``live: false`` with ``effective_from: "redeploy"``: the process
          has to apply the change itself and can't yet.  That is the case
          for ``enabled`` when it differs from whether the process was
          built with the subsystem (it is built once, at construction), and
          for any change to a subsystem the process doesn't have.
          ``pending_fields`` lists the fields and ``note`` says what to do.

        The values are always stored in the process's config.  The running
        subsystems read that same object, so on a built subsystem every
        field, including the health window sizes, applies at its next
        check.

        When the process implements ``apply_governance_config(section)``,
        it is called after the config changed and every change is live.
        """
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        body = await self._json_body(request)
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        if not body:
            return _error_response(
                "MISSING_FIELD", f"Send the {section} fields to change as a JSON object", 422
            )
        refused = self._refuse_env_references(body, section)
        if refused is not None:
            return refused

        built = getattr(process, _SUBSYSTEMS[section], None) is not None
        config_section = getattr(process.config, section)
        previous = {
            key: getattr(config_section, key) for key in body if hasattr(config_section, key)
        }
        error = _apply_patch(config_section, body)
        if error is not None:
            return error

        live_effect = "next_budget_check" if section == "budget" else "immediate"
        result: dict[str, Any] = {"name": name, f"{section}_updated": True}
        apply = getattr(process, "apply_governance_config", None)
        if callable(apply):
            try:
                await _maybe_await(apply(section))
            except (ValueError, TypeError) as exc:
                # The process can't use the new values: put the old ones
                # back (and re-apply them), so config and process agree.
                for key, value in previous.items():
                    setattr(config_section, key, value)
                try:
                    await _maybe_await(apply(section))
                except Exception:
                    logger.warning(
                        "OrchestrationAPI: could not re-apply the previous %s config of %s",
                        log_safe(section),
                        log_safe(name),
                        exc_info=True,
                    )
                return _failure_response(
                    "INVALID_CONFIG",
                    f"The process can't apply this {section} config (nothing changed)",
                    exc,
                    422,
                )
            result.update(effective_from=live_effect, live=True)
            return _json_response(result)

        enabled = getattr(process.config, section).enabled
        if enabled != built:
            note = (
                f"The process was built {'without' if not built else 'with'} {section} "
                f"and can't {'enable' if enabled else 'disable'} it while it exists. "
                f"The config now has enabled={str(enabled).lower()}; redeploy the process "
                "(DELETE it, then POST /api/v1/processes) to apply it."
            )
        elif not built:
            note = (
                f"{section} is disabled for this process: the values are stored in its "
                "config and used once it is redeployed with enabled=true."
            )
        else:
            result.update(effective_from=live_effect, live=True)
            return _json_response(result)

        result.update(effective_from="redeploy", live=False, pending_fields=sorted(body), note=note)
        return _json_response(result)

    # ------------------------------------------------------------------
    # Human communication
    # ------------------------------------------------------------------

    def _message_fields(
        self, body: dict[str, Any], *, default_type: str
    ) -> tuple[dict[str, Any], web.Response | None]:
        """Validate the fields shared by ``/messages`` and ``/ask``."""
        from .inbox import MessageType

        content = body.get("content")
        if not content:
            return {}, _error_response("MISSING_FIELD", "'content' is required", 422)
        if not isinstance(content, str):
            return {}, _error_response("INVALID_FIELD", "'content' must be a string", 422)
        msg_type = body.get("message_type", default_type)
        try:
            message_type = MessageType(msg_type)
        except ValueError:
            return {}, _error_response(
                "INVALID_TYPE",
                f"Invalid message_type: {msg_type!r}. Must be: directive, context, question, correction",
                422,
            )
        priority = body.get("priority", "normal")
        if priority not in _PRIORITIES:
            return {}, _error_response(
                "INVALID_FIELD", f"'priority' must be one of: {', '.join(_PRIORITIES)}", 422
            )
        sender_id = body.get("sender_id")
        if sender_id is not None and not isinstance(sender_id, str):
            return {}, _error_response("INVALID_FIELD", "'sender_id' must be a string", 422)
        metadata = body.get("metadata", {})
        if not isinstance(metadata, dict):
            return {}, _error_response("INVALID_FIELD", "'metadata' must be a JSON object", 422)
        return {
            "content": content,
            "message_type": message_type,
            "priority": priority,
            "sender_id": sender_id,
            "metadata": metadata,
        }, None

    def _inbox_or_error(self, process: Any, action: str) -> tuple[Any, web.Response | None]:
        """The process's inbox, or the error for a disabled inbox / closed process."""
        inbox = getattr(process, "_inbox", None)
        if inbox is None:
            return None, _error_response(
                "INBOX_DISABLED",
                "Message inbox is not enabled for this process. Set inbox.enabled=true in config.",
                400,
            )
        state = _state_value(process)
        if state in _CLOSED_STATES:
            return None, _error_response(
                "PROCESS_NOT_RUNNING",
                f"Cannot {action} process '{process.name}' in {state} state; start it first",
                409,
            )
        return inbox, None

    async def _handle_send_message(self, request: web.Request) -> web.Response:
        """Queue a message: ``201``, ``409`` for a stopped/failed process."""
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        body = await self._json_body(request)
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        inbox, error = self._inbox_or_error(process, "send messages to")
        if error is not None:
            return error
        fields, error = self._message_fields(body, default_type="context")
        if error is not None:
            return error

        ttl = body.get("ttl")
        if ttl is not None and (not _is_number(ttl) or ttl < 0):
            return _error_response("INVALID_FIELD", "'ttl' must be a number of seconds >= 0", 422)
        expires_at = time.time() + ttl if ttl else None

        from .inbox import InboxMessage

        message = InboxMessage(expires_at=expires_at, **fields)
        msg_id, error = await _inbox_add(inbox, message)
        if error is not None:
            return error

        return _json_response(
            {
                "message_id": msg_id,
                "status": "queued",
                "expires_at": message.expires_at,
            },
            status=201,
        )

    async def _handle_ask(self, request: web.Request) -> web.Response:
        """Ask a question and long-poll for the answer.

        ``200`` with ``status: "answered"`` and the ``response`` text
        (secret values ``[REDACTED]``), or ``status: "timeout"`` and
        ``response: null`` when the agent did not answer within
        ``timeout`` seconds (default 120, at most 3600).
        """
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        body = await self._json_body(request)
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        inbox, error = self._inbox_or_error(process, "ask questions to")
        if error is not None:
            return error
        body = {**body, "message_type": "question"}
        fields, error = self._message_fields(body, default_type="question")
        if error is not None:
            return error
        timeout = body.get("timeout", 120)
        if not _is_number(timeout) or not 0 < timeout <= _MAX_ASK_TIMEOUT:
            return _error_response(
                "INVALID_FIELD",
                f"'timeout' must be a number of seconds in (0, {_MAX_ASK_TIMEOUT:g}]",
                422,
            )

        from .inbox import InboxMessage

        message = InboxMessage(
            expires_at=time.time() + timeout + 60,  # Extra buffer beyond poll timeout
            **fields,
        )
        msg_id, error = await _inbox_add(inbox, message)
        if error is not None:
            return error

        # Long-poll: wait for the agent to respond
        from .inbox import InboxFullError, InboxMessageRejected, InboxQuestionRemoved

        try:
            response = await inbox.wait_for_response(msg_id, timeout=timeout)
        except asyncio.TimeoutError:
            return _json_response({"question_id": msg_id, "status": "timeout", "response": None})
        except InboxMessageRejected as exc:  # removed by a later scan
            return _failure_response("MESSAGE_REJECTED", _MESSAGE_REJECTED_TEXT, exc, 422)
        except InboxQuestionRemoved:  # DELETE /inbox while waiting
            return _error_response(
                "QUESTION_REMOVED",
                f"The question {msg_id} was removed from the inbox (the inbox was cleared) "
                "before it was answered",
                410,
            )
        except InboxFullError:  # evicted to make room for another message
            return _error_response(
                "QUESTION_REMOVED",
                f"The question {msg_id} was evicted from the full inbox before it was "
                "answered (raise inbox.max_messages, or send it with a higher priority)",
                410,
            )
        redact = getattr(process, "_redact", None)
        answer = redact(response.content) if callable(redact) else response.content
        return _json_response(
            {
                "question_id": msg_id,
                "status": "answered",
                "response": answer,
                "answered_at": response.answered_at,
                "invocation_id": response.invocation_id,
            }
        )

    async def _handle_get_inbox(self, request: web.Request) -> web.Response:
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
            if getattr(process, "_inbox", None) is None:
                return _json_response(
                    {
                        "enabled": False,
                        "pending_messages": 0,
                        "pending_questions": 0,
                        "messages": [],
                    }
                )
            status = await process._inbox.status()
            redact = getattr(process, "_redact", None)
            if callable(redact):
                status = redact(status)
            return _json_response({"enabled": True, **status})
        except KeyError:
            return _not_found(name)

    async def _handle_clear_inbox(self, request: web.Request) -> web.Response:
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
            if getattr(process, "_inbox", None) is not None:
                await process._inbox.clear()
            return web.Response(status=204)
        except KeyError:
            return _not_found(name)

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    async def _handle_get_metrics(self, request: web.Request) -> web.Response:
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            status = self._runtime.process_status(name)
            return _json_response(status)
        except KeyError:
            return _not_found(name)

    async def _handle_get_context(self, request: web.Request) -> web.Response:
        """State and writable keys (``writable_keys: []`` with
        ``all_keys_writable: true`` when every key is writable).

        Secret values in the state are shown as ``[REDACTED]``, as in the
        journal and the state recovered from it."""
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        ctx = process.context
        writable = ctx.writable_keys
        state = ctx.state_snapshot()
        redact = getattr(process, "_redact", None)
        if callable(redact):
            state = redact(state)
        return _json_response(
            {
                "state": state,
                "writable_keys": writable or [],
                "all_keys_writable": writable is None,
            }
        )

    async def _handle_update_context(self, request: web.Request) -> web.Response:
        """Write context state, all keys or none.

        Body: ``{"state": {key: value, ...}}``.  Every key is checked first;
        if any is not writable the answer is ``403 WRITE_DENIED`` naming
        them, and nothing is written.  Writes are recorded with source
        ``"api"``.
        """
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        body = await self._json_body(request)
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        unknown = sorted(set(body) - {"state"})
        if unknown:
            return _error_response(
                "UNKNOWN_FIELD", f"Unknown field(s): {', '.join(unknown)}. Allowed: state", 422
            )
        state_updates = body.get("state")
        if state_updates is None:
            return _error_response("MISSING_FIELD", "'state' is required", 422)
        if not isinstance(state_updates, dict):
            return _error_response("INVALID_FIELD", "'state' must be a JSON object", 422)

        ctx = process.context
        denied = ctx.denied_keys(state_updates)
        if denied:
            return _error_response(
                "WRITE_DENIED",
                f"Not writable: {', '.join(denied)}. Allowed: {', '.join(ctx.writable_keys or [])}."
                " Nothing was written.",
                403,
            )
        updated_keys = ctx.update(state_updates, source="api")
        return _json_response({"name": name, "updated_keys": updated_keys})

    # ------------------------------------------------------------------
    # Trigger management
    # ------------------------------------------------------------------

    def _find_config_trigger(self, process: Any, trigger_id: str) -> tuple[int | None, bool]:
        """Index of the config trigger with *trigger_id* (``(None, False)`` if none).

        The second value is ``True`` when *trigger_id* used the deprecated,
        position-based ``static_<n>`` form.
        """
        triggers = process.config.triggers
        ids = config_trigger_ids(triggers)
        if trigger_id in ids:
            return ids.index(trigger_id), False
        match = re.fullmatch(r"static_(\d+)", trigger_id)
        if match and int(match.group(1)) < len(triggers):
            return int(match.group(1)), True
        return None, False

    async def _handle_list_triggers(self, request: web.Request) -> web.Response:
        """List all triggers (config + agent-added) for a process.

        Config triggers have stable IDs (see :func:`config_trigger_ids`);
        ``hmac_secret`` is masked, and so are the values of
        ``custom_config`` keys that look like credentials (``token``,
        ``secret``, ``password``, ``api_key``, ``auth``...).
        """
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        triggers: list[dict[str, Any]] = []
        for tid, tc in zip(
            config_trigger_ids(process.config.triggers), process.config.triggers, strict=True
        ):
            try:
                config = tc.model_dump(mode="json", exclude={"filter_expression"})
            except Exception:
                config = {"type": tc.type}
            if config.get("custom_config"):
                config["custom_config"] = _mask_custom_config(config["custom_config"])
            triggers.append({"id": tid, "type": tc.type, "source": "config", "config": config})
        for t in getattr(process, "_dynamic_triggers", []):
            tid = getattr(t, "trigger_id", None) or str(id(t))
            kind = getattr(t, "trigger_type", None) or type(t).__name__
            triggers.append({"id": tid, "type": kind, "source": "dynamic"})
        return _json_response({"name": name, "triggers": triggers, "count": len(triggers)})

    async def _handle_add_trigger(self, request: web.Request) -> web.Response:
        """Add a trigger to a process's config.

        Answers ``201`` with the new ``trigger_id``, ``live`` and
        ``effective_from``: ``"immediate"`` when the process starts the
        trigger now (it implements ``add_trigger``), ``"restart"`` when a
        running process only creates it on its next start, ``"next_start"``
        for a process that isn't running.
        """
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        body = await self._json_body(request)
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        if not body.get("type"):
            return _error_response("MISSING_FIELD", "'type' is required", 422)
        refused = self._refuse_env_references(body, "trigger")
        if refused is not None:
            return refused
        from .config import TriggerConfig

        try:
            tc = TriggerConfig(**body)
        except (ValidationError, ValueError, TypeError) as exc:
            return _invalid_config("Invalid trigger config", exc)
        refused = self._path_refusal(self._trigger_path_problems([tc], ""))
        if refused is not None:
            return refused
        triggers = process.config.triggers
        trigger_id = config_trigger_ids([tc])[0]
        if trigger_id in config_trigger_ids(triggers):
            return _error_response(
                "TRIGGER_EXISTS",
                f"An identical trigger is already configured (id '{trigger_id}')",
                409,
            )

        state = _state_value(process)
        add = getattr(process, "add_trigger", None)
        if callable(add):
            try:
                returned = await _maybe_await(add(tc))
            except ValueError as exc:
                return _failure_response(
                    "INVALID_CONFIG", "The process refused the trigger config", exc, 422
                )
            if isinstance(returned, str):
                trigger_id = returned
            # add_trigger waits for a start in progress: judge by the state now.
            live = _state_value(process) in _ACTIVE_STATES
            effective = "immediate" if live else "next_start"
        else:
            triggers.append(tc)
            live = False
            effective = "restart" if state in _ACTIVE_STATES else "next_start"
        return _json_response(
            {
                "name": name,
                "trigger_added": True,
                "trigger_id": trigger_id,
                "trigger_type": tc.type,
                "live": live,
                "effective_from": effective,
            },
            status=201,
        )

    async def _handle_remove_trigger(self, request: web.Request) -> web.Response:
        """Remove a trigger by ID.

        An agent-added (dynamic) trigger is stopped at once.  A config
        trigger is removed from the config; ``live`` says whether the
        running trigger stopped too (the process implements
        ``remove_trigger``) or keeps firing until the process restarts
        (``effective_from: "restart"``).
        """
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        trigger_id = request.match_info["trigger_id"]
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        state = _state_value(process)
        remove = getattr(process, "remove_trigger", None)

        dynamic = getattr(process, "_dynamic_triggers", [])
        for i, t in enumerate(dynamic):
            tid = getattr(t, "trigger_id", None) or str(id(t))
            if tid == trigger_id:
                if callable(remove):
                    await _maybe_await(remove(trigger_id))
                else:
                    removed = dynamic.pop(i)
                    if hasattr(removed, "stop"):
                        await removed.stop()
                return _json_response(
                    {
                        "name": name,
                        "trigger_removed": trigger_id,
                        "live": True,
                        "effective_from": "immediate",
                    }
                )

        index, legacy = self._find_config_trigger(process, trigger_id)
        if index is None:
            return _error_response("TRIGGER_NOT_FOUND", f"Trigger '{trigger_id}' not found", 404)
        stable_id = config_trigger_ids(process.config.triggers)[index]
        result: dict[str, Any] = {"name": name, "trigger_removed": stable_id}
        if legacy:
            message = (
                f"Trigger ID '{trigger_id}' is position-based and deprecated; use the stable "
                f"ID '{stable_id}' from GET /triggers"
            )
            logger.warning("OrchestrationAPI: %s", log_safe(message, 500))
            result["warning"] = message
        if callable(remove):
            await _maybe_await(remove(stable_id))
            # Live when a running trigger was stopped; a process that isn't
            # running just doesn't create it on its next start.
            live = _state_value(process) in _ACTIVE_STATES
            effective = "immediate" if live else "next_start"
        else:
            process.config.triggers.pop(index)
            live = state not in _ACTIVE_STATES
            effective = "restart" if state in _ACTIVE_STATES else "next_start"
            if state in _ACTIVE_STATES:
                result["note"] = (
                    "Removed from the config; the running trigger keeps firing until the "
                    "process restarts."
                )
        result.update(live=live, effective_from=effective)
        return _json_response(result)

    # ------------------------------------------------------------------
    # Secret management
    # ------------------------------------------------------------------

    @staticmethod
    def _secret_metadata(secrets: Any) -> list[dict[str, Any]]:
        """Metadata of every secret (never a value), with ``expires_at``.

        Uses ``SecretScope.list_metadata()``; ``expires_at`` (Unix time) is
        derived from its ``expires_in`` (seconds left, ``None`` for no
        expiry).
        """
        describe = getattr(secrets, "list_metadata", None)
        if callable(describe):
            items = [dict(item) for item in describe()]
        else:  # a scope without list_metadata(): names and expiry only
            entries = getattr(secrets, "_secrets", {})
            items = []
            for sn in secrets.list_names():
                expires = getattr(entries.get(sn), "expires_at", None)  # monotonic
                items.append(
                    {
                        "name": sn,
                        "active": True,
                        "source": getattr(entries.get(sn), "source", None),
                        "expires_in": None
                        if expires is None
                        else round(max(0.0, expires - time.monotonic()), 3),
                    }
                )
        for item in items:
            left = item.get("expires_in")
            item["expires_at"] = time.time() + left if _is_number(left) else None
        return items

    async def _handle_list_secrets(self, request: web.Request) -> web.Response:
        """List secret metadata (never values).

        Per secret: ``name``, ``active``, ``source``, ``expires_in``
        (seconds left) and ``expires_at`` (Unix time), both ``null`` for a
        secret without a TTL, plus the scope's rotation and access counts.
        """
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        secrets = getattr(process, "_secrets", None)
        if secrets is None:
            return _json_response({"name": name, "secrets": [], "enabled": False})
        info = self._secret_metadata(secrets)
        return _json_response({"name": name, "secrets": info, "enabled": True})

    async def _handle_rotate_secret(self, request: web.Request) -> web.Response:
        """Rotate a secret value (immediate effect).

        Body: ``{"value": "...", "ttl": <seconds, optional>}``.  ``ttl``
        sets a new lifetime counted from now; without it the secret keeps
        its expiry (an expired secret gets its configured TTL again).
        ``404 SECRET_NOT_FOUND`` for a name the process has no secret for.
        The answer carries the secret's new metadata, never its value.
        """
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        secret_name = request.match_info["secret_name"]
        body = await self._json_body(request)
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        secrets = getattr(process, "_secrets", None)
        if secrets is None:
            return _error_response("SECRETS_DISABLED", "Secret scoping not enabled", 400)
        new_value = body.get("value")
        if not new_value:
            return _error_response("MISSING_FIELD", "'value' is required", 422)
        if not isinstance(new_value, str):
            return _error_response("INVALID_FIELD", "'value' must be a string", 422)
        kwargs: dict[str, Any] = {}
        if "ttl" in body:
            ttl = body["ttl"]
            if not _is_number(ttl) or ttl <= 0:
                return _error_response(
                    "INVALID_FIELD", "'ttl' must be a number of seconds > 0", 422
                )
            kwargs["ttl"] = ttl
        unknown = sorted(set(body) - {"value", "ttl"})
        if unknown:
            return _error_response(
                "UNKNOWN_FIELD", f"Unknown field(s): {', '.join(unknown)}. Allowed: value, ttl", 422
            )
        try:
            await secrets.rotate(secret_name, new_value, **kwargs)
        except KeyError:
            return _error_response(
                "SECRET_NOT_FOUND",
                f"Process '{name}' has no secret named '{secret_name}'",
                404,
            )
        except Exception as exc:
            # Never log or echo the exception: it could quote the value.
            # The log line carries an error_id (the client gets it too)
            # and the exception type, not the secret's name or value; the
            # audit log records the route with the name.
            error_id = uuid.uuid4().hex[:12]
            logger.warning(
                "OrchestrationAPI: rotating a secret of process %s failed: %s (error_id=%s)",
                log_safe(name),
                type(exc).__name__,
                error_id,
            )
            return _json_response(
                {
                    "error": {
                        "code": "ROTATION_FAILED",
                        "message": f"Rotating secret '{secret_name}' failed",
                        "error_id": error_id,
                    }
                },
                status=400,
            )
        info = next(
            (m for m in self._secret_metadata(secrets) if m.get("name") == secret_name), None
        )
        return _json_response({"name": name, "secret_rotated": secret_name, "secret": info})

    async def _handle_revoke_secrets(self, request: web.Request) -> web.Response:
        """Revoke all secrets (zero-fill, immediate)."""
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
            secrets = getattr(process, "_secrets", None)
            if secrets is None:
                return _error_response("SECRETS_DISABLED", "Secret scoping not enabled", 400)
            await secrets.revoke_all()
            return _json_response({"name": name, "secrets_revoked": True})
        except KeyError:
            return _not_found(name)

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------

    async def _handle_get_journal(self, request: web.Request) -> web.Response:
        """Read the newest journal entries (``?limit=`` 1-500, default 50;
        ``?type=`` filters by entry type).  Each entry has its ``entry_id``
        (a rewind target)."""
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        journal = getattr(process, "_journal", None)
        if journal is None:
            return _json_response({"name": name, "entries": [], "journal_enabled": False})
        raw_limit = request.query.get("limit", "50")
        try:
            limit = int(raw_limit)
        except ValueError:
            limit = 0
        if not 1 <= limit <= 500:
            return _error_response("INVALID_FIELD", "'limit' must be an integer from 1 to 500", 422)
        entry_type = request.query.get("type") or None
        # The process files its entries under its name (so they
        # survive a restart, which gets a new process_id).
        entries = await journal.read(process.name, entry_type=entry_type)
        if entry_type:
            entries = [e for e in entries if getattr(e, "entry_type", "") == entry_type]
        entries = entries[-limit:]
        serialized = [
            {
                "entry_id": getattr(e, "entry_id", None),
                "entry_type": getattr(e, "entry_type", "unknown"),
                "timestamp": _iso(getattr(e, "timestamp", 0)),
                "data": getattr(e, "data", {}),
            }
            for e in entries
        ]
        return _json_response({"name": name, "entries": serialized, "count": len(serialized)})

    # ------------------------------------------------------------------
    # Rewind
    # ------------------------------------------------------------------

    def _rewind_args(
        self, process: Any, target: Any, mode: Any
    ) -> tuple[str, str, web.Response | None]:
        from .journal import RewindMode

        if getattr(process, "_journal", None) is None or not hasattr(process, "rewind"):
            return (
                "",
                "",
                _error_response(
                    "JOURNAL_DISABLED",
                    "The process has no journal; rewind needs one (set its journal config)",
                    400,
                ),
            )
        if not target:
            return (
                "",
                "",
                _error_response(
                    "MISSING_FIELD",
                    "'target_entry_id' is required (an entry_id from /journal)",
                    422,
                ),
            )
        if not isinstance(target, str):
            return (
                "",
                "",
                _error_response("INVALID_FIELD", "'target_entry_id' must be a string", 422),
            )
        modes = [m.value for m in RewindMode]
        if not isinstance(mode, str) or mode not in modes:
            return (
                "",
                "",
                _error_response("INVALID_FIELD", f"'mode' must be one of {', '.join(modes)}", 422),
            )
        return target, mode, None

    async def _handle_rewind_plan(self, request: web.Request) -> web.Response:
        """Preview a rewind: ``?target_entry_id=`` (required), ``?mode=`` (default
        ``both``).  Changes nothing."""
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        target, mode, err = self._rewind_args(
            process,
            request.query.get("target_entry_id") or request.query.get("target"),
            request.query.get("mode", "both"),
        )
        if err is not None:
            return err
        try:
            plan = await process.rewind_plan(target, mode)
        except ValueError as exc:
            return _entry_not_found(target, exc)
        return _json_response({"name": name, "plan": plan.to_dict()})

    async def _handle_rewind(self, request: web.Request) -> web.Response:
        """Rewind a process to just before a journal entry.

        Body: ``target_entry_id`` (required), ``mode`` (``both`` — the
        default —, ``conversation_only``, ``code_only``, ``summarize`` or
        ``cancel``) and ``actor`` (recorded in the journal; default
        ``"api"``).  A running process is suspended for the rewind and
        resumed afterwards.  Non-destructive: the journal is appended to.
        """
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        body = await self._json_body(request)
        target, mode, err = self._rewind_args(
            process, body.get("target_entry_id"), body.get("mode", "both")
        )
        if err is not None:
            return err
        actor = body.get("actor", "api")
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
            return _error_response(
                "INVALID_FIELD", "'actor' must be a non-empty string (max 200 chars)", 422
            )
        try:
            result = await process.rewind(target, mode, actor=actor)
        except ValueError as exc:
            return _entry_not_found(target, exc)
        except TimeoutError as exc:
            return _failure_response(
                "REWIND_BUSY",
                f"Process '{name}' still had a run in flight; nothing was rewound (retry when "
                "the run has finished)",
                exc,
                409,
            )
        plan = result.plan
        return _json_response(
            {
                "name": name,
                "mode": mode,
                "target_entry_id": target,
                "rewind_entry_id": result.rewind_entry_id,
                "entries_affected": plan.entries_affected if plan is not None else 0,
                "context_keys": sorted(process.context.state_keys()),
                "conversation_messages": process.status().get("conversation_messages", 0),
                "state": _state_value(process),
                "plan": plan.to_dict() if plan is not None else None,
            }
        )

    # ------------------------------------------------------------------
    # Mission control
    # ------------------------------------------------------------------

    async def _handle_get_mission(self, request: web.Request) -> web.Response:
        """Get mission state and evaluation history."""
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        mission = getattr(process, "_mission", None)
        if mission is None:
            return _json_response({"name": name, "mission_enabled": False})
        state = {
            "state": _state_value(mission),
            "objective": getattr(getattr(mission, "_config", None), "objective", ""),
            "invocation_count": getattr(mission, "invocation_count", 0),
            "evaluations": [
                {
                    "achieved": getattr(ev, "achieved", False),
                    "confidence": getattr(ev, "confidence", 0),
                    "reasoning": getattr(ev, "reasoning", ""),
                    "progress_summary": getattr(ev, "progress_summary", ""),
                    "invocation_number": getattr(ev, "invocation_number", None),
                    "source": getattr(ev, "source", None),
                    "timestamp": _iso(getattr(ev, "timestamp", None)),
                }
                for ev in getattr(mission, "evaluations", [])
            ],
        }
        return _json_response({"name": name, "mission_enabled": True, "mission": state})

    async def _mission_action(
        self, request: web.Request, action: str, allowed: frozenset[str]
    ) -> web.Response:
        """Run ``fail``/``pause``/``resume`` on a mission in one of *allowed* states."""
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        body = await self._json_body(request)
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        mission = getattr(process, "_mission", None)
        if mission is None:
            return _error_response("MISSION_DISABLED", "Mission not configured", 400)
        current = _state_value(mission)
        if current not in allowed:
            return _error_response(
                "MISSION_STATE",
                f"Cannot {action} a mission in state '{current}' "
                f"(allowed in: {', '.join(sorted(allowed))})",
                409,
            )
        if action == "fail":
            reason = body.get("reason", "Manually failed via API")
            if not isinstance(reason, str):
                return _error_response("INVALID_FIELD", "'reason' must be a string", 422)
            fail_mission = getattr(process, "fail_mission", None)
            if callable(fail_mission):
                await _maybe_await(fail_mission(reason))
            else:
                self._fail_mission_and_notify(process, mission, reason)
            return _json_response({"name": name, "mission_failed": True, "reason": reason})
        getattr(mission, action)()
        return _json_response(
            {"name": name, f"mission_{action}d": True, "state": _state_value(mission)}
        )

    @staticmethod
    def _fail_mission_and_notify(process: Any, mission: Any, reason: str) -> None:
        """Fail *mission* and emit ``mission.failed`` as the runtime does."""
        try:
            payload = mission.fail(reason, source="manual")
        except TypeError:  # a tracker whose fail() takes only the reason
            payload = mission.fail(reason)
        notifier = getattr(process, "_event_notifier", None)
        if notifier is None:
            return
        from promptise.events import emit_event

        data: dict[str, Any] = {"process_name": process.name, "reason": "manual"}
        if isinstance(payload, dict):
            data["message"] = payload.get("message", reason)
        else:
            data["message"] = reason
        metadata = getattr(process, "_event_metadata", None)
        emit_event(
            notifier,
            "mission.failed",
            "critical",
            data,
            agent_id=process.name,
            metadata=metadata() if callable(metadata) else None,
        )

    async def _handle_fail_mission(self, request: web.Request) -> web.Response:
        """Fail an active or paused mission (``409 MISSION_STATE`` otherwise).

        The reason is stored with the mission and a ``mission.failed`` event
        (``reason: "manual"``, ``message``: the given reason) is emitted
        when the process has an event notifier.
        """
        return await self._mission_action(request, "fail", frozenset({"active", "paused"}))

    async def _handle_pause_mission(self, request: web.Request) -> web.Response:
        """Pause an active mission (``409 MISSION_STATE`` otherwise)."""
        return await self._mission_action(request, "pause", frozenset({"active"}))

    async def _handle_resume_mission(self, request: web.Request) -> web.Response:
        """Resume a paused mission (``409 MISSION_STATE`` otherwise)."""
        return await self._mission_action(request, "resume", frozenset({"paused"}))

    # ------------------------------------------------------------------
    # Health details
    # ------------------------------------------------------------------

    async def _handle_get_anomalies(self, request: web.Request) -> web.Response:
        """Get anomaly history for a process (type, description, time)."""
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        health = getattr(process, "_health", None)
        if health is None:
            return _json_response({"name": name, "health_enabled": False, "anomalies": []})
        anomalies = [
            {
                "type": getattr(a, "anomaly_type", "unknown"),
                "description": getattr(a, "description", ""),
                "timestamp": _iso(getattr(a, "timestamp", 0)),
            }
            for a in getattr(health, "anomalies", [])
        ]
        return _json_response(
            {"name": name, "health_enabled": True, "anomalies": anomalies, "count": len(anomalies)}
        )

    async def _handle_clear_anomalies(self, request: web.Request) -> web.Response:
        """Clear anomaly history and cooldowns.

        After clearing, a recurring anomaly is reported (and acted on) at
        once instead of being held back by the previous one's cooldown.
        """
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        health = getattr(process, "_health", None)
        if health is None:
            return _error_response("HEALTH_DISABLED", "Health monitoring not configured", 400)
        cleared = len(getattr(health, "anomalies", []))
        clear = getattr(health, "clear_anomalies", None)
        if callable(clear):
            clear()
        else:
            # anomalies is a property that returns a copy: clear the internals
            for attr in ("_anomalies", "_new_anomalies", "_last_anomaly_time"):
                store = getattr(health, attr, None)
                if store is not None:
                    store.clear()
            if hasattr(health, "_unrecovered"):
                health._unrecovered = False
        return _json_response({"name": name, "anomalies_cleared": cleared})

    # ------------------------------------------------------------------
    # Process suspend/resume
    # ------------------------------------------------------------------

    async def _handle_suspend(self, request: web.Request) -> web.Response:
        """Suspend a process (pause trigger processing)."""
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        try:
            await process.suspend()
        except Exception as exc:
            return _failure_response(
                "SUSPEND_FAILED",
                f"Process '{name}' can't be suspended in state '{_state_value(process)}'",
                exc,
                400,
            )
        return _json_response({"name": name, "suspended": True, "state": _state_value(process)})

    async def _handle_resume(self, request: web.Request) -> web.Response:
        """Resume a suspended process."""
        auth_err = self._require_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        try:
            process = self._get_process(name)
        except KeyError:
            return _not_found(name)
        try:
            await process.resume()
        except Exception as exc:
            return _failure_response(
                "RESUME_FAILED",
                f"Process '{name}' can't be resumed in state '{_state_value(process)}'",
                exc,
                400,
            )
        return _json_response({"name": name, "resumed": True, "state": _state_value(process)})
