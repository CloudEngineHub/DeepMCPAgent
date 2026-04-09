# Context Engine

Unified context assembly with token budgeting. Controls exactly what the LLM sees, in what order, with how many tokens — and trims gracefully when the context window is tight.

```python
from promptise import build_agent, ContextEngine

engine = ContextEngine(model="openai:gpt-5-mini")
engine.add_layer("company_policy", priority=7, content="We follow GDPR strictly.")

agent = await build_agent(
    servers=servers, model="openai:gpt-5-mini", context_engine=engine,
)
```

---

## Why

Without the Context Engine, context is assembled ad-hoc — memory, strategies, prompt blocks, conversation history, and runtime context all inject independently with no coordination. If the total exceeds the model's context window, the LLM either errors out or silently truncates (often losing the user's message at the end).

The Context Engine:
- **Knows the model's context window** (auto-detected or explicit)
- **Counts tokens exactly** (tiktoken for OpenAI, estimation fallback)
- **Assigns priorities** to every context layer
- **Trims by priority** when budget exceeded — conversation history first, then memory, then strategies
- **Reports** per-layer token usage for debugging and optimization

---

## Built-in Layers

The engine registers 13 standard layers with default priorities:

| Priority | Layer | Required | Description |
|----------|-------|----------|-------------|
| 10 | `identity` | ✅ | Agent identity/instructions (never dropped) |
| 10 | `user_message` | ✅ | Current user query (never dropped) |
| 9 | `tools` | ✅ | Tool definitions (never dropped) |
| 9 | `flow` | | Conversation flow phase context |
| 8 | `prompt_blocks` | | PromptBlocks assembly |
| 8 | `output_format` | | Expected output structure |
| 7 | Custom layers | | Developer-added layers |
| 6 | `context_state` | | Runtime: AgentContext state |
| 6 | `mission` | | Runtime: mission objective |
| 5 | `budget` | | Runtime: budget remaining |
| 4 | `inbox` | | Runtime: human operator messages |
| 3 | `memory` | | Long-term memory recall |
| 2 | `strategies` | | Learned strategies from failures |
| 1 | `conversation` | | Conversation history (oldest dropped first) |

Required layers (priority ≥ 9 + `required=True`) are never trimmed, even if they exceed the budget.

---

## Token Counting

The engine uses **exact token counting** when possible:

| Model Provider | Method | Accuracy |
|---------------|--------|----------|
| OpenAI (GPT-*) | `tiktoken` library | Exact |
| Others | Character estimation (chars ÷ 3.5) | ~90% |
| Custom | Your own `Tokenizer` implementation | Developer-defined |

Install tiktoken for exact OpenAI counting: `pip install tiktoken`

### Custom tokenizer

```python
class MyTokenizer:
    def count(self, text: str) -> int:
        return len(my_custom_tokenize(text))

engine = ContextEngine(tokenizer=MyTokenizer())
```

---

## Custom Layers

Add domain-specific context at any priority level:

```python
engine = ContextEngine(model="openai:gpt-5-mini")

# Company knowledge — high priority, always included
engine.add_layer("company_policy", priority=7, content="We follow GDPR...", required=True)

# Seasonal context — medium priority, may be trimmed if window is tight
engine.add_layer("seasonal", priority=4, content="It's Q4 — focus on year-end reports.")

# Background info — low priority, first to be trimmed
engine.add_layer("background", priority=1, content="Historical context about the project...")
```

---

## Trimming

When total context exceeds the token budget, the engine trims lowest-priority non-required layers first:

1. **Conversation history** (priority 1) — oldest messages removed first, preserving user/assistant pairs
2. **Strategies** (priority 2) — truncated from the end
3. **Memory** (priority 3) — truncated from the end
4. **Custom layers** — by their configured priority
5. **Required layers** — NEVER trimmed

```python
report = engine.get_report()
print(f"Budget: {report.budget} tokens")
print(f"Used: {report.total_tokens} tokens ({report.utilization:.0%})")
print(f"Trimmed: {report.trimmed_layers}")
for layer in report.layers:
    print(f"  {layer['name']}: {layer['tokens']} tokens {'(trimmed)' if layer['trimmed'] else ''}")
```

---

## Configuration

```python
ContextEngine(
    model="openai:gpt-5-mini",         # Auto-detect context window (128K)
    model_context_window=128_000,       # Or set explicitly
    response_reserve=4096,              # Reserve tokens for the response
    tokenizer=my_tokenizer,             # Custom token counter
    auto_register_builtins=True,        # Register standard layers
)
```

### Model context windows (auto-detected)

| Model | Window |
|-------|--------|
| GPT-4 | 8,192 |
| GPT-4 Turbo / GPT-4o / GPT-5 | 128,000 |
| Claude 3/4 (all variants) | 200,000 |
| Llama 3 | 8,192 |
| Gemini 2.0 Flash | 1,048,576 |
| Mistral Large | 128,000 |

---

## Opt-In Design

The Context Engine is **completely optional**. Without it, the agent uses the existing context injection system (which works fine for most use cases). Enable it when you need:

- Precise token budgeting for small-context models (8K-32K)
- Custom context layers with controlled priority
- Assembly reports for debugging context usage
- Guaranteed no context window overflow

---

## API Reference

### ContextEngine

| Method | Description |
|---|---|
| `register_layer(name, *, priority, required, trim_strategy)` | Register a named context layer. Higher priority = kept longer during trimming. |
| `set_content(name, content)` | Set or update a layer's content. |
| `get_content(name) -> str` | Get a layer's current content. |
| `clear_content(name)` | Clear a single layer's content (keeps the registration). |
| `clear_all()` | Clear all layer contents (keeps registrations). |
| `remove_layer(name)` | Remove a layer entirely (unregister). |
| `assemble() -> list[dict]` | Assemble all layers into a LangGraph-compatible message list with token budgeting. |
| `count_tokens(text) -> int` | Count tokens using the configured tokenizer. |
| `get_layer_info() -> list[dict]` | Introspect all registered layers (name, priority, token estimate). |

### ContextLayer

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Layer identifier |
| `priority` | `int` | Higher = more important (10 = identity, 1 = conversation) |
| `content` | `str` | Current content |
| `required` | `bool` | If True, never trimmed |
| `trim_strategy` | `str` | `"truncate"` (default) or `"conversation"` (pair-preserving) |

### AssemblyReport

Returned by `assemble()` via `engine.last_report`:

| Field | Type | Description |
|---|---|---|
| `total_tokens` | `int` | Total tokens across all included layers |
| `budget` | `int` | Configured token budget |
| `utilization` | `float` | `total_tokens / budget` (0.0 to 1.0) |
| `layers` | `list[dict]` | Per-layer breakdown (name, priority, tokens, required, trimmed) |
| `trimmed_layers` | `list[str]` | Names of layers that were trimmed |

### Built-in Layer Names

When used with `build_agent(context_engine=engine)`, these layers are auto-populated:

| Name | Priority | Content source |
|---|---|---|
| `identity` | 10 | Agent instructions |
| `user_message` | 10 | Current user input |
| `memory` | 3 | Memory recall results |
| `strategies` | 2 | Adaptive strategy learnings |

Register custom layers for domain-specific context:

```python
engine.register_layer("company_policy", priority=7, required=True)
engine.set_content("company_policy", "Never discuss competitor pricing.")
```

---

## What's Next?

- [Memory](memory.md) — the recall layer that feeds into the engine
- [Adaptive Strategy](adaptive-strategy.md) — the strategies layer
- [Observability](observability.md) — track context usage alongside token costs
