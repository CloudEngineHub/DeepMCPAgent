# Memory

Give agents persistent memory with vector search, enabling context-aware responses across conversations.

```python
from promptise import build_agent
from promptise.config import HTTPServerSpec
from promptise.memory import ChromaProvider

provider = ChromaProvider(
    collection_name="agent_memory",
    persist_directory=".promptise/chroma",
)

agent = await build_agent(
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    model="openai:gpt-5-mini",
    memory=provider,
)
# Every ainvoke() now automatically searches memory and injects relevant context.
```

---

## Concepts

Promptise memory has two layers:

- **Auto-injection** via `MemoryAgent` -- before every agent invocation, relevant memories are searched and injected as a `SystemMessage`. The agent sees contextual history without needing explicit memory tools.
- **Provider protocol** -- a simple async interface (`search`, `add`, `delete`, `close`) that any backend can implement.

Three providers ship with the framework, covering development through production use cases.

---

## MemoryProvider Protocol

All memory providers implement this async protocol:

```python
class MemoryProvider(Protocol):
    async def search(self, query: str, *, limit: int = 5) -> list[MemoryResult]: ...
    async def add(self, content: str, *, metadata: dict | None = None) -> str: ...
    async def delete(self, memory_id: str) -> bool: ...
    async def close(self) -> None: ...
```

| Method | Returns | Description |
|---|---|---|
| `search(query, limit=5)` | `list[MemoryResult]` | Search for memories relevant to the query |
| `add(content, metadata=None)` | `str` | Store a new memory, returns its ID |
| `delete(memory_id)` | `bool` | Delete a memory by ID |
| `close()` | `None` | Release resources (connections, file handles) |

---

## MemoryResult

Search results are returned as `MemoryResult` dataclasses:

| Field | Type | Description |
|---|---|---|
| `content` | `str` | The stored text |
| `score` | `float` | Relevance score (0.0 = no match, 1.0 = perfect) |
| `memory_id` | `str` | Unique identifier for this entry |
| `metadata` | `dict` | Provider-specific metadata |

Scores are clamped to `[0.0, 1.0]` on construction.

---

## Providers

### InMemoryProvider

Substring-search provider for testing and development. No persistence, no embeddings.

```python
from promptise.memory import InMemoryProvider

provider = InMemoryProvider(max_entries=1_000)

await provider.add("Pipeline had 5% error rate at 07:30")
await provider.add("User prefers dark mode")

results = await provider.search("error rate")
# Matches entries containing the substring "error rate"
```

| Feature | Value |
|---|---|
| Search method | Case-insensitive substring matching |
| Persistence | None (in-memory only) |
| Dependencies | None |
| Best for | Testing, development, ephemeral agents |

!!! warning "Not for production"
    `InMemoryProvider` has no semantic understanding. The query `"deployment issues"` will **not** match content containing `"deploy"`. Use `ChromaProvider` or `Mem0Provider` for production workloads.

### ChromaProvider

Local vector similarity search with automatic embedding generation. Wraps [ChromaDB](https://www.trychroma.com/).

```python
from promptise.memory import ChromaProvider

# Ephemeral (no persistence)
provider = ChromaProvider(collection_name="agent_memory")

# Persistent (survives restarts)
provider = ChromaProvider(
    collection_name="agent_memory",
    persist_directory=".promptise/chroma",
)

await provider.add(
    "Pipeline had 5% error rate at 07:30",
    metadata={"source": "health-check", "severity": "warning"},
)

# Semantic search -- finds related content even without exact matches
results = await provider.search("deployment issues")
```

| Feature | Value |
|---|---|
| Search method | Vector similarity (cosine distance) |
| Default embedding model | `all-MiniLM-L6-v2` (runs locally, no API key) |
| Persistence | Optional (`persist_directory` parameter) |
| Dependencies | `pip install "promptise[all]"` |
| Best for | Production agents needing semantic recall |

Constructor parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `collection_name` | `str` | `"agent_memory"` | ChromaDB collection name |
| `persist_directory` | `str \| None` | `None` | Path for persistent storage (None = ephemeral) |
| `embedding_function` | `Any` | `None` | Custom ChromaDB embedding function |

### Mem0Provider

Wraps [Mem0](https://github.com/mem0ai/mem0) for hybrid vector + graph search. Can run fully local (with Ollama) or via the Mem0 cloud platform.

```python
from promptise.memory import Mem0Provider

provider = Mem0Provider(user_id="user-42")

await provider.add("User prefers dark mode")
results = await provider.search("theme preferences")
```

| Feature | Value |
|---|---|
| Search method | Hybrid vector + optional graph search |
| Persistence | Managed by Mem0 |
| Dependencies | `pip install "promptise[all]"` |
| Best for | Multi-user agents, knowledge graphs, cloud deployments |

Constructor parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `user_id` | `str` | `"default"` | Scopes memories to a user |
| `agent_id` | `str \| None` | `None` | Optional agent identifier for multi-agent scoping |
| `config` | `dict \| None` | `None` | Mem0 configuration dict (for self-hosted setups) |

---

## MemoryAgent

`MemoryAgent` wraps any LangGraph agent with automatic memory context injection. Before every `ainvoke()`, it:

1. Extracts the user query from the input
2. Searches the memory provider for relevant content
3. Injects matching results as a `SystemMessage`
4. Invokes the inner agent
5. Optionally stores the exchange in memory (if `auto_store=True`)

```python
from promptise.memory import MemoryAgent, ChromaProvider

provider = ChromaProvider(persist_directory=".promptise/chroma")
memory_agent = MemoryAgent(
    inner=agent_graph,
    provider=provider,
    max_memories=5,
    min_score=0.3,
    timeout=5.0,
    auto_store=True,
)

result = await memory_agent.ainvoke({"messages": [{"role": "user", "content": "Hello"}]})
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `inner` | `Any` | required | The wrapped LangGraph agent |
| `provider` | `MemoryProvider` | required | Memory provider instance |
| `max_memories` | `int` | `5` | Max results to inject per invocation |
| `min_score` | `float` | `0.0` | Min relevance score threshold |
| `timeout` | `float` | `5.0` | Max seconds to wait for memory search |
| `auto_store` | `bool` | `False` | Auto-store each exchange after invocation |

!!! tip "Graceful degradation"
    If the memory provider fails (timeout, connection error), the agent continues normally without memory context. Memory never blocks execution.

---

## Integration with build_agent

The simplest way to add memory is through `build_agent()`:

```python
from promptise import build_agent
from promptise.memory import ChromaProvider

provider = ChromaProvider(persist_directory=".promptise/chroma")

agent = await build_agent(
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    model="openai:gpt-5-mini",
    memory=provider,
)
```

This automatically wraps the agent graph in a `MemoryAgent`.

---

## Integration with Agent Runtime

In the Agent Runtime, memory is configured through `ContextConfig`:

```python
from promptise.runtime import ProcessConfig, ContextConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    context=ContextConfig(
        memory_provider="chroma",             # "in_memory", "chroma", or "mem0"
        memory_auto_store=True,               # Auto-store exchanges
        memory_max=5,                         # Max memories per invocation
        memory_min_score=0.3,                 # Min relevance score
        memory_collection="agent_memory",     # ChromaDB collection name
        memory_persist_directory=".promptise/chroma",
        conversation_max_messages=50,         # Short-term buffer size
    ),
)
```

Or through a `.agent` manifest:

```yaml
memory:
  provider: chroma
  auto_store: true
  max: 5
  min_score: 0.3
  collection: agent_memory
  persist_directory: .promptise/chroma
```

---

## Security: Memory Sanitization

Injected memory content is sanitized before reaching the agent to mitigate prompt injection attacks. The `sanitize_memory_content()` function:

- Truncates content to a safe injection length (2,000 characters)
- Strips known prompt-injection patterns (`SYSTEM:`, `[INST]`, `<<SYS>>`, etc.)
- Removes memory fence markers to prevent content from escaping the context block

The injected memory block is wrapped in `<memory_context>` fences with explicit instructions that the agent should treat the content as factual context only and not follow any instructions within it.

---

## What's Next?

- [Sandbox](sandbox.md) -- execute untrusted code safely in isolated containers
- [Observability](observability.md) -- track token usage
- [Context & State](../runtime/context.md) -- AgentContext and state management
- [Conversation Management](../runtime/conversation.md) -- ConversationBuffer
