# Troubleshooting

## zsh: no matches found: .[dev,deep]
Quote extras:
```bash
pip install -e ".[dev,deep]"
```

## PEP 668: externally managed environment (macOS)
Create/activate a venv:
```bash
python3 -m venv .venv && source .venv/bin/activate
```

## 404 when connecting (Client failed to connect: Session terminated)
Ensure your server exposes a **path** (e.g., `/mcp`) and your client uses it:
```python
HTTPServerSpec(url="http://127.0.0.1:8000/mcp", transport="http")
```

## AttributeError: `_FastMCPTool` has no attribute `_client`
Use the version where `_client` and `_tool_name` are `PrivateAttr` and set in `__init__`. Update your package.

## High token usage
Tool-calling models add overhead. Use smaller models while developing.

## Deprecation warnings (LangGraph)
`config_schema` warnings are safe. We’ll migrate to `context_schema` when stable.
