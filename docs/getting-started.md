# Getting Started

This guide takes you from zero to a working **MCP-only agent**.

## 1) Start a sample MCP server (HTTP)

```bash
python examples/servers/math_server.py
```
This exposes an MCP endpoint at **http://127.0.0.1:8000/mcp**.

## 2) Bring your own model (BYOM)

DeepMCPAgent requires a model — either a **LangChain model instance** or a **provider id string** that `init_chat_model()` understands.

### Example (OpenAI via LangChain)
```python
from langchain_openai import ChatOpenAI
model = ChatOpenAI(model="gpt-4.1")
```

### Example (Anthropic via LangChain)
```python
from langchain_anthropic import ChatAnthropic
model = ChatAnthropic(model="claude-3-5-sonnet-latest")
```

### Example (Ollama local)
```python
from langchain_community.chat_models import ChatOllama
model = ChatOllama(model="llama3.1")
```

## 3) Build the agent

```python
import asyncio
from deepmcpagent import HTTPServerSpec, build_deep_agent

async def main():
    servers = {
        "math": HTTPServerSpec(
            url="http://127.0.0.1:8000/mcp",
            transport="http",     # or "sse"
        ),
    }

    graph, loader = await build_deep_agent(
        servers=servers,
        model=model,  # required
    )

    result = await graph.ainvoke({"messages":[{"role":"user","content":"add 21 and 21 using tools"}]})
    print(result)

asyncio.run(main())
```
