# Prompt Testing

A purpose-built test framework for validating prompt behavior. `PromptTestCase` provides mocked LLM calls, context injection, and specialized assertions for latency, schema compliance, and guard behavior.

## Quick Example

```python
import pytest
from promptise.prompts import prompt
from promptise.prompts.testing import PromptTestCase
from promptise.prompts.context import UserContext

@prompt(model="openai:gpt-5-mini")
async def analyze_sentiment(text: str) -> str:
    """Analyze the sentiment of this text: {text}"""

class TestSentiment(PromptTestCase):
    prompt = analyze_sentiment

    async def test_positive(self):
        with self.mock_llm("positive"):
            result = await self.run_prompt("I love this product!")
            self.assert_contains(result, "positive")

    async def test_with_context(self):
        with self.mock_context(user=UserContext(expertise_level="expert")):
            with self.mock_llm("expert analysis: strongly positive"):
                result = await self.run_prompt("Great product")
                self.assert_contains(result, "expert")
```

## Concepts

`PromptTestCase` is a base class for prompt tests. It works with both `pytest` and `unittest.TestCase` patterns. Key features:

- **`mock_llm(response)`** -- mock the LLM call to return a fixed string, eliminating API costs during testing.
- **`mock_context(**contexts)`** -- temporarily inject world contexts into the prompt under test.
- **Specialized assertions** -- purpose-built for prompt validation (schema, latency, guards, content).
- **Stats access** -- retrieve `PromptStats` from the last call for latency assertions.

## Setting Up Tests

### Configure the Prompt Under Test

Set the `prompt` class attribute to the `Prompt` you want to test:

```python
from promptise.prompts.testing import PromptTestCase

class TestMyPrompt(PromptTestCase):
    prompt = my_analysis_prompt
```

### Running Prompts

Use `run_prompt()` to execute the prompt:

```python
async def test_basic(self):
    with self.mock_llm("expected response"):
        result = await self.run_prompt("input text")
        assert result == "expected response"
```

Use `run_with_stats()` to get both the result and execution stats:

```python
async def test_with_stats(self):
    with self.mock_llm("response"):
        result, stats = await self.run_with_stats("input text")
        self.assert_latency(stats, max_ms=5000)
```

## Mocking

### Mocking LLM Calls

`mock_llm(response)` patches the LLM to return a fixed string. No API calls are made:

```python
async def test_mocked(self):
    with self.mock_llm("The sentiment is positive."):
        result = await self.run_prompt("I love it!")
        self.assert_contains(result, "positive")
```

### Mocking Context

`mock_context(**contexts)` temporarily adds world contexts to the prompt:

```python
from promptise.prompts.context import UserContext, BaseContext

async def test_with_user(self):
    with self.mock_context(
        user=UserContext(name="Alice", expertise_level="expert"),
        project=BaseContext(name="Q1 Report"),
    ):
        with self.mock_llm("Expert-level analysis..."):
            result = await self.run_prompt("quarterly data")
            self.assert_contains(result, "Expert")
```

The original world contexts are restored when the context manager exits.

### Combining Mocks

Stack `mock_llm` and `mock_context` for isolated tests:

```python
async def test_full_isolation(self):
    with self.mock_context(user=UserContext(expertise_level="beginner")):
        with self.mock_llm("Simple explanation of the data."):
            result = await self.run_prompt("complex technical data")
            self.assert_contains(result, "Simple")
            self.assert_not_contains(result, "error")
```

## Assertions

### Content Assertions

```python
# Assert result contains a substring
self.assert_contains(result, "recommendation")

# Assert result does NOT contain a substring
self.assert_not_contains(result, "error")
```

### Schema Assertions

Validate that the result matches an expected type (works with dataclasses, Pydantic models, and basic types):

```python
from pydantic import BaseModel

class Analysis(BaseModel):
    summary: str
    confidence: float

self.assert_schema(result, Analysis)
self.assert_schema(result, str)
self.assert_schema(result, dict)
```

### Latency Assertions

```python
result, stats = await self.run_with_stats("test input")

# Assert latency is within limit
self.assert_latency(stats, max_ms=5000)
```

### Context Provider Assertions

Verify that a specific context provider contributed to the prompt:

```python
self.assert_context_provided(stats, "ToolContextProvider")
```

### Guard Assertions

Assert that the result was not blocked by a guard:

```python
self.assert_guard_passed(result)
```

## Test Patterns

### Testing with Real LLM Calls

For integration tests that validate actual LLM behavior (requires API key):

```python
import os
import pytest

class TestRealLLM(PromptTestCase):
    prompt = analyze_sentiment

    @pytest.mark.skipif(
        not os.getenv("OPENAI_API_KEY"),
        reason="Requires OPENAI_API_KEY",
    )
    async def test_real_sentiment(self):
        result = await self.run_prompt("I absolutely love this product!")
        self.assert_contains(result, "positive")
```

### Testing Guard Behavior

```python
from promptise.prompts.guards import GuardError

class TestGuards(PromptTestCase):
    prompt = guarded_prompt

    async def test_blocked_input(self):
        with self.mock_llm("should not reach here"):
            with pytest.raises(GuardError) as exc_info:
                await self.run_prompt("Contains secret data")
            assert exc_info.value.guard_name == "content_filter"

    async def test_clean_input(self):
        with self.mock_llm("Analysis complete"):
            result = await self.run_prompt("Clean input data")
            self.assert_guard_passed(result)
```

### Testing Different User Contexts

```python
class TestExpertiseAdaptation(PromptTestCase):
    prompt = adaptive_prompt

    async def test_beginner_response(self):
        with self.mock_context(user=UserContext(expertise_level="beginner")):
            with self.mock_llm("Here is a simple explanation..."):
                result = await self.run_prompt("Explain neural networks")
                self.assert_contains(result, "simple")

    async def test_expert_response(self):
        with self.mock_context(user=UserContext(expertise_level="expert")):
            with self.mock_llm("The gradient descent optimization..."):
                result = await self.run_prompt("Explain neural networks")
                self.assert_contains(result, "gradient")
```

## API Summary

### PromptTestCase

| Method | Returns | Description |
|--------|---------|-------------|
| `run_prompt(*args, **kwargs)` | `Any` | Execute the prompt under test |
| `run_with_stats(*args, **kwargs)` | `tuple[Any, PromptStats]` | Execute and return result + stats |

### Context Managers

| Method | Description |
|--------|-------------|
| `mock_llm(response)` | Mock the LLM to return a fixed string |
| `mock_context(**contexts)` | Temporarily inject world contexts |

### Assertions

| Method | Description |
|--------|-------------|
| `assert_schema(result, expected_type)` | Assert result matches type |
| `assert_contains(result, substring)` | Assert substring is present |
| `assert_not_contains(result, substring)` | Assert substring is absent |
| `assert_latency(stats, max_ms)` | Assert latency within limit |
| `assert_context_provided(stats, name)` | Assert context provider was used |
| `assert_guard_passed(result)` | Assert not blocked by a guard |

!!! tip "Mock for speed, real for confidence"
    Use `mock_llm()` in unit tests for fast, deterministic feedback loops. Use real LLM calls in integration tests (guarded by API key checks) for end-to-end validation.

!!! tip "pytest compatibility"
    `PromptTestCase` works seamlessly with pytest. Define your test classes with `PromptTestCase` as a base, use `async def test_*` methods, and run with `pytest --asyncio-mode=auto`.

!!! warning "Set the prompt attribute"
    Calling `run_prompt()` without setting the `prompt` class attribute raises `ValueError`. Always set `prompt = my_prompt` on your test class.

## What's Next

- [Guards](guards.md) -- Input/output validation to test against
- [Inspector](inspector.md) -- Trace prompt assembly for debugging
- [Prompt Chaining](chaining.md) -- Compose prompts into pipelines
