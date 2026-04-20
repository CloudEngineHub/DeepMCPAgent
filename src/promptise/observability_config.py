"""Configuration for Promptise's plug-and-play observability system.

Provides :class:`ObservabilityConfig` — the single dataclass that controls
what gets observed, how much detail is captured, and where events are sent.

Usage::

    from promptise import build_agent, ObservabilityConfig, ObserveLevel

    # Minimal — just turn it on (defaults to STANDARD level + HTML report)
    agent = await build_agent(servers=..., model=..., observe=True)

    # Full enterprise configuration
    config = ObservabilityConfig(
        level=ObserveLevel.FULL,
        session_name="production-audit",
        record_prompts=True,
        transporters=[TransporterType.HTML, TransporterType.STRUCTURED_LOG, TransporterType.CONSOLE],
        output_dir="./reports",
        log_file="./logs/agent.jsonl",
        console_live=True,
    )
    agent = await build_agent(servers=..., model=..., observe=config)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ObserveLevel(str, Enum):
    """Controls how much detail the observability system captures."""

    OFF = "off"
    """Observability disabled."""

    BASIC = "basic"
    """Tool calls + agent I/O + errors only."""

    STANDARD = "standard"
    """Everything in BASIC plus every LLM turn with token usage and latency."""

    FULL = "full"
    """Everything in STANDARD plus prompt/response content and streaming
    tokens."""


class TransporterType(str, Enum):
    """Available backends for receiving observability events."""

    HTML = "html"
    """Self-contained interactive HTML report (default)."""

    JSON = "json"
    """JSON file export (full session dump + NDJSON streaming)."""

    STRUCTURED_LOG = "log"
    """JSON log lines, one per event.  Compatible with ELK, Datadog,
    Splunk, CloudWatch, and other enterprise logging pipelines."""

    CONSOLE = "console"
    """Real-time Rich console output with color-coded events."""

    PROMETHEUS = "prometheus"
    """Prometheus metrics (counters, histograms) for Grafana dashboards."""

    OTLP = "otlp"
    """OpenTelemetry span export via OTLP gRPC.  Requires the
    ``[all]`` extra: ``pip install "promptise[all]"``."""

    WEBHOOK = "webhook"
    """HTTP POST each event (or batch) to a configurable URL."""

    CALLBACK = "callback"
    """Invoke a user-provided Python callable for each event."""


# Backward-compatible alias
ExportFormat = TransporterType


@dataclass
class ObservabilityConfig:
    """Configuration for the observability system.

    Pass as ``observe=config`` to :func:`build_agent` or use the
    shorthand ``observe=True`` for sensible defaults.

    Examples::

        # Defaults: STANDARD level, HTML transporter
        ObservabilityConfig()

        # Enterprise: full detail, multiple transporters
        ObservabilityConfig(
            level=ObserveLevel.FULL,
            record_prompts=True,
            transporters=[
                TransporterType.HTML,
                TransporterType.STRUCTURED_LOG,
                TransporterType.CONSOLE,
                TransporterType.PROMETHEUS,
            ],
            output_dir="./observability",
            log_file="./logs/events.jsonl",
            console_live=True,
            correlation_id="req-abc-123",
        )
    """

    # --- Capture level -------------------------------------------------------

    level: ObserveLevel = ObserveLevel.STANDARD
    """How much detail to capture."""

    session_name: str = "promptise"
    """Human-readable session identifier embedded in reports/logs."""

    record_prompts: bool = False
    """When True, store full prompt/response text in metadata.
    Off by default for privacy."""

    max_entries: int = 100_000
    """Maximum timeline entries before oldest are evicted (ring buffer)."""

    # --- Transporters --------------------------------------------------------

    transporters: list[TransporterType] = field(
        default_factory=lambda: [TransporterType.HTML],
    )
    """Which transporter backends receive events."""

    # --- Transporter-specific config -----------------------------------------

    output_dir: str | None = None
    """Directory for HTML and JSON output files."""

    log_file: str | None = None
    """File path for the STRUCTURED_LOG transporter."""

    console_live: bool = False
    """When True with CONSOLE transporter, start a background thread that
    prints events in real-time."""

    webhook_url: str | None = None
    """Target URL for the WEBHOOK transporter."""

    webhook_headers: dict[str, str] = field(default_factory=dict)
    """Custom HTTP headers for the WEBHOOK transporter (e.g. auth tokens)."""

    otlp_endpoint: str = "http://localhost:4317"
    """gRPC endpoint for the OTLP transporter."""

    prometheus_port: int = 9090
    """Port for the Prometheus metrics endpoint."""

    on_event: Callable[..., Any] | None = None
    """User callback for the CALLBACK transporter.  Receives a single
    :class:`TimelineEntry` argument."""

    # --- Correlation ---------------------------------------------------------

    correlation_id: str | None = None
    """Optional correlation ID that ties all events to an external
    request or trace."""
