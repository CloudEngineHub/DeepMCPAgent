# Guards

Runtime enforcers that validate and transform input before the LLM call and output after it. Guards protect your prompts from bad data going in and bad data coming out.

## Quick Example

```python
from promptise.prompts import prompt
from promptise.prompts.guards import guard, content_filter, length

@prompt(model="openai:gpt-5-mini")
@guard(
    content_filter(blocked=["secret", "password"]),
    length(max_length=2000),
)
async def analyze(text: str) -> str:
    """Analyze: {text}"""

result = await analyze("Quarterly revenue grew 15%...")
```

If the input contains a blocked word, the guard raises `GuardError` before the LLM is ever called. If the output exceeds 2000 characters, the guard raises after the call returns.

## Concepts

Guards implement two async methods:

- **`check_input(input_text)`** -- runs before the LLM call. Returns the (possibly transformed) text or raises `GuardError` to reject.
- **`check_output(output)`** -- runs after the LLM call. Returns the (possibly transformed) output or raises `GuardError` to reject.

Guards are attached to prompts via the `@guard()` decorator. Multiple guards execute in order -- each guard's output feeds the next.

## Built-In Guards

### ContentFilterGuard

Block or require specific words (case-insensitive) in both input and output.

```python
from promptise.prompts.guards import content_filter

# Block dangerous words in input AND output
safe = content_filter(blocked=["secret", "password", "ssn"])

# Require specific words in output
quality = content_filter(required=["recommendation", "conclusion"])

# Combine both
strict = content_filter(
    blocked=["confidential"],
    required=["summary"],
)
```

### LengthGuard

Enforce character length bounds on output.

```python
from promptise.prompts.guards import length

# Maximum length only
concise = length(max_length=500)

# Minimum length only
detailed = length(min_length=200)

# Both bounds
bounded = length(min_length=100, max_length=2000)
```

### SchemaStrictGuard

Validates that LLM output is well-formed JSON. Raises `GuardError` if the output cannot be parsed as JSON. Non-string outputs are passed through unchanged.

```python
from promptise.prompts.guards import schema_strict

strict = schema_strict()
```

### InputValidatorGuard

Wrap any callable (sync or async) as an input-only validator. The callable receives input text and must return text or raise an exception.

```python
from promptise.prompts.guards import input_validator, GuardError

def require_revenue_data(text: str) -> str:
    if "revenue" not in text.lower():
        raise GuardError("Input must mention revenue", guard_name="require_revenue")
    return text

revenue_guard = input_validator(require_revenue_data)
```

Async validators work the same way:

```python
async def check_against_blocklist(text: str) -> str:
    # Could call an external API here
    return text

async_guard = input_validator(check_against_blocklist)
```

### OutputValidatorGuard

Wrap any callable (sync or async) as an output-only validator. The callable receives the LLM output and returns the (possibly transformed) value.

```python
from promptise.prompts.guards import output_validator

def clean_whitespace(output: str) -> str:
    return output.strip().replace("  ", " ")

clean_guard = output_validator(clean_whitespace)
```

## Using the `@guard` Decorator

Attach one or more guards to a prompt with the `@guard()` decorator. Stack it below `@prompt`:

```python
from promptise.prompts import prompt
from promptise.prompts.guards import guard, content_filter, length, schema_strict

@prompt(model="openai:gpt-5-mini")
@guard(
    content_filter(blocked=["secret"]),
    length(max_length=2000),
    schema_strict(max_retries=2),
)
async def summarize(text: str) -> str:
    """Summarize: {text}"""
```

The `@guard` decorator can be applied before or after `@prompt` -- it detects whether it is decorating a `Prompt` object or a plain function and acts accordingly.

## Custom Guards

Implement the `Guard` protocol for full control over both input and output:

```python
import re
from typing import Any
from promptise.prompts.guards import GuardError

class PiiRedactionGuard:
    """Redact SSNs from input before sending to the LLM."""

    async def check_input(self, input_text: str) -> str:
        return re.sub(r"\d{3}-\d{2}-\d{4}", "[REDACTED]", input_text)

    async def check_output(self, output: Any) -> Any:
        if isinstance(output, str):
            return re.sub(r"\d{3}-\d{2}-\d{4}", "[REDACTED]", output)
        return output
```

Attach your custom guard the same way:

```python
@prompt(model="openai:gpt-5-mini")
@guard(PiiRedactionGuard(), content_filter(blocked=["password"]))
async def process(text: str) -> str:
    """Process: {text}"""
```

## Handling GuardError

When a guard rejects input or output, it raises `GuardError` with a descriptive message and the guard's name:

```python
from promptise.prompts.guards import GuardError

try:
    result = await analyze("Contains secret data")
except GuardError as e:
    print(f"Guard '{e.guard_name}' rejected: {e.reason}")
    # Guard 'content_filter' rejected: Input contains blocked word: 'secret'
```

## API Summary

| Factory / Class | Description |
|-----------------|-------------|
| `content_filter(blocked=[], required=[])` | Block or require specific words (case-insensitive) |
| `length(min_length=None, max_length=None)` | Enforce output character length bounds |
| `schema_strict(max_retries=3)` | Retry on schema validation failure |
| `input_validator(fn)` | Wrap any callable as an input-only validator |
| `output_validator(fn)` | Wrap any callable as an output-only validator |
| `guard(*guards)` | Decorator that attaches guards to a `Prompt` |
| `GuardError` | Exception raised when a guard rejects |
| `Guard` | Protocol with `check_input()` and `check_output()` methods |

!!! tip "Guards are transformers, not just validators"
    Guards can transform data, not just reject it. Use `InputValidatorGuard` to sanitize input (redact PII, normalize whitespace) and `OutputValidatorGuard` to clean up output (strip extra whitespace, fix formatting) before it reaches your application.

!!! warning "Guard ordering matters"
    Guards execute in the order they are passed to `@guard()`. Place content filters before length checks -- a blocked-word rejection is cheaper than measuring the length of content you are about to reject anyway.

## What's Next

- [Strategies](strategies.md) -- Control how the agent reasons
- [Context System](context.md) -- Inject dynamic information into prompts
- [PromptBuilder](builder.md) -- Fluent API for runtime prompt construction
