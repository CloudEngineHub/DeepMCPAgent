# Processors

Processors are functions that run before (preprocessors) or after (postprocessors) a PromptNode's LLM call. They transform state, validate inputs, parse outputs, and enrich results.

## Usage

```python
from promptise.engine import PromptNode
from promptise.engine.processors import (
    context_enricher, json_extractor, confidence_scorer,
    chain_preprocessors, chain_postprocessors,
)

node = PromptNode("analyze",
    preprocessor=context_enricher(),
    postprocessor=chain_postprocessors(json_extractor(), confidence_scorer()),
)
```

## Preprocessors

Preprocessors run before the LLM call and modify `state` in place.

**Signature:** `(state: GraphState, config: dict) -> None`

### context_enricher

Adds timestamp and iteration info to `state.context`.

```python
context_enricher(include_timestamp=True, include_iteration=True)
```

Sets `_timestamp` (UTC ISO), `_iteration`, and `_tools_called` in context.

### state_summarizer

Truncates long string values in `state.context` to save tokens.

```python
state_summarizer(max_context_chars=2000)
```

Any string value exceeding `max_context_chars` is truncated with `... (truncated)`.

### input_validator

Validates that required keys exist in `state.context`. Raises `ValueError` if missing.

```python
input_validator(required_keys=["query", "data_source"])
```

## Postprocessors

Postprocessors run after the LLM call and transform the output.

**Signature:** `(output: Any, state: GraphState, config: dict) -> Any`

### json_extractor

Parses JSON from LLM output strings. Handles nested braces correctly (e.g. `{"a": {"b": 1}}`). If the output is already a dict, filters to specified keys.

```python
json_extractor()                        # Extract first JSON object from text
json_extractor(keys=["answer", "confidence"])  # Only keep these keys
```

### confidence_scorer

Adds a `_confidence` score (0.1-1.0) based on hedging language in the output text.

```python
confidence_scorer()
# Detects: "might", "maybe", "perhaps", "possibly", "unclear", "I think", etc.
# Returns: {"text": ..., "_confidence": 0.85}
```

### state_writer

Writes specific output fields to `state.context` keys. Only writes keys that exist in the output dict.

```python
state_writer(fields={"answer": "final_answer", "confidence": "score"})
# output["answer"] → state.context["final_answer"]
# output["confidence"] → state.context["score"]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `fields` | `dict[str, str]` | *required* | Mapping of `output_key → state.context_key` |

### output_truncator

Truncates output text to a maximum character length. Appends `"..."` when truncated.

```python
output_truncator(max_chars=4000)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_chars` | `int` | `4000` | Maximum output length in characters |

## Combinators

### chain_preprocessors

Combines multiple preprocessors into one. Runs sequentially in order.

```python
preprocessor = chain_preprocessors(
    context_enricher(),
    input_validator(required_keys=["query"]),
    state_summarizer(max_context_chars=1000),
)
```

### chain_postprocessors

Combines multiple postprocessors into one. Pipes output through each function sequentially.

```python
postprocessor = chain_postprocessors(
    json_extractor(keys=["answer", "confidence"]),
    confidence_scorer(),
    state_writer(fields={"answer": "final_answer"}),
)
```

## Custom Processors

Write your own processors using the same signatures:

```python
# Custom preprocessor
def inject_user_profile(state: GraphState, config: dict) -> None:
    user_id = state.context.get("user_id")
    if user_id:
        state.context["user_profile"] = db.get_user(user_id)

# Custom postprocessor
def extract_citations(output: Any, state: GraphState, config: dict) -> Any:
    if isinstance(output, str):
        citations = re.findall(r'\[(\d+)\]', output)
        return {"text": output, "citations": citations}
    return output

# Use them
PromptNode("personalized",
    preprocessor=chain_preprocessors(inject_user_profile, context_enricher()),
    postprocessor=extract_citations,
)
```
