# Streaming with Tool Visibility

Stream agent execution with real-time tool activity — see which tools are being called, their results, and the LLM generating text token by token.

```python
async for event in agent.astream_with_tools(
    {"messages": [{"role": "user", "content": "Check my order status"}]},
    caller=CallerContext(user_id="user-42"),
):
    match event.type:
        case "tool_start":
            print(f"🔧 {event.tool_display_name}...")
        case "tool_end":
            print(f"   → {event.tool_summary}")
        case "token":
            print(event.text, end="", flush=True)
        case "done":
            print(f"\n✅ Done in {event.duration_ms:.0f}ms")
```

Output:
```
🔧 Searching orders...
   → Found: Order #4521, shipped March 20
🔧 Getting tracking info...
   → Status: In transit, ETA March 25
Your order #4521 shipped on March 20 and is currently in transit.
Expected delivery: March 25.
✅ Done in 2340ms
```

---

## Event Types

| Type | Class | When | Key Fields |
|------|-------|------|-----------|
| `tool_start` | `ToolStartEvent` | Tool begins executing | `tool_name`, `tool_display_name`, `arguments`, `tool_index` |
| `tool_end` | `ToolEndEvent` | Tool finishes | `tool_name`, `tool_summary`, `duration_ms`, `success` |
| `token` | `TokenEvent` | LLM generates a token | `text`, `cumulative_text` |
| `done` | `DoneEvent` | Agent finished | `full_response`, `tool_calls`, `duration_ms`, `cache_hit` |
| `error` | `ErrorEvent` | Something went wrong | `message`, `recoverable` |

---

## Server-Sent Events (FastAPI)

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()

@app.post("/chat")
async def chat(request: ChatRequest):
    async def event_stream():
        async for event in agent.astream_with_tools(
            {"messages": [{"role": "user", "content": request.message}]},
            caller=CallerContext(user_id=request.user_id),
        ):
            yield f"data: {event.to_json()}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

### Frontend (JavaScript)

```javascript
const source = new EventSource("/chat");
source.onmessage = (e) => {
  const event = JSON.parse(e.data);
  switch (event.type) {
    case "tool_start":
      showToolIndicator(event.tool_display_name);
      break;
    case "tool_end":
      updateToolResult(event.tool_summary);
      break;
    case "token":
      appendText(event.text);
      break;
    case "done":
      finishResponse(event.full_response);
      break;
    case "error":
      showError(event.message);
      break;
  }
};
```

---

## Tool Display Names

Tool names are automatically converted to human-readable strings:

| Raw Name | Display Name |
|----------|-------------|
| `search_customers` | Searching customers |
| `get_order_status` | Getting order status |
| `hr_list_employees` | Listing employees |
| `create_ticket` | Creating ticket |
| `deploy_service` | Deploying service |

Override with custom names:

```python
async for event in agent.astream_with_tools(
    input,
    tool_display_names={
        "pg_query": "Querying database",
        "s3_upload": "Uploading to cloud storage",
    },
)
```

---

## Tool Result Summaries

Tool results are automatically summarized for display:

- JSON dicts: `"name: Alice, status: active (+3 more)"`
- JSON lists: `"Found 5 result(s)"`
- Plain text: truncated to 120 characters

---

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `input` | `dict` | required | Agent input (same as `ainvoke`) |
| `caller` | `CallerContext` | `None` | Per-request identity |
| `include_arguments` | `bool` | `True` | Include tool arguments in events |
| `tool_display_names` | `dict[str, str]` | `None` | Custom display name overrides |

---

## Security

- **Argument redaction**: Tool arguments are run through guardrails before streaming. PII and credentials are replaced with labels.
- **Error sanitization**: `ErrorEvent.message` is always generic — no internal stack traces, file paths, or database URLs.
- **Stream cancellation**: Closing the SSE connection cancels the agent's async task. No wasted LLM calls.
- **No cumulative arguments**: `TokenEvent.cumulative_text` only contains LLM text, never tool arguments.

---

## Integration

`astream_with_tools()` runs the full agent pipeline — same guarantees as `ainvoke()`:

- ✅ Input guardrails (block injection → ErrorEvent)
- ✅ Memory injection (relevant context before LLM call)
- ✅ Output guardrails (PII redaction on final response)
- ✅ Observability (callback handler records timeline)
- ✅ Event notifications (invocation.start/complete emitted)
- ✅ CallerContext propagation (multi-user safe)

---

## Helper Functions

### tool_display_name()

Convert raw MCP tool names to human-readable labels:

```python
from promptise.streaming import tool_display_name

tool_display_name("search_customers")      # "Searching customers"
tool_display_name("get_order_status")      # "Getting order status"
tool_display_name("deploy_production")     # "Deploying production"
```

| Parameter | Type | Description |
|---|---|---|
| `tool_name` | `str` | Raw tool name (e.g. `"search_customers"`) |
| `overrides` | `dict[str, str] \| None` | Custom name mappings (tool_name → display text) |
| **Returns** | `str` | Human-readable display name |

### tool_summary()

Summarize tool output for the stream:

```python
from promptise.streaming import tool_summary

tool_summary('{"results": [...100 items...]}')  # '{"results": [...100 items...]}'[:120]
tool_summary(None)                               # "Done"
tool_summary("")                                 # "Done"
```

| Parameter | Type | Description |
|---|---|---|
| `result` | `str \| None` | Raw tool output |
| `max_length` | `int` | Truncation limit (default: 120) |
| **Returns** | `str` | Summarized output |

### Event Serialization

All stream events support serialization:

```python
event.to_dict()  # Returns dict
event.to_json()  # Returns JSON string (for SSE)
```

---

## What's Next?

- [Events & Notifications](events.md) -- webhook alerts for invocation, tool, and guardrail events
- [Guardrails](guardrails.md) -- security scanning that redacts tool arguments in the stream
- [Observability](observability.md) -- detailed execution traces alongside streaming
