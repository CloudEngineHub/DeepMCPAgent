"""Configuration schemas for the agent runtime.

All configuration is expressed as Pydantic ``BaseModel`` subclasses with
sensible defaults.  Atomic configs (:class:`TriggerConfig`,
:class:`JournalConfig`, etc.) are composed into :class:`ProcessConfig`
(single process) and :class:`RuntimeConfig` (multi-process manager).

Two preset configurations are provided for common environments:

* :data:`DEFAULT_DEVELOPMENT_CONFIG` — local-only defaults.
* :data:`DEFAULT_PRODUCTION_CONFIG` — ``distributed.enabled=True``.

The two presets differ only in that flag, which is informational: no part
of the runtime starts distributed coordination from it (use
:class:`~promptise.runtime.distributed.RuntimeCoordinator` and the
runtime transport explicitly).  Production hardening is per process:
set ``journal``, ``restart_policy``, ``health``, ``budget`` and
``trigger_delivery`` on each :class:`ProcessConfig`.
"""

from __future__ import annotations

import ipaddress
import re
import warnings
from collections.abc import Callable
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

_ENV_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}")


def is_env_reference(value: str) -> bool:
    """Whether *value* contains an ``${VAR}`` / ``${VAR:-default}`` reference."""
    return bool(_ENV_REFERENCE.search(value))


# ---------------------------------------------------------------------------
# Execution modes
# ---------------------------------------------------------------------------


class ExecutionMode(str, Enum):
    """Agent execution mode.

    Attributes:
        STRICT: Agent cannot modify itself.  Current behavior, default.
        OPEN: Agent can adapt identity, memory, tools, and triggers at
            runtime — a self-evolving, autonomous agent.
    """

    STRICT = "strict"
    OPEN = "open"


class OpenModeConfig(BaseModel):
    """Guardrails for open execution mode.

    Controls what the agent is allowed to self-modify and how far.
    Only takes effect when ``ProcessConfig.execution_mode`` is
    :attr:`ExecutionMode.OPEN`.

    Attributes:
        allow_identity_change: Agent can modify its own instructions.
        allow_tool_creation: Agent can define new Python tools.
        allow_mcp_connect: Agent can connect to new MCP servers.
        allow_trigger_management: Agent can add/remove triggers.
        allow_memory_management: Agent can explicitly store/search/forget.
        max_custom_tools: Max number of agent-created tools.
        max_dynamic_triggers: Max number of dynamically added triggers.
        max_instruction_length: Max character length for modified instructions.
        max_rebuilds: Max agent rebuilds (hot reloads) per lifetime of the
            process object; restarts do not reset it. A change the limit
            refuses is not applied, now or after a restart.
            ``rollback()`` is not refused. ``None`` = unlimited.
        allowed_mcp_urls: Whitelist of MCP server URLs (empty = any).
        sandbox_custom_tools: Execute agent-written tools in sandbox.
        allow_process_spawn: Agent can spawn new processes within the
            runtime.  Defaults to ``False``.
        max_spawned_processes: Max number of processes this agent can spawn.
    """

    allow_identity_change: bool = Field(True, description="Agent can modify its own instructions")
    allow_tool_creation: bool = Field(True, description="Agent can define new Python tools")
    allow_mcp_connect: bool = Field(True, description="Agent can connect to new MCP servers")
    allow_trigger_management: bool = Field(True, description="Agent can add/remove triggers")
    allow_memory_management: bool = Field(
        True, description="Agent can explicitly store/search/forget memories"
    )
    max_custom_tools: int = Field(20, ge=0, description="Max number of agent-created tools")
    max_dynamic_triggers: int = Field(
        10, ge=0, description="Max number of dynamically added triggers"
    )
    max_instruction_length: int = Field(
        10_000, ge=100, description="Max character length for modified instructions"
    )
    max_rebuilds: int | None = Field(
        None, description="Max agent rebuilds per lifetime (None = unlimited)"
    )
    allowed_mcp_urls: list[str] = Field(
        default_factory=list,
        description="Whitelist of MCP server URLs (empty = any)",
    )
    sandbox_custom_tools: bool = Field(True, description="Execute agent-written tools in sandbox")
    allow_process_spawn: bool = Field(
        False,
        description=(
            "Agent can spawn new processes within the runtime. "
            "Defaults to False — enable explicitly when needed."
        ),
    )
    max_spawned_processes: int = Field(
        3, ge=0, description="Max number of processes this agent can spawn"
    )


# ---------------------------------------------------------------------------
# Atomic configs
# ---------------------------------------------------------------------------


class _WarnOnUnknownKeys(BaseModel):
    """Base for section configs: unknown keys warn instead of vanishing.

    Before 1.4.4 these models dropped unknown keys without a word, so a
    misspelt option (``error_windw``) silently kept its default.  They are
    still ignored, for backward compatibility, but now emit a
    ``FutureWarning`` naming them; a future release will reject them, as
    :class:`ProcessConfig` and :class:`TriggerConfig` already do.  Manifests
    and the orchestration API reject them today.
    """

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _warn_unknown_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            unknown = sorted(str(k) for k in data if k not in cls.model_fields)
            if unknown:
                warnings.warn(
                    f"{cls.__name__}: unknown key(s) {unknown} are ignored; "
                    f"valid keys are {sorted(cls.model_fields)}. Unknown keys will be "
                    "rejected in a future release.",
                    FutureWarning,
                    stacklevel=2,
                )
        return data


#: Filesystem events a ``file_watch`` trigger can react to.
FILE_WATCH_EVENTS = ("created", "modified", "deleted", "moved")

#: Default signature header per webhook signature scheme.
SIGNATURE_HEADERS = {
    "generic": "X-Webhook-Signature",
    "github": "X-Hub-Signature-256",
    "stripe": "Stripe-Signature",
}


class TriggerConfig(BaseModel):
    """Configuration for a single trigger.

    Each trigger ``type`` requires a different subset of fields.  The
    :meth:`_validate_trigger_fields` model validator enforces this for
    built-in types, and rejects bad cron expressions, time zones, filter
    expressions and source ranges when the config is created rather than
    when the process first runs.  Custom trigger types use the
    ``custom_config`` dict.  Unknown keys are rejected, so a misspelt
    option fails loudly instead of being ignored.

    Attributes:
        type: Trigger type (built-in or custom-registered via
            :func:`~promptise.runtime.triggers.register_trigger_type`).
        cron_expression: Cron expression (``cron`` type only).  Five
            fields, or six with a trailing seconds field
            (``"* * * * * */10"`` fires every 10 seconds).
        cron_timezone: IANA time zone the cron expression is read in
            (``cron`` only).  ``None`` means UTC.
        webhook_path: URL path for the webhook endpoint (``webhook`` only).
        webhook_port: Listening port (``webhook`` only).
        webhook_host: Interface to bind (``webhook`` only).  Defaults to
            ``127.0.0.1`` (loopback); use ``0.0.0.0`` to accept requests
            from other machines.
        hmac_secret: Shared secret for request signatures (``webhook``
            only).  When set, unsigned or wrongly signed requests get 401.
        signature_scheme: How the signature is computed and sent:
            ``generic`` (``X-Webhook-Signature: sha256=<hex>``), ``github``
            (``X-Hub-Signature-256: sha256=<hex>``) or ``stripe``
            (``Stripe-Signature: t=<ts>,v1=<hex>`` over ``"<ts>.<body>"``).
        signature_header: Header carrying the signature.  Defaults to the
            scheme's standard header.
        signature_tolerance: Max age in seconds of a timestamped
            (``stripe``) signature, to stop replays.
        allowed_sources: Client IPs or CIDR ranges allowed to call the
            webhook (empty = any).  Others get 403.
        webhook_max_body_bytes: Largest request body the webhook accepts,
            in bytes (``webhook`` only, default 1 MiB).  Larger bodies get
            ``413``.
        watch_path: Directory to watch (``file_watch`` only).
        watch_patterns: Glob patterns (``file_watch`` only), matched
            against the file name.
        watch_events: Filesystem events to react to (``file_watch`` only):
            any of ``created``, ``modified``, ``deleted``, ``moved``.
        watch_debounce_seconds: Window in which events for the same file
            are merged into one (``file_watch`` only).
        watch_recursive: Also watch subdirectories (``file_watch`` only,
            default ``True``).
        watch_backend: How changes are detected (``file_watch`` only):
            ``"auto"`` (native OS notifications through ``watchdog`` when
            it is installed, otherwise polling), ``"watchdog"`` (fail if
            it isn't installed) or ``"polling"`` (scan the directory every
            ``watch_poll_interval`` seconds; use it for network shares and
            bind mounts, where OS notifications are unreliable).
        watch_poll_interval: Seconds between scans with the polling
            backend (``file_watch`` only).
        watch_max_pending_events: How many changed files the trigger holds
            while the process is busy (``file_watch`` only).  Events wait
            for the process instead of being dropped; changes to a file
            that is already waiting are merged into its event.  Only when
            this many *different* files are waiting is a new one dropped,
            with an error log and a count in
            ``FileWatchTrigger.dropped_events``.
        event_type: EventBus event type string (``event`` only).
        event_source: Optional source filter (``event`` only).
        topic: MessageBroker topic (``message`` only).
        custom_config: Additional key-value configuration for custom
            trigger types.
        filter_expression: Cheap pre-filter evaluated before the agent
            runs (all types, optional).  Either a safe expression string
            such as ``"payload['action'] == 'opened'"`` (see
            :mod:`promptise.runtime.triggers.filters`) or a callable that
            takes the :class:`~promptise.runtime.triggers.base.TriggerEvent`
            and returns a bool.  Events that don't match are skipped.

    Setting an option that belongs to another built-in trigger type (for
    example ``watch_path`` on a ``cron`` trigger) is an error, since the
    trigger would ignore it.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., description="Trigger type (built-in or custom-registered)")

    # -- Cron --
    cron_expression: str | None = Field(None, description="Cron expression (e.g. '*/5 * * * *')")
    cron_timezone: str | None = Field(
        None, description="IANA time zone for the cron schedule (None = UTC)"
    )

    # -- Webhook --
    webhook_path: str = Field("/webhook", description="Webhook URL path")
    webhook_port: int = Field(9090, gt=1024, le=65535, description="Webhook listen port")
    webhook_host: str = Field(
        "127.0.0.1", description="Interface to bind the webhook server to (0.0.0.0 = all)"
    )
    hmac_secret: SecretStr | None = Field(
        None, description="Shared secret for webhook request signatures"
    )
    signature_scheme: Literal["generic", "github", "stripe"] = Field(
        "generic", description="Webhook signature format"
    )
    signature_header: str | None = Field(
        None, description="Header carrying the signature (default: the scheme's header)"
    )
    signature_tolerance: int = Field(
        300, gt=0, description="Max age in seconds of a timestamped (stripe) signature"
    )
    allowed_sources: list[str] = Field(
        default_factory=list,
        description="Client IPs / CIDR ranges allowed to call the webhook (empty = any)",
    )
    webhook_max_body_bytes: int = Field(
        1024 * 1024,
        ge=1,
        le=1024 * 1024 * 1024,
        description="Largest webhook request body in bytes (larger bodies get 413)",
    )

    # -- File watch --
    watch_path: str | None = Field(None, description="Directory to watch for changes")
    watch_patterns: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Glob patterns for file watch",
    )
    watch_events: list[str] = Field(
        default_factory=lambda: ["created", "modified"],
        description="Filesystem events to react to (created, modified, deleted, moved)",
    )
    watch_debounce_seconds: float = Field(
        0.5, ge=0, description="Window in which events for one file are merged"
    )
    watch_recursive: bool = Field(True, description="Also watch subdirectories")
    watch_backend: Literal["auto", "watchdog", "polling"] = Field(
        "auto",
        description="Change detection: auto (watchdog if installed), watchdog or polling",
    )
    watch_poll_interval: float = Field(
        1.0, gt=0, le=3600, description="Seconds between scans with the polling backend"
    )
    watch_max_pending_events: int = Field(
        10_000,
        ge=1,
        description="Changed files held while the process is busy before new ones are dropped",
    )

    # -- Event --
    event_type: str | None = Field(None, description="EventBus event type to subscribe to")
    event_source: str | None = Field(None, description="Optional event source filter")

    # -- Message --
    topic: str | None = Field(None, description="MessageBroker topic to subscribe to")

    # -- Custom --
    custom_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional configuration for custom trigger types",
    )

    # -- Common --
    filter_expression: str | Callable[..., Any] | None = Field(
        None,
        description=(
            "Filter evaluated before the agent runs: a safe expression string "
            "or a callable taking the TriggerEvent"
        ),
    )

    @model_validator(mode="after")
    def _validate_trigger_fields(self) -> TriggerConfig:
        """Enforce type-specific required fields and validate values early."""
        self._reject_options_of_other_types()
        if self.type == "cron":
            if not self.cron_expression:
                raise ValueError("Cron trigger requires 'cron_expression'")
            from .exceptions import TriggerError
            from .triggers.cron import validate_cron_expression, validate_timezone

            try:
                validate_cron_expression(self.cron_expression)
                if self.cron_timezone is not None:
                    validate_timezone(self.cron_timezone)
            except TriggerError as exc:
                raise ValueError(str(exc)) from exc
        if self.type == "file_watch":
            if not self.watch_path:
                raise ValueError("File watch trigger requires 'watch_path'")
            if not self.watch_events:
                raise ValueError("File watch trigger requires at least one entry in 'watch_events'")
            unknown = sorted(set(self.watch_events) - set(FILE_WATCH_EVENTS))
            if unknown:
                raise ValueError(
                    f"Unknown watch_events {unknown}; choose from {list(FILE_WATCH_EVENTS)}"
                )
            if self.watch_backend == "watchdog":
                import importlib.util

                if importlib.util.find_spec("watchdog") is None:
                    raise ValueError(
                        "watch_backend='watchdog' needs the 'watchdog' package "
                        "(pip install watchdog); use 'auto' or 'polling' instead"
                    )
        if self.type == "event" and not self.event_type:
            raise ValueError("Event trigger requires 'event_type'")
        if self.type == "message" and not self.topic:
            raise ValueError("Message trigger requires 'topic'")
        if self.hmac_secret is None and (
            self.signature_scheme != "generic" or self.signature_header is not None
        ):
            raise ValueError("'signature_scheme' and 'signature_header' require 'hmac_secret'")
        if self.hmac_secret is not None:
            secret = self.hmac_secret.get_secret_value()
            if not secret:
                raise ValueError("'hmac_secret' is empty")
            if secret == "**********":
                # What model_dump(mode="json") / RuntimeConfig.to_dict() write
                # in place of the secret: serialised configs don't carry it.
                raise ValueError(
                    "'hmac_secret' is the masked placeholder from a serialised config; "
                    "pass the real secret (for example via ${ENV_VAR} in a manifest)"
                )
        for source in self.allowed_sources:
            try:
                ipaddress.ip_network(source, strict=False)
            except ValueError as exc:
                raise ValueError(f"Invalid entry in 'allowed_sources': {source!r}") from exc
        if isinstance(self.filter_expression, str):
            from .triggers.filters import validate_filter_expression

            validate_filter_expression(self.filter_expression)
        return self

    def _reject_options_of_other_types(self) -> None:
        """Fail on options that the configured built-in type would ignore.

        Only values that differ from the default count, so a config that
        was dumped (all fields present) and loaded again still validates.
        Custom trigger types receive the whole config and are not checked.
        """
        if self.type not in _TRIGGER_TYPE_FIELDS:
            return
        allowed = _TRIGGER_TYPE_FIELDS[self.type] | _COMMON_TRIGGER_FIELDS
        ignored = []
        for name in sorted(self.model_fields_set - allowed):
            field_info = type(self).model_fields[name]
            default = field_info.get_default(call_default_factory=True)
            if getattr(self, name) != default:
                ignored.append(name)
        if ignored:
            owners = sorted(
                {t for t, fields in _TRIGGER_TYPE_FIELDS.items() for n in ignored if n in fields}
            )
            raise ValueError(
                f"Option(s) {ignored} belong to {owners} triggers and would be ignored "
                f"by a {self.type!r} trigger; remove them or change 'type'"
            )

    @property
    def effective_signature_header(self) -> str:
        """The header the webhook reads the signature from."""
        return self.signature_header or SIGNATURE_HEADERS[self.signature_scheme]


#: TriggerConfig fields each built-in trigger type reads.
_TRIGGER_TYPE_FIELDS: dict[str, frozenset[str]] = {
    "cron": frozenset({"cron_expression", "cron_timezone"}),
    "webhook": frozenset(
        {
            "webhook_path",
            "webhook_port",
            "webhook_host",
            "hmac_secret",
            "signature_scheme",
            "signature_header",
            "signature_tolerance",
            "allowed_sources",
            "webhook_max_body_bytes",
        }
    ),
    "file_watch": frozenset(
        {
            "watch_path",
            "watch_patterns",
            "watch_events",
            "watch_debounce_seconds",
            "watch_recursive",
            "watch_backend",
            "watch_poll_interval",
            "watch_max_pending_events",
        }
    ),
    "event": frozenset({"event_type", "event_source"}),
    "message": frozenset({"topic"}),
}

#: TriggerConfig fields that apply to every trigger type.
_COMMON_TRIGGER_FIELDS = frozenset({"type", "filter_expression", "custom_config"})


class TriggerDeliveryConfig(BaseModel):
    """How a process handles trigger events that can't be processed.

    Attributes:
        max_retries: Extra attempts for an event whose agent run raised.
            ``0`` (default) never retries.  A retry re-runs the whole agent
            turn, so tools with side effects may run twice — only enable
            it when those tools are idempotent or gated by approval.
        retry_backoff: Delay in seconds before the first retry; doubles
            with each further attempt.
        retry_backoff_max: Upper bound for the retry delay.
        dead_letter_size: How many undeliverable events to keep in
            :attr:`AgentProcess.dead_letters` (oldest dropped first).
        scan_payloads: Run a prompt-injection scan on each trigger payload
            before the agent sees it.  Flagged events are dead-lettered
            instead of run.  Uses ``ProcessConfig.guardrails`` when it is a
            scanner, otherwise an injection-only
            :class:`~promptise.guardrails.PromptiseSecurityScanner` (needs
            ``transformers``; the model loads when the process starts).
        max_payload_chars: Longest payload rendering placed in the prompt;
            longer payloads are truncated with a marker.
    """

    model_config = ConfigDict(extra="forbid")

    max_retries: int = Field(0, ge=0, le=10, description="Retries for a failed agent run")
    retry_backoff: float = Field(2.0, gt=0, description="First retry delay in seconds")
    retry_backoff_max: float = Field(60.0, gt=0, description="Max retry delay in seconds")
    dead_letter_size: int = Field(100, ge=0, description="Undeliverable events kept")
    scan_payloads: bool = Field(False, description="Prompt-injection scan on trigger payloads")
    max_payload_chars: int = Field(
        20_000, ge=200, description="Max characters of payload placed in the prompt"
    )


class JournalConfig(_WarnOnUnknownKeys):
    """Journal (durable audit log) configuration.

    Attributes:
        level: Detail level — ``none`` (disabled), ``checkpoint``
            (state snapshots per cycle), or ``full`` (every side effect).
        backend: Storage backend.
        path: Directory for the file backend.
        recover_on_start: On a process object's first :meth:`start`, when
            the journal holds a checkpoint for the process name, restore
            what it recorded: the context state (with the
            ``context_update`` entries written after the checkpoint), the
            mission, the conversation history and the invocation count.
            Recovered keys replace the same keys of
            ``context.initial_state``.  A clean :meth:`stop` writes a
            checkpoint too, so the next run continues from it.  ``True``
            by default (a journal is opt-in, and recovery is what it is
            for); ``False`` always starts fresh.
    """

    level: Literal["none", "checkpoint", "full"] = Field(
        "checkpoint", description="Journal detail level"
    )
    backend: Literal["file", "memory"] = Field("file", description="Journal storage backend")
    path: str = Field(".promptise/journal", description="Base directory for journal files")
    recover_on_start: bool = Field(
        True, description="Restore state from the last checkpoint on the first start"
    )


class ContextConfig(_WarnOnUnknownKeys):
    """AgentContext configuration.

    Attributes:
        writable_keys: State keys the agent is allowed to write to.
            Empty list means **all** keys are writable.
        memory_provider: Memory provider type (or ``None`` to disable).
        memory_max: Max memories to inject per invocation.
        memory_min_score: Min relevance score for memory injection.
        memory_timeout: Seconds to wait for a memory search or auto-store
            write (a slower write finishes in the background).
        memory_auto_store: Automatically store exchanges in long-term memory.
        memory_collection: Collection name for ChromaDB backend.
        memory_persist_directory: Persist directory for ChromaDB.
        memory_user_id: User ID for Mem0 scoping.
        conversation_max_messages: Max messages in conversation buffer
            (short-term memory).  ``0`` means **unlimited**, not disabled.
        conversation_history: Set to ``False`` to turn short-term memory
            off, so every run sees only the current trigger event.
        conversation_clear_on_stop: Clear the conversation history when
            the process stops.  ``False`` (default): the history survives
            ``stop()`` / ``start()`` and restarts of the same process
            object; :meth:`~AgentProcess.close` always clears it.
        state_tool: Give the agent an ``update_state(key, value)`` tool
            that writes to its context state (limited to
            ``writable_keys`` when set).  Off by default: without
            ``writable_keys`` the agent can write any key.
        file_mounts: Mapping of logical name → filesystem path.
        env_prefix: Only expose environment variables with this prefix.
        initial_state: Pre-populated key-value state.

    Unknown keys are ignored with a ``FutureWarning``.
    """

    writable_keys: list[str] = Field(
        default_factory=list,
        description="State keys the agent can write (empty = all writable)",
    )

    # -- Long-term memory --
    memory_provider: Literal["in_memory", "chroma", "mem0"] | None = Field(
        None, description="Memory provider type (None = disabled)"
    )
    memory_max: int = Field(5, ge=1, description="Max memories per invocation")
    memory_min_score: float = Field(0.0, ge=0.0, le=1.0, description="Min relevance score")
    memory_timeout: float = Field(
        5.0, gt=0, description="Seconds to wait for memory search / auto-store"
    )
    memory_auto_store: bool = Field(
        False,
        description="Automatically store exchanges in long-term memory",
    )
    memory_collection: str = Field("agent_memory", description="Collection name for ChromaDB")
    memory_persist_directory: str | None = Field(None, description="Persist directory for ChromaDB")
    memory_user_id: str = Field("default", description="User ID for Mem0 scoping")

    # -- Short-term memory --
    conversation_max_messages: int = Field(
        100,
        ge=0,
        description="Max messages in conversation buffer (0 = unlimited)",
    )
    conversation_history: bool = Field(
        True,
        description="Carry short-term conversation history between runs (False = each run starts fresh)",
    )
    conversation_clear_on_stop: bool = Field(
        False, description="Clear the conversation history on stop() (default: keep it)"
    )

    # -- Agent-writable state --
    state_tool: bool = Field(
        False, description="Give the agent an update_state tool for its context state"
    )

    # -- Environment --
    file_mounts: dict[str, str] = Field(
        default_factory=dict, description="Logical name → filesystem path"
    )
    env_prefix: str = Field("AGENT_", description="Prefix for exposed environment variables")
    initial_state: dict[str, Any] = Field(default_factory=dict, description="Pre-populated state")


# ---------------------------------------------------------------------------
# Escalation & governance configs
# ---------------------------------------------------------------------------


class EscalationTarget(_WarnOnUnknownKeys):
    """Where to send escalation notifications.

    Attributes:
        webhook_url: HTTP endpoint to POST a JSON payload to.
        event_type: EventBus event type to emit.
    """

    webhook_url: str | None = Field(None, description="Webhook endpoint URL")
    event_type: str | None = Field(None, description="EventBus event type")

    @field_validator("webhook_url")
    @classmethod
    def _check_webhook_url(cls, v: str | None) -> str | None:
        if v is not None and not v.lower().startswith(("http://", "https://")):
            raise ValueError(f"webhook_url must be an http:// or https:// URL, got {v!r}")
        return v


class ToolCostAnnotation(_WarnOnUnknownKeys):
    """Cost and reversibility annotation for a tool.

    Attributes:
        cost_weight: Abstract cost units per invocation (default 1.0).
        irreversible: Whether the tool performs an irreversible action.
    """

    cost_weight: float = Field(1.0, gt=0, description="Cost weight per call")
    irreversible: bool = Field(False, description="Irreversible action flag")


class SecretScopeConfig(_WarnOnUnknownKeys):
    """Per-process secret scoping configuration.

    Attributes:
        enabled: Enable secret scoping for this process.
        secrets: Secret name to value or environment reference.  A
            reference (``${VAR}``, ``${VAR:-default}`` or text containing
            them) is resolved from ``os.environ`` when the process starts,
            not when the config is built, so the config holds no secret
            value.  Values are stored as :class:`~pydantic.SecretStr`:
            ``repr()`` and JSON dumps show ``**********`` for literal
            values (references are shown as written).
        default_ttl: Default TTL in seconds (``None`` = no expiry).
        ttls: Per-secret TTL overrides in seconds.  Every name must be a
            key of ``secrets``.
        revoke_on_stop: Zero-fill and remove all secrets on stop.

    Unknown keys are ignored with a ``FutureWarning``.
    """

    enabled: bool = Field(False, description="Enable secret scoping")
    secrets: dict[str, SecretStr] = Field(
        default_factory=dict, description="Secret name → value or ${ENV_VAR}"
    )
    default_ttl: float | None = Field(
        None, gt=0, description="Default TTL in seconds (None = no expiry)"
    )
    ttls: dict[str, float] = Field(default_factory=dict, description="Per-secret TTL overrides")
    revoke_on_stop: bool = Field(True, description="Revoke all secrets on stop")

    @field_validator("secrets")
    @classmethod
    def _reject_masked_secrets(cls, v: dict[str, SecretStr]) -> dict[str, SecretStr]:
        for name, value in v.items():
            if value.get_secret_value() == "**********":
                raise ValueError(
                    f"Secret {name!r} is the masked placeholder from a serialised config; "
                    "pass the real value or an ${ENV_VAR} reference"
                )
        return v

    @field_serializer("secrets", when_used="json")
    def _dump_secrets(self, v: dict[str, SecretStr]) -> dict[str, str]:
        # Environment references carry no secret, so they survive a dump;
        # literal values are masked like any SecretStr.
        return {
            name: (raw if is_env_reference(raw) else "**********")
            for name, raw in ((n, s.get_secret_value()) for n, s in v.items())
        }

    @model_validator(mode="after")
    def _validate_ttls(self) -> SecretScopeConfig:
        unknown = sorted(set(self.ttls) - set(self.secrets))
        if unknown:
            raise ValueError(
                f"'ttls' names secrets that are not in 'secrets': {unknown}; "
                f"configured secrets: {sorted(self.secrets)}"
            )
        bad = sorted(name for name, ttl in self.ttls.items() if ttl <= 0)
        if bad:
            raise ValueError(f"'ttls' must be positive numbers of seconds; check {bad}")
        return self


class BudgetConfig(_WarnOnUnknownKeys):
    """Autonomy budget configuration (unknown keys warn and are ignored).

    Attributes:
        enabled: Enable budget tracking for this process.
        max_tool_calls_per_run: Max tool calls per invocation.
        max_llm_turns_per_run: Max LLM turns per invocation.
        max_cost_per_run: Max cost units per invocation.
        max_irreversible_per_run: Max irreversible actions per invocation.
        max_tool_calls_per_day: Max tool calls in a day.
        max_runs_per_day: Max invocations in a day.
        max_cost_per_day: Max cost units in a day.
        tool_costs: Per-tool cost annotations.
        on_exceeded: Action on budget violation.
        escalation: Escalation target for violations.
        daily_reset_hour_utc: UTC hour to reset daily counters.
        inject_remaining: Inject remaining budget into agent context.
    """

    enabled: bool = Field(False, description="Enable budget tracking")
    max_tool_calls_per_run: int | None = Field(None, ge=1)
    max_llm_turns_per_run: int | None = Field(None, ge=1)
    max_cost_per_run: float | None = Field(None, gt=0)
    max_irreversible_per_run: int | None = Field(None, ge=0)
    max_tool_calls_per_day: int | None = Field(None, ge=1)
    max_runs_per_day: int | None = Field(None, ge=1)
    max_cost_per_day: float | None = Field(None, gt=0)
    tool_costs: dict[str, ToolCostAnnotation] = Field(default_factory=dict)
    on_exceeded: Literal["pause", "stop", "escalate"] = Field("pause")
    escalation: EscalationTarget | None = Field(None)
    daily_reset_hour_utc: int = Field(0, ge=0, le=23)
    inject_remaining: bool = Field(True)


class HealthConfig(_WarnOnUnknownKeys):
    """Behavioral health monitoring configuration.

    Attributes:
        enabled: Enable health monitoring for this process.
        stuck_threshold: Same tool+args N times in a row = stuck.
        loop_window: Tool calls to examine for loop detection.
        loop_min_repeats: Min pattern repeats to trigger.
        empty_threshold: N consecutive trivial final replies = anomaly.
        empty_max_chars: A final reply of **at most** this many
            characters (after stripping whitespace) is trivial.  With the
            default 10, a 10-character reply counts as empty and an
            11-character one doesn't; ``0`` counts only blank replies.
        error_window: Number of recent invocations the error rate is
            computed over.  The rate is checked once the window is full.
        error_rate_threshold: An error rate at or above this (0–1) in a
            full window is an anomaly.
        on_anomaly: What the process does when a run produced an anomaly
            (a ``health.anomaly`` event is emitted in every case):
            ``"log"`` logs it; ``"pause"`` suspends the process;
            ``"escalate"`` sends the escalation notification to
            ``escalation`` (when set) and suspends the process;
            ``"stop"`` stops the process.
        cooldown: Seconds before the same anomaly (same kind and same
            evidence: the same stuck call or loop) is reported again. Set
            0 to report every repeat, e.g. for an in-run guard.
            :meth:`HealthMonitor.clear_anomalies` resets it.
        escalation: Escalation target for anomalies.

    Window sizes can be changed on a running monitor (for example with
    ``PATCH /api/v1/processes/{name}/health``); the monitor resizes its
    windows and keeps the recorded data.  Unknown keys are ignored with a
    ``FutureWarning``.
    """

    enabled: bool = Field(False, description="Enable health monitoring")
    stuck_threshold: int = Field(3, ge=2)
    loop_window: int = Field(20, ge=4)
    loop_min_repeats: int = Field(2, ge=2)
    empty_threshold: int = Field(3, ge=2)
    empty_max_chars: int = Field(10, ge=0, description="Replies of at most this length are empty")
    error_window: int = Field(10, ge=2)
    error_rate_threshold: float = Field(0.5, gt=0, le=1.0)
    on_anomaly: Literal["log", "pause", "escalate", "stop"] = Field("log")
    cooldown: float = Field(300.0, ge=0)
    escalation: EscalationTarget | None = Field(None)


class MissionConfig(_WarnOnUnknownKeys):
    """Mission-oriented process configuration.

    Attributes:
        enabled: Enable mission tracking for this process.
        objective: What the agent is trying to achieve.
        success_criteria: How to judge completion.
        eval_model: LLM for evaluation (None = use process model).
        eval_every: Evaluate every N invocations.
        confidence_threshold: Below this = pause + escalate.
        timeout_hours: Max hours (0 = no timeout).
        max_invocations: Max invocations (0 = unlimited).
        auto_complete: Stop the process when the mission is achieved
            (``False`` keeps it running; its later invocations are
            skipped, since the mission is completed).
        on_complete: Deprecated and ignored.  It was never read: what
            happens on completion is decided by ``auto_complete`` alone.
            Setting it emits a ``FutureWarning``; it will be removed.
        escalation: Escalation target for low confidence.
        success_check: Optional programmatic check, called with the
            :class:`~promptise.runtime.mission.MissionEvidence` before the
            LLM judge: ``True`` (achieved) or ``False`` (not achieved)
            decides, ``None`` falls through to the judge.  Python only
            (not in manifests or the API) and never serialised.

    Unknown keys are ignored with a ``FutureWarning``.
    """

    enabled: bool = Field(False, description="Enable mission tracking")
    objective: str = Field("", description="Mission objective")
    success_criteria: str = Field("", description="Success criteria")
    eval_model: str | None = Field(None)
    eval_every: int = Field(1, ge=1)
    confidence_threshold: float = Field(0.7, ge=0, le=1.0)
    timeout_hours: float = Field(0.0, ge=0)
    max_invocations: int = Field(0, ge=0)
    auto_complete: bool = Field(True)
    on_complete: Literal["stop", "continue", "suspend"] | None = Field(
        None, description="Deprecated and ignored; use auto_complete."
    )
    escalation: EscalationTarget | None = Field(None)
    success_check: Callable[[Any], bool | None] | None = Field(
        None, exclude=True, description="Programmatic success check (Python only)"
    )

    @field_validator("on_complete")
    @classmethod
    def _warn_on_complete_ignored(cls, v: str | None) -> str | None:
        if v is not None:
            warnings.warn(
                "MissionConfig.on_complete is ignored and will be removed: a completed "
                "mission stops the process when auto_complete=True (the default). Set "
                "auto_complete=False to keep the process running after completion.",
                FutureWarning,
                stacklevel=2,
            )
        return v


# ---------------------------------------------------------------------------
# Composite configs
# ---------------------------------------------------------------------------


class InboxConfig(BaseModel):
    """Configuration for the human-to-agent message inbox.

    Attributes:
        enabled: Enable the message inbox.
        max_messages: Maximum messages in the inbox at once.
        max_message_length: Maximum characters per message.
        default_ttl: Default time-to-live in seconds (0 = no expiry).
        max_ttl: Maximum allowed TTL in seconds.
        rate_limit_per_sender: Maximum messages per sender per hour.
        scan_with_guardrails: Scan incoming messages for prompt injection.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(False, description="Enable the message inbox")
    max_messages: int = Field(50, gt=0, description="Max inbox size")
    max_message_length: int = Field(2000, gt=0, description="Max chars per message")
    default_ttl: float = Field(3600, ge=0, description="Default TTL in seconds")
    max_ttl: float = Field(86400, ge=0, description="Max TTL in seconds")
    rate_limit_per_sender: int = Field(20, gt=0, description="Max messages per sender per hour")
    scan_with_guardrails: bool = Field(True, description="Scan messages for injection")

    @model_validator(mode="after")
    def _validate_ttls(self) -> InboxConfig:
        if self.max_ttl > 0 and self.default_ttl > self.max_ttl:
            raise ValueError(
                f"'default_ttl' ({self.default_ttl:g}s) is longer than 'max_ttl' "
                f"({self.max_ttl:g}s); lower default_ttl or raise max_ttl"
            )
        return self


class ProcessConfig(BaseModel):
    """Configuration for a single agent process.

    Composes all atomic configs and adds process-level settings.

    Attributes:
        model: LLM model identifier (e.g. ``openai:gpt-5-mini``).
        instructions: System prompt for the agent.
        servers: MCP server specifications (same format as ``.superagent``).
        triggers: Trigger configurations.
        journal: Journal configuration.  Off by default (``level="none"``);
            pass a :class:`JournalConfig` to record transitions,
            invocations and checkpoints (``JournalConfig()`` itself
            defaults to ``level="checkpoint"``).
        context: AgentContext configuration.
        concurrency: Max concurrent trigger invocations.
        heartbeat_interval: Heartbeat period in seconds.
        idle_timeout: Seconds of inactivity before suspending (0 = never).
        max_lifetime: Max process lifetime in seconds (0 = unlimited).
        max_consecutive_failures: Consecutive failures before FAILED state.
        restart_policy: When to restart the process automatically.
            ``on_failure`` restarts it after it enters ``FAILED`` (e.g.
            ``max_consecutive_failures`` reached); ``always`` also
            restarts it when it stops itself at ``max_lifetime``.  An
            explicit :meth:`~AgentProcess.stop` never triggers a restart.
        max_restarts: Max consecutive restart attempts after failures.
            The count resets after a successful invocation.
        restart_backoff: Delay in seconds before the first restart
            attempt; doubles on each consecutive attempt (capped at 60s).
        trigger_delivery: Retries, dead-letter list and payload scanning
            for trigger events.
        guardrails: Passed through to :func:`~promptise.agent.build_agent`
            when the agent is built, as are ``observe``, ``cache``,
            ``optimize_tools``, ``adaptive`` and ``max_invocation_time``.

    Unknown keys are rejected, so a misspelt option fails loudly instead
    of being ignored.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field("openai:gpt-5-mini", description="LLM model ID")
    instructions: str | None = Field(None, description="System prompt")
    execution_mode: ExecutionMode = Field(
        ExecutionMode.STRICT,
        description="Agent execution mode: strict (immutable) or open (self-modifying)",
    )
    open_mode: OpenModeConfig = Field(
        default_factory=OpenModeConfig,
        description="Guardrails for open execution mode (ignored in strict mode)",
    )
    servers: dict[str, Any] = Field(default_factory=dict, description="MCP server specifications")
    triggers: list[TriggerConfig] = Field(
        default_factory=list, description="Trigger configurations"
    )
    journal: JournalConfig = Field(
        default_factory=lambda: JournalConfig(level="none"),
        description="Journal configuration (off unless set)",
    )
    context: ContextConfig = Field(
        default_factory=ContextConfig, description="AgentContext configuration"
    )
    concurrency: int = Field(1, ge=1, le=100, description="Max concurrent invocations")
    heartbeat_interval: float = Field(10.0, gt=0, description="Heartbeat interval (seconds)")
    idle_timeout: float = Field(0.0, ge=0, description="Idle timeout before suspend (0 = never)")
    max_lifetime: float = Field(0.0, ge=0, description="Max lifetime in seconds (0 = unlimited)")
    max_consecutive_failures: int = Field(
        3, gt=0, description="Consecutive failures before FAILED state"
    )
    restart_policy: Literal["always", "on_failure", "never"] = Field(
        "never", description="Restart policy for failed processes"
    )
    max_restarts: int = Field(3, ge=0, description="Max restart attempts")
    restart_backoff: float = Field(
        1.0, ge=0, description="Initial restart delay in seconds (doubles, max 60s)"
    )

    # -- Governance (all opt-in, zero overhead when disabled) --
    secrets: SecretScopeConfig = Field(
        default_factory=SecretScopeConfig,
        description="Per-process secret scoping",
    )
    budget: BudgetConfig = Field(
        default_factory=BudgetConfig,
        description="Autonomy budget limits",
    )
    health: HealthConfig = Field(
        default_factory=HealthConfig,
        description="Behavioral health monitoring",
    )
    mission: MissionConfig = Field(
        default_factory=MissionConfig,
        description="Mission-oriented process configuration",
    )

    # -- Message inbox (opt-in) --
    inbox: InboxConfig = Field(
        default_factory=InboxConfig,
        description="Human-to-agent message inbox configuration",
    )

    # -- Human-in-the-loop approval (opt-in) --
    approval: Any | None = Field(
        None,
        description=(
            "ApprovalPolicy instance or dict for human-in-the-loop "
            "tool call approval. When set, matching tools require human "
            "approval before execution."
        ),
    )

    # -- Agent identity (opt-in) --
    identity: Any | None = Field(
        None,
        description=(
            "AgentIdentity instance — who this process is. Attributes the "
            "process's actions to the agent and, when verifiable, presents "
            "its credential to the MCP servers it calls. Built from the "
            "manifest's 'identity' block."
        ),
    )

    # -- Trigger delivery (retries, dead letters, payload scanning) --
    trigger_delivery: TriggerDeliveryConfig = Field(
        default_factory=TriggerDeliveryConfig,
        description="Retries, dead-letter list and payload scanning for trigger events",
    )

    # -- Agent capabilities passed through to build_agent() (opt-in) --
    guardrails: Any | None = Field(
        None,
        description="PromptiseSecurityScanner (or any check_input/check_output guard)",
    )
    observe: Any | None = Field(None, description="Observability: True or an ObservabilityConfig")
    cache: Any | None = Field(None, description="SemanticCache instance for LLM responses")
    optimize_tools: Any | None = Field(None, description="Semantic tool-selection settings")
    adaptive: Any | None = Field(None, description="Adaptive strategy settings")
    max_invocation_time: float = Field(
        0, ge=0, description="Per-invocation timeout in seconds (0 = none)"
    )


class DistributedConfig(BaseModel):
    """Distributed runtime coordination configuration.

    Attributes:
        enabled: Enable distributed mode.
        coordinator_url: URL of the coordinator node.
        transport_port: Port for the management HTTP transport.
        discovery_method: How runtime nodes discover each other.
        heartbeat_interval: Health check interval for node monitoring.
    """

    enabled: bool = Field(False, description="Enable distributed mode")
    coordinator_url: str | None = Field(None, description="Coordinator node URL")
    transport_port: int = Field(9100, gt=1024, le=65535, description="Management transport port")
    discovery_method: Literal["registry", "multicast"] = Field(
        "registry", description="Node discovery method"
    )
    heartbeat_interval: float = Field(
        15.0, gt=0, description="Node health check interval (seconds)"
    )


class RuntimeConfig(BaseModel):
    """Top-level runtime configuration (composite).

    Aggregates per-process configs and global settings.

    Attributes:
        processes: Named process configurations.
        distributed: Distributed coordination settings.
    """

    processes: dict[str, ProcessConfig] = Field(
        default_factory=dict, description="Named process configurations"
    )
    distributed: DistributedConfig = Field(
        default_factory=DistributedConfig,
        description="Distributed coordination",
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict.

        Secrets (``TriggerConfig.hmac_secret``) are written as
        ``"**********"``; supply them again when loading the dict.
        """
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeConfig:
        """Deserialize from a dict."""
        return cls.model_validate(data)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

#: Local-only preset (``distributed.enabled=False``, the default).
DEFAULT_DEVELOPMENT_CONFIG = RuntimeConfig(
    distributed=DistributedConfig(enabled=False),
)

#: Preset with ``distributed.enabled=True``.  That is its only difference
#: from :data:`DEFAULT_DEVELOPMENT_CONFIG`, and the flag is informational
#: (see the module docstring): it does not change how processes run.
DEFAULT_PRODUCTION_CONFIG = RuntimeConfig(
    distributed=DistributedConfig(enabled=True),
)
