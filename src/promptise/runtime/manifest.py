"""Schema and loader for ``.agent`` manifest files.

The ``.agent`` format is a YAML file that declaratively defines an agent
process: model, instructions, servers, triggers, world state,
memory, and config.

Example ``.agent`` file::

    version: "1.0"
    name: data-watcher
    model: openai:gpt-5-mini
    agent_pattern: peoatr
    instructions: |
      You monitor data pipelines and alert on anomalies.

    servers:
      data_tools:
        type: http
        url: http://localhost:8000/mcp

    triggers:
      - type: cron
        cron_expression: "*/5 * * * *"

    world:
      pipeline_status: healthy

    config:
      concurrency: 2
      heartbeat_interval: 30

Usage::

    from promptise.runtime.manifest import load_manifest

    manifest = load_manifest("agents/watcher.agent")
    config = manifest_to_process_config(manifest)

Every key is checked: an unknown key anywhere in a section (``health:``,
``context:``, ``memory:``, ``config:``, a server entry, ...) fails
validation with the list of valid keys, instead of being dropped.

``${VAR}`` references are resolved when the manifest is loaded, except in
``secrets.secrets``: those stay references and are resolved when the
process starts (see :class:`~promptise.runtime.secrets.SecretScope`), so
the loaded config never holds a secret value.  A reference to an unset
variable without a default still fails at load time.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from promptise.config import HTTPServerSpec, StdioServerSpec

from .config import (
    BudgetConfig,
    ContextConfig,
    ExecutionMode,
    HealthConfig,
    HookConfig,
    JournalConfig,
    MissionConfig,
    OpenModeConfig,
    ProcessConfig,
    SecretScopeConfig,
    TriggerConfig,
)
from .exceptions import ManifestEnvVarError, ManifestError, ManifestValidationError

logger = logging.getLogger(__name__)


class AgentManifestSchema(BaseModel):
    """Root schema for ``.agent`` manifest files.

    Attributes:
        version: Schema version (currently ``"1.0"``).
        name: Process name (unique identifier).
        model: LLM model ID.
        instructions: System prompt.
        agent_pattern: Reasoning pattern name, as in
            :class:`~promptise.runtime.config.ProcessConfig.agent_pattern`
            (``react``, ``peoatr``, ``research``, ...).  Unset runs ReAct.
        servers: MCP server specifications.
        triggers: Trigger configurations (list of dicts).
        world: Initial world state (key-value).
        memory: Memory provider configuration.
        journal: Journal configuration.
        config: Additional process config overrides.
        hooks: Lifecycle hooks, each an external command run on an event
            (see :class:`~promptise.runtime.config.HookConfig`)::

                hooks:
                  - event: user_prompt_submit
                    command: ["./hooks/audit.sh"]
                    timeout: 5
                    fail_closed: true

        entrypoint: Reserved; not read by the runtime.  Python hooks are
            registered in code (``process.hooks`` or ``HookConfig(
            callback=...)``).
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal["1.0"] = Field("1.0", description="Schema version")
    name: str = Field(..., description="Process name")
    model: str = Field("openai:gpt-5-mini", description="LLM model ID")
    instructions: str | None = Field(None, description="System prompt")
    agent_pattern: str | None = Field(
        None,
        description="Reasoning pattern name (react, peoatr, research, ...); unset runs ReAct",
    )
    identity: dict[str, Any] | None = Field(
        None,
        description=(
            "Agent identity — who is acting. Same shape as the .superagent "
            "'identity' block: provider (local/entra/aws/gcp/spiffe/oidc/"
            "auto), agent_id, and provider options."
        ),
    )
    servers: dict[str, Any] = Field(default_factory=dict, description="MCP server specifications")
    triggers: list[dict[str, Any]] = Field(
        default_factory=list, description="Trigger configurations"
    )
    world: dict[str, Any] = Field(default_factory=dict, description="Initial world state")
    memory: dict[str, Any] | None = Field(None, description="Memory provider config")
    journal: dict[str, Any] | None = Field(None, description="Journal configuration")
    config: dict[str, Any] = Field(default_factory=dict, description="Process config overrides")
    execution_mode: str | None = Field(None, description="Execution mode: 'strict' or 'open'")
    open_mode: dict[str, Any] | None = Field(None, description="Open mode guardrails configuration")
    entrypoint: str | None = Field(None, description="Reserved; not read by the runtime")
    hooks: list[dict[str, Any]] = Field(
        default_factory=list, description="Lifecycle hooks (external commands)"
    )
    secrets: dict[str, Any] | None = Field(
        None, description="Per-process secret scoping configuration"
    )
    budget: dict[str, Any] | None = Field(None, description="Autonomy budget configuration")
    health: dict[str, Any] | None = Field(
        None, description="Behavioral health monitoring configuration"
    )
    mission: dict[str, Any] | None = Field(
        None, description="Mission-oriented process configuration"
    )
    context: dict[str, Any] | None = Field(
        None,
        description=(
            "ContextConfig fields (writable_keys, file_mounts, env_prefix, "
            "conversation_max_messages, conversation_history, initial_state, "
            "memory_*): any ContextConfig field"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_top_level_keys(cls, data: Any) -> Any:
        """Unknown top-level keys, named with "did you mean" hints."""
        if isinstance(data, dict):
            unknown = sorted(str(k) for k in data if k not in cls.model_fields)
            if unknown:
                raise ValueError(
                    _unknown_keys_message("manifest", unknown, sorted(cls.model_fields))
                )
        return data

    @model_validator(mode="after")
    def _validate_manifest(self) -> AgentManifestSchema:
        """Validate cross-field constraints and every section's keys."""
        # Every trigger is validated whole here (keys, types, the fields its
        # type needs), so a typo fails at load / 'promptise runtime
        # validate' and not when the process is built.
        for i, trigger in enumerate(self.triggers):
            if not isinstance(trigger, dict):
                raise ValueError(f"triggers[{i}]: must be a mapping, got {type(trigger).__name__}")
            if "type" not in trigger:
                raise ValueError(f"Trigger at index {i} is missing required 'type' field")
            _check_model(f"triggers[{i}] ({trigger['type']})", trigger, TriggerConfig)
        if self.agent_pattern is not None:
            from .config import check_process_agent_pattern

            check_process_agent_pattern(self.agent_pattern, argument="agent_pattern")
        if self.execution_mode is not None:
            valid_modes = [m.value for m in ExecutionMode]
            if self.execution_mode not in valid_modes:
                raise ValueError(
                    f"execution_mode: {self.execution_mode!r} is not one of {valid_modes}"
                )
        if self.memory is not None:
            unknown = sorted(str(k) for k in set(self.memory) - set(_MEMORY_KEYS))
            if unknown:
                raise ValueError(_unknown_keys_message("memory", unknown, sorted(_MEMORY_KEYS)))
        if self.context is not None:
            _check_keys("context", self.context, ContextConfig)
            if self.world and self.context.get("initial_state"):
                raise ValueError(
                    "Initial state is set twice, in 'world:' and in 'context: initial_state'; "
                    "use one of them"
                )
            for key, field_name in _MEMORY_KEYS.items():
                if self.memory and key in self.memory and field_name in self.context:
                    raise ValueError(
                        f"'memory: {key}' and 'context: {field_name}' set the same option; "
                        "use one of them"
                    )
        for section, model_cls in (
            ("journal", JournalConfig),
            ("open_mode", OpenModeConfig),
            ("secrets", SecretScopeConfig),
            ("budget", BudgetConfig),
            ("health", HealthConfig),
            ("mission", MissionConfig),
        ):
            value = getattr(self, section)
            if value is not None:
                _check_keys(section, value, model_cls)
        for name, spec in self.servers.items():
            if isinstance(spec, dict):
                _server_spec(name, spec)  # raises on a bad entry
        if self.entrypoint is not None:
            warnings.warn(
                "manifest 'entrypoint' is not read by the runtime (it never was); remove "
                "it. Declare hooks in the 'hooks:' section (commands) or register Python "
                "hooks in code with process.hooks.",
                FutureWarning,
                stacklevel=2,
            )
        for i, hook in enumerate(self.hooks):
            if "callback" in hook:
                raise ValueError(
                    f"hooks[{i}]: a manifest hook runs a 'command'; Python callbacks are "
                    "registered in code (process.hooks)"
                )
            _check_model(f"hooks[{i}]", hook, HookConfig)
        if self.identity is not None:
            from promptise.superagent_schema import IdentityConfig

            _check_model("identity", self.identity, IdentityConfig)
        self._check_config_overrides()
        return self

    def _check_config_overrides(self) -> None:
        """``config:`` may hold any ProcessConfig field, once."""
        unknown = sorted(str(k) for k in set(self.config) - set(ProcessConfig.model_fields))
        if unknown:
            raise ValueError(
                _unknown_keys_message("config", unknown, sorted(ProcessConfig.model_fields))
            )
        # Keys that also have a dedicated section: setting both would
        # silently drop one of them.
        dedicated = {
            "model": "model" in self.model_fields_set,
            "instructions": self.instructions is not None,
            "agent_pattern": self.agent_pattern is not None,
            "identity": self.identity is not None,
            "execution_mode": self.execution_mode is not None,
            "open_mode": self.open_mode is not None,
            "servers": bool(self.servers),
            "triggers": bool(self.triggers),
            "journal": self.journal is not None,
            "context": bool(self.world) or self.memory is not None or self.context is not None,
            "secrets": self.secrets is not None,
            "budget": self.budget is not None,
            "health": self.health is not None,
            "mission": self.mission is not None,
            "hooks": bool(self.hooks),
        }
        clashes = sorted(k for k in self.config if dedicated.get(k))
        if clashes:
            raise ValueError(
                f"config: {clashes} also set by the manifest's own section(s); "
                "set each option in one place (the top-level section)"
            )
        if self.config:
            # The values themselves (types, limits, nested sections).
            _check_model("config", dict(self.config), ProcessConfig)


#: ``memory:`` keys and the ContextConfig field each one sets.
_MEMORY_KEYS = {
    "provider": "memory_provider",
    "collection": "memory_collection",
    "persist_directory": "memory_persist_directory",
    "user_id": "memory_user_id",
    "auto_store": "memory_auto_store",
    "max": "memory_max",
    "min_score": "memory_min_score",
    "timeout": "memory_timeout",
}

_HTTP_TRANSPORTS = ("http", "streamable-http", "sse")

#: The journal of a manifest without a ``journal:`` section: checkpoints in
#: ``.promptise/journal`` (file backend), recovered on start.  Code-built
#: :class:`ProcessConfig` has no journal by default; see
#: :func:`manifest_to_process_config` for why manifests differ.
MANIFEST_DEFAULT_JOURNAL = JournalConfig()


def _unknown_keys_message(section: str, unknown: list[str], valid: list[str]) -> str:
    """``section: unknown key(s) [...]`` with "did you mean" hints."""
    import difflib

    hints = []
    for key in unknown:
        close = difflib.get_close_matches(key, valid, n=1)
        if close:
            hints.append(f"{key!r} -> {close[0]!r}")
    message = f"{section}: unknown key(s) {unknown}; valid keys are {valid}"
    if hints:
        message += f" (did you mean {', '.join(hints)}?)"
    return message


def _check_model(section: str, data: Any, model_cls: type[BaseModel]) -> None:
    """Validate *data* as *model_cls*; a clear ``ValueError`` when it fails.

    Unknown keys are named with "did you mean" hints; other problems are
    listed as ``section.field: message``.
    """
    if not isinstance(data, dict):
        raise ValueError(f"{section}: must be a mapping, got {type(data).__name__}")
    valid = sorted(model_cls.model_fields)
    unknown = sorted(str(k) for k in data if k not in model_cls.model_fields)
    if unknown:
        raise ValueError(_unknown_keys_message(section, unknown, valid))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            model_cls.model_validate(data)
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            where = ".".join(str(part) for part in error.get("loc", ()))
            label = f"{section}.{where}" if where else section
            problems.append(f"{label}: {error.get('msg', 'invalid')}")
        raise ValueError("; ".join(problems)) from None


def _check_keys(section: str, data: Any, model_cls: type[BaseModel]) -> None:
    """Validate *data* against *model_cls*, rejecting unknown keys at any depth.

    Section models warn on unknown keys (for backward compatibility in
    Python code); in a manifest that warning becomes an error.
    """
    if not isinstance(data, dict):
        raise ValueError(f"{section}: must be a mapping, got {type(data).__name__}")
    unknown = sorted(set(data) - set(model_cls.model_fields))
    if unknown:
        raise ValueError(_unknown_keys_message(section, unknown, sorted(model_cls.model_fields)))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", FutureWarning)
        model_cls.model_validate(data)
    for warning in caught:
        text = str(warning.message)
        if issubclass(warning.category, FutureWarning) and "unknown key(s)" in text:
            detail = text.split(". Unknown keys will")[0].replace(" are ignored", "")
            raise ValueError(f"{section}: {detail}")
        # Anything else (e.g. a deprecation) is passed on.
        warnings.warn_explicit(warning.message, warning.category, warning.filename, warning.lineno)


def _server_spec(name: str, server_data: dict[str, Any]) -> HTTPServerSpec | StdioServerSpec:
    """Build the spec for one ``servers:`` entry, passing every key on.

    ``type`` names the transport (``http``, ``streamable-http``, ``sse``
    or ``stdio``); an explicit ``transport`` wins.  Unknown keys fail.
    """
    data = dict(server_data)
    kind = data.pop("type", None)
    try:
        if "url" in data or kind in _HTTP_TRANSPORTS:
            if kind not in (None, *_HTTP_TRANSPORTS):
                raise ValueError(f"type {kind!r} is not an HTTP transport {list(_HTTP_TRANSPORTS)}")
            if kind is not None:
                data.setdefault("transport", kind)
            return HTTPServerSpec.model_validate(data)
        if "command" in data:
            if kind not in (None, "stdio"):
                raise ValueError(f"type {kind!r} doesn't go with 'command' (use 'stdio')")
            if data.get("transport") == "stdio":
                data.pop("transport")
            return StdioServerSpec.model_validate(data)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'entry'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ValueError(f"servers.{name}: {detail}") from None
    except ValueError as exc:
        raise ValueError(f"servers.{name}: {exc}") from None
    raise ValueError(f"servers.{name}: needs 'url' (an HTTP server) or 'command' (a stdio server)")


def load_manifest(path: str | Path) -> AgentManifestSchema:
    """Load and validate a ``.agent`` manifest file.

    Args:
        path: Path to the ``.agent`` YAML file.

    Returns:
        Validated :class:`AgentManifestSchema`.

    Raises:
        ManifestError: If the file cannot be read or parsed.
        ManifestEnvVarError: A ``${VAR}`` names an unset environment
            variable (also an :class:`~promptise.exceptions.EnvVarNotFoundError`).
        ManifestValidationError: If schema validation fails.
    """
    path = Path(path)
    if not path.exists():
        raise ManifestError(f"Manifest not found: {path}")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"Cannot read manifest: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestError(f"Manifest must be a YAML mapping, got {type(data).__name__}")

    # Resolve environment variables, except secret values: those stay
    # ${VAR} references until the process starts (SecretScope resolves
    # them), so the loaded config never holds a secret in plain text.
    from promptise.exceptions import EnvVarNotFoundError

    try:
        data = _resolve_manifest_env(data)
    except EnvVarNotFoundError as exc:
        raise ManifestEnvVarError(str(exc), file_path=str(path)) from exc

    try:
        return AgentManifestSchema.model_validate(data)
    except ValidationError as exc:
        raise ManifestValidationError(
            f"Manifest validation failed: {exc}",
            errors=exc.errors(),
            file_path=str(path),
        ) from exc


def _resolve_manifest_env(data: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``${VAR}`` references in a manifest's data.

    Raises:
        EnvVarNotFoundError: A referenced variable is not set.
    """
    from promptise.env_resolver import (
        has_env_reference,
        resolve_env_in_dict,
        validate_all_env_vars_available,
    )
    from promptise.exceptions import EnvVarNotFoundError

    secret_templates: dict[str, Any] | None = None
    secrets_section = data.get("secrets")
    if isinstance(secrets_section, dict) and isinstance(secrets_section.get("secrets"), dict):
        secret_templates = secrets_section["secrets"]
        for secret_name, template in secret_templates.items():
            if isinstance(template, str) and has_env_reference(template):
                missing = validate_all_env_vars_available({"_": template})
                if missing:
                    raise EnvVarNotFoundError(missing[0], context=f"secrets.secrets.{secret_name}")
        data = {**data, "secrets": {**secrets_section, "secrets": {}}}

    # Hook env values stay ${VAR} references too: HookConfig resolves them
    # when the hook runs (a missing variable still fails here).
    hook_envs: dict[int, dict[str, Any]] = {}
    hooks_section = data.get("hooks")
    if isinstance(hooks_section, list):
        hooks_copy = list(hooks_section)
        for index, hook in enumerate(hooks_copy):
            if isinstance(hook, dict) and isinstance(hook.get("env"), dict):
                hook_envs[index] = hook["env"]
                for env_name, template in hook["env"].items():
                    if isinstance(template, str) and has_env_reference(template):
                        missing = validate_all_env_vars_available({"_": template})
                        if missing:
                            raise EnvVarNotFoundError(
                                missing[0], context=f"hooks[{index}].env.{env_name}"
                            )
                hooks_copy[index] = {**hook, "env": {}}
        data = {**data, "hooks": hooks_copy}

    data = resolve_env_in_dict(data)
    if secret_templates is not None:
        data["secrets"]["secrets"] = secret_templates
    for index, env in hook_envs.items():
        data["hooks"][index]["env"] = env
    return data


def save_manifest(manifest: AgentManifestSchema, path: str | Path) -> None:
    """Write a manifest to a YAML file.

    Args:
        manifest: The manifest to save.
        path: Destination file path.
    """
    path = Path(path)
    data = manifest.model_dump(mode="json", exclude_none=True)
    path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def validate_manifest(path: str | Path) -> list[str]:
    """Validate a manifest file, returning a list of warnings.

    Args:
        path: Path to the manifest file.

    Returns:
        List of warning messages (empty if fully valid).

    Raises:
        ManifestError: If the file cannot be loaded.
        ManifestValidationError: If the schema is invalid.
    """
    warnings: list[str] = []
    manifest = load_manifest(path)

    if not manifest.instructions:
        warnings.append("No instructions provided: agent will use default behavior")
    if not manifest.triggers:
        warnings.append("No triggers defined: agent will only respond to manual events")
    if not manifest.servers:
        warnings.append("No MCP servers configured: agent has no tools")

    return warnings


def manifest_to_process_config(
    manifest: AgentManifestSchema,
) -> ProcessConfig:
    """Convert a manifest into a :class:`ProcessConfig`.

    Args:
        manifest: Validated manifest schema.

    Returns:
        A :class:`ProcessConfig` ready to pass to :class:`AgentProcess`.
    Every section is passed on whole: each ``context:`` key sets the
    ContextConfig field of the same name, ``world:`` sets
    ``context.initial_state`` and ``memory:`` sets the ``memory_*``
    fields.  Unknown keys were rejected when the manifest was validated.
    """
    # Parse triggers
    triggers: list[TriggerConfig] = []
    for trigger_data in manifest.triggers:
        triggers.append(TriggerConfig.model_validate(trigger_data))

    # Journal.  Deliberately different from ProcessConfig (no journal):
    # a manifest describes a long-running process started by the CLI,
    # whose state should survive a restart and whose 'promptise runtime
    # logs' / 'rewind' need entries, so without a 'journal:' section it
    # gets MANIFEST_DEFAULT_JOURNAL (checkpoints in a file, recovered on
    # start).  'journal: {level: none}' turns it off.
    journal = MANIFEST_DEFAULT_JOURNAL.model_copy(deep=True)
    if manifest.journal:
        journal = JournalConfig.model_validate(manifest.journal)

    # Parse context
    context_kwargs: dict[str, Any] = {"initial_state": manifest.world}
    if manifest.memory:
        for key, field_name in _MEMORY_KEYS.items():
            if key in manifest.memory:
                context_kwargs[field_name] = manifest.memory[key]
    # Every ContextConfig field can be set in the 'context' block
    if manifest.context:
        context_kwargs.update(manifest.context)
        if not manifest.context.get("initial_state"):
            context_kwargs["initial_state"] = manifest.world

    context = ContextConfig(**context_kwargs)

    # Parse execution mode
    execution_mode = ExecutionMode.STRICT
    if manifest.execution_mode:
        execution_mode = ExecutionMode(manifest.execution_mode)

    # Parse open mode config
    open_mode = OpenModeConfig()
    if manifest.open_mode:
        open_mode = OpenModeConfig.model_validate(manifest.open_mode)

    # Parse servers — convert manifest dicts to ServerSpec instances
    servers: dict[str, HTTPServerSpec | StdioServerSpec | Any] = {}
    for server_name, server_data in manifest.servers.items():
        if isinstance(server_data, dict):
            servers[server_name] = _server_spec(server_name, server_data)
        else:
            servers[server_name] = server_data

    # Parse governance configs
    secrets_cfg = SecretScopeConfig()
    if manifest.secrets:
        secrets_cfg = SecretScopeConfig.model_validate(manifest.secrets)

    budget_cfg = BudgetConfig()
    if manifest.budget:
        budget_cfg = BudgetConfig.model_validate(manifest.budget)

    health_cfg = HealthConfig()
    if manifest.health:
        health_cfg = HealthConfig.model_validate(manifest.health)

    mission_cfg = MissionConfig()
    if manifest.mission:
        mission_cfg = MissionConfig.model_validate(manifest.mission)

    # Build the agent identity (env vars were already resolved in load_manifest).
    identity = None
    if manifest.identity:
        from promptise.superagent_schema import IdentityConfig

        identity = IdentityConfig.model_validate(manifest.identity).to_identity()

    # Build ProcessConfig with overrides from manifest.config
    config_data: dict[str, Any] = {
        "model": manifest.model,
        "instructions": manifest.instructions,
        "identity": identity,
        "agent_pattern": manifest.agent_pattern,
        "execution_mode": execution_mode.value,
        "open_mode": open_mode.model_dump(),
        "servers": servers,
        "triggers": [t.model_dump() for t in triggers],
        "journal": journal.model_dump(),
        "context": context.model_dump(),
        "secrets": secrets_cfg.model_dump(),
        "budget": budget_cfg.model_dump(),
        "health": health_cfg.model_dump(),
        "mission": mission_cfg.model_dump(),
    }
    if manifest.hooks:
        config_data["hooks"] = [HookConfig.model_validate(h) for h in manifest.hooks]

    # Apply any extra config overrides
    for key, value in manifest.config.items():
        config_data[key] = value

    return ProcessConfig.model_validate(config_data)
