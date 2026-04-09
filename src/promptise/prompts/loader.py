"""Load and save prompts as ``.prompt`` YAML files.

Prompts are **general-purpose templates** — model-agnostic and portable.
The model is a runtime concern chosen when the prompt is executed, not
baked into the file.

Supports:
- ``.prompt``, ``.prompt.yaml``, ``.prompt.yml`` extensions
- Environment variable resolution via ``${VAR}`` / ``${VAR:-default}``
- Single prompts and multi-prompt suites
- Loading from file, URL, or entire directory
- Saving existing prompts back to YAML

Example file (``analyze.prompt``)::

    name: analyze
    version: "1.0.0"
    description: Expert data analysis prompt
    author: team-data
    tags: [analysis, reporting]

    template: |
      Analyze the following data: {text}
      Focus area: {focus}

    arguments:
      text:
        description: The data to analyze
        required: true
      focus:
        description: Specific area to focus on
        required: false
        default: general

    strategy: chain_of_thought
    perspective: analyst
    constraints:
      - Must include confidence scores

Example usage::

    from promptise.prompts.loader import load_prompt, save_prompt

    prompt = load_prompt("prompts/analyze.prompt")
    result = await prompt(text="quarterly figures...")

    save_prompt(prompt, "prompts/exported.prompt", version="1.0.0")
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from ..env_resolver import resolve_env_in_dict
from ..exceptions import SuperAgentError

__all__ = [
    "PromptFileError",
    "PromptValidationError",
    "PromptFileSchema",
    "load_prompt",
    "load_url",
    "save_prompt",
    "load_directory",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PromptFileError(SuperAgentError):
    """Error loading or saving a ``.prompt`` file."""


class PromptValidationError(PromptFileError):
    """Schema validation failed for a ``.prompt`` file."""

    def __init__(
        self,
        message: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        file_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.errors = errors or []
        self.file_path = file_path


# ---------------------------------------------------------------------------
# Schema (Pydantic models)
# ---------------------------------------------------------------------------


class ArgumentSchema(BaseModel):
    """Schema for a single prompt argument."""

    description: str = ""
    required: bool = True
    default: str | None = None


class GuardSchema(BaseModel):
    """Schema for a single guard definition.

    The ``type`` field selects the guard constructor; remaining fields
    are passed as keyword arguments.
    """

    model_config = ConfigDict(extra="allow")

    type: str


class PromptFileSchema(BaseModel):
    """Pydantic schema for ``.prompt`` YAML files.

    All metadata fields (name, version, description, author, tags) are
    first-class top-level fields.
    """

    model_config = ConfigDict(extra="forbid")

    # Identity & metadata
    name: str
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    tags: list[str] = []

    # Template (optional for suites — suites define templates per-prompt)
    template: str = ""

    # Arguments
    arguments: dict[str, ArgumentSchema] = {}

    # Pipeline components
    strategy: str | list[str] | None = None
    perspective: str | dict[str, str] | None = None
    constraints: list[str] = []
    guards: list[GuardSchema] = []
    world: dict[str, dict[str, Any]] = {}

    # Optional runtime defaults
    model: str | None = None
    observe: bool = False
    return_type: str | None = None

    # Suite fields
    suite: bool = False
    defaults: dict[str, Any] | None = None
    prompts: dict[str, dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _validate_template_or_suite(self) -> PromptFileSchema:
        """Require ``template`` for single prompts; require ``prompts`` for suites."""
        if self.suite:
            if not self.prompts:
                raise ValueError("Suite files must define 'prompts'")
        else:
            if not self.template:
                raise ValueError("Non-suite files must define 'template'")
        return self


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

# Lazy-initialised to avoid circular imports.  Populated on first use
# by ``_ensure_lookups()``.

_STRATEGY_MAP: dict[str, Any] | None = None
_PERSPECTIVE_MAP: dict[str, Any] | None = None
_STRATEGY_REVERSE: dict[type, str] | None = None
_PERSPECTIVE_REVERSE: dict[type, str] | None = None
_GUARD_CONSTRUCTORS: dict[str, tuple[type, dict[str, str]]] | None = None
_GUARD_ATTRS: dict[type, tuple[str, dict[str, str]]] | None = None
_WORLD_CONTEXT_TYPES: dict[str, type] | None = None


def _ensure_lookups() -> None:
    """Populate lookup tables on first use (avoids circular imports)."""
    global _STRATEGY_MAP, _PERSPECTIVE_MAP, _STRATEGY_REVERSE
    global _PERSPECTIVE_REVERSE, _GUARD_CONSTRUCTORS, _GUARD_ATTRS
    global _WORLD_CONTEXT_TYPES

    if _STRATEGY_MAP is not None:
        return

    from .context import (
        BaseContext,
        ConversationContext,
        EnvironmentContext,
        ErrorContext,
        OutputContext,
        TeamContext,
        UserContext,
    )
    from .guards import (
        ContentFilterGuard,
        LengthGuard,
        SchemaStrictGuard,
    )
    from .strategies import (
        AdvisorPerspective,
        AnalystPerspective,
        ChainOfThoughtStrategy,
        CreativePerspective,
        CriticPerspective,
        CustomPerspective,
        DecomposeStrategy,
        PlanAndExecuteStrategy,
        SelfCritiqueStrategy,
        StructuredReasoningStrategy,
        advisor,
        analyst,
        chain_of_thought,
        creative,
        critic,
        decompose,
        plan_and_execute,
        self_critique,
        structured_reasoning,
    )

    _STRATEGY_MAP = {
        "chain_of_thought": chain_of_thought,
        "structured_reasoning": structured_reasoning,
        "self_critique": self_critique,
        "plan_and_execute": plan_and_execute,
        "decompose": decompose,
    }

    _PERSPECTIVE_MAP = {
        "analyst": analyst,
        "critic": critic,
        "advisor": advisor,
        "creative": creative,
    }

    _STRATEGY_REVERSE = {
        ChainOfThoughtStrategy: "chain_of_thought",
        StructuredReasoningStrategy: "structured_reasoning",
        SelfCritiqueStrategy: "self_critique",
        PlanAndExecuteStrategy: "plan_and_execute",
        DecomposeStrategy: "decompose",
    }

    _PERSPECTIVE_REVERSE = {
        AnalystPerspective: "analyst",
        CriticPerspective: "critic",
        AdvisorPerspective: "advisor",
        CreativePerspective: "creative",
        CustomPerspective: "custom",
    }

    _GUARD_CONSTRUCTORS = {
        "content_filter": (ContentFilterGuard, {"blocked": "blocked", "required": "required"}),
        "length": (LengthGuard, {"min_length": "min_length", "max_length": "max_length"}),
        "schema_strict": (SchemaStrictGuard, {}),
    }

    # Maps guard class → (type_name, {yaml_key: actual_attribute_name})
    # Used by _guards_to_list() for serialization. Attribute names must match
    # the actual instance attributes in guards.py (not the constructor params).
    _GUARD_ATTRS = {
        ContentFilterGuard: ("content_filter", {"blocked": "_blocked", "required": "_required"}),
        LengthGuard: ("length", {"min_length": "_min", "max_length": "_max"}),
        SchemaStrictGuard: ("schema_strict", {}),
    }

    _WORLD_CONTEXT_TYPES = {
        "UserContext": UserContext,
        "EnvironmentContext": EnvironmentContext,
        "ConversationContext": ConversationContext,
        "TeamContext": TeamContext,
        "ErrorContext": ErrorContext,
        "OutputContext": OutputContext,
        "BaseContext": BaseContext,
    }


# ---------------------------------------------------------------------------
# Internal: schema → Prompt conversion
# ---------------------------------------------------------------------------

_RETURN_TYPE_MAP: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
}

_VALID_EXTENSIONS = {".prompt"}
_VALID_SUFFIXES = {".prompt.yaml", ".prompt.yml"}


def _is_prompt_file(path: Path) -> bool:
    """Check if *path* has a valid ``.prompt`` extension."""
    name = path.name
    if any(name.endswith(s) for s in _VALID_SUFFIXES):
        return True
    return path.suffix in _VALID_EXTENSIONS


def _resolve_strategy(value: str | list[str]) -> Any:
    """Resolve a strategy name (or list for composite) to a Strategy object."""
    _ensure_lookups()
    assert _STRATEGY_MAP is not None

    if isinstance(value, list):
        strategies = []
        for name in value:
            if name not in _STRATEGY_MAP:
                raise PromptFileError(
                    f"Unknown strategy: {name!r}. Available: {list(_STRATEGY_MAP.keys())}"
                )
            strategies.append(_STRATEGY_MAP[name])
        # Compose via + operator
        result = strategies[0]
        for s in strategies[1:]:
            result = result + s
        return result

    if value not in _STRATEGY_MAP:
        raise PromptFileError(
            f"Unknown strategy: {value!r}. Available: {list(_STRATEGY_MAP.keys())}"
        )
    return _STRATEGY_MAP[value]


def _resolve_perspective(value: str | dict[str, str]) -> Any:
    """Resolve a perspective name or custom dict to a Perspective object."""
    _ensure_lookups()
    assert _PERSPECTIVE_MAP is not None

    if isinstance(value, dict):
        from .strategies import CustomPerspective

        role = value.get("role", "")
        instructions = value.get("instructions", "")
        if not role:
            raise PromptFileError("Custom perspective requires a 'role' field")
        return CustomPerspective(role=role, instructions=instructions)

    if value not in _PERSPECTIVE_MAP:
        raise PromptFileError(
            f"Unknown perspective: {value!r}. Available: {list(_PERSPECTIVE_MAP.keys())}"
        )
    return _PERSPECTIVE_MAP[value]


def _resolve_guard(guard_schema: GuardSchema) -> Any:
    """Resolve a guard schema to a Guard instance."""
    _ensure_lookups()
    assert _GUARD_CONSTRUCTORS is not None

    guard_type = guard_schema.type
    if guard_type not in _GUARD_CONSTRUCTORS:
        raise PromptFileError(
            f"Unknown guard type: {guard_type!r}. Available: {list(_GUARD_CONSTRUCTORS.keys())}"
        )

    cls, param_map = _GUARD_CONSTRUCTORS[guard_type]
    # Extract extra fields from the schema (everything except 'type')
    extra = guard_schema.model_dump(exclude={"type"})
    # Map YAML field names to constructor parameter names
    kwargs: dict[str, Any] = {}
    for yaml_key, ctor_key in param_map.items():
        if yaml_key in extra:
            kwargs[ctor_key] = extra[yaml_key]
    return cls(**kwargs)


def _resolve_world(world_dict: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Resolve world context definitions to BaseContext instances."""
    _ensure_lookups()
    assert _WORLD_CONTEXT_TYPES is not None

    from .context import BaseContext

    result: dict[str, Any] = {}
    for key, fields in world_dict.items():
        fields = dict(fields)  # copy
        type_name = fields.pop("type", None)
        if type_name and type_name in _WORLD_CONTEXT_TYPES:
            ctx_cls = _WORLD_CONTEXT_TYPES[type_name]
        else:
            ctx_cls = BaseContext
        result[key] = ctx_cls(**fields)
    return result


def _schema_to_prompt(schema: PromptFileSchema) -> Any:
    """Convert a validated :class:`PromptFileSchema` to a :class:`Prompt`.

    Uses :class:`PromptBuilder` internally to construct the prompt from
    the parsed schema fields.
    """
    from .builder import PromptBuilder

    builder = PromptBuilder(schema.name)
    builder.template(schema.template)

    # Model — use a default placeholder if not specified
    if schema.model:
        builder.model(schema.model)
    # else: builder already defaults to "openai:gpt-5-mini"

    builder.observe(schema.observe)

    # Return type
    if schema.return_type:
        if schema.return_type not in _RETURN_TYPE_MAP:
            raise PromptFileError(
                f"Unknown return_type: {schema.return_type!r}. "
                f"Available: {list(_RETURN_TYPE_MAP.keys())}"
            )
        builder.output_type(_RETURN_TYPE_MAP[schema.return_type])

    # Strategy
    if schema.strategy:
        builder.strategy(_resolve_strategy(schema.strategy))

    # Perspective
    if schema.perspective:
        builder.perspective(_resolve_perspective(schema.perspective))

    # Constraints
    if schema.constraints:
        builder.constraint(*schema.constraints)

    # Guards
    for guard_schema in schema.guards:
        builder.guard(_resolve_guard(guard_schema))

    # World contexts
    if schema.world:
        world_contexts = _resolve_world(schema.world)
        builder.world(**world_contexts)

    # Build the prompt
    p = builder.build()

    # Override the signature with explicit argument definitions if provided
    if schema.arguments:
        params: list[inspect.Parameter] = []
        for arg_name, arg_schema in schema.arguments.items():
            if arg_schema.required:
                param = inspect.Parameter(
                    arg_name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            else:
                param = inspect.Parameter(
                    arg_name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=arg_schema.default or "",
                )
            params.append(param)
        p._sig = inspect.Signature(params)

    # Store metadata directly on the Prompt
    p._description = schema.description
    p._author = schema.author
    p._tags = list(schema.tags)
    p._version = schema.version
    p._argument_schemas = dict(schema.arguments)

    return p


def _schema_to_suite(schema: PromptFileSchema) -> Any:
    """Convert a suite schema to a :class:`PromptSuite`."""
    from .core import Prompt
    from .suite import PromptSuite

    if not schema.prompts:
        raise PromptFileError("Suite file must have a 'prompts' section")

    defaults = schema.defaults or {}

    # Build each sub-prompt
    prompt_objects: dict[str, Prompt] = {}
    for pname, pdata in schema.prompts.items():
        # Merge defaults into sub-prompt data (sub-prompt overrides)
        merged = dict(defaults)
        merged.update(pdata)
        merged.setdefault("name", pname)

        # Inherit suite-level metadata if not set per-prompt
        merged.setdefault("description", "")
        merged.setdefault("author", schema.author)
        merged.setdefault("tags", list(schema.tags))

        # Must have template
        if "template" not in merged:
            raise PromptFileError(f"Suite prompt {pname!r} missing 'template' field")

        sub_schema = PromptFileSchema.model_validate(merged)
        prompt_objects[pname] = _schema_to_prompt(sub_schema)

    # Create a dynamic PromptSuite subclass with the prompts as class attrs.
    # Note: we do NOT set default_strategy / default_perspective /
    # default_constraints on the class because the merge step above already
    # applied defaults into each sub-prompt.  Setting them here would cause
    # __init_subclass__ → _apply_suite_defaults() to duplicate them.
    attrs: dict[str, Any] = {}
    for pname, p in prompt_objects.items():
        attrs[pname] = p

    suite_cls = type(schema.name, (PromptSuite,), attrs)
    suite = suite_cls()

    # Store metadata
    suite._description = schema.description
    suite._author = schema.author
    suite._tags = list(schema.tags)

    return suite


# ---------------------------------------------------------------------------
# Internal: Prompt → schema conversion (for save)
# ---------------------------------------------------------------------------


def _prompt_to_dict(
    p: Any,
    *,
    version: str | None = None,
    author: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Convert a :class:`Prompt` to a YAML-serializable dict."""
    _ensure_lookups()
    assert _STRATEGY_REVERSE is not None
    assert _PERSPECTIVE_REVERSE is not None

    data: dict[str, Any] = {"name": p.name}

    # Version
    ver = version or getattr(p, "_version", None) or "0.0.0"
    data["version"] = ver

    # Metadata
    desc = description or getattr(p, "_description", "") or ""
    if desc:
        data["description"] = desc
    auth = author or getattr(p, "_author", "") or ""
    if auth:
        data["author"] = auth
    t = tags or getattr(p, "_tags", None) or []
    if t:
        data["tags"] = list(t)

    # Template
    data["template"] = p.template

    # Arguments from signature (prefer stored ArgumentSchema for descriptions)
    stored_schemas: dict[str, Any] = getattr(p, "_argument_schemas", {})
    if p._sig.parameters:
        args: dict[str, dict[str, Any]] = {}
        for pname, param in p._sig.parameters.items():
            if pname in stored_schemas:
                schema = stored_schemas[pname]
                arg_data: dict[str, Any] = {"description": schema.description or pname}
                arg_data["required"] = schema.required
                if schema.default is not None:
                    arg_data["default"] = schema.default
            else:
                arg_data = {"description": pname}
                if param.default is inspect.Parameter.empty:
                    arg_data["required"] = True
                else:
                    arg_data["required"] = False
                    if param.default != "":
                        arg_data["default"] = str(param.default)
            args[pname] = arg_data
        data["arguments"] = args

    # Strategy
    if p._strategy is not None:
        from .strategies import CompositeStrategy

        if isinstance(p._strategy, CompositeStrategy):
            names = []
            for s in p._strategy._strategies:
                stype = type(s)
                if stype in _STRATEGY_REVERSE:
                    names.append(_STRATEGY_REVERSE[stype])
            if names:
                data["strategy"] = names
        else:
            stype = type(p._strategy)
            if stype in _STRATEGY_REVERSE:
                data["strategy"] = _STRATEGY_REVERSE[stype]

    # Perspective
    if p._perspective is not None:
        from .strategies import CustomPerspective

        if isinstance(p._perspective, CustomPerspective):
            data["perspective"] = {
                "role": p._perspective.role,
                "instructions": p._perspective.instructions,
            }
        else:
            ptype = type(p._perspective)
            if ptype in _PERSPECTIVE_REVERSE:
                data["perspective"] = _PERSPECTIVE_REVERSE[ptype]

    # Constraints
    if p._constraints:
        data["constraints"] = list(p._constraints)

    # Guards
    if p._input_guards:
        guards_data = _guards_to_list(p._input_guards)
        if guards_data:
            data["guards"] = guards_data

    # World
    if p._world:
        world_data: dict[str, dict[str, Any]] = {}
        for key, ctx in p._world.items():
            ctx_dict = ctx.to_dict()
            ctx_type = type(ctx).__name__
            if ctx_type != "BaseContext":
                ctx_dict["type"] = ctx_type
            world_data[key] = ctx_dict
        data["world"] = world_data

    # Model — only include if explicitly set (not the builder default)
    if p.model and p.model != "openai:gpt-5-mini":
        data["model"] = p.model

    return data


def _guards_to_list(guards: list[Any]) -> list[dict[str, Any]]:
    """Convert guard instances to YAML-serializable dicts."""
    _ensure_lookups()
    assert _GUARD_ATTRS is not None

    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for g in guards:
        # Deduplicate (guards are added to both input and output lists)
        gid = id(g)
        if gid in seen:
            continue
        seen.add(gid)

        gcls = type(g)
        if gcls not in _GUARD_ATTRS:
            continue
        type_name, attr_map = _GUARD_ATTRS[gcls]
        entry: dict[str, Any] = {"type": type_name}
        for yaml_key, attr_name in attr_map.items():
            val = getattr(g, attr_name, None)
            if val is not None:
                entry[yaml_key] = val
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_prompt(
    path: str | Path,
    *,
    register: bool = False,
) -> Any:
    """Load a prompt from a ``.prompt`` YAML file.

    Args:
        path: Path to the ``.prompt`` file.
        register: If ``True``, register the prompt in the global
            :data:`~promptise.prompts.registry.registry` with its
            name and version.

    Returns:
        A :class:`~promptise.prompts.core.Prompt` instance, or a
        :class:`~promptise.prompts.suite.PromptSuite` when the file
        has ``suite: true``.

    Raises:
        PromptFileError: File not found, invalid format, or parse error.
        PromptValidationError: Schema validation failed.

    Example::

        prompt = load_prompt("prompts/analyze.prompt")
        result = await prompt(text="quarterly figures...")
    """
    filepath = Path(path).resolve()

    if not filepath.exists():
        raise PromptFileError(f"File not found: {filepath}")

    if not _is_prompt_file(filepath):
        raise PromptFileError(
            f"Invalid file extension. Expected .prompt, "
            f".prompt.yaml, or .prompt.yml. Got: {filepath.name}"
        )

    # Load YAML
    try:
        with filepath.open("r", encoding="utf-8") as f:
            raw_data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise PromptFileError(f"YAML parse error in {filepath}: {exc}") from exc

    if not isinstance(raw_data, dict):
        raise PromptFileError(
            f"Invalid YAML format in {filepath}: expected dict, got {type(raw_data).__name__}"
        )

    # Resolve environment variables
    raw_data = resolve_env_in_dict(raw_data)

    # Validate schema
    try:
        schema = PromptFileSchema.model_validate(raw_data)
    except ValidationError as exc:
        raise PromptValidationError(
            f"Schema validation failed for {filepath}",
            errors=[dict(e) for e in exc.errors()],
            file_path=str(filepath),
        ) from exc

    # Build prompt or suite
    if schema.suite:
        result = _schema_to_suite(schema)
    else:
        result = _schema_to_prompt(schema)

    # Register if requested
    if register and not schema.suite:
        from .registry import registry as global_registry

        global_registry.register(schema.name, schema.version, result)

    return result


async def load_url(
    url: str,
    *,
    register: bool = False,
) -> Any:
    """Load a prompt from a URL (e.g. GitHub raw file).

    Args:
        url: HTTP(S) URL pointing to a ``.prompt`` YAML file.
        register: If ``True``, register in the global registry.

    Returns:
        A :class:`~promptise.prompts.core.Prompt` or
        :class:`~promptise.prompts.suite.PromptSuite`.

    Raises:
        PromptFileError: HTTP error or parse error.

    Example::

        prompt = await load_url(
            "https://raw.githubusercontent.com/org/prompts/main/analyze.prompt"
        )
    """
    try:
        import httpx
    except ImportError as exc:
        raise PromptFileError(
            "httpx is required for load_url(). Install it with: pip install httpx"
        ) from exc

    # SSRF protection: reject private/internal URLs
    import asyncio
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    try:
        loop = asyncio.get_running_loop()
        addr_info = await loop.run_in_executor(None, socket.getaddrinfo, hostname, None)
        for _, _, _, _, sockaddr in addr_info:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                raise PromptFileError(
                    f"URL {url!r} resolves to private/internal address {ip}. "
                    "Refusing to fetch for security reasons."
                )
    except socket.gaierror as exc:
        raise PromptFileError(f"Cannot resolve hostname for {url!r}: {exc}") from exc

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text
    except Exception as exc:
        raise PromptFileError(f"Failed to fetch {url}: {exc}") from exc

    # Parse YAML
    try:
        raw_data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PromptFileError(f"YAML parse error from {url}: {exc}") from exc

    if not isinstance(raw_data, dict):
        raise PromptFileError(
            f"Invalid YAML from {url}: expected dict, got {type(raw_data).__name__}"
        )

    # Resolve env vars
    raw_data = resolve_env_in_dict(raw_data)

    # Validate
    try:
        schema = PromptFileSchema.model_validate(raw_data)
    except ValidationError as exc:
        raise PromptValidationError(
            f"Schema validation failed for {url}",
            errors=[dict(e) for e in exc.errors()],
            file_path=url,
        ) from exc

    # Build
    if schema.suite:
        result = _schema_to_suite(schema)
    else:
        result = _schema_to_prompt(schema)

    # Register
    if register and not schema.suite:
        from .registry import registry as global_registry

        global_registry.register(schema.name, schema.version, result)

    return result


def save_prompt(
    prompt: Any,
    path: str | Path,
    *,
    version: str | None = None,
    author: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> None:
    """Save a :class:`~promptise.prompts.core.Prompt` to a YAML file.

    Args:
        prompt: The Prompt instance to save.
        path: Destination file path.
        version: Version string (overrides prompt's stored version).
        author: Author name (overrides prompt's stored author).
        description: Description (overrides prompt's stored description).
        tags: Tags list (overrides prompt's stored tags).

    Example::

        save_prompt(my_prompt, "prompts/analyze.prompt",
                    version="2.0.0", author="data-team")
    """
    data = _prompt_to_dict(
        prompt,
        version=version,
        author=author,
        description=description,
        tags=tags,
    )

    filepath = Path(path)
    with filepath.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def load_directory(
    path: str | Path,
    *,
    register: bool = False,
) -> Any:
    """Load all ``.prompt`` files from a directory into a registry.

    Args:
        path: Directory path to scan (recursively).
        register: If ``True``, also register each prompt in the global registry.

    Returns:
        A :class:`~promptise.prompts.registry.PromptRegistry` containing
        all loaded prompts.

    Example::

        registry = load_directory("prompts/")
    """
    from .registry import PromptRegistry

    dirpath = Path(path)
    if not dirpath.is_dir():
        raise PromptFileError(f"Directory not found: {dirpath}")

    reg = PromptRegistry()
    patterns = ["**/*.prompt", "**/*.prompt.yaml", "**/*.prompt.yml"]

    for pattern in patterns:
        for filepath in sorted(dirpath.glob(pattern)):
            if not filepath.is_file():
                continue
            loaded = load_prompt(filepath, register=register)

            # Only register non-suite prompts
            if hasattr(loaded, "_version"):
                ver = loaded._version or "0.0.0"
                try:
                    reg.register(loaded.name, ver, loaded)
                except ValueError:
                    pass  # skip duplicates

    return reg
