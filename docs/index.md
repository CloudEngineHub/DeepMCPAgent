---
title: DeepMCPAgent
---

<div align="center">
  <img src="/images/icon.png" width="120" alt="DeepMCPAgent Logo"/>
  <h1>DeepMCPAgent</h1>
  <p><em>Model-agnostic LangChain/LangGraph agents powered entirely by MCP tools over HTTP/SSE.</em></p>
</div>

---

## Why DeepMCPAgent?

- 🔌 **Zero manual wiring** — discover tools dynamically from MCP servers
- 🌐 **External APIs welcome** — HTTP / SSE servers with headers & auth
- 🧠 **Bring your own model** — any LangChain chat model (OpenAI, Anthropic, Ollama, Groq, local, …)
- ⚡ **DeepAgents loop (optional)** — or **LangGraph ReAct** fallback if not installed
- 🛠️ **Typed tools** — JSON Schema → Pydantic → LangChain `BaseTool`
- 🧪 **Quality** — mypy (strict), ruff, pytest, GitHub Actions

---

## TL;DR (Quickstart)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install "deepmcpagent[deep]"
python examples/servers/math_server.py  # serves http://127.0.0.1:8000/mcp
python examples/use_agent.py
```
