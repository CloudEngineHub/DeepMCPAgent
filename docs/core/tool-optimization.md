# Tool Optimization

Reduce the token cost of MCP tool definitions sent to the LLM with every invocation.

```python
from promptise import build_agent
from promptise.config import HTTPServerSpec

agent = await build_agent(
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    model="openai:gpt-5-mini",
    optimize_tools=True,  # ~40% token savings on tool definitions
)
```

---

## The Problem

When an agent connects to MCP servers, every tool's full name, description, and JSON Schema is sent to the LLM on every invocation via function-calling. With 20-50+ tools, this costs 5,000-15,000+ tokens per call — just for tool definitions. This is the single largest token cost after the conversation itself.

## How It Works

Tool optimization operates at two layers:

**Layer 1: Static optimization** (applied once at build time) — reduces the per-tool token cost without changing which tools are available:

- **Schema minification** — strips `description` metadata from Pydantic Field schemas. The LLM still sees field names, types, and required status — but not verbose per-field descriptions.
- **Description truncation** — caps tool-level descriptions at N characters (word boundary).
- **Depth flattening** — replaces deeply nested objects with `dict` beyond a configurable depth.

**Layer 2: Semantic tool selection** (applied per invocation) — the biggest optimization. Instead of sending all 50 tools, only the most relevant tools are selected for each query:

1. At build time, all tool descriptions are embedded using a lightweight local model
2. Before each `ainvoke()`, the user's query is embedded and compared against tool descriptions
3. Only the top-K most relevant tools are included in the LLM call
4. A `request_more_tools` fallback tool is included so the agent can self-recover if the semantic selection missed something

---

## Quick Start

### One-liner

```python
agent = await build_agent(
    servers=servers, model="openai:gpt-5-mini",
    optimize_tools=True,  # Uses "minimal" preset
)
```

### Preset levels

```python
# Static optimization only — safe, no behavioral change
agent = await build_agent(servers=servers, model="openai:gpt-5-mini", optimize_tools="minimal")

# Deeper minification + nested description stripping
agent = await build_agent(servers=servers, model="openai:gpt-5-mini", optimize_tools="standard")

# Full semantic selection — biggest savings, per-invocation tool filtering
agent = await build_agent(servers=servers, model="openai:gpt-5-mini", optimize_tools="semantic")
```

### Fine-grained control

```python
from promptise import ToolOptimizationConfig, OptimizationLevel

agent = await build_agent(
    servers=servers,
    model="openai:gpt-5-mini",
    optimize_tools=ToolOptimizationConfig(
        level=OptimizationLevel.SEMANTIC,
        max_description_length=100,
        semantic_top_k=5,
        preserve_tools={"critical_payment_tool", "auth_tool"},
    ),
)
```

---

## Three Preset Levels

| Setting | `minimal` | `standard` | `semantic` |
|---|---|---|---|
| Schema minification | Yes | Yes | Yes |
| Max description length | 200 chars | 150 chars | 100 chars |
| Strip nested descriptions | No | Yes | Yes |
| Max schema depth | No limit | 3 | 2 |
| Semantic selection | No | No | Yes |
| Semantic top-K | — | — | 8 |
| Fallback tool | — | — | Yes |
| **Estimated savings** | **~40%** | **~55%** | **~85%** |

---

## Configuration Reference

### ToolOptimizationConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `level` | `OptimizationLevel \| None` | `None` | Preset level. Any explicit field overrides the preset. |
| `minify_schema` | `bool \| None` | from preset | Strip `description` from Pydantic Field metadata |
| `max_description_length` | `int \| None` | from preset | Truncate tool descriptions at N chars |
| `strip_nested_descriptions` | `bool \| None` | from preset | Remove descriptions from nested model fields |
| `max_schema_depth` | `int \| None` | from preset | Flatten nested objects beyond this depth to `dict` |
| `semantic_selection` | `bool \| None` | from preset | Enable per-invocation semantic tool selection |
| `semantic_top_k` | `int \| None` | from preset | Number of tools to select per invocation |
| `always_include_fallback` | `bool \| None` | from preset | Include `request_more_tools` fallback |
| `embedding_model` | `str \| None` | `"all-MiniLM-L6-v2"` | Model name or **local path** for sentence-transformers |
| `preserve_tools` | `set[str] \| None` | `None` | Tool names that are never optimized and always selected |

### OptimizationLevel

| Level | Value |
|---|---|
| `OptimizationLevel.MINIMAL` | `"minimal"` |
| `OptimizationLevel.STANDARD` | `"standard"` |
| `OptimizationLevel.SEMANTIC` | `"semantic"` |

---

## Semantic Selection Details

### Embedding model

Semantic selection embeds tool descriptions using `sentence-transformers`. The default model is `all-MiniLM-L6-v2` (384 dimensions, no API key needed). On first use it downloads from HuggingFace Hub and caches locally at `~/.cache/huggingface/`. After that, it runs fully offline.

#### Local / air-gapped deployments

For enterprises that cannot access external networks, download the model files once and point to a local directory:

```bash
# Download the model on a machine with internet
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2').save('/models/all-MiniLM-L6-v2')"
```

```python
# Use the local model — zero network calls
agent = await build_agent(
    servers=servers,
    model="openai:gpt-5-mini",
    optimize_tools=ToolOptimizationConfig(
        level=OptimizationLevel.SEMANTIC,
        embedding_model="/models/all-MiniLM-L6-v2",
    ),
)
```

You can also use any other `sentence-transformers`-compatible model:

```python
optimize_tools=ToolOptimizationConfig(
    level=OptimizationLevel.SEMANTIC,
    embedding_model="BAAI/bge-small-en-v1.5",  # or any local path
)
```

### The `request_more_tools` fallback

When semantic selection is active, a special fallback tool is automatically included:

```
Tool: request_more_tools
Description: "If you need a tool that is not currently available, call this
             to see all available tools and their descriptions."
```

If the semantic search missed a relevant tool, the agent can self-recover by calling this and then retrying. This ensures the agent is never stuck.

### `preserve_tools`

Tools listed in `preserve_tools` are:

1. Never optimized (full descriptions and schemas are preserved)
2. Always included in semantic selection (regardless of relevance score)

Use this for critical tools that the agent must always have access to:

```python
optimize_tools=ToolOptimizationConfig(
    level=OptimizationLevel.SEMANTIC,
    preserve_tools={"process_payment", "verify_identity"},
)
```

---

## Combining with Other Features

Tool optimization composes with all other agent features:

```python
agent = await build_agent(
    servers=servers,
    model="openai:gpt-5-mini",
    optimize_tools="standard",
    observe=True,           # observability still tracks all tool calls
    memory=provider,        # memory injection happens before tool selection
    sandbox=True,           # sandbox tools are added after optimization
)
```

---

## FAQ

**Does optimization affect tool call quality?**

Schema minification removes per-field descriptions but keeps field names, types, and required status. Most LLMs infer field purpose from well-named parameters (e.g., `user_id`, `email`, `start_date`). For tools with ambiguous parameter names, use `preserve_tools` to exempt them.

**What if semantic selection picks the wrong tools?**

The `request_more_tools` fallback lets the agent self-recover. It lists all available tools, and the agent can retry with the right one. In practice, semantic selection with top-K=8 covers most use cases.

**Does this work with all LLM providers?**

Yes. Tool optimization modifies the tool definitions before they reach LangChain — it works with OpenAI, Anthropic, Ollama, and any other provider.

---

## What's Next?

- [Building Agents](agents/building-agents.md) — the `build_agent()` function and all its parameters
- [Memory](memory.md) — persistent memory with vector search
- [Observability](observability.md) — track token usage and see exactly what the LLM receives
