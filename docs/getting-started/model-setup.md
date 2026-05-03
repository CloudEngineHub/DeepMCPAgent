---
title: Model Setup — Use any LLM with Promptise Foundry (OpenAI, Anthropic, Ollama)
description: Set up any LLM in Promptise Foundry with a single string — OpenAI GPT, Anthropic Claude, Ollama (local), Mistral, Google Gemini, or any LangChain BaseChatModel. Model-agnostic by design. Switch providers by changing one line.
keywords: Promptise model setup, OpenAI agent, Anthropic agent, Ollama agent, Claude AI agent, local LLM agent, model agnostic AI agent
---

# Model Setup

Promptise supports any LLM provider that LangChain integrates with. Models are specified as a string in the format `"provider:model-name"`.

## Provider String Format

```python
agent = await build_agent(
    model="openai:gpt-5-mini",   # provider:model-name
    servers=...,
)
```

## Supported Providers

| Provider | Format | Example | Env Variable |
|----------|--------|---------|--------------|
| OpenAI | `openai:model` | `openai:gpt-5-mini` | `OPENAI_API_KEY` |
| Anthropic | `anthropic:model` | `anthropic:claude-sonnet-4` | `ANTHROPIC_API_KEY` |
| Google | `google:model` | `google:gemini-2.5-pro` | `GOOGLE_API_KEY` |
| Ollama | `ollama:model` | `ollama:llama3` | _(local, no key needed)_ |

Set the appropriate environment variable for your provider:

```bash
# OpenAI
export OPENAI_API_KEY=sk-...

# Anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# Google
export GOOGLE_API_KEY=AIza...
```

## The `ModelLike` Type

The `model` parameter accepts three types:

### 1. Provider String (recommended)

The simplest option. Promptise uses LangChain's `init_chat_model` to resolve the string:

```python
agent = await build_agent(
    model="openai:gpt-5-mini",
    servers=my_servers,
)
```

### 2. LangChain Chat Model Instance

Pass a pre-configured `BaseChatModel` for full control over parameters like temperature, max tokens, and base URL:

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="gpt-5-mini",
    temperature=0.2,
    max_tokens=4096,
)

agent = await build_agent(
    model=llm,
    servers=my_servers,
)
```

### 3. LangChain Runnable

Any LangChain `Runnable` that accepts chat messages and returns a response. Useful for custom chains or model wrappers:

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model="gpt-5-mini").with_retry(max_retries=3)

agent = await build_agent(
    model=llm,
    servers=my_servers,
)
```

## Using Ollama (Local Models)

Run models locally with [Ollama](https://ollama.com) -- no API key required:

```bash
# Install and start Ollama, then pull a model
ollama pull llama3
```

```python
agent = await build_agent(
    model="ollama:llama3",
    servers=my_servers,
)
```

!!! warning "Local model limitations"
    Local models vary in their ability to use tools reliably. For production agent systems with tool calling, cloud providers (OpenAI, Anthropic) provide the most consistent results.

## Default Model

All documentation examples use `openai:gpt-5-mini` as the default. It offers a good balance of capability, speed, and cost for development and testing.
