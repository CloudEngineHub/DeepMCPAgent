## What are "Cross Agents"?

**Cross agents** are other agent graphs (LangGraph ReAct or DeepAgents loops) that you expose to a calling agent as tools. Each peer is surfaced as:

- `ask_agent_<name>` — forward a single question to a specific peer and return its final answer.
- `broadcast_to_agents` — ask multiple peers the same question in parallel and return a mapping of answers.

This makes multi-agent patterns feel like calling any other MCP tool: the caller reasons about _when_ to delegate; the peer focuses on _how_ to solve its slice.

---

## How it works (high level)

1. You build (or already have) one or more agent runnables: `Runnable[{messages}] -> Result`.
2. Wrap those peers with `CrossAgent(agent=..., description=...)`.
3. Pass `cross_agents={...}` into `build_deep_agent(...)` for your main agent.
4. The builder automatically attaches:

   - one `ask_agent_<peer>` tool per peer
   - an optional `broadcast_to_agents` tool

5. During planning, the main agent can call these tools like any other MCP tool.

Internally, the tools:

- Package your prompt as `{ "messages": [...] }` and call `peer.ainvoke(...)`.
- Extract a “best final text” from the peer’s result (compatible with LangGraph/DeepAgents common shapes).
- Return that text (or a dict of texts for broadcast) to the caller.

---

## Installation & prerequisites

You already have DeepMCPAgent. Ensure your env can run your chosen chat model(s) via LangChain’s `init_chat_model` strings (e.g., `openai:gpt-4.1`, `anthropic:messages-2025-xx`), and that any MCP servers you want are reachable.

> Works with both **DeepAgents** (if installed) and the **LangGraph prebuilt ReAct agent** fallback.

---

## Quick start

### 1) Build a specialist peer agent

```python
from deepmcpagent.agent import build_deep_agent
from deepmcpagent.config import HTTPServerSpec

research_graph, _ = await build_deep_agent(
    servers={"web": HTTPServerSpec(url="http://127.0.0.1:8000/mcp")},
    model="openai:gpt-4o-mini",
)
```

### 2) Build your main agent and attach the peer

```python
from deepmcpagent.agent import build_deep_agent
from deepmcpagent.cross_agent import CrossAgent

main_graph, _ = await build_deep_agent(
    servers={"files": HTTPServerSpec(url="http://127.0.0.1:9000/mcp")},
    model="openai:gpt-4.1",
    cross_agents={
        "researcher": CrossAgent(
            agent=research_graph,
            description="Focused web researcher that gathers and summarizes sources."
        )
    },
)
```

### 3) Use it in a chat loop

```python
result = await main_graph.ainvoke({
    "messages": [{"role": "user", "content": "Draft a brief on Topic X"}]
})
# During planning the main agent may call:
#   - ask_agent_researcher(message=..., context?=..., timeout_s?=...)
#   - broadcast_to_agents(message=..., peers?=[...], timeout_s?=...)
```

---

## Tool API

### `ask_agent_<name>`

**Purpose:** Ask a specific peer a question and get the peer’s final answer.

**Input schema**

```json
{
  "message": "string (required)",
  "context": "string (optional)",
  "timeout_s": "number (optional, seconds)"
}
```

- `message`: The user-level query to forward.
- `context`: Extra caller context (constraints, partial results, style guide). Sent first as a `system` message to bias many executors helpfully.
- `timeout_s`: Per-call timeout; returns a “Timed out” message if exceeded.

**Return:** `string` — best-effort final text from the peer.

---

### `broadcast_to_agents`

**Purpose:** Ask multiple peers the same question in parallel.

**Input schema**

```json
{
  "message": "string (required)",
  "peers": ["string", "... (optional)"],
  "timeout_s": "number (optional, seconds)"
}
```

- `peers`: Subset of peer names; omit to use all.
- `timeout_s`: Per-peer timeout.

**Return:** `object` mapping peer name → final text answer, e.g.

```json
{
  "researcher": "Summary ...",
  "editor": "Refined draft ...",
  "critic": "Risks list ..."
}
```

---

## Examples & patterns

### Specialist delegation

- **Researcher → Writer → Editor**: The main agent requests sources via `ask_agent_researcher`, drafts with local tools, then sends the draft to an `editor` peer for tone/clarity.

### Ensemble consensus

- Broadcast to `{ "math", "python", "reasoner" }`, then summarize or vote on the best answer.

### Safety gatekeeping

- Route candidate content to a `safety` peer for policy checks before finalizing.

### RAG aggregator

- A peer dedicated to retrieval; main agent calls it when tool descriptions mention “search”, “vector”, or “db” tasks.

---

## Tracing & debugging

Enable tool tracing to see cross-agent calls and outputs:

```bash
deepmcpagent run \
  --model-id openai:gpt-4.1 \
  --http "name=files url=http://127.0.0.1:9000/mcp" \
  --trace
```

Programmatically, pass `trace_tools=True` into `build_deep_agent(...)` to get console prints like:

```
→ Invoking tool: ask_agent_researcher with {'message': '...'}
✔ Tool result from ask_agent_researcher: <peer final text>
```

---

## Error handling & timeouts

- Transport/peer errors surface as `MCPClientError` or `ValueError` (e.g., unknown peer in broadcast).
- Use `timeout_s` to keep the caller responsive.
- The broadcast tool returns `"Timed out"` for slow peers without failing the whole call.

---

## Design notes

- **Zero new infra:** Peers are plain in-process runnables; no extra MCP servers needed to talk agent-to-agent.
- **LLM-native delegation:** Uses standard tool calls, so planning remains transparent and controllable.
- **Composable & optional:** `cross_agents` is a single optional arg; if omitted, nothing changes.
- **Parallel fan-out:** Broadcast leverages `anyio.gather` for concurrent peer calls.

---

## Compatibility

- **DeepAgents available:** Uses the DeepAgents loop under the hood when present.
- **Otherwise:** Falls back to LangGraph’s `create_react_agent`. Prompt injection via `system_prompt` / `state_modifier` is handled across versions.

---

## Security & privacy boundaries

- Only the `message` (and optional `context`) are sent to peers.
- Avoid passing secrets in `context`. Prefer secret storage and tool-level auth for sensitive operations.

---

## Performance tips

- Keep peers focused and lightweight; the caller can decide _when_ to delegate.
- Use `timeout_s` for high-latency peers or external retrieval.
- Consider smaller/cheaper models for “filter” or “triage” peers; reserve larger models for synthesis.

---

## Testing

### Unit test a single ask

```python
import pytest
from langchain_core.runnables import RunnableLambda
from deepmcpagent.cross_agent import CrossAgent, make_cross_agent_tools

async def fake_peer(inputs):
    return {"messages": [{"role": "assistant", "content": "ok"}]}

def test_ask_agent_tool():
    peer = CrossAgent(agent=RunnableLambda(fake_peer))
    tools = make_cross_agent_tools({"peer": peer})
    ask = next(t for t in tools if t.name == "ask_agent_peer")
    out = pytest.run(asyncio.run(ask._arun(message="ping")))  # or use anyio
    assert out == "ok"
```

### Integration test with your builder

- Build two agents with trivial models or stubs.
- Attach via `cross_agents`.
- Invoke a prompt that forces a tool call (e.g., “Ask the researcher for sources and summarize”).

---

## Migration from single-agent setups

You don’t have to change your MCP servers or model configs. Introduce peers gradually by:

1. Building a small specialist peer (`research`, `editor`, `critic`).
2. Attaching it via `cross_agents`.
3. Nudging your system prompt to allow delegation: _“If a peer tool is available and more capable, delegate.”_

---

## Reference (Public API)

### `CrossAgent`

```python
CrossAgent(
  agent: Runnable[Any, Any],
  description: str = ""
)
```

### `build_deep_agent(..., cross_agents=...)`

```python
main_graph, loader = await build_deep_agent(
  servers=...,                # Mapping[str, ServerSpec]
  model="openai:gpt-4.1",     # or BaseChatModel or Runnable
  instructions=None,          # optional
  trace_tools=True,           # optional
  cross_agents={
    "researcher": CrossAgent(agent=peer_graph, description="..."),
    # more peers...
  }
)
```

**Auto-added tools**

- `ask_agent_<name>(message: str, context?: str, timeout_s?: float) -> str`
- `broadcast_to_agents(message: str, peers?: list[str], timeout_s?: float) -> dict[str, str]`

---

## Roadmap

- Remote peers (HTTP/SSE) with the same API.
- Streaming replies & “live debate” orchestration.
- Capability tagging & auto-routing (“use the best peer for X”).
- Observability hooks (spans/metrics per peer call).

---

_That’s it—plug in a peer, flip on tracing, and you’ve got cooperative agents without extra plumbing._
