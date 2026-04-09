# RAG (Retrieval-Augmented Generation)

A small, pluggable foundation for adding document retrieval to Promptise agents. The framework ships with base classes you subclass for your own loader, chunker, embedder, and vector store — plus a batteries-included reference implementation (in-memory store + recursive text chunker) you can use immediately or swap out piece by piece as you productionise.

```python
from promptise import (
    build_agent,
    RAGPipeline,
    RecursiveTextChunker,
    InMemoryVectorStore,
    rag_to_tool,
)
from promptise.config import HTTPServerSpec

# Your code: plug in a loader + embedder
pipeline = RAGPipeline(
    loader=MyMarkdownLoader("./knowledge_base"),
    chunker=RecursiveTextChunker(chunk_size=800, overlap=100),
    embedder=MyOpenAIEmbedder(),
    store=InMemoryVectorStore(),
)
await pipeline.index()

# Expose retrieval as a tool the agent can call
docs_tool = rag_to_tool(
    pipeline,
    name="search_internal_docs",
    description="Search Acme Corp's engineering wiki.",
)

agent = await build_agent(
    model="openai:gpt-5-mini",
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    extra_tools=[docs_tool],
)
```

---

## Architecture

```
┌──────────────┐   ┌──────────┐   ┌──────────┐   ┌──────────────┐
│ DocumentLoader│-->│ Chunker │-->│ Embedder │-->│ VectorStore │
└──────────────┘   └──────────┘   └──────────┘   └──────────────┘
                                                        │
                                                        v
                                                  rag_to_tool()
                                                        │
                                                        v
                                                  LangChain Tool
                                                        │
                                                        v
                                                    Your Agent
```

Four base classes, a pipeline orchestrator, and a tool adapter. That's it. Each base class has one or two methods — override them, ship your own implementation, and pass it to `RAGPipeline`.

---

## Core Data Types

| Type | Purpose |
|---|---|
| `Document` | A raw text document with `id`, `text`, and metadata. Whatever your loader produces. |
| `Chunk` | A piece of a document with `id`, `document_id`, `text`, optional `embedding`, and metadata. Produced by the chunker. |
| `RetrievalResult` | A chunk plus a `score` in `[0.0, 1.0]`. Returned by `search()` and `retrieve()`. |
| `IndexReport` | Summary of `pipeline.index()`: documents loaded, chunks created, chunks stored, errors, duration. |

All four are `@dataclass`-based — cheap to construct, easy to pickle, introspectable.

---

## Base Classes (override these)

### DocumentLoader

Load raw documents from wherever they live: filesystem, S3, Notion, Confluence, Postgres, a REST API. The base class has one method:

```python
class DocumentLoader:
    async def load(self) -> list[Document]:
        raise NotImplementedError
```

**Example — loading markdown files from disk:**

```python
from pathlib import Path
from promptise.rag import Document, DocumentLoader

class MarkdownLoader(DocumentLoader):
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    async def load(self) -> list[Document]:
        return [
            Document(
                id=str(p.relative_to(self.root)),
                text=p.read_text(encoding="utf-8"),
                metadata={"source": str(p), "title": p.stem},
            )
            for p in self.root.rglob("*.md")
        ]
```

That's it. The pipeline calls `await loader.load()` during `index()` and feeds the documents to the chunker.

### Chunker

Split documents into retrievable chunks. The base class:

```python
class Chunker:
    async def chunk(self, document: Document) -> list[Chunk]:
        raise NotImplementedError
```

**Built-in: `RecursiveTextChunker`** — splits on natural separators (`\n\n`, `\n`, `. `, ` `) with configurable `chunk_size` and `overlap`. Good default for prose, markdown, and code.

```python
from promptise import RecursiveTextChunker

chunker = RecursiveTextChunker(
    chunk_size=800,       # target chars per chunk
    overlap=100,          # overlap between consecutive chunks
    separators=None,      # defaults to ["\n\n", "\n", ". ", " ", ""]
)
```

Chunk IDs are deterministic (`{document_id}:chunk-{i}`) so re-indexing the same document produces stable IDs — useful for incremental updates.

**Rolling your own** is ~20 lines. Subclass `Chunker` and return a list of `Chunk` objects with `document_id`, `text`, and any metadata you want to pass through.

### Embedder

Turn text into dense vectors. The base class has one method plus one property:

```python
class Embedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    @property
    def dimension(self) -> int:
        raise NotImplementedError
```

**Example — OpenAI embeddings:**

```python
from openai import AsyncOpenAI
from promptise.rag import Embedder

class OpenAIEmbedder(Embedder):
    def __init__(self, model: str = "text-embedding-3-small") -> None:
        self.client = AsyncOpenAI()
        self.model = model
        self._dim = 1536 if "small" in model else 3072

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self.client.embeddings.create(
            model=self.model,
            input=texts,
        )
        return [d.embedding for d in resp.data]

    @property
    def dimension(self) -> int:
        return self._dim
```

Swap for Cohere, VoyageAI, sentence-transformers, Ollama, or an air-gapped local model by changing this one class.

### VectorStore

Persist chunks and serve similarity queries. The base class:

```python
class VectorStore:
    async def add(self, chunks: list[Chunk]) -> None: ...
    async def search(
        self,
        query_embedding: list[float],
        *,
        limit: int = 5,
        filter: dict | None = None,
    ) -> list[RetrievalResult]: ...
    async def delete(self, chunk_ids: list[str]) -> None: ...
    async def delete_by_document(self, document_id: str) -> int: ...
    async def count(self) -> int: ...
    async def close(self) -> None: ...
```

**Built-in: `InMemoryVectorStore`** — cosine-similarity over an in-process list. Zero external dependencies, no persistence. Great for tests, notebooks, and small corpora (< ~10k chunks). When you outgrow it, subclass and wire up Pinecone, Qdrant, Weaviate, pgvector, Milvus, or Chroma.

```python
from promptise import InMemoryVectorStore

store = InMemoryVectorStore(dimension=1536)  # optional dimension enforcement
```

**Rolling your own** means implementing `add`, `search`, and `delete` against your backend. ~50-100 lines for most vector DBs.

---

## RAGPipeline

The orchestrator. Ties a loader + chunker + embedder + store together and exposes two operations: `index()` (build the index) and `retrieve()` (query it).

```python
pipeline = RAGPipeline(
    loader=MyLoader(),
    chunker=RecursiveTextChunker(chunk_size=800, overlap=100),
    embedder=MyEmbedder(),
    store=InMemoryVectorStore(),
)

# Ingest everything from the loader
report = await pipeline.index()
print(f"Indexed {report.documents_loaded} docs -> {report.chunks_stored} chunks")
print(f"Duration: {report.duration_seconds:.2f}s")

# Retrieve
results = await pipeline.retrieve("how do I configure memory?", limit=5)
for r in results:
    print(f"[{r.score:.2f}] {r.metadata.get('source')}: {r.text[:80]}")
```

### Incremental indexing

Pass explicit documents to skip the loader for ad-hoc ingestion:

```python
from promptise import Document

await pipeline.index(documents=[
    Document(id="note-1", text="Meeting notes: ..."),
])
```

### Deletion

Remove a document and all its chunks:

```python
removed = await pipeline.delete_document("note-1")
```

### Defaults

`Chunker` defaults to `RecursiveTextChunker(chunk_size=1000, overlap=100)` if you don't supply one. Every other component is required.

---

## rag_to_tool — expose retrieval to an agent

The glue that turns a `RAGPipeline` into a LangChain tool the agent can call via `extra_tools`. The agent sees a single tool with a `query` argument; calling it runs retrieval and returns formatted results.

```python
from promptise import rag_to_tool

docs_tool = rag_to_tool(
    pipeline,
    name="search_product_docs",
    description="Search Acme Corp's product documentation. Use for how-to questions.",
    limit=5,
    format="markdown",  # "markdown" (default), "json", or "text"
)
```

| Parameter | Default | Purpose |
|---|---|---|
| `name` | `"search_knowledge_base"` | Tool name the LLM sees. Make it specific. |
| `description` | Generic | What's in the knowledge base. The LLM uses this to decide when to call the tool. |
| `limit` | `5` | Default result count. LLM can override. |
| `format` | `"markdown"` | How results are returned to the LLM. |

**Format cheat sheet:**

- `"markdown"` — human + LLM friendly, includes source and title headers
- `"json"` — structured (array of `{score, text, metadata}`), best for downstream processing
- `"text"` — plain text with source prefix, minimal token overhead

---

## Production patterns

### Content hashing for dedup

Use `content_hash()` as part of your document ID to detect unchanged documents and skip re-embedding:

```python
from promptise.rag import content_hash

doc_id = f"{source_path}:{content_hash(text)}"
```

The hash is a stable 12-character string derived from the text — same text always hashes to the same value.

### Metadata filtering

`InMemoryVectorStore.search()` supports exact-match metadata filters. Your custom stores should do the same:

```python
results = await store.search(
    query_embedding,
    limit=5,
    filter={"category": "support", "status": "published"},
)
```

### Hybrid search

`VectorStore` is just a protocol. Subclass to add BM25, reranking, recency boosts, or any hybrid strategy. Return `RetrievalResult` with your combined score.

### Multi-store composition

Run multiple pipelines for different corpora and expose each as its own tool. The agent picks the right one based on the description:

```python
docs_tool = rag_to_tool(docs_pipeline, name="search_docs", description="Product docs.")
tickets_tool = rag_to_tool(tickets_pipeline, name="search_tickets", description="Support tickets.")

agent = await build_agent(
    model="openai:gpt-5-mini",
    extra_tools=[docs_tool, tickets_tool],
)
```

---

## When to use RAG vs. Memory

Both inject external context into the LLM. Different lifecycles:

| | RAG | Memory |
|---|---|---|
| **Source** | Your documents (filesystem, wiki, tickets) | Conversation history, facts the agent observed |
| **Write path** | Offline indexing | Live during agent runs |
| **Trigger** | LLM explicitly calls the tool | Auto-injected before every invocation |
| **Scale** | Millions of chunks | Thousands of memories per user |
| **Typical use** | "What's our refund policy?" | "User's preferred deployment target is GKE" |

Use both together: memory for who the user is, RAG for what the knowledge base says.

---

## Testing

The in-memory components are designed for tests — zero dependencies, deterministic behavior. Wire them up with a fake embedder and you've got an end-to-end RAG test in ~20 lines:

```python
from promptise.rag import (
    Document,
    Embedder,
    InMemoryVectorStore,
    RAGPipeline,
    RecursiveTextChunker,
)

class FakeEmbedder(Embedder):
    async def embed(self, texts):
        return [[float(len(t) % 10) / 10, float(t.count("a")) / max(len(t), 1)] for t in texts]

    @property
    def dimension(self):
        return 2

async def test_retrieval():
    pipeline = RAGPipeline(
        chunker=RecursiveTextChunker(chunk_size=200),
        embedder=FakeEmbedder(),
        store=InMemoryVectorStore(),
    )
    await pipeline.index(documents=[
        Document(id="d1", text="The capital of France is Paris."),
    ])
    results = await pipeline.retrieve("France capital")
    assert any("Paris" in r.text for r in results)
```

See `tests/test_rag.py` in the repo for the full test suite.

---

## Related

- [Memory](memory.md) — auto-injected context from conversation history
- [Tool Optimization](tool-optimization.md) — semantic tool selection for agents with large tool sets
- [Building Agents](agents/building-agents.md) — full `build_agent()` reference
