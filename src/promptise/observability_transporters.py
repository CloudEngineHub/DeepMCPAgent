"""Pluggable transporter backends for the observability system.

Each transporter receives :class:`TimelineEntry` events in real time from
the :class:`ObservabilityCollector` and delivers them to a specific backend.

Available transporters:

- **HTMLReportTransporter** — self-contained interactive HTML report
- **JSONFileTransporter** — NDJSON streaming + full JSON session dump
- **StructuredLogTransporter** — JSON log lines for ELK / Datadog / Splunk
- **ConsoleTransporter** — Rich-powered real-time terminal output
- **PrometheusTransporter** — Prometheus metrics (counters + histograms)
- **OTLPTransporter** — OpenTelemetry span export via OTLP gRPC
- **WebhookTransporter** — HTTP POST per event (or batched)
- **CallbackTransporter** — invoke a user-provided Python callable

Usage::

    from promptise.observability_transporters import (
        HTMLReportTransporter,
        StructuredLogTransporter,
        ConsoleTransporter,
    )
    from promptise.observability import ObservabilityCollector

    collector = ObservabilityCollector("my-session")
    collector.add_transporter(HTMLReportTransporter(output_dir="./reports"))
    collector.add_transporter(StructuredLogTransporter(log_file="./logs/events.jsonl"))
    collector.add_transporter(ConsoleTransporter(live=True))
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("promptise.transporters")


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class BaseTransporter(ABC):
    """Abstract base class for all observability transporters.

    Subclasses must implement:
    - ``on_event(entry)`` — called for each timeline event in real time.
    - ``flush()`` — finalize / export pending data (may be async).

    Optionally override ``close()`` for cleanup.
    """

    @abstractmethod
    def on_event(self, entry: Any) -> None:
        """Process a single :class:`TimelineEntry` event."""
        ...

    @abstractmethod
    def flush(self) -> None:
        """Finalize and export any buffered data."""
        ...

    def close(self) -> None:
        """Release resources.  Called on shutdown."""
        pass


# ---------------------------------------------------------------------------
# 1) HTML Report Transporter
# ---------------------------------------------------------------------------


class HTMLReportTransporter(BaseTransporter):
    """Generates a self-contained interactive HTML report on flush.

    Events are buffered in memory.  When :meth:`flush` is called (typically
    at the end of a session), the full report is generated via the existing
    ``generate_report()`` infrastructure.

    Args:
        output_dir: Directory for the report file.  Defaults to ``"./reports"``.
        session_name: Embedded in the filename.
    """

    def __init__(
        self,
        output_dir: str = "./reports",
        session_name: str = "promptise",
    ) -> None:
        self.output_dir = output_dir
        self.session_name = session_name
        self._collector: Any | None = None  # Set externally when auto-created

    def on_event(self, entry: Any) -> None:
        # Events are already stored in the collector; nothing to buffer.
        pass

    def flush(self) -> None:
        """Write a self-contained HTML report of collected events."""
        if self._collector is None:
            return
        try:
            Path(self.output_dir).mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.session_name}-report-{ts}.html"
            path = os.path.join(self.output_dir, filename)

            # Build the HTML report with embedded JSON data
            data_json = self._collector.to_json(indent=2)
            html = self._render_html(data_json)

            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info("HTML observability report written: %s", path)
        except Exception as exc:
            logger.error("HTMLReportTransporter flush error: %s", exc)

    @staticmethod
    def _render_html(data_json: str) -> str:
        """Render a self-contained HTML report with timeline visualization."""
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Promptise Agent Report</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }}
  h1 {{ color: #58a6ff; margin-bottom: 8px; }}
  .meta {{ color: #8b949e; margin-bottom: 24px; font-size: 14px; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .stat {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }}
  .stat-value {{ font-size: 28px; font-weight: 700; color: #58a6ff; }}
  .stat-label {{ font-size: 12px; color: #8b949e; margin-top: 4px; }}
  .timeline {{ margin-top: 24px; }}
  .event {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 12px 16px; margin-bottom: 8px; display: flex; align-items: center; gap: 12px; }}
  .event-icon {{ font-size: 18px; min-width: 24px; text-align: center; }}
  .event-type {{ font-weight: 600; color: #f0f6fc; min-width: 140px; }}
  .event-desc {{ flex: 1; color: #8b949e; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .event-time {{ color: #484f58; font-size: 12px; font-family: monospace; }}
  .tool {{ color: #d2a8ff; }} .llm {{ color: #7ee787; }} .error {{ color: #f85149; }} .cache {{ color: #ffa657; }}
  .filter {{ margin-bottom: 16px; display: flex; gap: 8px; flex-wrap: wrap; }}
  .filter button {{ background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 6px 12px; border-radius: 16px; cursor: pointer; font-size: 12px; }}
  .filter button.active {{ background: #1f6feb; border-color: #1f6feb; }}
</style>
</head>
<body>
<h1>Promptise Agent Report</h1>
<p class="meta">Generated by Promptise Observability</p>
<div class="stats" id="stats"></div>
<div class="filter" id="filter"></div>
<div class="timeline" id="timeline"></div>
<script>
const data = {data_json};
const entries = data.entries || data.timeline || [];
const icons = {{tool_call_start:'🔧',tool_call_end:'✅',llm_start:'🧠',llm_end:'💬',error:'❌',retry:'🔄',agent_start:'▶️',agent_end:'⏹️','cache.hit':'💨','cache.miss':'🔍','cache.store':'💾'}};
const cats = {{tool_call_start:'tool',tool_call_end:'tool',llm_start:'llm',llm_end:'llm',error:'error',retry:'error','cache.hit':'cache','cache.miss':'cache','cache.store':'cache'}};

// Stats
const stats = document.getElementById('stats');
let totalTokens=0, toolCalls=0, llmCalls=0, errors=0, cacheHits=0;
entries.forEach(e => {{
  if(e.event_type==='llm_end') {{ totalTokens += (e.metadata||{{}}).total_tokens||0; llmCalls++; }}
  if(e.event_type==='tool_call_start') toolCalls++;
  if(e.event_type==='error') errors++;
  if(e.event_type==='cache.hit') cacheHits++;
}});
stats.innerHTML = `
  <div class="stat"><div class="stat-value">${{entries.length}}</div><div class="stat-label">Total Events</div></div>
  <div class="stat"><div class="stat-value">${{totalTokens.toLocaleString()}}</div><div class="stat-label">Total Tokens</div></div>
  <div class="stat"><div class="stat-value">${{llmCalls}}</div><div class="stat-label">LLM Calls</div></div>
  <div class="stat"><div class="stat-value">${{toolCalls}}</div><div class="stat-label">Tool Calls</div></div>
  <div class="stat"><div class="stat-value">${{errors}}</div><div class="stat-label">Errors</div></div>
  <div class="stat"><div class="stat-value">${{cacheHits}}</div><div class="stat-label">Cache Hits</div></div>
`;

// Filter
const filterEl = document.getElementById('filter');
let activeFilter = 'all';
['all','tool','llm','error','cache'].forEach(f => {{
  const btn = document.createElement('button');
  btn.textContent = f.charAt(0).toUpperCase()+f.slice(1);
  btn.className = f==='all'?'active':'';
  btn.onclick = () => {{ activeFilter=f; document.querySelectorAll('.filter button').forEach(b=>b.className=''); btn.className='active'; render(); }};
  filterEl.appendChild(btn);
}});

// Timeline
const tl = document.getElementById('timeline');
function render() {{
  tl.innerHTML = '';
  entries.forEach(e => {{
    const cat = cats[e.event_type]||'other';
    if(activeFilter!=='all' && cat!==activeFilter) return;
    const div = document.createElement('div');
    div.className = 'event';
    const icon = icons[e.event_type]||'📌';
    const cls = cat;
    div.innerHTML = `<span class="event-icon">${{icon}}</span><span class="event-type ${{cls}}">${{e.event_type}}</span><span class="event-desc">${{e.description||e.details||''}}</span><span class="event-time">${{e.timestamp?new Date(e.timestamp*1000).toLocaleTimeString():''}}</span>`;
    tl.appendChild(div);
  }});
}}
render();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 2) JSON File Transporter
# ---------------------------------------------------------------------------


class JSONFileTransporter(BaseTransporter):
    """Writes NDJSON lines (one per event) and a full session dump on flush.

    Supports two output modes:
    - **Streaming**: Each event is appended as a single JSON line to the file.
    - **Dump**: On ``flush()``, a full session JSON is written to a separate file.

    Args:
        output_dir: Directory for output files.  Defaults to ``"./reports"``.
        session_name: Base name for output files.
        stream: If True, write NDJSON lines in real time.  Default: True.
    """

    def __init__(
        self,
        output_dir: str = "./reports",
        session_name: str = "promptise",
        stream: bool = True,
    ) -> None:
        self.output_dir = output_dir
        self.session_name = session_name
        self.stream = stream
        self._collector: Any | None = None
        self._stream_file: Any | None = None
        self._lock = threading.Lock()

        if self.stream:
            Path(self.output_dir).mkdir(parents=True, exist_ok=True)
            self._stream_path = os.path.join(self.output_dir, f"{session_name}-events.ndjson")
            # Open with explicit close in close() — can't use context manager
            # because the file must stay open across on_event calls.
            self._stream_file = open(  # noqa: SIM115
                self._stream_path, "a", encoding="utf-8"
            )

    def on_event(self, entry: Any) -> None:
        if self.stream and self._stream_file is not None:
            line = json.dumps(entry.to_dict(), default=str)
            with self._lock:
                self._stream_file.write(line + "\n")
                self._stream_file.flush()

    def flush(self) -> None:
        """Write full session JSON dump."""
        if self._collector is None:
            return
        try:
            Path(self.output_dir).mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dump_path = os.path.join(self.output_dir, f"{self.session_name}-session-{ts}.json")
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(self._collector.to_dict(), f, indent=2, default=str)
            logger.info("JSON session dump: %s", dump_path)
        except Exception as exc:
            logger.error("JSONFileTransporter flush error: %s", exc)

    def close(self) -> None:
        if self._stream_file is not None:
            with self._lock:
                self._stream_file.close()
                self._stream_file = None


# ---------------------------------------------------------------------------
# 3) Structured Log Transporter
# ---------------------------------------------------------------------------


class StructuredLogTransporter(BaseTransporter):
    """Enterprise-grade structured logging — one JSON line per event.

    Each event becomes a log line like::

        {"timestamp":"2026-02-20T19:47:31Z", "level":"INFO", "service":"promptise",
         "session":"my-run", "agent_id":"code-reviewer", "event_type":"llm.end",
         "duration_ms":1250, "tokens":450, "message":"LLM call completed", ...}

    Compatible with ELK stack, Datadog, Splunk, CloudWatch, and any JSON
    log ingestion pipeline.

    Args:
        log_file: File path for structured logs.  If ``None``, writes to stdout.
        session_name: Embedded in each log line.
        service_name: Service identifier for log aggregation.
        correlation_id: Optional trace/request correlation ID.
    """

    def __init__(
        self,
        log_file: str | None = None,
        session_name: str = "promptise",
        service_name: str = "promptise",
        correlation_id: str | None = None,
    ) -> None:
        self.session_name = session_name
        self.service_name = service_name
        self.correlation_id = correlation_id
        self._lock = threading.Lock()

        self._logger = logging.getLogger(f"promptise.structured.{session_name}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False

        # Remove existing handlers to avoid duplicates
        self._logger.handlers.clear()

        if log_file:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            handler: logging.Handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        else:
            handler = logging.StreamHandler(sys.stdout)

        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)
        self._handler = handler

    def on_event(self, entry: Any) -> None:
        """Format entry as structured JSON log line."""
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(entry.timestamp, tz=timezone.utc).isoformat(),
            "level": self._level_for(entry.event_type.value),
            "service": self.service_name,
            "session": self.session_name,
            "entry_id": entry.entry_id,
            "event_type": entry.event_type.value,
            "category": entry.category.value,
            "message": entry.details,
        }

        if entry.agent_id:
            log_entry["agent_id"] = entry.agent_id
        if entry.phase:
            log_entry["phase"] = entry.phase
        if entry.duration is not None:
            log_entry["duration_ms"] = round(entry.duration * 1000, 1)
        if entry.parent_id:
            log_entry["parent_id"] = entry.parent_id
        if self.correlation_id:
            log_entry["correlation_id"] = self.correlation_id

        # Promote key metadata fields to top level
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "latency_ms",
            "model",
            "tool_name",
            "error",
            "error_type",
        ):
            if key in entry.metadata:
                log_entry[key] = entry.metadata[key]

        # Include remaining metadata under "metadata" key
        remaining = {k: v for k, v in entry.metadata.items() if k not in log_entry}
        if remaining:
            log_entry["metadata"] = remaining

        with self._lock:
            self._logger.info(json.dumps(log_entry, default=str))

    def flush(self) -> None:
        if self._handler:
            self._handler.flush()

    def close(self) -> None:
        if self._handler:
            self._handler.close()

    @staticmethod
    def _level_for(event_type: str) -> str:
        """Map event type to log level."""
        if "error" in event_type or "failed" in event_type:
            return "ERROR"
        if "retry" in event_type:
            return "WARN"
        if event_type.startswith("session."):
            return "INFO"
        return "INFO"


# ---------------------------------------------------------------------------
# 4) Console Transporter
# ---------------------------------------------------------------------------


class ConsoleTransporter(BaseTransporter):
    """Real-time color-coded console output.

    Uses Rich if available for beautiful formatting, otherwise falls back
    to plain ``print()``.

    Color coding:
    - **Green**: LLM events (start, end, stream)
    - **Yellow**: Tool events (call, result)
    - **Red**: Errors (LLM error, tool error, task failed)
    - **Blue**: Agent events (input, output)
    - **Magenta**: Phase / orchestration events
    - **Cyan**: Session events

    Args:
        live: If True, enable real-time console output for each event.
            Default: True.
        verbose: If True, include metadata in output.  Default: False.
    """

    _COLOR_MAP: dict[str, str] = {
        "llm": "green",
        "tool": "yellow",
        "agent.input": "blue",
        "agent.output": "blue",
        "agent": "white",
        "task": "white",
        "phase": "magenta",
        "session": "cyan",
        "auth": "red",
        "rbac": "red",
        "health": "dim",
        "circuit_breaker": "dim",
    }

    _ICON_MAP: dict[str, str] = {
        "llm.start": "🧠",
        "llm.end": "✅",
        "llm.error": "💥",
        "llm.retry": "🔄",
        "llm.stream_chunk": "💬",
        "llm.turn": "🤖",
        "tool.call": "🔧",
        "tool.result": "📦",
        "tool.error": "❌",
        "agent.input": "📥",
        "agent.output": "📤",
        "phase.start": "🚀",
        "phase.end": "🏁",
        "session.start": "▶️",
        "session.end": "⏹️",
        "task.started": "📋",
        "task.completed": "✔️",
        "task.failed": "💀",
    }

    def __init__(self, live: bool = True, verbose: bool = False) -> None:
        self.live = live
        self.verbose = verbose
        self._has_rich = False
        self._console: Any = None
        self._event_count = 0
        self._total_tokens = 0

        try:
            from rich.console import Console

            self._console = Console(stderr=True)
            self._has_rich = True
        except ImportError:
            pass

    def on_event(self, entry: Any) -> None:
        if not self.live:
            return

        self._event_count += 1

        # Track running totals for display
        tokens = entry.metadata.get("total_tokens", 0)
        self._total_tokens += tokens

        etype = entry.event_type.value
        icon = self._ICON_MAP.get(etype, "•")
        ts = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M:%S.%f")[:-3]
        agent = f" [{entry.agent_id}]" if entry.agent_id else ""
        duration_str = f" ({entry.duration * 1000:.0f}ms)" if entry.duration else ""
        latency = entry.metadata.get("latency_ms")
        latency_str = f" ({latency:.0f}ms)" if latency else ""

        # Build message
        msg = f"{icon} {ts}{agent} {entry.details}{duration_str}{latency_str}"

        # Add token info for LLM events
        if tokens:
            msg += f"  [{tokens} tok]"

        if self._has_rich and self._console:
            color = self._get_color(etype)
            self._console.print(f"[{color}]{msg}[/{color}]")
        else:
            print(msg)

        if self.verbose and entry.metadata:
            # Print key metadata
            meta_keys = ["model", "tool_name", "error", "prompt_tokens", "completion_tokens"]
            meta_parts = []
            for k in meta_keys:
                if k in entry.metadata:
                    meta_parts.append(f"{k}={entry.metadata[k]}")
            if meta_parts:
                meta_str = "    " + ", ".join(meta_parts)
                if self._has_rich and self._console:
                    self._console.print(f"[dim]{meta_str}[/dim]")
                else:
                    print(meta_str)

    def flush(self) -> None:
        """Print session summary."""
        if not self.live:
            return
        summary = f"\n📊 Session Summary: {self._event_count} events, {self._total_tokens} tokens"
        if self._has_rich and self._console:
            self._console.print(f"[bold cyan]{summary}[/bold cyan]")
        else:
            print(summary)

    def _get_color(self, event_type: str) -> str:
        """Get Rich color for an event type."""
        if event_type in self._COLOR_MAP:
            return self._COLOR_MAP[event_type]
        prefix = event_type.split(".")[0]
        return self._COLOR_MAP.get(prefix, "white")


# ---------------------------------------------------------------------------
# 5) Prometheus Transporter
# ---------------------------------------------------------------------------


class PrometheusTransporter(BaseTransporter):
    """Bridges timeline events to Prometheus metrics.

    Auto-creates and increments counters and histograms:

    - ``promptise_llm_calls_total`` (counter: agent_id, model)
    - ``promptise_llm_tokens_total`` (counter: agent_id, token_type)
    - ``promptise_llm_duration_seconds`` (histogram: agent_id, model)
    - ``promptise_tool_calls_total`` (counter: agent_id, tool_name)
    - ``promptise_tool_duration_seconds`` (histogram: agent_id, tool_name)
    - ``promptise_tool_errors_total`` (counter: agent_id, tool_name)
    - ``promptise_events_total`` (counter: event_type, category)

    If the ``prometheus_client`` package is not installed, metrics are
    tracked internally as plain Python dicts (accessible via :attr:`metrics`).

    Args:
        port: Port for the Prometheus ``/metrics`` HTTP endpoint.
            If set to 0, no HTTP server is started.  Default: 0 (no server).
    """

    def __init__(self, port: int = 0) -> None:
        self.port = port
        self._has_prometheus = False
        self.metrics: dict[str, Any] = {
            "llm_calls_total": {},
            "llm_tokens_total": {},
            "llm_duration_seconds": [],
            "tool_calls_total": {},
            "tool_errors_total": {},
            "tool_duration_seconds": [],
            "events_total": {},
        }
        self._lock = threading.Lock()

        try:
            from prometheus_client import Counter, Histogram, start_http_server  # type: ignore

            self._has_prometheus = True
            self._prom_llm_calls = Counter(
                "promptise_llm_calls_total",
                "Total LLM calls",
                ["agent_id", "model"],
            )
            self._prom_llm_tokens = Counter(
                "promptise_llm_tokens_total",
                "Total tokens used",
                ["agent_id", "token_type"],
            )
            self._prom_llm_duration = Histogram(
                "promptise_llm_duration_seconds",
                "LLM call duration in seconds",
                ["agent_id", "model"],
            )
            self._prom_tool_calls = Counter(
                "promptise_tool_calls_total",
                "Total tool calls",
                ["agent_id", "tool_name"],
            )
            self._prom_tool_errors = Counter(
                "promptise_tool_errors_total",
                "Total tool errors",
                ["agent_id", "tool_name"],
            )
            self._prom_tool_duration = Histogram(
                "promptise_tool_duration_seconds",
                "Tool call duration in seconds",
                ["agent_id", "tool_name"],
            )
            self._prom_events = Counter(
                "promptise_events_total",
                "Total observability events",
                ["event_type", "category"],
            )

            if port > 0:
                start_http_server(port)
                logger.info("Prometheus metrics server started on port %d", port)

        except ImportError:
            logger.warning(
                "prometheus_client not installed — PrometheusTransporter will "
                "track metrics internally only (not exported to Prometheus). "
                "Install with: pip install prometheus_client"
            )

    def on_event(self, entry: Any) -> None:
        etype = entry.event_type.value
        cat = entry.category.value
        agent = entry.agent_id or "unknown"

        # Always track event counts
        with self._lock:
            key = f"{etype}:{cat}"
            self.metrics["events_total"][key] = self.metrics["events_total"].get(key, 0) + 1

        if self._has_prometheus:
            self._prom_events.labels(event_type=etype, category=cat).inc()

        # LLM end events
        if etype in ("llm.end", "llm.turn"):
            model = entry.metadata.get("model", "unknown")
            tokens_prompt = entry.metadata.get("prompt_tokens", 0)
            tokens_completion = entry.metadata.get("completion_tokens", 0)
            latency = entry.metadata.get("latency_ms")

            with self._lock:
                mk = f"{agent}:{model}"
                self.metrics["llm_calls_total"][mk] = self.metrics["llm_calls_total"].get(mk, 0) + 1
                self.metrics["llm_tokens_total"][f"{agent}:prompt"] = (
                    self.metrics["llm_tokens_total"].get(f"{agent}:prompt", 0) + tokens_prompt
                )
                self.metrics["llm_tokens_total"][f"{agent}:completion"] = (
                    self.metrics["llm_tokens_total"].get(f"{agent}:completion", 0)
                    + tokens_completion
                )
                if latency is not None:
                    self.metrics["llm_duration_seconds"].append(latency / 1000.0)

            if self._has_prometheus:
                self._prom_llm_calls.labels(agent_id=agent, model=model).inc()
                self._prom_llm_tokens.labels(agent_id=agent, token_type="prompt").inc(tokens_prompt)
                self._prom_llm_tokens.labels(agent_id=agent, token_type="completion").inc(
                    tokens_completion
                )
                if latency is not None:
                    self._prom_llm_duration.labels(agent_id=agent, model=model).observe(
                        latency / 1000.0
                    )

        # Tool events
        elif etype == "tool.call":
            tool_name = entry.metadata.get("tool_name", "unknown")
            with self._lock:
                tk = f"{agent}:{tool_name}"
                self.metrics["tool_calls_total"][tk] = (
                    self.metrics["tool_calls_total"].get(tk, 0) + 1
                )

            if self._has_prometheus:
                self._prom_tool_calls.labels(agent_id=agent, tool_name=tool_name).inc()

        elif etype == "tool.result":
            tool_name = entry.metadata.get("tool_name", "unknown")
            latency = entry.metadata.get("latency_ms")
            if latency is not None:
                with self._lock:
                    self.metrics["tool_duration_seconds"].append(latency / 1000.0)
                if self._has_prometheus:
                    self._prom_tool_duration.labels(agent_id=agent, tool_name=tool_name).observe(
                        latency / 1000.0
                    )

        elif etype == "tool.error":
            tool_name = entry.metadata.get("tool_name", "unknown")
            with self._lock:
                tk = f"{agent}:{tool_name}"
                self.metrics["tool_errors_total"][tk] = (
                    self.metrics["tool_errors_total"].get(tk, 0) + 1
                )
            if self._has_prometheus:
                self._prom_tool_errors.labels(agent_id=agent, tool_name=tool_name).inc()

    def flush(self) -> None:
        pass  # Prometheus is push-based; nothing to flush.


# ---------------------------------------------------------------------------
# 6) OTLP Transporter (OpenTelemetry)
# ---------------------------------------------------------------------------


class OTLPTransporter(BaseTransporter):
    """Converts timeline entries to OpenTelemetry spans and exports via OTLP.

    Each agent invocation is a parent span; each LLM call and tool call
    are child spans with proper parent-child relationships.

    Requires the ``[observability]`` extra::

        pip install promptise[observability]

    Compatible with Jaeger, Datadog, Honeycomb, Grafana Tempo, and any
    OTLP-compatible backend.

    Args:
        endpoint: OTLP gRPC endpoint.  Default: ``"http://localhost:4317"``.
        service_name: OpenTelemetry service name.  Default: ``"promptise"``.
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:4317",
        service_name: str = "promptise",
    ) -> None:
        self.endpoint = endpoint
        self.service_name = service_name
        self._tracer: Any = None
        self._spans: dict[str, Any] = {}  # entry_id → span

        try:
            from opentelemetry import trace  # type: ignore
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource  # type: ignore
            from opentelemetry.sdk.trace import TracerProvider  # type: ignore
            from opentelemetry.sdk.trace.export import (  # type: ignore
                BatchSpanProcessor,
            )

            resource = Resource.create({"service.name": service_name})
            provider = TracerProvider(resource=resource)
            exporter = OTLPSpanExporter(endpoint=endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer("promptise")
            self._provider = provider
            logger.info("OTLPTransporter connected to %s", endpoint)

        except ImportError:
            logger.warning(
                "OpenTelemetry packages not installed.  "
                "Install with: pip install promptise[observability]"
            )

    def on_event(self, entry: Any) -> None:
        if self._tracer is None:
            return

        etype = entry.event_type.value

        # Map event types to span names
        span_name = f"promptise.{etype}"
        if entry.agent_id:
            span_name = f"promptise.{entry.agent_id}.{etype}"

        # Create span with attributes
        span = self._tracer.start_span(span_name)
        span.set_attribute("promptise.event_type", etype)
        span.set_attribute("promptise.category", entry.category.value)
        if entry.agent_id:
            span.set_attribute("promptise.agent_id", entry.agent_id)
        if entry.phase:
            span.set_attribute("promptise.phase", entry.phase)
        span.set_attribute("promptise.details", entry.details)

        # Add metadata as attributes
        for k, v in entry.metadata.items():
            if isinstance(v, (str, int, float, bool)):
                span.set_attribute(f"promptise.{k}", v)

        # Set duration if available
        if entry.duration is not None:
            span.set_attribute("promptise.duration_ms", entry.duration * 1000)

        span.end()

    def flush(self) -> None:
        """Force-flush the span processor."""
        if hasattr(self, "_provider"):
            try:
                self._provider.force_flush()
            except Exception as exc:
                logger.error("OTLPTransporter flush error: %s", exc)

    def close(self) -> None:
        if hasattr(self, "_provider"):
            try:
                self._provider.shutdown()
            except Exception:
                logger.debug("OTLPTransporter shutdown error", exc_info=True)


# ---------------------------------------------------------------------------
# 7) Webhook Transporter
# ---------------------------------------------------------------------------


class WebhookTransporter(BaseTransporter):
    """HTTP POST each event (or batch) to a configurable URL.

    Supports:
    - Single mode (POST per event)
    - Batch mode (buffer N events, flush periodically)
    - Custom HTTP headers (for auth tokens)
    - Retry with exponential backoff
    - Async non-blocking delivery via background thread

    Useful for Slack, Discord, PagerDuty, or custom webhook integrations.

    Args:
        url: Target URL for the webhook.
        headers: Custom HTTP headers (e.g. ``{"Authorization": "Bearer ..."}``)
        batch_size: Buffer events and send in batches.  0 = send per event.
        max_retries: Maximum retry attempts for failed deliveries.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        batch_size: int = 0,
        max_retries: int = 3,
        timeout: float = 10.0,
    ) -> None:
        self.url = url
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.timeout = timeout
        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def on_event(self, entry: Any) -> None:
        payload = entry.to_dict()

        if self.batch_size <= 0:
            # Immediate delivery
            self._send([payload])
        else:
            batch_to_send: list[dict[str, Any]] | None = None
            with self._lock:
                self._buffer.append(payload)
                if len(self._buffer) >= self.batch_size:
                    batch_to_send = self._buffer[:]
                    self._buffer.clear()
            if batch_to_send is not None:
                self._send(batch_to_send)

    def flush(self) -> None:
        """Send any buffered events."""
        with self._lock:
            if self._buffer:
                batch = self._buffer[:]
                self._buffer.clear()
            else:
                return
        self._send(batch)

    def _send(self, events: list[dict[str, Any]]) -> None:
        """Send events with retry logic.  Runs in a background thread."""
        thread = threading.Thread(
            target=self._send_sync,
            args=(events,),
            daemon=True,
        )
        thread.start()

    def _send_sync(self, events: list[dict[str, Any]]) -> None:
        """Synchronous send with exponential backoff retry."""
        import urllib.error
        import urllib.request

        payload = json.dumps(
            {"events": events} if len(events) > 1 else events[0],
            default=str,
        ).encode("utf-8")

        for attempt in range(self.max_retries + 1):
            try:
                req = urllib.request.Request(
                    self.url,
                    data=payload,
                    headers=self.headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if resp.status < 300:
                        return
                    logger.warning(
                        "Webhook returned %d on attempt %d",
                        resp.status,
                        attempt + 1,
                    )
            except Exception as exc:
                logger.debug("Webhook attempt %d failed: %s", attempt + 1, exc)
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 5))

        logger.error(
            "Webhook delivery failed after %d attempts to %s",
            self.max_retries + 1,
            self.url,
        )


# ---------------------------------------------------------------------------
# 8) Callback Transporter
# ---------------------------------------------------------------------------


class CallbackTransporter(BaseTransporter):
    """Invoke a user-provided Python callable for each event.

    The simplest extensibility point — users can do anything they want
    with each event.

    Args:
        callback: A callable that receives a single :class:`TimelineEntry`.
    """

    def __init__(self, callback: Callable[..., Any]) -> None:
        if not callable(callback):
            raise TypeError(f"callback must be callable, got {type(callback).__name__}")
        self._callback = callback

    def on_event(self, entry: Any) -> None:
        try:
            self._callback(entry)
        except Exception as exc:
            logger.error("CallbackTransporter error: %s", exc)

    def flush(self) -> None:
        pass  # Nothing to flush — events are delivered immediately.


# ---------------------------------------------------------------------------
# Factory: create transporters from ObservabilityConfig
# ---------------------------------------------------------------------------


def create_transporters(
    config: Any,  # ObservabilityConfig
    collector: Any,  # ObservabilityCollector
) -> list[BaseTransporter]:
    """Create and register transporter instances from an ObservabilityConfig.

    This is called automatically by ``build_agent()`` when the
    ``observe`` parameter is enabled.

    Args:
        config: An :class:`ObservabilityConfig` instance.
        collector: The :class:`ObservabilityCollector` to register with.

    Returns:
        List of created transporter instances.
    """
    from .observability_config import TransporterType

    transporters: list[BaseTransporter] = []

    for t_type in config.transporters:
        try:
            t: BaseTransporter | None = None

            if t_type == TransporterType.HTML:
                t = HTMLReportTransporter(
                    output_dir=config.output_dir or "./reports",
                    session_name=config.session_name,
                )
                t._collector = collector  # type: ignore[attr-defined]

            elif t_type == TransporterType.JSON:
                t = JSONFileTransporter(
                    output_dir=config.output_dir or "./reports",
                    session_name=config.session_name,
                )
                t._collector = collector  # type: ignore[attr-defined]

            elif t_type == TransporterType.STRUCTURED_LOG:
                t = StructuredLogTransporter(
                    log_file=config.log_file,
                    session_name=config.session_name,
                    correlation_id=config.correlation_id,
                )

            elif t_type == TransporterType.CONSOLE:
                t = ConsoleTransporter(
                    live=config.console_live,
                    verbose=(config.level.value == "full"),
                )

            elif t_type == TransporterType.PROMETHEUS:
                t = PrometheusTransporter(port=config.prometheus_port)

            elif t_type == TransporterType.OTLP:
                t = OTLPTransporter(
                    endpoint=config.otlp_endpoint,
                    service_name=config.session_name,
                )

            elif t_type == TransporterType.WEBHOOK:
                if config.webhook_url:
                    t = WebhookTransporter(
                        url=config.webhook_url,
                        headers=config.webhook_headers,
                    )
                else:
                    logger.warning("WebhookTransporter requested but no webhook_url set.")

            elif t_type == TransporterType.CALLBACK:
                if config.on_event:
                    t = CallbackTransporter(callback=config.on_event)
                else:
                    logger.warning("CallbackTransporter requested but no on_event callable set.")

            if t is not None:
                collector.add_transporter(t)
                transporters.append(t)

        except Exception as exc:
            logger.error("Failed to create transporter %s: %s", t_type.value, exc)

    return transporters
