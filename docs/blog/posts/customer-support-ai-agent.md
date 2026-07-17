---
title: "How to Build a Customer Support AI Agent, Step by Step"
description: "Most tutorials stop at a single-turn RAG bot. This one ships the hard parts real support needs: conversation phases that change behavior (greet to classify…"
keywords: "customer support ai agent, build a support chatbot, ai customer service agent, support agent with knowledge base, escalation ai agent"
date: 2026-07-16
slug: customer-support-ai-agent
categories:
  - Use Cases
---

# How to Build a Customer Support AI Agent, Step by Step

A production customer support ai agent is not a single-turn question-and-answer bot with a vector store bolted on. Real support is a conversation that changes shape as it goes — you greet, you figure out what's actually wrong, you look up the order, and only then do you resolve or escalate. Most tutorials ship the easy 20% (retrieve an article, paste it into a prompt) and skip the parts that break in production: behavior that shifts across turns, a reply that gets validated before it sends, escalation rules, and history that survives a restart. By the end of this guide you'll have built all of those with Promptise Foundry, and you'll be able to run the whole thing with your own `OPENAI_API_KEY`.

## What a real support agent needs beyond RAG

Retrieval-augmented generation answers "what does the docs page say?" That's necessary but nowhere near sufficient. A customer service interaction has stages, and each stage wants a different behavior from the model:

- **Greet** — acknowledge the customer, ask for an order or account ID.
- **Classify** — figure out the issue type and urgency before committing to an answer.
- **Resolve** — search the knowledge base, look up real data, propose one concrete next step.
- **Escalate** — hand off to a human (or another agent) when the issue is out of scope.

If you cram all of that into one static system prompt, the model tries to do everything at once: it proposes refunds before it knows the account, or asks for an order ID it was already given. The fix is a system prompt that evolves as the conversation moves — plus durable history so the agent doesn't forget the order number the moment your process recycles. We'll build both.

## Give the agent a knowledge base with an MCP server

A support agent with knowledge base access needs real data — articles, customer records, order status — not hallucinated policy. In Promptise you expose that data as tools on an MCP server, and the agent discovers and calls them automatically. Here's a compact server; the [full customer support lab](../../guides/lab-customer-support.md) expands it with CRM lookups and ticket creation.

```python
# support_server.py
from promptise.mcp.server import MCPServer

server = MCPServer("support-tools")

ARTICLES = {
    "shipping": "Standard shipping: 5-7 business days. Express: 2-3 days. Tracking 24h after dispatch.",
    "returns": "30-day return policy. Item must be unused. Refund within 5 business days.",
}
ORDERS = {"ORD-5001": {"status": "shipped", "item": "Widget Pro", "tracking": "TRK-88421"}}

@server.tool()
async def search_articles(query: str) -> str:
    """Search the knowledge base for articles matching a topic."""
    hits = [f"[{k}] {v}" for k, v in ARTICLES.items() if k in query.lower()]
    return "\n".join(hits) or "No articles found."

@server.tool()
async def lookup_order(order_id: str) -> str:
    """Look up order status and tracking by order ID."""
    o = ORDERS.get(order_id.upper())
    return f"{o['item']}: {o['status']}, tracking {o['tracking']}" if o else "Order not found."

if __name__ == "__main__":
    server.run(transport="stdio")
```

Because tool schemas are generated from your type hints, the agent knows `lookup_order` takes an `order_id: str` and returns a string — no manual wiring. Point an agent at this server and it can search articles and check tracking on its own.

## Conversation phases that change behavior (greet → classify → resolve)

This is the piece most build-alongs miss. Promptise's `ConversationFlow` is a small state machine where the system prompt is reassembled every turn from whichever blocks are active in the current phase. You define phases as `@phase`-decorated handlers, and each handler activates or deactivates prompt blocks. Base blocks (identity, non-negotiable rules) stay on for the whole conversation.

```python
from promptise.prompts.flows import ConversationFlow, TurnContext, phase
from promptise.prompts.blocks import Identity, Rules, Section

class SupportFlow(ConversationFlow):
    base_blocks = [
        Identity("Customer support specialist", traits=["empathetic", "precise"]),
        Rules(["Never blame the customer", "Never promise a refund you can't confirm"]),
    ]

    @phase("greet", initial=True)
    async def greet(self, ctx: TurnContext) -> None:
        ctx.activate(Section("greet", "Welcome the customer and ask for their order or account ID."))

    @phase("classify")
    async def classify(self, ctx: TurnContext) -> None:
        ctx.deactivate("greet")
        ctx.activate(Section("classify", "Identify the issue type and urgency before answering."))

    @phase("resolve")
    async def resolve(self, ctx: TurnContext) -> None:
        ctx.deactivate("classify")
        ctx.activate(Section("resolve", "Search the knowledge base and propose one concrete next step."))

flow = SupportFlow()
prompt = await flow.start()                       # enters the greet phase
prompt = await flow.next_turn("My order is late")  # process the customer message
await flow.transition("classify")                  # move phases when you're ready
```

The `greet` prompt never mentions refunds; the `resolve` prompt does. That separation is what stops the model from short-circuiting to a solution before it has the facts. Transitions are yours to drive — from a classifier, a keyword rule, or the model's own signal — and the full block, hook, and slot API lives in the [ConversationFlow documentation](../../prompting/flows.md) linked from the prompting guides.

## Persist history that survives restarts

An LLM is stateless: without durable storage, every deploy or crashed pod erases in-flight conversations, and a naive in-memory dict will happily serve one customer's thread to another. Promptise handles this with a `ConversationStore` protocol and four backends — in-memory, SQLite, Postgres, and Redis — behind the same `chat()` method. `chat()` loads history for the session, appends the new message, invokes the model, and persists the transcript on every call. Passing a `caller` also enforces **session ownership**: the first turn assigns the session to that user, and a different user hitting the same `session_id` is denied.

Here's the whole thing wired together and runnable — it connects to `support_server.py` from earlier, persists to SQLite, and turns on guardrails:

```python
import asyncio
from promptise import build_agent, CallerContext
from promptise.config import StdioServerSpec
from promptise.conversations import SQLiteConversationStore


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"support": StdioServerSpec(command="python", args=["support_server.py"])},
        conversation_store=SQLiteConversationStore("support.db"),
        guardrails=True,   # 6 local detection heads: injection, PII, secrets, NER, content, custom
        instructions=(
            "You are a customer support agent. Greet, classify the issue, search the "
            "knowledge base, then propose one concrete next step. Always look up real "
            "data with your tools — never guess."
        ),
    )

    caller = CallerContext(user_id="C-1001", roles=["customer"])
    sid = "session-C-1001-001"

    # First turn owns the session and persists it.
    print(await agent.chat("Hi, my order ORD-5001 is late.", session_id=sid, caller=caller))

    # A later turn — even after a full restart — reloads history from support.db,
    # so the agent still knows the order number.
    print(await agent.chat("Can you check the tracking?", session_id=sid, caller=caller))

    await agent.shutdown()


asyncio.run(main())
```

Set `OPENAI_API_KEY`, run it once, then run it again: the second process still answers correctly because the transcript lives in `support.db`, not in memory. When you outgrow a single node, switching to `PostgresConversationStore` or `RedisConversationStore` is a one-line constructor change — the `chat()` calls and ownership checks are identical. The [conversation persistence deep dive](conversation-persistence.md) covers the backend trade-offs and the ownership model in full.

## Escalation ai agent rules: validate, then hand off

The last mile of any ai customer service agent is knowing when *not* to answer. Two mechanisms cover it:

- **Validate before sending.** Turning on `guardrails=True` (above) runs six local detection heads, redacting PII a model might echo back and blocking prompt-injection attempts hidden in a customer message. Nothing leaves without passing those checks.
- **Escalate out of scope.** For sensitive actions — issuing a refund, deleting an account — you gate the tool behind human approval rather than letting the model act unilaterally. And when an issue genuinely needs a specialist, hand it to another agent instead of forcing one model to know everything.

That hand-off is where a single support bot grows into a small team: a front-line agent that triages, a billing agent that owns refunds, a technical agent that reads logs. Promptise's [multi-agent coordination guide](../../guides/multi-agent-teams.md) shows the `ask_peer()` and shared-server patterns for exactly this, and the companion post [How to Build Multi-Agent Systems in Python: 2026 Guide](multi-agent-systems-python.md) walks the topology end to end. For the broader menu of what you can assemble from these pieces — from a 30-minute build to a full platform — skim the [showcase](../../resources/showcase.md).

## When a plain RAG bot is the better fit

Be honest about scope. If your "support" surface is a single FAQ page and every question is one-shot — "what are your hours?" — a phase state machine and a persistence layer are overhead you don't need. A stateless retrieval prompt over your docs will ship faster and cost less to run. Reach for the full flow only when conversations are genuinely multi-turn, when you need history to survive restarts, or when you have to prove *who* accessed *which* thread. The same goes for escalation: if you have no humans in the loop and no irreversible actions, skip the approval gate. Add each layer when a real requirement demands it, not because a tutorial listed it.

## Frequently asked questions

### How is this different from a RAG chatbot?

A RAG chatbot retrieves a document and answers one question in isolation. A customer support ai agent maintains a multi-turn conversation whose behavior changes by phase (greet, classify, resolve), looks up live data through tools, validates its reply before sending, and persists history so it survives restarts. RAG is one capability inside the larger agent, not the whole thing.

### How do I stop one customer from reading another's chat history?

Pass a `caller` (a `CallerContext` with a `user_id`) into `chat()`. The first turn assigns session ownership; a different user hitting the same `session_id` is denied. Ownership is enforced across the session methods too, and listing sessions filters by user at the database level — so you get isolation without writing that code yourself.

### Which conversation store should I use in production?

Use `SQLiteConversationStore` for local dev and single-node apps, `PostgresConversationStore` for multi-node production, and `RedisConversationStore` for high-throughput ephemeral sessions. They share one protocol, so moving between them is a constructor swap with no other code changes.

## Next steps

Clone the customer support lab and run it with your own `OPENAI_API_KEY` in under 15 minutes — the [full lab guide](../../guides/lab-customer-support.md) has every step, including CRM lookups and ticket escalation. New to the framework? Start with the [Quick Start](../../getting-started/quickstart.md) to stand up an agent in a few lines, then layer in phases and persistence from here.
