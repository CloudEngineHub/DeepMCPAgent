"""RAG pipeline with rag_to_tool — basic usage.

Demonstrates:
  - Subclassing DocumentLoader and Embedder for your provider
  - Building a RAGPipeline with RecursiveTextChunker + InMemoryVectorStore
  - Wrapping the pipeline as a LangChain tool via rag_to_tool()
  - Passing the tool to build_agent() via extra_tools

Requires:
    OPENAI_API_KEY environment variable set.

Run:
    .venv/bin/python examples/rag/basic_rag.py
"""

from __future__ import annotations

import asyncio

from openai import AsyncOpenAI

from promptise import (
    Document,
    DocumentLoader,
    Embedder,
    InMemoryVectorStore,
    RAGPipeline,
    RecursiveTextChunker,
    build_agent,
    rag_to_tool,
)

# ---------------------------------------------------------------------------
# 1. Implement a DocumentLoader — load your documents from anywhere
# ---------------------------------------------------------------------------


class StaticLoader(DocumentLoader):
    """Loads a few hardcoded documents for demonstration."""

    async def load(self) -> list[Document]:
        return [
            Document(
                id="faq-refunds",
                text=(
                    "Refund policy: Customers can request a full refund within "
                    "30 days of purchase. After 30 days, a 50% refund is offered "
                    "up to 90 days. No refunds after 90 days."
                ),
                metadata={"source": "FAQ", "category": "refunds"},
            ),
            Document(
                id="faq-shipping",
                text=(
                    "Shipping: Standard shipping takes 5-7 business days. Express "
                    "shipping takes 1-2 business days. Free shipping on orders "
                    "over $50."
                ),
                metadata={"source": "FAQ", "category": "shipping"},
            ),
            Document(
                id="faq-returns",
                text=(
                    "Returns: Items must be returned in original packaging. "
                    "Electronics have a 15-day return window. Clothing has a "
                    "45-day return window. Sale items are final sale."
                ),
                metadata={"source": "FAQ", "category": "returns"},
            ),
        ]


# ---------------------------------------------------------------------------
# 2. Implement an Embedder — wrap your embedding provider
# ---------------------------------------------------------------------------


class OpenAIEmbedder(Embedder):
    """Wraps OpenAI's text-embedding-3-small model."""

    def __init__(self) -> None:
        self._client = AsyncOpenAI()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.embeddings.create(
            model="text-embedding-3-small",
            input=texts,
        )
        return [d.embedding for d in resp.data]

    @property
    def dimension(self) -> int:
        return 1536


# ---------------------------------------------------------------------------
# 3. Wire everything together
# ---------------------------------------------------------------------------


async def main() -> None:
    # Build the pipeline
    pipeline = RAGPipeline(
        loader=StaticLoader(),
        chunker=RecursiveTextChunker(chunk_size=300, overlap=50),
        embedder=OpenAIEmbedder(),
        store=InMemoryVectorStore(),
    )

    # Index documents
    report = await pipeline.index()
    print(f"Indexed {report.documents_loaded} docs → {report.chunks_stored} chunks")

    # Test retrieval standalone
    results = await pipeline.retrieve("How long do I have to get a refund?", limit=2)
    for r in results:
        print(f"  [{r.score:.2f}] {r.text[:80]}...")

    # Wrap as a tool and give it to an agent
    faq_tool = rag_to_tool(
        pipeline,
        name="search_faq",
        description="Search the company FAQ for answers about refunds, shipping, and returns.",
    )

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        extra_tools=[faq_tool],
        instructions="You are a customer support agent. Use the search_faq tool to answer questions.",
    )

    result = await agent.ainvoke(
        {
            "messages": [{"role": "user", "content": "Can I return a laptop after 20 days?"}],
        }
    )
    print("\nAgent response:")
    print(result["messages"][-1].content)

    await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
