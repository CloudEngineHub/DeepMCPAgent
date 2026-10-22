---
seo_title: "Prompt Testing: Mock LLMs and Assertions"
description: "Test prompts with PromptTestCase: mock LLM calls and context, and assert on content, output schema, latency, context providers and guard behaviour."
---

# Prompt Testing

A small test helper for prompts. `PromptTestCase` provides mocked LLM calls, context injection, and assertions for content, result type, latency and guards.

## Quick Example

```python
import pytest
from promptise.prompts import prompt, PromptInspector
from promptise.prompts.testing import PromptTestCase
from promptise.prompts.context import context, user_context, UserContext

inspector = PromptInspector()

@prompt(model="openai:gpt-5-mini", inspect=inspector)
@context(user_context())
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
            with self.mock_llm("strongly positive"):
                await self.run_prompt("Great product")
        # The mocked model ignores the prompt; check what would have been sent
        assert "**Expertise:** expert" in inspector.last().input_text
```

`mock_llm()` returns your fixed text whatever the prompt says, so assertions on the result only test parsing, strategies and guards. To test what the prompt *sends*, attach a [`PromptInspector`](inspector.md) and assert on `trace.input_text`, or call `await prompt.render_async(...)`.

## Concepts

`PromptTestCase` is a plain base class (not a `unittest.TestCase`). With pytest, use `pytest-asyncio` (`asyncio_mode = "auto"`, or mark each test). With unittest, also inherit from `unittest.IsolatedAsyncioTestCase` so `async def test_*` methods run. Key features:

- **`mock_llm(response)`**: mock the LLM call to return a fixed string, eliminating API costs during testing.
- **`mock_context(**contexts)`**: temporarily inject world contexts into the prompt under test.
- **Specialized assertions**: purpose-built for prompt validation (schema, latency, guards, content).
- **Stats access**: retrieve `PromptStats` from the last call for latency assertions.

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

`mock_llm(response)` patches the chat model factory used by standalone prompt calls (`promptise.prompts.core.init_chat_model`) so the model returns a fixed string. No API calls are made, and no API key is needed. It does not affect agents built with `build_agent()`:

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

The original world contexts are restored when the context manager exits. `mock_context()` changes the shared `Prompt` object while it is active, so don't run tests that use it on the same prompt concurrently. World contexts only reach the prompt text through a [context provider](context.md) such as `user_context()`.

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

Check the result's type with `isinstance` (works with dataclasses, Pydantic models, and basic types; it does not validate field values):

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

`stats` is the prompt's `last_stats`, a `PromptStats`:

| Field | Type | Description |
|-------|------|-------------|
| `prompt_name` | `str` | The prompt's name |
| `model` | `str` | The model string |
| `latency_ms` | `float` | Wall time of the call, including guards |
| `input_tokens` / `output_tokens` | `int` | From the model's reply (LangChain `usage_metadata`); `0` when the provider reports no usage |
| `context_providers_used` | `list[str] \| None` | Class names of the attached context providers |
| `strategy` / `perspective` | `str` | `repr()` of the strategy and perspective, or `""` |

### Context Provider Assertions

Verify that a context provider of a given class was attached to the prompt (it checks `stats.context_providers_used`, not whether the provider produced text):

```python
self.assert_context_provided(stats, "UserContextProvider")
```

### Guard Assertions

Assert that the result was not blocked by a guard. Guards raise, so this only checks that the result is not `None`; test a rejection with `pytest.raises(GuardError)` as below:

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
| `assert_schema(result, expected_type)` | Assert `isinstance(result, expected_type)` |
| `assert_contains(result, substring)` | Assert substring is present |
| `assert_not_contains(result, substring)` | Assert substring is absent |
| `assert_latency(stats, max_ms)` | Assert latency within limit |
| `assert_context_provided(stats, name)` | Assert a provider class name is in `stats.context_providers_used` |
| `assert_guard_passed(result)` | Assert the result is not `None` |

All assertions raise `AssertionError`. `run_prompt()` and `mock_context()` raise `ValueError` when the `prompt` attribute is not set.

!!! tip "Mock for speed, real for confidence"
    Use `mock_llm()` in unit tests for fast, deterministic feedback loops. Use real LLM calls in integration tests (guarded by API key checks) for end-to-end validation.

!!! tip "pytest compatibility"
    Define your test classes with `PromptTestCase` as a base, use `async def test_*` methods, install `pytest-asyncio`, and run with `pytest --asyncio-mode=auto`.

!!! warning "Set the prompt attribute"
    Calling `run_prompt()` without setting the `prompt` class attribute raises `ValueError`. Always set `prompt = my_prompt` on your test class.

## What's Next

- [Guards](guards.md): Input/output validation to test against
- [Inspector](inspector.md): Trace prompt assembly for debugging
- [Prompt Chaining](chaining.md): Compose prompts into pipelines

!!! tip "Learn the concept"

    - [How to Test LLM Prompts and AI Agents in Python](https://www.promptise.com/guides/view/test-llm-prompts-ai-agents-python): Unit test prompts with pytest and catch regressions in CI.
    - [How to Evaluate AI Agents: Evals, LLM Judges and pass^k](https://www.promptise.com/guides/view/evaluate-ai-agents-python): Golden tasks, LLM judges and pass^k for agents.
    - [Topic hub: Testing and evals](https://www.promptise.com/guides/topics/testing-evals): Every guide on testing and evals.
