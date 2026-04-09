# Memory API Reference

Agent memory integration layer with auto-injection. Provides a `MemoryProvider` protocol and adapters for external memory backends (Mem0, ChromaDB) plus a lightweight `InMemoryProvider` for testing. The key feature is auto-injection: `MemoryAgent` wraps any LangGraph agent and transparently prepends relevant memories to every invocation.

## MemoryResult

::: promptise.memory.MemoryResult
    options:
      show_source: false
      heading_level: 3

## MemoryProvider

::: promptise.memory.MemoryProvider
    options:
      show_source: false
      heading_level: 3

## InMemoryProvider

::: promptise.memory.InMemoryProvider
    options:
      show_source: false
      heading_level: 3

## ChromaProvider

::: promptise.memory.ChromaProvider
    options:
      show_source: false
      heading_level: 3

## Mem0Provider

::: promptise.memory.Mem0Provider
    options:
      show_source: false
      heading_level: 3

## MemoryAgent

::: promptise.memory.MemoryAgent
    options:
      show_source: false
      heading_level: 3

## sanitize_memory_content

::: promptise.memory.sanitize_memory_content
    options:
      show_source: false
      heading_level: 3

