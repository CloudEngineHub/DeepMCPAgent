---
seo_title: "Default System Prompt for build_agent"
description: "The default system prompt every build_agent agent receives: tool-first behaviour, schema awareness and citing tool results, and how to override or extend it."
---

# Default System Prompt

An agent built with `build_agent` without `instructions` receives a default system
prompt that establishes baseline behaviour: tool usage discipline, schema inspection, and
result citation.  The default prompt lives in a single module so it can be
reviewed, overridden, or extended without touching any builder logic.

**Source:** `src/promptise/prompt.py`

## Quick example

```python
import asyncio
from promptise import build_agent

async def main():
    # Uses the default system prompt automatically
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Hello, who are you?"}]}
    )
    print(result["messages"][-1].content)

asyncio.run(main())
```

## Concepts

### The default prompt

The `DEFAULT_SYSTEM_PROMPT` constant contains the following instructions:

```
You are a capable deep agent. Use available tools from connected MCP servers
to plan and execute tasks. Always inspect tool descriptions and input schemas
before calling them. Be precise and avoid hallucinating tool arguments.
Prefer calling tools rather than guessing, and cite results from tools clearly.
```

This prompt establishes five key behaviours:

1. **Tool-first mindset**: the agent should use tools rather than guess answers.
2. **Schema awareness**: always read tool descriptions and input schemas before calling.
3. **Precision**: avoid hallucinating tool argument values.
4. **Tool preference**: prefer calling a tool over making up a response.
5. **Citation**: cite tool results clearly in the final answer.

### Overriding the prompt

Pass `instructions` to `build_agent` to replace the default:

```python
import asyncio
from promptise import build_agent

async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        instructions=(
            "You are a financial analyst agent. Use tools to fetch market data. "
            "Always provide numerical evidence and cite your data sources."
        ),
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What is AAPL's P/E ratio?"}]}
    )
    print(result["messages"][-1].content)

asyncio.run(main())
```

### Extending the default

To add instructions on top of the default rather than replacing it entirely,
import the constant and concatenate:

```python
import asyncio
from promptise import build_agent
from promptise.prompt import DEFAULT_SYSTEM_PROMPT

async def main():
    custom_prompt = (
        DEFAULT_SYSTEM_PROMPT
        + "\n\nAdditional instructions: Always respond in formal English. "
        "Include a confidence score (0-100) with every answer."
    )
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        instructions=custom_prompt,
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Explain recursion"}]}
    )
    print(result["messages"][-1].content)

asyncio.run(main())
```

## API summary

| Export | Type | Description |
|--------|------|-------------|
| `DEFAULT_SYSTEM_PROMPT` | `str` | The system prompt an agent gets when you pass no `instructions` |

### When the default applies

| `build_agent(...)` | System prompt |
|---|---|
| no `instructions`, no `flow` | `DEFAULT_SYSTEM_PROMPT` |
| `instructions="..."` | your text only (the default is not added) |
| `instructions=Prompt` or a `PromptBuilder` result | the rendered prompt |
| `flow=ConversationFlow(...)` without `instructions` | the flow's prompt only |

## Tips and gotchas

!!! tip
    The default prompt is intentionally short and generic.  For production
    agents, always pass domain-specific `instructions` that describe
    the agent's role, available tools, expected output format, and any
    constraints.

!!! warning
    Replacing the system prompt removes all default instructions, including
    the "inspect tool schemas before calling" directive.  If your custom prompt
    does not include similar guidance, the LLM may produce more tool-calling
    errors.

!!! tip
    You can inspect the prompt at any time without reading the source file:

    ```python
    from promptise.prompt import DEFAULT_SYSTEM_PROMPT
    print(DEFAULT_SYSTEM_PROMPT)
    ```

## What's next

- [Building Agents](agents/building-agents.md): pass `instructions` to `build_agent`
- [Callback Handler](callback-handler.md): observe how the agent uses the prompt
- [Tools](tools.md): the tools the prompt tells the agent to inspect

!!! tip "Learn the concept"

    - [System Prompt Best Practices for AI Agents in Python](https://www.promptise.com/guides/view/ai-agent-system-prompt-best-practices): What belongs in an agent's system prompt and how to test it.
