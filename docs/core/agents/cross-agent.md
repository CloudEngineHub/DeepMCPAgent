# Cross-Agent Delegation

Enable agents to delegate tasks to peer agents using auto-generated tools like `ask_agent_researcher` and `broadcast_to_agents`.

## Quick Example

```python
import asyncio
from promptise import build_agent
from promptise.config import HTTPServerSpec
from promptise.cross_agent import CrossAgent

async def main():
    # Build a specialist peer agent
    researcher = await build_agent(
        servers={"search": HTTPServerSpec(url="http://localhost:8001/mcp")},
        model="openai:gpt-5-mini",
        instructions="You are a web research specialist.",
    )

    # Build the main agent with delegation to the peer
    agent = await build_agent(
        servers={"files": HTTPServerSpec(url="http://localhost:8002/mcp")},
        model="openai:gpt-5-mini",
        cross_agents={
            "researcher": CrossAgent(agent=researcher, description="Web research"),
        },
    )

    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": "Research the latest trends in AI safety"}]
    })
    print(result["messages"][-1].content)

    await agent.shutdown()
    await researcher.shutdown()

asyncio.run(main())
```

## Concepts

Cross-agent delegation lets a primary agent call peer agents as if they were regular tools. When you pass the `cross_agents` parameter to `build_agent()`, Promptise generates two kinds of tools:

1. **Per-peer ask tools** -- For each peer named `<name>`, a tool called `ask_agent_<name>` is created. The primary agent calls this tool to forward a message to that specific peer and receive its response.
2. **Broadcast tool** -- A single `broadcast_to_agents` tool that sends the same message to multiple peers in parallel and returns a mapping of peer name to response.

Peers are standard LangChain `Runnable` objects (typically `PromptiseAgent` instances returned by `build_agent()`). No new infrastructure is required -- delegation happens in-process via async calls.

```
Primary Agent
  |
  |-- ask_agent_researcher(message="...")  --> Researcher Agent --> response
  |-- ask_agent_analyst(message="...")     --> Analyst Agent   --> response
  |-- broadcast_to_agents(message="...")   --> [all peers]     --> {name: response}
```

## The `CrossAgent` Dataclass

`CrossAgent` is a frozen dataclass that wraps a peer agent with metadata for tool generation.

```python
from promptise.cross_agent import CrossAgent

peer = CrossAgent(
    agent=researcher_agent,                 # any LangChain Runnable
    description="Searches the web and summarizes findings",
)
```

| Field | Type | Default | Description |
|---|---|---|---|
| `agent` | `Runnable[Any, Any]` | **required** | The peer agent. Must accept `{"messages": [...]}` input and return a result with extractable text. |
| `description` | `str` | `""` | One-line description used in the auto-generated tool's docstring. Helps the primary agent decide when to delegate. |

!!! tip "Write good descriptions"
    The `description` field directly influences when the LLM chooses to delegate. Be specific: `"Accurate math calculations and equation solving"` is better than `"Math agent"`.

## Auto-Generated Tools

### `ask_agent_<name>`

One tool per peer. The primary agent uses this to delegate a specific task to a single peer.

**Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `message` | `str` | Yes | The message to forward to the peer agent (becomes a user message). |
| `context` | `str \| None` | No | Optional caller context (constraints, partial results, style guide). Injected as a system message before the user message. |
| `timeout_s` | `float \| None` | No | Optional timeout in seconds. If exceeded, returns `"Timed out waiting for peer agent reply."` instead of raising. |

**Example tool call (as seen by the LLM):**

```json
{
  "name": "ask_agent_researcher",
  "arguments": {
    "message": "Find the top 3 papers on transformer architectures from 2025",
    "context": "Focus on efficiency improvements, not architecture changes",
    "timeout_s": 30.0
  }
}
```

### `broadcast_to_agents`

A single tool that fans out a question to multiple peers concurrently. Each peer runs in parallel; timeouts and errors are captured per peer so one slow or failing peer does not block the others.

**Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `message` | `str` | Yes | The message sent to all selected peers. |
| `peers` | `list[str] \| None` | No | Subset of peer names to consult. If omitted, all registered peers are queried. |
| `timeout_s` | `float \| None` | No | Per-peer timeout in seconds. Peers that exceed the timeout return `"Timed out"`. |

**Return value:** A `dict[str, str]` mapping each peer name to its response text (or `"Timed out"` / `"Error: <message>"`).

## Detailed Walkthrough

### Two Peers Delegating to Each Other

This example builds two specialist agents and a coordinator that can delegate to both:

```python
import asyncio
from promptise import build_agent
from promptise.config import HTTPServerSpec
from promptise.cross_agent import CrossAgent

async def main():
    # --- Build specialist agents ---
    researcher = await build_agent(
        servers={"search": HTTPServerSpec(url="http://localhost:8001/mcp")},
        model="openai:gpt-5-mini",
        instructions="You are a web research specialist. Find accurate information.",
    )

    analyst = await build_agent(
        servers={"data": HTTPServerSpec(url="http://localhost:8002/mcp")},
        model="openai:gpt-5-mini",
        instructions="You are a data analyst. Analyze data and produce insights.",
    )

    # --- Build coordinator with delegation to both ---
    coordinator = await build_agent(
        servers={},  # no direct MCP tools needed
        model="openai:gpt-5-mini",
        instructions=(
            "You coordinate research tasks. Delegate research to the researcher "
            "and data analysis to the analyst. Synthesize their results."
        ),
        cross_agents={
            "researcher": CrossAgent(
                agent=researcher,
                description="Searches the web for information and summarizes findings",
            ),
            "analyst": CrossAgent(
                agent=analyst,
                description="Analyzes datasets and produces statistical insights",
            ),
        },
    )

    # The coordinator can now call:
    #   ask_agent_researcher(message=..., context=..., timeout_s=...)
    #   ask_agent_analyst(message=..., context=..., timeout_s=...)
    #   broadcast_to_agents(message=..., peers=[...], timeout_s=...)

    result = await coordinator.ainvoke({
        "messages": [{
            "role": "user",
            "content": "Research recent AI safety papers and analyze their citation trends",
        }]
    })
    print(result["messages"][-1].content)

    await coordinator.shutdown()
    await researcher.shutdown()
    await analyst.shutdown()

asyncio.run(main())
```

### Using Timeouts

Timeouts prevent a slow peer from blocking the primary agent indefinitely. When a timeout fires, the tool returns a string message instead of raising an exception.

```python
# Per-peer ask with timeout
# If the researcher takes longer than 15 seconds, the tool returns
# "Timed out waiting for peer agent reply."
result = await coordinator.ainvoke({
    "messages": [{
        "role": "user",
        "content": "Research quantum computing breakthroughs in the last month",
    }]
})

# Broadcast with per-peer timeout
# Each peer gets 10 seconds. Slow peers return "Timed out" in the result dict.
```

The timeout is enforced using `anyio.move_on_after`, which cancels the peer call cleanly without leaking resources.

### Using Context

The optional `context` parameter lets the caller inject constraints or partial results into the peer's conversation. It is inserted as a system message before the user message.

```python
# The LLM might produce a tool call like:
# ask_agent_analyst(
#     message="Analyze the correlation between paper length and citation count",
#     context="Use only papers from 2024-2025. The researcher already found 47 relevant papers."
# )
```

### Disabling the Broadcast Tool

If you only need per-peer ask tools without the broadcast capability, use `make_cross_agent_tools()` directly:

```python
from promptise.cross_agent import CrossAgent, make_cross_agent_tools

peers = {
    "researcher": CrossAgent(agent=researcher, description="Web research"),
}

tools = make_cross_agent_tools(peers, include_broadcast=False)
# Returns only [ask_agent_researcher], no broadcast_to_agents
```

You can also customize the tool name prefix:

```python
tools = make_cross_agent_tools(
    peers,
    tool_name_prefix="delegate_to_",  # produces "delegate_to_researcher"
)
```

## API Summary

| Symbol | Import | Description |
|---|---|---|
| `CrossAgent` | `from promptise.cross_agent import CrossAgent` | Frozen dataclass wrapping a peer agent with `agent` and `description` fields. |
| `make_cross_agent_tools()` | `from promptise.cross_agent import make_cross_agent_tools` | Creates LangChain tools from a `Mapping[str, CrossAgent]`. Parameters: `peers`, `tool_name_prefix` (default `"ask_agent_"`), `include_broadcast` (default `True`). |
| `cross_agents` param | `build_agent(..., cross_agents={...})` | Pass a dict of `name -> CrossAgent` to automatically attach delegation tools to the agent. |

!!! tip "Peer agents are just Runnables"
    Any LangChain `Runnable` works as a peer -- it does not have to be a `PromptiseAgent`. A custom chain, a `PromptGraphEngine`, or even a mock runnable for testing all work as long as they accept `{"messages": [...]}` input.

!!! tip "In-process only"
    Cross-agent delegation is in-process. For remote agent delegation across machines, connect agents to shared MCP servers so they can exchange data through tools.

!!! warning "Shutdown order"
    Shut down the coordinator first, then the peers. If a peer is shut down while the coordinator is still running, delegation calls to that peer will fail.

!!! warning "Circular delegation"
    At the code level, nothing prevents agent A from delegating to agent B and agent B delegating back to agent A, which could create infinite loops. Design your delegation graph as a DAG (directed acyclic graph) to avoid this. The `.superagent` file loader detects circular *file references* automatically, but runtime circular delegation must be avoided by design.

## What's Next?

- [Building Agents](building-agents.md) -- the `build_agent()` function reference.
- [SuperAgent Files](superagent-files.md) -- define cross-agent references declaratively in YAML.
- [Agent Runtime](../../runtime/index.md) -- long-running agents with triggers and lifecycle management.
