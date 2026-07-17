---
title: "RAG vs Agent Memory: Which Does Your Agent Need?"
description: "An honest teardown of the conflation 'memory = RAG.' Memory is per-user facts injected automatically; RAG is a document-retrieval tool the agent chooses to…"
keywords: "RAG for agents, agent memory vs retrieval, when to use RAG, document retrieval for LLM agents, RAG vs vector memory"
date: 2026-07-16
slug: rag-for-agents
categories:
  - Memory & RAG
---

# RAG vs Agent Memory: Which Does Your Agent Need?

If you have shopped around for RAG for agents, you have probably noticed that "memory" and "RAG" get used as if they were the same feature. They are not. Both push external text into the model's context, but they have different sources, different write paths, and — critically — different triggers. Confuse them and you end up bolting a document-retrieval pipeline onto a problem that a simple per-user fact store would have solved, or vice versa. By the end of this post you will have a clear decision table, a runnable Promptise Foundry example, and an honest read on when Promptise's built-in RAG is enough versus when you should reach for a dedicated ingestion library.

## Agent memory vs retrieval: two different lifecycles

The cleanest way to separate the two is by asking *who decides when the context shows up*.

**Memory is auto-injected.** In Promptise, a [memory provider](../../core/memory.md) is searched before every single invocation. The agent does not choose to look things up — the framework runs a similarity search against the user's stored memories and injects the relevant ones into the system prompt automatically, with prompt-injection mitigation applied. Memory holds facts the agent *observed*: "the user's preferred deployment target is GKE," "they are on the enterprise plan," "they already tried restarting the service."

**RAG is a tool the agent calls.** A RAG pipeline is exposed to the model as a tool with a `query` argument. The LLM decides, mid-reasoning, that it needs to look something up, calls the tool, and the retrieved chunks come back as a tool result. Nothing happens unless the model asks for it. RAG holds documents you *own*: the refund policy, the engineering wiki, last quarter's incident reports.

That difference in trigger — automatic versus model-initiated — is the whole ballgame. It drives cost, latency, and how you reason about correctness.

## What agent memory actually does

Memory is per-user, relatively small, and written live during agent runs. You configure it once on `build_agent()` and forget about it:

```python
from promptise import build_agent
from promptise.memory import ChromaProvider

agent = await build_agent(
    model="openai:gpt-5-mini",
    memory=ChromaProvider(persist_directory="./memory"),
    instructions="You are a support agent. Remember what each user tells you.",
)
```

From then on, every `ainvoke` and `chat` call transparently searches that store scoped to the caller and prepends the top matches. Promptise ships three providers — `InMemoryProvider` for tests, `ChromaProvider` for local persistent vector search, and `Mem0Provider` for enterprise graph memory — all behind the same one-parameter interface. If you want the full mental model for how observed facts accumulate over a user's lifetime, the [AI Agent Memory: The Complete Guide for Python Devs](ai-agent-memory.md) post walks through it end to end.

One more thing memory is *not*: it is not your conversation transcript. Short-term turn-by-turn history is handled separately by a [conversation store](../../core/conversations.md) (`chat()` loads, invokes, and persists automatically). Memory is the distilled, cross-session layer that sits on top. Three distinct systems, three distinct jobs.

## What RAG for agents actually does

RAG — retrieval-augmented generation — is document retrieval for LLM agents. You index a corpus offline, then let the agent query it on demand. Promptise's RAG foundation is deliberately small: four base classes you subclass (`DocumentLoader`, `Chunker`, `Embedder`, `VectorStore`), a `RAGPipeline` that orchestrates the `load → chunk → embed → store → retrieve` flow, and `rag_to_tool()` to expose the result to an agent. The full contract lives in the [RAG documentation](../../core/rag.md).

Here is a complete, runnable pipeline. It uses the built-in `RecursiveTextChunker` and `InMemoryVectorStore`, plugs in a real OpenAI embedder, and hands the pipeline to an agent as a tool with `rag_to_tool`:

```python
import asyncio
from openai import AsyncOpenAI
from promptise import (
    build_agent,
    Document,
    RAGPipeline,
    RecursiveTextChunker,
    InMemoryVectorStore,
    rag_to_tool,
)
from promptise.rag import Embedder


class OpenAIEmbedder(Embedder):
    def __init__(self, model: str = "text-embedding-3-small") -> None:
        self.client = AsyncOpenAI()
        self.model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self.client.embeddings.create(model=self.model, input=texts)
        return [d.embedding for d in resp.data]

    @property
    def dimension(self) -> int:
        return 1536


async def main():
    # 1. Wire up the pipeline: chunker + embedder + store
    pipeline = RAGPipeline(
        chunker=RecursiveTextChunker(chunk_size=500, overlap=50),
        embedder=OpenAIEmbedder(),
        store=InMemoryVectorStore(),
    )

    # 2. Index a couple of policy documents (skip the loader for ad-hoc ingestion)
    await pipeline.index(documents=[
        Document(id="refunds", text=(
            "Refunds are processed within 5 business days. Customers must "
            "request a refund within 30 days of the original purchase date."
        )),
        Document(id="shipping", text=(
            "Standard shipping takes 3-5 business days. Express shipping is "
            "next-day for orders placed before 2pm local time."
        )),
    ])

    # 3. Expose retrieval as a tool the agent can choose to call
    docs_tool = rag_to_tool(
        pipeline,
        name="search_policies",
        description="Search Acme Corp policy docs (refunds, shipping).",
    )

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        extra_tools=[docs_tool],
        instructions="Answer customer questions using the policy search tool.",
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "How long do refunds take?"}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

The agent sees `search_policies`, decides the refund question warrants a lookup, calls the tool, and answers from the retrieved chunk. Because it is a normal tool call, the retrieval is counted by the budget, health, and journal subsystems just like any other tool — no special casing. Swap `InMemoryVectorStore` for a Pinecone or Qdrant subclass and the agent code above does not change at all.

## RAG vs vector memory: the decision table

Both are backed by vector search, which is exactly why they get conflated. But the lifecycle decides which one you want:

| Question | Reach for **Memory** | Reach for **RAG** |
|---|---|---|
| Source of the context? | Facts the agent observed about a user | Documents you own and curate |
| Who writes it? | The agent, live, during runs | You, offline, during indexing |
| Who triggers retrieval? | The framework, automatically | The LLM, by calling a tool |
| Scope? | Per-user, thousands of memories | Global corpus, millions of chunks |
| Typical query? | "What plan is this user on?" | "What's our refund policy?" |
| Cost of being wrong? | Missing personalization | Hallucinated facts |

A useful heuristic on *when to use RAG*: if the answer lives in a document that is the same for every user, it is RAG. If the answer is specific to *this* user and was learned by talking to them, it is memory. And you will frequently want both — memory to know *who* is asking, RAG to know *what the knowledge base says*. They compose cleanly because RAG is just another entry in `extra_tools` while memory is a separate `build_agent()` parameter.

## When a dedicated RAG library is the better fit

Here is the honest part. Promptise's RAG for agents is a *foundation*, not a batteries-included ingestion platform. The base classes give you the structure and the in-memory reference implementation; you supply the loader and embedder. That is a great fit when:

- You want retrieval that lives inside the agent's governance (budget, journals, health) with zero extra dependencies.
- Your corpus is modest and you are happy adapting one `VectorStore` subclass to your database.
- You want to control chunking, filtering, and formatting explicitly.

It is *not* the better fit when you need a heavy ingestion pipeline — PDF/HTML/OCR parsing, dozens of connectors, automatic incremental sync, query rewriting, or built-in rerankers. If you are living in that world, **LlamaIndex** or **LangChain** ship those out of the box, and there is no shame in using them. The clean way to combine them is to wrap their retriever in a Promptise `VectorStore` (or a plain tool) so the agent still gets the governance layer while their library does the ingestion heavy lifting. Promptise does not try to out-feature a dedicated RAG framework; it tries to make retrieval a first-class, governed tool in an agent runtime.

## Frequently asked questions

### Is agent memory just RAG over conversation history?

No. Memory is per-user and auto-injected before every invocation without the model asking; RAG is a global document corpus the model queries by calling a tool. They share vector search as an implementation detail but differ in source, write path, and trigger. Conversation history is a third, separate thing handled by the [conversation store](../../core/conversations.md).

### Can I use RAG and memory in the same agent?

Yes, and it is common. Pass a memory provider via the `memory=` parameter and one or more RAG tools via `extra_tools=`. Memory personalizes the agent to the user; RAG grounds it in your documents. Neither interferes with the other.

### Do RAG tool calls count against my agent's budget?

Yes. `rag_to_tool()` returns a standard tool, so a retrieval is an ordinary tool call — tracked by the budget, health-monitoring, and journal subsystems. That means a runaway retrieval loop trips the same governance guardrails as any other tool. See the [memory guide](ai-agent-memory.md) and the [long-term memory walkthrough](llm-long-term-memory.md) for how the pieces fit with persistence.

## Next steps

Skim the decision table above, then wire up memory, RAG, or both for your specific retrieval pattern — the two compose without stepping on each other. Start from the [Quick Start](../../getting-started/quickstart.md) to get an agent running, then follow the [RAG documentation](../../core/rag.md) to plug in your own loader and vector store, and the [memory guide](../../core/memory.md) to add auto-injected per-user context. `pip install promptise` and you have both in the same framework.
