---
title: "Your Vector Store Is Local — Are Your Embeddings?"
description: "Teams stand up Chroma or FAISS locally and assume their retrieval is air-gapped — but if the embedding step calls a hosted embeddings API, every chunk of…"
keywords: "offline embeddings for AI agents, local embedding model RAG, air-gapped vector search, sentence-transformers offline agent, local embeddings without cloud API"
date: 2026-07-16
slug: offline-embeddings-for-ai-agents
categories:
  - Air-Gapped & Sovereign
---

# Your Vector Store Is Local — Are Your Embeddings?

Running **offline embeddings for AI agents** is the difference between retrieval that is genuinely air-gapped and retrieval that quietly ships your entire private corpus to a third party twice. Most teams get the vector store right — they stand up Chroma, FAISS, Qdrant, or pgvector inside their own network, confirm the database never phones home, and check the box marked "on-prem." Then they wire an embeddings step in front of it that calls a hosted API, and every chunk of that private corpus egresses the moment it's indexed and again on every query. The vector *math* is local. The text that produces the vectors is not. This post walks through that embedding-egress trap and shows how Promptise Foundry pins **both** memory retrieval and semantic tool selection to a single local embedding model, so nothing leaves the host.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## The embedding-egress trap

A vector store doesn't store text — it stores vectors. To put a document *into* the store, something has to turn "Q3 pipeline had a 5% error rate at 07:30" into a list of floats. To *query* the store, the same something has to embed the user's question so the database can compare it against what's indexed. That "something" is the embedding model, and where it runs decides whether your data leaves the building.

Here's the trap in one sentence: **your vector store being local tells you nothing about where the embedding step runs.** If your embedder is a hosted API — the default in an enormous number of tutorials and starter templates — then:

- **At index time**, every chunk of every document you ingest is POSTed to a cloud endpoint. Your entire corpus, in plaintext, leaves the host once.
- **At query time**, every user question — often containing the sensitive part, the account number, the patient detail, the deal name — is POSTed again, on every single retrieval.

For a regulated, sovereign, or genuinely air-gapped deployment, that is the whole ballgame. It does not matter that Chroma is on local disk if the text that produced the vectors already round-tripped through someone else's data centre. The fix is not a bigger firewall; it's an embedding model that runs *in-process*, on your hardware, with no network dependency. That's the discipline this whole "Air-Gapped & Sovereign" series is built on — see [Air-Gapped AI Agent Framework: The On-Prem Guide](air-gapped-agent-framework.md) for the full deployment picture and [Why AI Agent Frameworks Fail in Air-Gapped Networks](air-gapped-ai-agent.md) for the failure modes that catch teams by surprise.

## One local model, two surfaces

An agent that retrieves has *two* places an embedder can leak from, and teams almost always audit only the first.

1. **Memory retrieval.** Before each turn, the agent searches its long-term memory for relevant context and injects the hits. That search embeds the query.
2. **Semantic tool selection.** With a large MCP tool set, sending all 50 tool schemas on every call is wasteful, so the agent embeds the query and the tool descriptions and keeps only the most relevant tools. That selection embeds the query *again*.

Two surfaces, two embedding steps, two chances to egress. Promptise closes both with the same local model by default. `ChromaProvider` embeds with `all-MiniLM-L6-v2` running locally via Sentence Transformers — no API key, no embeddings endpoint — as documented in the [Memory guide](../../core/memory.md). Semantic tool optimization defaults to the *same* `all-MiniLM-L6-v2`, running locally through `sentence-transformers`, as documented in [Tool Optimization](../../core/tool-optimization.md). One model, both surfaces, offline out of the box.

Start with the proof that costs nothing to run. The block below is runnable end-to-end with `pip install "promptise[all]"` and **no API key at all** — because the embedding step never touches the network:

```python
import asyncio

from promptise.memory import ChromaProvider


async def main() -> None:
    # all-MiniLM-L6-v2 runs locally via Sentence Transformers.
    # No embeddings API, no key — index and query are both offline.
    memory = ChromaProvider(
        collection_name="agent_memory",
        persist_directory=".promptise/chroma",
    )

    # Index time: this text is embedded on-host and never egresses.
    await memory.add(
        "Q3 pipeline had a 5% error rate at 07:30",
        metadata={"source": "health-check", "severity": "warning"},
    )
    await memory.add("The customer's preferred deployment target is on-prem GKE")

    # Query time: the question is embedded on-host too.
    results = await memory.search("deployment issues", limit=3)
    for r in results:
        print(f"[{r.score:.2f}] {r.content}")

    await memory.close()


asyncio.run(main())
```

Note that the query `"deployment issues"` matches the pipeline-error and deployment-target notes even though neither contains those exact words — that's real semantic similarity, computed entirely on your machine. Now wire the same local model into a full agent so *both* surfaces stay offline. The embedding stays local; only the LLM call itself needs a network (swap in `ollama:...` to close even that):

```python
import asyncio

from promptise import build_agent, ToolOptimizationConfig, OptimizationLevel
from promptise.config import HTTPServerSpec
from promptise.memory import ChromaProvider


async def main() -> None:
    memory = ChromaProvider(
        collection_name="agent_memory",
        persist_directory=".promptise/chroma",
    )  # surface 1: memory retrieval, local embeddings

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
        memory=memory,
        optimize_tools=ToolOptimizationConfig(
            level=OptimizationLevel.SEMANTIC,  # surface 2: tool selection,
        ),                                     # same local all-MiniLM-L6-v2
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Summarize today's pipeline health."}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

You didn't configure two embedders. You didn't reach for a hosted embeddings API on either surface. The framework's default *is* the local model, on both.

## Pin it to a local path for a true air-gap

The default model auto-downloads from HuggingFace Hub once and then runs offline forever. For a machine that has *never* had outbound access — the real air-gap case — you don't want that first download to happen on the target host at all. Download the model once on a connected machine, copy the directory over, and point both surfaces at the local path. No component ever reaches for the network.

```bash
# On a machine with internet, once — produces a self-contained model directory:
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2').save('/models/all-MiniLM-L6-v2')"
# Then copy /models/all-MiniLM-L6-v2 to the air-gapped host.
```

```python
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from promptise import build_agent, ToolOptimizationConfig, OptimizationLevel
from promptise.config import HTTPServerSpec
from promptise.memory import ChromaProvider

# One path. Both surfaces. Zero egress.
LOCAL_MODEL = "/models/all-MiniLM-L6-v2"

memory = ChromaProvider(
    persist_directory=".promptise/chroma",
    embedding_function=SentenceTransformerEmbeddingFunction(model_name=LOCAL_MODEL),
)

agent = await build_agent(
    model="ollama:llama3",  # keep the LLM local too, for a fully offline agent
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    memory=memory,
    optimize_tools=ToolOptimizationConfig(
        level=OptimizationLevel.SEMANTIC,
        embedding_model=LOCAL_MODEL,  # tool selection reads the same directory
    ),
)
```

`ChromaProvider.embedding_function` accepts any Chroma embedding function — here a Sentence Transformers one pointed at your directory — and `ToolOptimizationConfig.embedding_model` accepts a model name *or a local directory path*. Set them to the same `LOCAL_MODEL` and you've pinned the entire retrieval stack to one on-disk model. This is exactly the CTA of this article: point `ChromaProvider` and semantic tool optimization at a local embedding-model path and run retrieval with zero egress.

## What other frameworks do today

Being precise here matters, because offline embeddings are **not** something Promptise invented and no honest comparison should imply otherwise.

- **LangChain / LangGraph** absolutely support local embeddings: `HuggingFaceEmbeddings` (and `SentenceTransformerEmbeddings`) wrap Sentence Transformers and run on-host with no API call. You can build a fully local RAG stack with them today. The two real deltas are (1) the *default* many teams reach for — and that most quickstarts show — is `OpenAIEmbeddings`, a hosted endpoint; and (2) memory retrieval and tool selection are separate components you assemble and configure independently. Pointing *both* at the same local model is a wiring exercise you own, not a default the framework guarantees.
- **LlamaIndex** likewise ships `HuggingFaceEmbedding` for local models, so an offline configuration is fully achievable. But `Settings.embed_model` has historically defaulted to a hosted OpenAI embedding, so the out-of-the-box path egresses unless you override it, and its tool-retriever index (`ObjectIndex`) is configured separately from its memory/index embedder — again, one shared local model across both surfaces is something you set up, not something you get by default.

So the honest framing is not "competitors can't do offline embeddings" — they can, and you should believe them when they say so. The delta is that in those frameworks, running local across *both* memory and tool selection is an opt-in you assemble from parts, with a hosted API sitting on the default path waiting to leak. Promptise makes local-by-default *structural*: `ChromaProvider` and `SEMANTIC` tool optimization both resolve to the same local `all-MiniLM-L6-v2` with no configuration, and both accept a single local-path override for the air-gap case. The capability is table stakes; the *default* and the *single-model integration across both surfaces* are the edge.

## The same discipline for your document corpus

Memory is conversation-derived context. When you need to retrieve from a real document corpus — a wiki, ticket history, a policy library — the same egress question applies to every chunk you index. Promptise's [RAG pipeline](../../core/rag.md) keeps the embedder a first-class, swappable component precisely so you can hold this line. The `Embedder` base class has exactly one required method, `embed(texts) -> list[list[float]]`, so wrapping a local Sentence Transformers model is a few lines, and it slots into `RAGPipeline` alongside the in-memory `InMemoryVectorStore` or your own local vector DB:

```python
from sentence_transformers import SentenceTransformer

from promptise.rag import Embedder


class LocalEmbedder(Embedder):
    def __init__(self, path: str = "/models/all-MiniLM-L6-v2") -> None:
        self._model = SentenceTransformer(path)  # loads from disk, no network

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, convert_to_numpy=True).tolist()

    @property
    def dimension(self) -> int:
        return self._model.get_sentence_embedding_dimension()
```

Drop that into `RAGPipeline(embedder=LocalEmbedder(), store=InMemoryVectorStore(), ...)` and your document ingestion is as offline as your memory and tool selection. One local model, one discipline, applied uniformly across every surface where text becomes a vector.

## Frequently asked questions

### If my vector store is on-prem, isn't my retrieval already air-gapped?

No — and this is the exact trap. A local vector store holds vectors, not the text that produced them. Something still has to embed both your documents (at index time) and every query (at query time). If that embedder is a hosted API, your corpus and your questions egress regardless of where the database lives. Retrieval is only air-gapped when the embedding step also runs on-host, which is what `ChromaProvider`'s local `all-MiniLM-L6-v2` and Promptise's local semantic tool selection give you by default.

### Does using local embeddings require an internet connection the first time?

The default `all-MiniLM-L6-v2` downloads from HuggingFace Hub once, caches locally, and runs offline after that. For a host that must *never* touch the network, skip the first download entirely: save the model on a connected machine (`SentenceTransformer(...).save("/models/all-MiniLM-L6-v2")`), copy the directory over, and point both `ChromaProvider(embedding_function=...)` and `ToolOptimizationConfig(embedding_model="/models/all-MiniLM-L6-v2")` at that path. From then on there is no network call at index or query time.

### Do I have to configure two different embedders for memory and tool selection?

No, and that's the point. Both surfaces default to the same local `all-MiniLM-L6-v2`, so the common case needs zero embedding configuration. When you want to pin to a local directory for an air-gap, you set the same path in two places — `ChromaProvider.embedding_function` and `ToolOptimizationConfig.embedding_model` — and the entire retrieval stack reads one model off disk.

### Is a small local model good enough for real retrieval?

`all-MiniLM-L6-v2` is 384-dimensional, fast, and a strong general-purpose default for semantic search and tool selection. If your domain needs a stronger or specialised model, both surfaces accept any Sentence Transformers-compatible model or local path — for example `BAAI/bge-small-en-v1.5` — so you can upgrade the embedder without changing anything else in your agent wiring.

### Can I keep the whole agent offline, including the LLM?

Yes. Local embeddings close the retrieval surfaces; to close the generation surface too, point `build_agent(model=...)` at a local runtime such as `ollama:llama3`. With a local model, local embeddings on both retrieval surfaces, and a local vector store, no part of the agent's core loop reaches the network.

## Next steps

Point `ChromaProvider` and semantic tool optimization at a local embedding-model path and run retrieval with zero egress. Start with the [Memory guide](../../core/memory.md) to see how `ChromaProvider` auto-injects local-embedded context, read [Tool Optimization](../../core/tool-optimization.md) for the semantic tool-selection knobs and the local-path override, and use the [RAG pipeline](../../core/rag.md) to extend the same offline discipline to your document corpus. For the surrounding deployment story, the [Air-Gapped AI Agent Framework guide](air-gapped-agent-framework.md) covers running the full stack on-prem, and [Why AI Agent Frameworks Fail in Air-Gapped Networks](air-gapped-ai-agent.md) explains the leaks — the embedding-egress trap chief among them — that quietly break "air-gapped" deployments.
