# FAQ

## Why MCP-only tools?
MCP provides a clean, standard way to expose tools. Agents should discover tools at runtime — not hardcode them.

## Can I connect to multiple servers?
Yes. Pass a dict of names → `HTTPServerSpec(...)`. All tools get merged for the agent.

## How do I authenticate to external MCP APIs?
Use the `headers` field in `HTTPServerSpec` (e.g., `Authorization: Bearer ...`).

## Do I need OpenAI?
No. You must pass a model, but it can be any LangChain chat model (Anthropic, Ollama, Groq, local LLMs, …).

## Can I run stdio servers?
FastMCP’s Python client is oriented around HTTP/SSE. If you have a stdio server, put an HTTP shim in front or use another adapter.
