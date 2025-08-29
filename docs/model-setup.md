# Model Setup (BYOM)

DeepMCPAgent **requires** a model — there is no fallback.

You may pass:
- a **LangChain chat model instance**, or
- a **provider id string** (forwarded to `langchain.chat_models.init_chat_model()`)

## Passing a model instance
```python
from langchain_openai import ChatOpenAI
model = ChatOpenAI(model="gpt-4.1")
graph, loader = await build_deep_agent(servers=servers, model=model)
```

## Passing a provider id string
```python
graph, loader = await build_deep_agent(
    servers=servers,
    model="openai:gpt-4.1"   # handled by LangChain init_chat_model
)
```

## Environment variables
Use provider-specific env vars (e.g., `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`).  
You can load a local `.env` via `python-dotenv`:

```python
from dotenv import load_dotenv
load_dotenv()
```

## Tips
- Prefer instances for fine-grained control (temperature, timeouts).
- Use smaller models for dev / testing to save latency & cost.
