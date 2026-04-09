# RAG API Reference

The `promptise.rag` module provides a pluggable foundation for retrieval-augmented generation: base classes you subclass for your loader, chunker, embedder, and vector store, plus a pipeline orchestrator and LangChain tool adapter.

See the [RAG guide](../core/rag.md) for architecture overview and usage patterns.

---

## Data Types

### Document

::: promptise.rag.Document
    options:
      show_source: false
      heading_level: 4

### Chunk

::: promptise.rag.Chunk
    options:
      show_source: false
      heading_level: 4

### RetrievalResult

::: promptise.rag.RetrievalResult
    options:
      show_source: false
      heading_level: 4

### IndexReport

::: promptise.rag.IndexReport
    options:
      show_source: false
      heading_level: 4

---

## Base Classes

Override these to plug in your own RAG backend.

### DocumentLoader

::: promptise.rag.DocumentLoader
    options:
      show_source: false
      heading_level: 4

### Chunker

::: promptise.rag.Chunker
    options:
      show_source: false
      heading_level: 4

### Embedder

::: promptise.rag.Embedder
    options:
      show_source: false
      heading_level: 4

### VectorStore

::: promptise.rag.VectorStore
    options:
      show_source: false
      heading_level: 4

---

## Built-in Implementations

### RecursiveTextChunker

::: promptise.rag.RecursiveTextChunker
    options:
      show_source: false
      heading_level: 4

### InMemoryVectorStore

::: promptise.rag.InMemoryVectorStore
    options:
      show_source: false
      heading_level: 4

---

## Pipeline

### RAGPipeline

::: promptise.rag.RAGPipeline
    options:
      show_source: false
      heading_level: 4

---

## Tool Adapter

### rag_to_tool

::: promptise.rag.rag_to_tool
    options:
      show_source: false
      heading_level: 4

---

## Utilities

### content_hash

::: promptise.rag.content_hash
    options:
      show_source: false
      heading_level: 4
