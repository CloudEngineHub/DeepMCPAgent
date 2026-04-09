"""Pydantic models for .superagent YAML schema validation.

This module defines the complete schema for .superagent configuration files,
including support for both simple and detailed model configurations, server
specifications, cross-agent references, and environment variable resolution.

The schema uses Pydantic v2 for strict validation and supports discriminated
unions for type-safe server configuration.
"""

from __future__ import annotations

import warnings
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# =============================================================================
# Model Configuration Types
# =============================================================================


class DetailedModelConfig(BaseModel):
    """Detailed model configuration with provider-specific parameters.

    This configuration format allows fine-grained control over model
    initialization, including API keys, temperature, token limits, and
    provider-specific parameters.

    Attributes:
        provider: Model provider (e.g., "openai", "anthropic", "ollama").
        name: Model name/ID (e.g., "gpt-4.1", "claude-opus-4.5").
        api_key: Optional API key (supports ${ENV_VAR} syntax).
        temperature: Optional temperature parameter (0.0-2.0).
        max_tokens: Optional maximum tokens for generation.
        timeout: Optional request timeout in seconds.
        base_url: Optional custom API base URL.
        extra: Additional provider-specific parameters.

    Examples:
        >>> config = DetailedModelConfig(
        ...     provider="openai",
        ...     name="gpt-4.1",
        ...     api_key="${OPENAI_API_KEY}",
        ...     temperature=0.7
        ... )
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(..., description="Model provider name")
    name: str = Field(..., description="Model name or ID")
    api_key: str | None = Field(None, description="API key (supports ${ENV_VAR})")
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(None, gt=0)
    timeout: int | None = Field(None, gt=0, description="Request timeout in seconds")
    base_url: str | None = Field(None, description="Custom API base URL")
    extra: dict[str, Any] = Field(
        default_factory=dict, description="Additional provider-specific parameters"
    )

    @field_validator("api_key")
    @classmethod
    def warn_direct_key(cls, v: str | None) -> str | None:
        """Warn if API key appears to be a direct value (not env var)."""
        if (
            v
            and not v.startswith("${")
            and (v.startswith("sk-") or v.startswith("pk-") or len(v) > 20)
        ):
            warnings.warn(
                "Direct API key detected in config. Consider using ${ENV_VAR} syntax for security.",
                UserWarning,
                stacklevel=2,
            )
        return v


# Simple form: just a string like "openai:gpt-4.1"
ModelConfig = str | DetailedModelConfig
"""Union type supporting both simple string and detailed model configuration.

Simple form:
    model: "openai:gpt-4.1"

Detailed form:
    model:
      provider: openai
      name: gpt-4.1
      temperature: 0.7
"""


# =============================================================================
# Server Configuration Types
# =============================================================================


class HTTPServerConfig(BaseModel):
    """HTTP/SSE MCP server configuration.

    Configuration for remote MCP servers accessible via HTTP, streamable HTTP,
    or Server-Sent Events (SSE) transports.

    Attributes:
        type: Always "http" for this variant (discriminator field).
        url: Full endpoint URL (supports ${ENV_VAR}).
        transport: Transport protocol ("http", "streamable-http", "sse").
        headers: Optional HTTP headers (values support ${ENV_VAR}).
        auth: Optional auth token (supports ${ENV_VAR}).

    Examples:
        >>> server = HTTPServerConfig(
        ...     type="http",
        ...     url="http://127.0.0.1:8000/mcp",
        ...     headers={"Authorization": "Bearer ${API_TOKEN}"}
        ... )
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["http"] = Field("http", description="Server type discriminator")
    url: str = Field(..., description="Full MCP endpoint URL")
    transport: Literal["http", "streamable-http", "sse"] = Field(
        "http", description="Transport protocol"
    )
    headers: dict[str, str] = Field(
        default_factory=dict, description="HTTP headers (values support ${ENV_VAR})"
    )
    auth: str | None = Field(None, description="Auth token (supports ${ENV_VAR})")


class StdioServerConfig(BaseModel):
    """Stdio (local process) MCP server configuration.

    Configuration for local MCP servers that communicate via standard
    input/output, typically a subprocess launched by the agent.

    Attributes:
        type: Always "stdio" for this variant (discriminator field).
        command: Executable command to launch.
        args: Command-line arguments.
        env: Environment variables (values support ${ENV_VAR}).
        cwd: Optional working directory.
        keep_alive: Whether to maintain persistent connection.

    Examples:
        >>> server = StdioServerConfig(
        ...     type="stdio",
        ...     command="python",
        ...     args=["-m", "mypkg.server"],
        ...     env={"API_KEY": "${MY_API_KEY}"}
        ... )
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["stdio"] = Field("stdio", description="Server type discriminator")
    command: str = Field(..., description="Executable command")
    args: list[str] = Field(default_factory=list, description="Command arguments")
    env: dict[str, str] = Field(
        default_factory=dict, description="Environment variables (values support ${ENV_VAR})"
    )
    cwd: str | None = Field(None, description="Working directory")
    keep_alive: bool = Field(True, description="Maintain persistent connection")


ServerConfig = Annotated[HTTPServerConfig | StdioServerConfig, Field(discriminator="type")]
"""Union type for server configurations with discriminated union on 'type' field.

The 'type' field ("http" or "stdio") determines which server configuration
variant is used. Pydantic automatically validates the correct fields based
on this discriminator.
"""


# =============================================================================
# Cross-Agent Configuration
# =============================================================================


class CrossAgentConfig(BaseModel):
    """Cross-agent reference configuration.

    Defines a reference to another agent's .superagent configuration file,
    allowing multi-agent coordination and delegation.

    Attributes:
        file: Path to referenced .superagent file (relative to current file).
        description: Human-readable description for tool discovery.

    Examples:
        >>> config = CrossAgentConfig(
        ...     file="./agents/math_specialist.superagent",
        ...     description="Specialized math and calculation agent"
        ... )
    """

    model_config = ConfigDict(extra="forbid")

    file: str = Field(..., description="Path to .superagent file")
    description: str = Field("", description="Agent description for tool discovery")


# =============================================================================
# Top-Level Agent Configuration
# =============================================================================


class AgentSection(BaseModel):
    """Agent-level configuration section.

    Defines the core agent configuration including model selection,
    system prompt, and tool tracing settings.

    Attributes:
        model: Model configuration (simple string or detailed object).
        instructions: Optional system prompt override.
        trace: Enable tool invocation tracing.

    Examples:
        >>> agent = AgentSection(
        ...     model="openai:gpt-4.1",
        ...     instructions="You are a helpful assistant.",
        ...     trace=True
        ... )
    """

    model_config = ConfigDict(extra="forbid")

    model: ModelConfig = Field(..., description="Model configuration")
    instructions: str | None = Field(None, description="System prompt override")
    trace: bool = Field(True, description="Enable tool tracing")


class SandboxConfigSection(BaseModel):
    """Sandbox configuration section for .superagent files.

    Attributes:
        backend: Container backend (docker, gvisor).
        image: Base container image.
        cpu_limit: Maximum CPU cores.
        memory_limit: Maximum memory (e.g., "4G").
        disk_limit: Maximum disk space (e.g., "10G").
        network: Network isolation mode (none, restricted, full).
        persistent: Keep workspace between runs.
        timeout: Max execution time in seconds.
        tools: Pre-installed tools list.
        workdir: Working directory inside container.
        env: Additional environment variables.
        allow_sudo: Allow sudo access in container.

    Examples:
        >>> config = SandboxConfigSection(
        ...     backend="gvisor",
        ...     cpu_limit=2,
        ...     memory_limit="4G"
        ... )
    """

    model_config = ConfigDict(extra="forbid")

    backend: Literal["docker", "gvisor"] = Field("docker", description="Container backend")
    image: str = Field("python:3.11-slim", description="Base container image")
    cpu_limit: int = Field(2, gt=0, le=32, description="Maximum CPU cores")
    memory_limit: str = Field("4G", description="Maximum memory")
    disk_limit: str = Field("10G", description="Maximum disk space")
    network: Literal["none", "restricted", "full"] = Field(
        "restricted", description="Network isolation mode"
    )
    persistent: bool = Field(False, description="Keep workspace between runs")
    timeout: int = Field(300, gt=0, le=3600, description="Max execution time in seconds")
    tools: list[str] = Field(default_factory=lambda: ["python"], description="Pre-installed tools")
    workdir: str = Field("/workspace", description="Working directory")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables")
    allow_sudo: bool = Field(False, description="Allow sudo access")


class MemorySection(BaseModel):
    """Memory configuration section for .superagent files.

    Attributes:
        provider: Memory provider type (``"in_memory"``, ``"chroma"``, ``"mem0"``).
        collection: ChromaDB collection name (chroma provider).
        persist_directory: ChromaDB persistence path (chroma provider).
        user_id: Mem0 user scope (mem0 provider).
        agent_id: Mem0 agent scope (mem0 provider).

    Examples:
        >>> config = MemorySection(provider="chroma", persist_directory=".promptise/chroma")
        >>> config = MemorySection(provider="mem0", user_id="user-123")
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["in_memory", "chroma", "mem0"] = Field(
        "in_memory", description="Memory provider type"
    )
    # ChromaDB options
    collection: str = Field("agent_memory", description="ChromaDB collection name")
    persist_directory: str | None = Field(None, description="ChromaDB persistence path")
    # Mem0 options
    user_id: str = Field("default", description="Mem0 user scope")
    agent_id: str | None = Field(None, description="Mem0 agent scope")
    # Legacy alias
    backend: str | None = Field(None, description="Deprecated: use 'provider' instead")
    path: str | None = Field(None, description="Deprecated: use provider-specific options")


class ObservabilitySection(BaseModel):
    """Observability configuration section for .superagent files.

    Attributes:
        level: Detail level (off, basic, standard, full).
        session_name: Human-readable session identifier.
        record_prompts: Store full prompt/response text.
        transporters: List of transporter type strings.
        output_dir: Directory for HTML and JSON output.
        log_file: File path for structured log transporter.
        console_live: Real-time console printing.
        webhook_url: Target URL for webhook transporter.
        prometheus_port: Port for Prometheus metrics.
        otlp_endpoint: gRPC endpoint for OpenTelemetry.
        correlation_id: External correlation ID.
    """

    model_config = ConfigDict(extra="forbid")

    level: Literal["off", "basic", "standard", "full"] = Field(
        "standard", description="Detail level"
    )
    session_name: str = Field("promptise", description="Session identifier")
    record_prompts: bool = Field(False, description="Store full prompt/response text")
    transporters: list[str] = Field(
        default_factory=lambda: ["html"], description="Transporter types"
    )
    output_dir: str | None = Field(None, description="Output directory")
    log_file: str | None = Field(None, description="Structured log file path")
    console_live: bool = Field(False, description="Real-time console output")
    webhook_url: str | None = Field(None, description="Webhook transporter URL")
    prometheus_port: int = Field(9090, description="Prometheus metrics port")
    otlp_endpoint: str = Field("http://localhost:4317", description="OpenTelemetry endpoint")
    correlation_id: str | None = Field(None, description="External correlation ID")


class ToolOptimizationSection(BaseModel):
    """Tool optimization configuration for .superagent files.

    Attributes:
        level: Optimization level (none, minimal, standard, aggressive, semantic).
        embedding_model: Sentence-transformers model name or local path.
        top_k: Number of tools to select per query (semantic mode).
        score_threshold: Minimum similarity score (semantic mode).
    """

    model_config = ConfigDict(extra="forbid")

    level: Literal["minimal", "standard", "semantic"] = Field(
        "semantic", description="Optimization level"
    )
    embedding_model: str = Field(
        "all-MiniLM-L6-v2", description="Embedding model name or local path"
    )
    top_k: int = Field(10, gt=0, description="Tools to select per query")
    score_threshold: float = Field(0.1, ge=0.0, le=1.0, description="Min similarity")


class CacheSection(BaseModel):
    """Semantic cache configuration for .superagent files.

    Attributes:
        backend: Cache backend — ``"memory"`` (default) or ``"redis"``.
        redis_url: Redis connection URL (required when backend is ``"redis"``).
        similarity_threshold: Minimum cosine similarity for a cache hit (0.0–1.0).
        default_ttl: Default time-to-live in seconds for cached entries.
        scope: Cache isolation — ``"per_user"``, ``"per_session"``, or ``"shared"``.
        max_entries_per_user: Maximum cached entries per user scope.
        embedding_model: Sentence-transformers model name or local path.
        encrypt_values: Encrypt cached values at rest (Redis backend).
    """

    model_config = ConfigDict(extra="forbid")

    backend: Literal["memory", "redis"] = Field("memory", description="Cache backend")
    redis_url: str | None = Field(None, description="Redis connection URL")
    similarity_threshold: float = Field(
        0.92, ge=0.0, le=1.0, description="Min similarity for cache hit"
    )
    default_ttl: int = Field(3600, gt=0, description="Default TTL in seconds")
    scope: Literal["per_user", "per_session", "shared"] = Field(
        "per_user", description="Cache isolation scope"
    )
    max_entries_per_user: int = Field(1000, gt=0, description="Max entries per user")
    embedding_model: str = Field(
        "all-MiniLM-L6-v2", description="Embedding model name or local path"
    )
    encrypt_values: bool = Field(False, description="Encrypt values at rest")


class ApprovalSection(BaseModel):
    """Human-in-the-loop approval configuration for .superagent files.

    Attributes:
        tools: Glob patterns for tool names requiring approval.
        handler: Handler type — ``"webhook"``, ``"callback"``, or ``"queue"``.
        webhook_url: Webhook URL (required when handler is ``"webhook"``).
        timeout: Seconds to wait for approval decision.
        on_timeout: Action when timeout expires — ``"deny"`` or ``"allow"``.
        max_pending: Maximum concurrent pending approvals.
        redact_sensitive: Redact PII/credentials in approval requests.
    """

    model_config = ConfigDict(extra="forbid")

    tools: list[str] = Field(..., description="Tool name patterns requiring approval")
    handler: Literal["webhook", "callback", "queue"] = Field(
        "webhook", description="Approval handler type"
    )
    webhook_url: str | None = Field(None, description="Webhook URL for approval requests")
    timeout: float = Field(300, gt=0, le=86400, description="Approval timeout in seconds")
    on_timeout: Literal["deny", "allow"] = Field("deny", description="Action on timeout")
    max_pending: int = Field(10, gt=0, description="Max concurrent pending approvals")
    redact_sensitive: bool = Field(True, description="Redact PII/credentials in requests")
    max_retries_after_deny: int = Field(3, gt=0, description="Max retries after denial")


class EventSinkConfig(BaseModel):
    """Configuration for a single event notification sink."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["webhook", "log"] = Field(
        ..., description="Sink type (callback/eventbus require Python, use build_agent())"
    )
    url: str | None = Field(None, description="Webhook URL")
    events: list[str] | None = Field(None, description="Event types to subscribe to (None = all)")
    headers: dict[str, str] = Field(default_factory=dict, description="Custom HTTP headers")
    secret: str | None = Field(None, description="HMAC signing secret")
    min_severity: str | None = Field(
        None, description="Minimum severity (info/warning/error/critical)"
    )
    max_retries: int = Field(3, ge=0, description="Max retry attempts")
    redact_sensitive: bool = Field(True, description="Redact PII in payloads")


class EventsSection(BaseModel):
    """Event notification configuration for .superagent files."""

    model_config = ConfigDict(extra="forbid")

    sinks: list[EventSinkConfig] = Field(default_factory=list, description="Notification sinks")


class AdaptiveSection(BaseModel):
    """Adaptive strategy (learning from failure) configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(True, description="Enable adaptive strategy learning")
    synthesis_threshold: int = Field(5, gt=0, description="Synthesize after N strategy failures")
    synthesis_model: str | None = Field(
        None, description="Model for synthesis (None = agent's model)"
    )
    max_strategies: int = Field(20, gt=0, description="Max stored strategies")
    auto_cleanup: bool = Field(True, description="Delete raw failure logs after synthesis")
    strategy_ttl: int = Field(0, ge=0, description="Strategy expiry in seconds (0 = never)")
    failure_retention: int = Field(50, gt=0, description="Max raw failure logs to keep")
    verify_human_feedback: bool = Field(True, description="LLM-as-judge on corrections")


class GuardrailsSection(BaseModel):
    """Security guardrails configuration."""

    model_config = ConfigDict(extra="forbid")

    detect_injection: bool = Field(True, description="Enable prompt injection detection")
    detect_pii: bool = Field(True, description="Enable PII detection and redaction")
    detect_credentials: bool = Field(True, description="Enable credential detection")
    detect_toxicity: bool = Field(False, description="Enable toxicity detection")
    injection_threshold: float = Field(
        0.85, ge=0.0, le=1.0, description="Injection confidence threshold"
    )
    warmup: bool = Field(True, description="Pre-load ML models at startup")


class SuperAgentSchema(BaseModel):
    """Root schema for .superagent YAML files.

    This is the top-level schema that validates the entire .superagent file
    structure. It supports versioning for future compatibility and ensures
    at least one of servers, cross_agents, or sandbox is configured.

    Attributes:
        version: Schema version (currently "1.0").
        agent: Agent-level configuration (model, instructions, trace).
        servers: Named MCP server configurations.
        cross_agents: Optional cross-agent references.
        sandbox: Optional sandbox configuration (bool or detailed config).
        memory: Optional memory configuration.
        observability: Optional observability configuration (True or detailed).
        optimize_tools: Optional tool optimization (True, string level, or detailed).

    Examples:
        >>> schema = SuperAgentSchema(
        ...     version="1.0",
        ...     agent=AgentSection(model="openai:gpt-5-mini"),
        ...     servers={"math": HTTPServerConfig(url="http://...")},
        ...     sandbox=True,
        ...     observability=True,
        ...     optimize_tools="semantic",
        ... )
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal["1.0"] = Field("1.0", description="Schema version")
    agent: AgentSection = Field(..., description="Agent configuration")
    servers: dict[str, ServerConfig] = Field(
        default_factory=dict, description="Named MCP server configurations"
    )
    cross_agents: dict[str, CrossAgentConfig] | None = Field(
        None, description="Cross-agent references"
    )
    sandbox: bool | SandboxConfigSection | None = Field(
        None, description="Sandbox configuration (True for defaults, or detailed config)"
    )
    memory: MemorySection | None = Field(
        None, description="Agent memory configuration for persistent knowledge"
    )
    observability: bool | ObservabilitySection | None = Field(
        None, description="Observability config (True for defaults, or detailed)"
    )
    optimize_tools: bool | str | ToolOptimizationSection | None = Field(
        None,
        description=("Tool optimization (True or 'semantic' for defaults, or detailed config)"),
    )
    cache: bool | CacheSection | None = Field(
        None,
        description=(
            "Semantic caching (True for defaults, or detailed config). "
            "Caches LLM responses for similar queries to reduce API costs."
        ),
    )
    approval: ApprovalSection | None = Field(
        None,
        description=(
            "Human-in-the-loop approval for sensitive tool calls. "
            "Pauses agent execution and awaits human decision."
        ),
    )
    events: EventsSection | None = Field(
        None,
        description=(
            "Webhook and event notification sinks. "
            "Emits structured notifications on invocation, tool, guardrail, "
            "budget, health, mission, and process events."
        ),
    )
    adaptive: bool | AdaptiveSection | None = Field(
        None,
        description=(
            "Adaptive strategy learning. Agents learn from failures "
            "and adjust approach across invocations."
        ),
    )
    guardrails: bool | GuardrailsSection | None = Field(
        None,
        description=(
            "Security guardrails. Blocks prompt injection, redacts PII, "
            "detects credentials. True for defaults."
        ),
    )
    max_invocation_time: float = Field(
        0,
        ge=0,
        description="Max seconds per invocation (0 = unlimited).",
    )

    @model_validator(mode="after")
    def validate_has_config(self) -> SuperAgentSchema:
        """Ensure at least servers, cross_agents, or sandbox is configured."""
        if not self.servers and not self.cross_agents and not self.sandbox:
            raise ValueError(
                "At least one of 'servers', 'cross_agents', or 'sandbox' must be configured"
            )
        return self
