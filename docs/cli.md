# CLI

Use DeepMCPAgent without writing Python.

## Version
```bash
deepmcpagent --version
```

## List tools
```bash
deepmcpagent list-tools   --http name=math url=http://127.0.0.1:8000/mcp transport=http   --model-id "openai:gpt-4.1"
```

## Interactive chat
```bash
deepmcpagent run   --http name=math url=http://127.0.0.1:8000/mcp transport=http   --model-id "openai:gpt-4.1"
```

### Flags
- `--http name=... url=... [transport=http|streamable-http|sse] [header.X=Y]`
- (kept for completeness) `--stdio name=... command=... args="..."`
- `--model-id` (required): provider string passed to LangChain’s `init_chat_model`
- `--instructions`: override system prompt

### Examples
```bash
# With auth header
deepmcpagent list-tools   --http name=ext url=https://api.example.com/mcp transport=http header.Authorization="Bearer TOKEN"   --model-id "anthropic:claude-3-5-sonnet-latest"
```
