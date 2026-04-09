# Model Fallback

Automatic failover across multiple LLM providers. If the primary model fails, the next one in the chain handles the request — no downtime, no manual switching.

```python
from promptise import build_agent, FallbackChain

agent = await build_agent(
    model=FallbackChain([
        "openai:gpt-5-mini",           # Primary
        "anthropic:claude-sonnet-4-20250514",    # Fallback 1
        "ollama:llama3",                # Fallback 2 (local, always up)
    ]),
    servers=servers,
)
# If OpenAI is down → Claude handles it. If both down → local Llama.
```

---

## Why

Single-provider agents are a single point of failure. When OpenAI has an outage at 3am, your agent goes down with it. With a fallback chain, the next provider picks up seamlessly. With 3 providers at 99.9% uptime each, compound availability reaches 99.9999%.

---

## How It Works

`FallbackChain` is a `BaseChatModel` — LangChain and Promptise treat it like any other model. Under the hood:

1. Primary model receives the request
2. If it fails (error, timeout, rate limit), the next model is tried
3. Each model has an independent **circuit breaker** — after N consecutive failures, the model is skipped entirely for a recovery period
4. When the recovery period elapses, one test request is sent (half-open state)
5. If the test succeeds, the circuit closes and the model resumes normal traffic

---

## Configuration

```python
FallbackChain(
    models=["openai:gpt-5-mini", "anthropic:claude-sonnet-4-20250514", "ollama:llama3"],
    timeout_per_model=15.0,   # 15s max per model attempt (0 = no limit)
    global_timeout=30.0,      # 30s max across ALL attempts (0 = no limit)
    failure_threshold=3,      # Circuit opens after 3 consecutive failures
    recovery_timeout=60.0,    # 60s before testing a tripped circuit
    on_fallback=my_callback,  # Optional: called on each fallback activation
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `models` | `list[str \| BaseChatModel]` | **required** | Ordered list of models. First is primary. |
| `timeout_per_model` | `float` | `0` | Max seconds per model attempt. `0` = provider default. |
| `global_timeout` | `float` | `0` | Max seconds across all attempts combined. |
| `failure_threshold` | `int` | `3` | Consecutive failures before circuit opens. |
| `recovery_timeout` | `float` | `60.0` | Seconds before a tripped circuit allows a test. |
| `on_fallback` | `callable \| None` | `None` | `(primary_id, fallback_id, error)` callback. |

---

## Circuit Breaker States

| State | Behavior |
|-------|----------|
| **Closed** | Normal operation. Requests go to this model. |
| **Open** | Model is skipped entirely. Too many recent failures. |
| **Half-open** | Recovery period elapsed. One test request is allowed. If it succeeds → closed. If it fails → open again. |

---

## Monitoring

```python
chain = FallbackChain([...])
status = chain.get_chain_status()
# [
#     {"model_id": "openai:gpt-5-mini", "state": "closed", "failures": 0, "is_primary": True},
#     {"model_id": "claude-sonnet", "state": "open", "failures": 5, "is_primary": False},
# ]

chain.active_model  # "openai:gpt-5-mini" (first non-skipped model)
```

---

## Fallback Notifications

Combine with the [Events](events.md) system to get notified when fallbacks activate:

```python
from promptise import FallbackChain, EventNotifier, WebhookSink

def on_fallback(primary, fallback, error):
    print(f"Switched from {primary} to {fallback}: {error}")

agent = await build_agent(
    model=FallbackChain(
        ["openai:gpt-5-mini", "anthropic:claude-sonnet-4-20250514"],
        on_fallback=on_fallback,
    ),
    events=EventNotifier(sinks=[
        WebhookSink("https://hooks.slack.com/...", events=["invocation.error"]),
    ]),
    servers=servers,
)
```

---

## What's Next?

- [Events](events.md) — get notified when models fail or fallbacks activate
- [Observability](observability.md) — track which model served each request
- [Building Agents](agents/building-agents.md) — full `build_agent()` parameter reference
