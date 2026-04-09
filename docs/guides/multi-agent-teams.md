# Multi-Agent Coordination

Build systems where multiple agents collaborate — sharing tools, delegating tasks, communicating through events, and coordinating through shared state.

## Architecture

Promptise provides four coordination primitives. Combine them to build any multi-agent topology.

```
┌─────────────────────────────────────────────────────────────┐
│                      AgentRuntime                           │
│                                                             │
│  ┌─────────┐  ask_peer()  ┌─────────┐  EventBus  ┌──────┐ │
│  │ Agent A  │────────────→│ Agent B  │←──────────→│Agent C│ │
│  │ research │             │ analysis │            │ write │ │
│  └────┬─────┘             └────┬─────┘            └───┬──┘ │
│       │                        │                      │     │
│       └────────────┬───────────┴──────────────────────┘     │
│                    │                                        │
│           ┌────────▼────────┐                               │
│           │  Shared MCP     │  ← Auth, rate limits,         │
│           │  Server         │     audit logging              │
│           └─────────────────┘                               │
└─────────────────────────────────────────────────────────────┘
```

## 1. Shared MCP Servers

Multiple agents connect to the same MCP server. The server enforces access policies per agent.

```python
from promptise.mcp.server import MCPServer, JWTAuth, AuthMiddleware
from promptise.mcp.server.guards import HasRole

# Build a shared tool server
server = MCPServer("team-tools")
auth = JWTAuth(secret="shared-secret")
server.middleware = [AuthMiddleware(auth)]

@server.tool(guards=[HasRole("researcher")])
async def web_search(query: str, ctx=None) -> str:
    """Search the web. Only agents with 'researcher' role can call this."""
    return await search_engine.query(query)

@server.tool(guards=[HasRole("writer")])
async def publish(title: str, content: str, ctx=None) -> str:
    """Publish content. Only agents with 'writer' role can call this."""
    return await cms.publish(title, content)
```

```python
from promptise import build_agent, HttpServerSpec

# Each agent connects to the same server with different roles
researcher = await build_agent(
    model="openai:gpt-5-mini",
    servers={"tools": HttpServerSpec(
        url="http://localhost:8080",
        bearer_token=jwt_for_role("researcher"),
    )},
)

writer = await build_agent(
    model="openai:gpt-5-mini",
    servers={"tools": HttpServerSpec(
        url="http://localhost:8080",
        bearer_token=jwt_for_role("writer"),
    )},
)
```

## 2. Cross-Agent Delegation

One agent delegates a subtask to another and awaits the result. Uses HTTP + JWT authentication.

```python
from promptise import build_agent

analyst = await build_agent(
    model="openai:gpt-5-mini",
    servers=my_servers,
    cross_agents={
        "researcher": "http://localhost:9001",
        "fact_checker": "http://localhost:9002",
    },
)

# In conversation, the analyst can delegate:
# "Ask the researcher to find recent papers on quantum computing"
# → analyst calls ask_peer("researcher", "Find recent papers on...")
# → researcher processes the request and returns findings
# → analyst continues with the results
```

**Programmatic delegation:**

```python
# ask_peer — single agent, await response
answer = await analyst.ask_peer(
    "researcher",
    "Find the top 3 papers on transformer architectures from 2025",
)

# broadcast — multiple agents, await all responses (with timeout)
answers = await analyst.broadcast(
    ["researcher", "fact_checker"],
    "Verify this claim: GPT-5 has 1 trillion parameters",
    timeout=30.0,  # Seconds — graceful degradation if a peer is slow
)
# answers = {"researcher": "...", "fact_checker": "..."}
```

## 3. EventBus Messaging

Decoupled pub/sub communication. Agents publish events; others subscribe by topic. No direct coupling.

```python
from promptise.runtime import AgentRuntime, AgentProcess
from promptise.runtime.triggers import EventTrigger, MessageTrigger

runtime = AgentRuntime()

# Research agent publishes findings
research_process = AgentProcess(
    name="researcher",
    agent_config={
        "model": "openai:gpt-5-mini",
        "servers": research_servers,
        "instructions": "Research topics and publish findings as events.",
    },
)

# Analysis agent subscribes to research findings
analysis_process = AgentProcess(
    name="analyst",
    agent_config={
        "model": "openai:gpt-5-mini",
        "servers": analysis_servers,
        "instructions": "Analyze research findings and produce reports.",
    },
    triggers=[
        # Fires when researcher publishes a "research.complete" event
        EventTrigger(event_type="research.complete"),
    ],
)

# Writer agent subscribes to analysis results via topic
writer_process = AgentProcess(
    name="writer",
    agent_config={
        "model": "openai:gpt-5-mini",
        "servers": writing_servers,
        "instructions": "Write reports from analysis results.",
    },
    triggers=[
        # Fires on any message to the "reports" topic
        MessageTrigger(topic="reports.*"),
    ],
)

runtime.add_process(research_process)
runtime.add_process(analysis_process)
runtime.add_process(writer_process)
await runtime.start_all()

# Kick off the pipeline
await runtime.event_bus.emit("research.start", {
    "topic": "AI safety trends 2025",
})
```

## 4. Shared Context & State

Agents in the same runtime can share state through `AgentContext` with write permissions.

```python
from promptise.runtime.context import AgentContext

# Create shared context with controlled write access
shared_ctx = AgentContext(
    initial_state={"project": "quarterly-report", "status": "in_progress"},
    write_permissions={
        "researcher": ["findings", "sources"],
        "analyst": ["analysis", "recommendations"],
        "writer": ["draft", "status"],
    },
)

# Researcher writes findings (allowed)
shared_ctx.set("findings", research_data, writer="researcher")

# Analyst reads findings, writes analysis (allowed)
findings = shared_ctx.get("findings")
shared_ctx.set("analysis", analysis_data, writer="analyst")

# Writer reads everything, writes draft (allowed)
shared_ctx.set("draft", final_draft, writer="writer")
shared_ctx.set("status", "complete", writer="writer")

# Researcher tries to write draft (denied — not in write_permissions)
# shared_ctx.set("draft", "...", writer="researcher")  # Raises PermissionError
```

## 5. Complete Example: Research Pipeline

Three agents collaborate on a research task: researcher gathers data, analyst evaluates quality, writer produces the report.

```python
import asyncio
from promptise import build_agent, StdioServerSpec
from promptise.runtime import AgentRuntime, AgentProcess
from promptise.runtime.triggers import EventTrigger
from promptise.runtime.journal import FileJournal

async def main():
    runtime = AgentRuntime()

    # Shared MCP server for all agents
    shared_servers = {
        "tools": StdioServerSpec(command="python", args=["-m", "team_tools"]),
    }

    # 1. Researcher — triggered manually or on schedule
    runtime.add_process(AgentProcess(
        name="researcher",
        agent_config={
            "model": "openai:gpt-5-mini",
            "servers": shared_servers,
            "instructions": (
                "Research the given topic thoroughly. Use web_search and "
                "document_search tools. When done, summarize your findings."
            ),
        },
        journal=FileJournal("./journals/researcher"),
    ))

    # 2. Analyst — triggered when researcher publishes findings
    runtime.add_process(AgentProcess(
        name="analyst",
        agent_config={
            "model": "openai:gpt-5-mini",
            "servers": shared_servers,
            "instructions": (
                "Evaluate the research findings for accuracy and completeness. "
                "Rate quality 1-5. If below 3, request more research."
            ),
        },
        triggers=[EventTrigger(event_type="research.complete")],
        journal=FileJournal("./journals/analyst"),
    ))

    # 3. Writer — triggered when analysis passes quality gate
    runtime.add_process(AgentProcess(
        name="writer",
        agent_config={
            "model": "openai:gpt-5-mini",
            "servers": shared_servers,
            "instructions": (
                "Write a clear, well-structured report from the research "
                "findings and analysis. Cite sources."
            ),
        },
        triggers=[EventTrigger(event_type="analysis.approved")],
        journal=FileJournal("./journals/writer"),
    ))

    await runtime.start_all()

    # Kick off the pipeline
    await runtime.inject_event("researcher", {
        "type": "research.start",
        "topic": "Impact of AI on software development productivity",
    })

    # Wait for completion (in production, use webhooks or polling)
    await asyncio.sleep(120)
    await runtime.stop_all()

asyncio.run(main())
```

## Patterns

### Fan-out / Fan-in

Multiple agents work on subtasks in parallel, results are merged.

```python
# Coordinator broadcasts subtasks
answers = await coordinator.broadcast(
    ["legal_reviewer", "technical_reviewer", "business_reviewer"],
    f"Review this proposal: {proposal}",
    timeout=60.0,
)
# Merge results from all reviewers
combined = "\n\n".join(f"**{name}**: {answer}" for name, answer in answers.items())
```

### Supervisor Pattern

One agent oversees others, delegating and checking quality.

```python
supervisor = await build_agent(
    model="openai:gpt-5-mini",
    servers=my_servers,
    cross_agents={
        "researcher": "http://localhost:9001",
        "writer": "http://localhost:9002",
        "editor": "http://localhost:9003",
    },
    instructions=(
        "You are a project supervisor. Delegate research to the researcher, "
        "writing to the writer, and editing to the editor. Check quality "
        "at each stage. If quality is below standard, send back for revision."
    ),
)
```

### Pipeline with Quality Gates

Sequential processing with validation between stages.

```python
from promptise.runtime.triggers import EventTrigger

# Each agent listens for the previous stage's completion event
stages = [
    ("data_collector", "collect.complete"),
    ("data_cleaner", "clean.complete"),
    ("analyzer", "analyze.complete"),
    ("reporter", "report.complete"),
]

for name, trigger_event in stages:
    runtime.add_process(AgentProcess(
        name=name,
        agent_config=configs[name],
        triggers=[EventTrigger(event_type=trigger_event)] if trigger_event != "collect.complete" else [],
    ))
```

## Error Handling

Multi-agent systems need graceful degradation:

```python
# Cross-agent delegation with timeout and fallback
try:
    answer = await agent.ask_peer("specialist", question, timeout=30.0)
except TimeoutError:
    # Peer didn't respond — handle locally
    answer = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})

# Broadcast with partial failure tolerance
answers = await agent.broadcast(
    ["agent_a", "agent_b", "agent_c"],
    question,
    timeout=30.0,
)
# answers may have fewer keys than requested if some agents failed
successful = {k: v for k, v in answers.items() if v is not None}
```

## When to Use What

| Pattern | Use When |
|---------|----------|
| **Shared MCP Server** | Agents need the same tools with different permissions |
| **ask_peer()** | One agent needs a specific answer from another |
| **broadcast()** | Multiple agents should process the same input in parallel |
| **EventBus** | Loose coupling — agents react to events without knowing who produces them |
| **Shared Context** | Agents need to read/write a common state object |
| **External Orchestration** | Complex DAG workflows, human-in-the-loop approval chains, or task decomposition beyond what EventBus provides |
