# Lab: Code Review Agent

Build an agent that reviews code for security vulnerabilities, performance issues, and best practices — using adversarial self-critique to catch false positives. Every finding must be justified with specific evidence.

## What You'll Build

An agent that:

- Reads code files and pull request diffs
- Analyzes for security, performance, and style issues
- Uses adversarial critique to challenge its own findings
- Justifies every issue with specific line references
- Produces a structured review with severity ratings
- Uses per-node model override (cheap model for classification, powerful for analysis)

## Prerequisites

```bash
pip install promptise
export OPENAI_API_KEY=sk-...
```

## Step 1 — Build the Code Tools Server

```python
# code_server.py
from promptise.mcp.server import MCPServer

server = MCPServer("code-tools")

# Sample codebase for review
FILES = {
    "auth.py": '''
import hashlib
import os

def hash_password(password: str) -> str:
    """Hash a password for storage."""
    salt = os.urandom(16)
    return hashlib.md5(password.encode()).hexdigest()  # Weak hash!

def verify_token(token: str) -> bool:
    """Verify an auth token."""
    # TODO: implement proper JWT verification
    return token == "admin-token-123"  # Hardcoded token!

def get_user(user_id: str) -> dict:
    query = f"SELECT * FROM users WHERE id = '{user_id}'"  # SQL injection!
    return {"id": user_id, "query": query}
''',
    "api.py": '''
from flask import Flask, request, jsonify
import subprocess

app = Flask(__name__)

@app.route("/execute", methods=["POST"])
def execute_code():
    """Execute user-provided code."""
    code = request.json.get("code", "")
    result = subprocess.run(code, shell=True, capture_output=True)  # RCE!
    return jsonify({"output": result.stdout.decode()})

@app.route("/users/<user_id>")
def get_user(user_id):
    # No authentication check!
    return jsonify(get_user_from_db(user_id))

@app.route("/upload", methods=["POST"])
def upload_file():
    f = request.files["file"]
    f.save(f"/uploads/{f.filename}")  # Path traversal!
    return "uploaded"
''',
    "utils.py": '''
import json
import time

def retry(func, max_retries=3):
    """Retry a function with no backoff."""
    for i in range(max_retries):
        try:
            return func()
        except Exception:
            time.sleep(0)  # No backoff!
            continue
    raise Exception("Max retries exceeded")

def parse_config(data: str) -> dict:
    """Parse configuration from string."""
    return eval(data)  # eval is dangerous!

CACHE = {}
def cached_query(key):
    """Cache with no eviction."""
    if key not in CACHE:
        CACHE[key] = expensive_query(key)  # Unbounded cache!
    return CACHE[key]
''',
}

@server.tool()
async def list_files() -> str:
    """List all files available for review."""
    return "Files:\n" + "\n".join(f"  - {name} ({len(content)} chars)" for name, content in FILES.items())

@server.tool()
async def read_file(filename: str) -> str:
    """Read the contents of a file."""
    content = FILES.get(filename)
    if not content:
        return f"File not found: {filename}. Available: {', '.join(FILES.keys())}"
    lines = content.strip().split("\n")
    numbered = [f"{i+1:3d} | {line}" for i, line in enumerate(lines)]
    return f"=== {filename} ===\n" + "\n".join(numbered)

@server.tool()
async def search_pattern(pattern: str) -> str:
    """Search all files for a pattern (case-insensitive)."""
    results = []
    for filename, content in FILES.items():
        for i, line in enumerate(content.strip().split("\n"), 1):
            if pattern.lower() in line.lower():
                results.append(f"  {filename}:{i} | {line.strip()}")
    return f"Found {len(results)} matches for '{pattern}':\n" + "\n".join(results) if results else f"No matches for: {pattern}"

if __name__ == "__main__":
    server.run(transport="stdio")
```

## Step 2 — Build the Review Agent with Adversarial Critique

The key pattern: **Read → Analyze → Critique → Justify → Synthesize**

The CritiqueNode challenges the agent's own findings — forcing it to distinguish real vulnerabilities from false positives. The JustifyNode requires specific line references for every claim.

```python
# review_agent.py
import asyncio
import sys
from promptise import build_agent
from promptise.config import StdioServerSpec
from promptise.engine import PromptGraph, PromptNode, NodeFlag
from promptise.engine.reasoning_nodes import (
    ThinkNode, CritiqueNode, JustifyNode, SynthesizeNode,
)

async def main():
    graph = PromptGraph("code-reviewer", nodes=[
        # Read all files first
        PromptNode("read_code",
            instructions=(
                "Read ALL files in the codebase using list_files and read_file. "
                "You must read every file before proceeding to analysis."
            ),
            inject_tools=True,
            is_entry=True,
            flags={NodeFlag.RETRYABLE},
        ),

        # Analyze for issues
        ThinkNode("analyze",
            focus_areas=[
                "security vulnerabilities (injection, RCE, auth bypass)",
                "performance issues (unbounded caches, missing backoff)",
                "code quality (eval usage, hardcoded secrets, missing validation)",
            ],
        ),

        # Challenge the findings — adversarial self-review
        CritiqueNode("challenge",
            severity_threshold=0.3,  # Low threshold = strict review
        ),

        # Justify every finding with line references
        JustifyNode("evidence"),

        # Produce structured report
        SynthesizeNode("report", is_terminal=True),
    ])

    agent = await build_agent(
        model="openai:gpt-4o-mini",
        servers={
            "code": StdioServerSpec(
                command=sys.executable,
                args=["code_server.py"],
            ),
        },
        agent_pattern=graph,
        instructions=(
            "You are a senior security engineer reviewing code. "
            "Find real vulnerabilities — not theoretical concerns. "
            "Every finding must cite a specific file and line number. "
            "Rate severity: CRITICAL, HIGH, MEDIUM, LOW."
        ),
    )

    result = await agent.ainvoke({
        "messages": [{
            "role": "user",
            "content": "Review all files for security vulnerabilities and code quality issues.",
        }]
    })

    for msg in reversed(result["messages"]):
        if getattr(msg, "type", "") == "ai" and msg.content:
            print(msg.content)
            break

    await agent.shutdown()

asyncio.run(main())
```

## Step 3 — Per-Node Model Override

Use a cheaper model for reading files, a powerful model for security analysis:

```python
# Cheap model reads files (just fetching data, no reasoning needed)
PromptNode("read_code",
    model_override="openai:gpt-4o-mini",
    ...
)

# Powerful model for security analysis
ThinkNode("analyze",
    model_override="openai:gpt-4o",  # Better at catching subtle vulns
    ...
)

# Cheap model for final formatting
SynthesizeNode("report",
    model_override="openai:gpt-4o-mini",
    ...
)
```

## Expected Output

The agent should find these real issues:

| File | Line | Severity | Issue |
|------|------|----------|-------|
| auth.py | 7 | CRITICAL | MD5 password hashing (use bcrypt/argon2) |
| auth.py | 12 | CRITICAL | Hardcoded auth token |
| auth.py | 15 | CRITICAL | SQL injection via string formatting |
| api.py | 10 | CRITICAL | Remote code execution via shell=True |
| api.py | 14 | HIGH | Missing authentication on user endpoint |
| api.py | 19 | HIGH | Path traversal in file upload |
| utils.py | 8 | MEDIUM | Retry with no exponential backoff |
| utils.py | 14 | HIGH | eval() on untrusted input |
| utils.py | 19 | MEDIUM | Unbounded cache (memory leak) |

## Why Adversarial Critique Matters

Without the CritiqueNode, agents produce generic reviews ("consider using parameterized queries"). With it:

- **False positives are challenged**: "Is this really exploitable, or just a style concern?"
- **Severity is calibrated**: The critique step forces honest assessment of impact
- **Evidence is required**: JustifyNode rejects findings that can't cite specific code

## Next Steps

- [Custom Reasoning Guide](custom-reasoning.md) — Build more complex patterns
- [Sandbox](../core/sandbox.md) — Run the agent's code analysis in a Docker sandbox
- [Reasoning Engine](../core/engine.md) — Deep dive into the node system
