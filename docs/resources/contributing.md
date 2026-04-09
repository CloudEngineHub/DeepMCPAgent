# Contributing

Guidelines for contributing to Promptise Foundry.

---

## Overview

Promptise Foundry is a production-grade agentic framework used by enterprises, corporates, and developers in real-world applications. Every contribution must reflect this standard.

---

## Environment Setup

### Requirements

- Python 3.11 or higher
- Virtual environment at `.venv/`

### Getting Started

```bash
# Clone the repository
git clone https://github.com/promptise/foundry.git
cd foundry

# Create and activate the virtual environment
python -m venv .venv
source .venv/bin/activate

# Install in development mode
pip install -e ".[dev,deep,sandbox]"
```

### Environment Variables

Set these for running examples and integration tests:

```bash
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## Package Structure

Source code lives in `src/promptise/`:

```
src/promptise/
    __init__.py              # Core entry point: build_agent
    config.py                # HTTPServerSpec, StdioServerSpec
    prompts/                 # Prompt engineering framework
    runtime/                 # Agent runtime and lifecycle
    mcp/                     # MCP server and client
    sandbox/                 # Sandbox execution
    cross_agent.py           # Cross-agent communication
```

The core entry point is:

```python
from promptise import build_agent
```

---

## Code Quality Standards

All code must be production-ready. No placeholders and no TODOs in shipped code.

### Type Hints

Type hints are required on all public APIs:

```python
async def build_agent(
    servers: dict[str, ServerSpec],
    model: str,
    instructions: str,
    *,
    trace_tools: bool = False,
) -> PromptiseAgent:
    ...
```

### Docstrings

Use Google-style docstrings on all public classes and functions:

```python
class AgentProcess:
    """Lifecycle container for an autonomous agent process.

    Wraps a PromptiseAgent with a state machine, trigger queue,
    heartbeat, and concurrency control.

    Args:
        name: Unique identifier for this process.
        config: Process configuration including model, servers, triggers.

    Example:
        >>> process = AgentProcess(name="watcher", config=config)
        >>> await process.start()
        >>> assert process.state == ProcessState.RUNNING
    """
```

### No RBAC

The framework uses capability-based `AgentAccessPolicy`, not role-based access control. Do not introduce RBAC patterns.

---

## Testing

### Running Tests

Use the virtual environment Python to run tests:

```bash
.venv/bin/python -m pytest tests/ -x -q
```

Skip known slow tests:

```bash
.venv/bin/python -m pytest tests/ -x -q \
    --ignore=tests/test_autonomous_swarm.py \
    --ignore=tests/test_sandbox_integration.py
```

### Writing Tests

- Every new feature must include tests
- Tests should be fast and deterministic
- Use fixtures from `tests/conftest.py` where available
- Name test files `test_<module>.py`

---

## Examples

### Rules for Examples

All examples **must** use real LLM calls via `build_agent()`. Never use mocks, stubs, or fake responses in examples.

- Default model: `openai:gpt-5-mini` (affordable, fast, reliable)
- Examples must be runnable end-to-end with just an API key environment variable set
- Documentation code samples must show real, working patterns from the actual codebase
- Never write "mock" or "stub" agents in examples

### Example Structure

Examples are organized by feature area:

```
examples/
    mcp/              # MCP server, client, and agent examples
    prompts/          # Prompt engineering framework
    runtime/          # Agent runtime and lifecycle
```

### Testing Examples

Before submitting, verify your example runs:

```bash
export OPENAI_API_KEY=sk-...
python examples/your_example.py
```

---

## Architecture Notes

Key architectural decisions to be aware of when contributing:

- **Prompt engineering**: Three independent layers (blocks, flows, graphs)
- **Agent runtime**: Process-based lifecycle with triggers and journals

---

## Pull Request Process

1. Create a feature branch from `main`
2. Make your changes following the code quality standards above
3. Add or update tests for your changes
4. Run the test suite and confirm all tests pass
5. Update documentation if your changes affect public APIs
6. Submit a pull request with a clear description of the changes

---

## Commit Messages

Use clear, descriptive commit messages:

```
feat: add WebhookTrigger retry logic with exponential backoff
fix: prevent AgentProcess from double-counting tool calls
docs: add Agent Runtime examples to gallery
test: add integration tests for MCPMultiClient routing
```
