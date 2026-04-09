# Observability

Track token usage and export execution traces across your agent systems.

```python
from promptise import build_agent
from promptise.config import HTTPServerSpec

# Simple: turn on observability with defaults
agent = await build_agent(
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    model="openai:gpt-5-mini",
    observe=True,
)

result = await agent.ainvoke({"messages": [{"role": "user", "content": "Hello"}]})
# An interactive HTML report is generated automatically.
```

---

## Concepts

Promptise observability is plug-and-play. Set `observe=True` and every LLM turn, tool call, token count, latency, retry, and error is captured automatically via a LangChain callback handler. Events are routed to one or more **transporters** -- HTML reports, structured logs, console output, Prometheus metrics, OpenTelemetry spans, or custom webhooks.

---

## ObservabilityConfig

For full control, pass an `ObservabilityConfig` instead of `True`:

```python
from promptise import build_agent
from promptise.observability_config import ObservabilityConfig, ObserveLevel, TransporterType

config = ObservabilityConfig(
    level=ObserveLevel.FULL,
    session_name="production-audit",
    record_prompts=True,
    transporters=[
        TransporterType.HTML,
        TransporterType.STRUCTURED_LOG,
        TransporterType.CONSOLE,
    ],
    output_dir="./reports",
    log_file="./logs/agent.jsonl",
    console_live=True,
)

agent = await build_agent(
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    model="openai:gpt-5-mini",
    observe=config,
)
```

### ObserveLevel

Controls the detail level of captured events:

| Level | Captures |
|---|---|
| `ObserveLevel.OFF` | Nothing (observability disabled) |
| `ObserveLevel.BASIC` | Tool calls, agent I/O, errors |
| `ObserveLevel.STANDARD` | Everything in BASIC + every LLM turn with token usage and latency |
| `ObserveLevel.FULL` | Everything in STANDARD + prompt/response content, streaming tokens |

### TransporterType

Available backends for receiving observability events:

| Transporter | Description |
|---|---|
| `TransporterType.HTML` | Self-contained interactive HTML report (default) |
| `TransporterType.JSON` | JSON file export (full session dump + NDJSON streaming) |
| `TransporterType.STRUCTURED_LOG` | JSON log lines for ELK, Datadog, Splunk, CloudWatch |
| `TransporterType.CONSOLE` | Real-time Rich console output with color-coded events |
| `TransporterType.PROMETHEUS` | Prometheus metrics (counters, histograms) for Grafana |
| `TransporterType.OTLP` | OpenTelemetry span export via OTLP gRPC |
| `TransporterType.WEBHOOK` | HTTP POST each event to a configurable URL |
| `TransporterType.CALLBACK` | Invoke a user-provided Python callable for each event |

### Transporter Classes

Each `TransporterType` maps to a concrete class in `promptise.observability_transporters`:

```python
from promptise.observability_transporters import (
    HTMLReportTransporter,
    JSONFileTransporter,
    StructuredLogTransporter,
    ConsoleTransporter,
    PrometheusTransporter,
    OTLPTransporter,
    WebhookTransporter,
    CallbackTransporter,
)
```

| Class | Constructor | Description |
|-------|------------|-------------|
| `HTMLReportTransporter` | `(output_dir="./reports", session_name="promptise")` | Self-contained HTML report |
| `JSONFileTransporter` | `(output_dir="./reports", session_name="promptise", stream=True)` | NDJSON streaming or full JSON dump |
| `StructuredLogTransporter` | `(log_file=None, session_name="promptise", service_name="promptise", correlation_id=None)` | ELK/Datadog/Splunk-compatible structured logs |
| `ConsoleTransporter` | `(live=True, verbose=False)` | Rich-powered real-time terminal output |
| `PrometheusTransporter` | `(port=0)` | Prometheus metrics endpoint (counters, histograms) |
| `OTLPTransporter` | `(endpoint="http://localhost:4317", service_name="promptise")` | OpenTelemetry spans export |
| `WebhookTransporter` | `(url, headers=None, batch_size=0, max_retries=3, timeout=10.0)` | HTTP POST to external endpoint |
| `CallbackTransporter` | `(callback)` | Invoke custom callable per event |

All implement `on_event(entry)`, `flush()`, and `close()`.

**Custom transporter selection:**

```python
config = ObservabilityConfig(
    level=ObserveLevel.FULL,
    transporters=[TransporterType.HTML, TransporterType.PROMETHEUS],
)
agent = await build_agent(
    servers=servers, model="openai:gpt-5-mini", observe=config,
)
```

**Custom callback example:**

```python
def my_handler(entry):
    print(f"[{entry.event_type}] {entry.agent_id}: {entry.description}")

transporter = CallbackTransporter(callback=my_handler)
```

### Configuration Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `level` | `ObserveLevel` | `STANDARD` | Detail level |
| `session_name` | `str` | `"promptise"` | Human-readable session identifier |
| `record_prompts` | `bool` | `False` | Store full prompt/response text (off by default for privacy) |
| `max_entries` | `int` | `100_000` | Max timeline entries before eviction |
| `transporters` | `list[TransporterType]` | `[HTML]` | Active transporters |
| `output_dir` | `str \| None` | `None` | Directory for HTML and JSON output |
| `log_file` | `str \| None` | `None` | File path for STRUCTURED_LOG transporter |
| `console_live` | `bool` | `False` | Real-time console printing |
| `webhook_url` | `str \| None` | `None` | Target URL for WEBHOOK transporter |
| `webhook_headers` | `dict[str, str]` | `{}` | Custom HTTP headers for webhooks |
| `otlp_endpoint` | `str` | `"http://localhost:4317"` | gRPC endpoint for OTLP |
| `prometheus_port` | `int` | `9090` | Port for Prometheus metrics |
| `on_event` | `Callable \| None` | `None` | User callback for CALLBACK transporter |
| `correlation_id` | `str \| None` | `None` | Ties all events to an external request/trace |

---

## PromptiseCallbackHandler

`PromptiseCallbackHandler` is the LangChain callback that bridges LLM events into the observability collector. It is instantiated once per agent and reused across multiple `ainvoke()` calls.

### Constructor

```python
from promptise.callback_handler import PromptiseCallbackHandler
from promptise.observability_config import ObserveLevel

handler = PromptiseCallbackHandler(
    collector,                          # ObservabilityCollector instance
    agent_id="my-agent",                # Optional agent identifier for timeline entries
    record_prompts=False,               # Store full prompt/response text (default: False)
    level=ObserveLevel.STANDARD,        # Detail level (default: STANDARD)
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `collector` | `ObservabilityCollector` | **required** | The collector that receives timeline events |
| `agent_id` | `str \| None` | `None` | Agent identifier for the collector timeline |
| `record_prompts` | `bool` | `False` | Whether to include full prompt/response text in events |
| `level` | `ObserveLevel` | `STANDARD` | Controls detail level: `BASIC` (tools + errors), `STANDARD` (+ LLM turns), `FULL` (+ streaming tokens) |

### Auto-Tracked Events

The handler automatically captures the following without any additional code:

- **LLM turns**: Start/end with latency timing, model name extraction
- **Token counts**: Prompt tokens, completion tokens, total tokens (from `LLMResult` and `usage_metadata`)
- **Tool calls**: Start/end with tool name, arguments, results, and latency
- **Errors**: LLM errors, tool errors, and chain-level errors with traceback
- **Retries**: Retry attempts with attempt number and triggering error
- **Chain events**: Top-level agent input/output with aggregate statistics
- **Streaming tokens**: Token-by-token accumulation at `FULL` level

### Session Totals

After running the agent, cumulative totals are available on the handler:

| Attribute | Type | Description |
|---|---|---|
| `total_prompt_tokens` | `int` | Total input tokens |
| `total_completion_tokens` | `int` | Total output tokens |
| `total_tokens` | `int` | Total tokens (prompt + completion) |
| `llm_call_count` | `int` | Number of LLM calls |
| `tool_call_count` | `int` | Number of tool calls |
| `error_count` | `int` | Number of errors |
| `retry_count` | `int` | Number of retries |

Use `handler.get_summary()` to retrieve all metrics as a dict.

---

## Post-Run Analysis

After running an agent with observability enabled, use the built-in reporting methods:

```python
# Get runtime statistics
stats = agent.get_stats()

# Generate a detailed report
report = agent.generate_report()
```

---

## Enterprise Configuration Example

A production setup with multiple transporters:

```python
from promptise.observability_config import ObservabilityConfig, ObserveLevel, TransporterType

config = ObservabilityConfig(
    level=ObserveLevel.FULL,
    session_name="production-audit",
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
    prometheus_port=9090,
    correlation_id="req-abc-123",
)
```

This configuration:

- Generates an interactive HTML report in `./observability/`
- Writes structured JSON log lines to `./logs/events.jsonl` (compatible with ELK, Datadog, Splunk)
- Prints color-coded events to the console in real time
- Exposes Prometheus metrics on port 9090
- Tags all events with the correlation ID `req-abc-123`

---

!!! tip "Privacy"
    `record_prompts` is `False` by default. Enable it only when you need full prompt/response content in your traces. This is particularly important in production environments handling sensitive data.

---

## What's Next?

- [CLI Reference](cli.md) -- `--observe` flag and CLI commands
- [Memory](memory.md) -- persistent memory with vector search
