# Prompt Chaining

Compose prompts into multi-step pipelines with sequential chains, concurrent execution, conditional branching, retry logic, and fallback strategies.

## Quick Example

```python
from promptise.prompts import prompt
from promptise.prompts.chain import chain, parallel, retry

@prompt(model="openai:gpt-5-mini")
async def extract_facts(text: str) -> str:
    """Extract key facts from: {text}"""

@prompt(model="openai:gpt-5-mini")
async def analyze_facts(text: str) -> str:
    """Analyze these facts: {text}"""

@prompt(model="openai:gpt-5-mini")
async def write_summary(text: str) -> str:
    """Write an executive summary: {text}"""

pipeline = chain(extract_facts, analyze_facts, write_summary)
result = await pipeline("Raw quarterly data...")
```

## Concepts

The chaining module provides five composition operators:

| Operator | Behavior |
|----------|----------|
| `chain` | Sequential -- output of each step feeds the next |
| `parallel` | Concurrent -- all prompts run at once on the same input |
| `branch` | Conditional -- route to different prompts based on a predicate |
| `retry` | Resilience -- retry a prompt with exponential backoff |
| `fallback` | Graceful degradation -- try alternatives on failure |

All operators return callable objects that behave like prompts. You can nest and combine them freely.

## Chain (Sequential)

`chain(*prompts)` executes prompts in order. Each prompt's output is passed as input to the next prompt. The final prompt's output is returned.

```python
from promptise.prompts.chain import chain

pipeline = chain(extract_facts, analyze_facts, write_summary)
result = await pipeline("Raw data...")
```

The data flow:

1. `extract_facts("Raw data...")` returns extracted facts.
2. `analyze_facts(extracted_facts)` returns analysis.
3. `write_summary(analysis)` returns the final summary.

### How Output Feeds Input

- **String output** -- passed as the first positional argument to the next prompt.
- **Dict output** -- unpacked as keyword arguments to the next prompt.
- **Other types** -- converted to string via `str()` and passed as the first argument.

```python
# String -> positional arg
# {"key": "value"} -> **kwargs
# 42 -> str(42) -> positional arg
```

!!! warning "Chain requires at least 2 prompts"
    Calling `chain()` with fewer than 2 prompts raises `ValueError`.

## Parallel (Concurrent)

`parallel(**prompts)` executes multiple prompts concurrently on the same input. Returns a dict mapping prompt names to their results.

```python
from promptise.prompts.chain import parallel

@prompt(model="openai:gpt-5-mini")
async def analyze_sentiment(text: str) -> str:
    """Analyze the sentiment of: {text}"""

@prompt(model="openai:gpt-5-mini")
async def extract_topics(text: str) -> str:
    """Extract main topics from: {text}"""

multi = parallel(sentiment=analyze_sentiment, topics=extract_topics)
results = await multi("Great product launch, revenue up 40%!")

print(results["sentiment"])  # Sentiment analysis
print(results["topics"])     # Topic extraction
```

All prompts receive the same arguments. Execution uses `asyncio.gather` for true concurrency.

!!! warning "Parallel requires at least 2 prompts"
    Calling `parallel()` with fewer than 2 named prompts raises `ValueError`.

## Branch (Conditional)

`branch(condition, routes, default=None)` routes to different prompts based on a condition function. The condition receives the same arguments as the prompts and must return a string key that matches one of the routes.

```python
from promptise.prompts.chain import branch

@prompt(model="openai:gpt-5-mini")
async def technical_response(text: str) -> str:
    """Provide a technical explanation: {text}"""

@prompt(model="openai:gpt-5-mini")
async def simple_response(text: str) -> str:
    """Explain in simple terms: {text}"""

@prompt(model="openai:gpt-5-mini")
async def default_response(text: str) -> str:
    """Respond to: {text}"""

def classify_input(text: str) -> str:
    if any(term in text.lower() for term in ["api", "algorithm", "protocol"]):
        return "technical"
    return "simple"

router = branch(
    condition=classify_input,
    routes={
        "technical": technical_response,
        "simple": simple_response,
    },
    default=default_response,
)

result = await router("Explain the TCP protocol")
# Routes to technical_response
```

If the condition returns a key not in `routes` and no `default` is set, a `ValueError` is raised.

## Retry (Exponential Backoff)

`retry(target, max_retries=3, backoff=1.0)` wraps a prompt with automatic retry on failure. The backoff doubles after each attempt.

```python
from promptise.prompts.chain import retry

resilient = retry(analyze_facts, max_retries=3, backoff=1.0)
result = await resilient("data...")
```

Backoff schedule with `backoff=1.0`:

| Attempt | Wait before retry |
|---------|-------------------|
| 1st failure | 1.0 seconds |
| 2nd failure | 2.0 seconds |
| 3rd failure | 4.0 seconds |
| 4th failure | (raises the exception) |

If all retries are exhausted, the last exception is raised.

## Fallback (Try Alternatives)

`fallback(primary, *alternatives)` tries prompts in order until one succeeds. The first successful result is returned.

```python
from promptise.prompts.chain import fallback

@prompt(model="openai:gpt-5-mini")
async def fast_analyze(text: str) -> str:
    """Quick analysis: {text}"""

@prompt(model="openai:gpt-5-mini")
async def thorough_analyze(text: str) -> str:
    """Detailed analysis: {text}"""

safe = fallback(fast_analyze, thorough_analyze)
result = await safe("quarterly data...")
```

If the primary fails, each alternative is tried in order. If all fail, the last exception is raised.

## Combining Operators

Operators are composable. Build complex pipelines by nesting them:

```python
from promptise.prompts.chain import chain, parallel, retry, fallback

# Retry the extraction step, then branch into parallel analysis
pipeline = chain(
    retry(extract_facts, max_retries=2),
    parallel(
        sentiment=analyze_sentiment,
        topics=extract_topics,
    ),
)

result = await pipeline("Raw data...")
# result is {"sentiment": "...", "topics": "..."}
```

## API Summary

| Function | Returns | Description |
|----------|---------|-------------|
| `chain(*prompts)` | Callable | Sequential execution, output feeds next input |
| `parallel(**prompts)` | Callable | Concurrent execution, returns dict of results |
| `branch(condition, routes, default=None)` | Callable | Conditional routing based on a predicate |
| `retry(target, max_retries=3, backoff=1.0)` | Callable | Exponential backoff retry |
| `fallback(primary, *alternatives)` | Callable | Try alternatives on failure |

!!! tip "Retry + Fallback together"
    Combine `retry` and `fallback` for maximum resilience: `fallback(retry(primary, max_retries=2), backup_prompt)`. This retries the primary prompt, then falls back to a backup if all retries fail.

!!! warning "Context propagation"
    The `PromptContext.state` dict carries data between chain steps. Use it to pass metadata that does not fit into the prompt's return value.

## What's Next

- [Strategies](strategies.md) -- Control how the agent reasons
- [Guards](guards.md) -- Input/output validation
- [Testing](testing.md) -- Test your prompt pipelines
