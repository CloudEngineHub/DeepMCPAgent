---
seo_title: "Node Preprocessors and Postprocessors"
description: "Functions that run before or after a PromptNode's LLM call: context enrichment, input checks, JSON extraction, confidence scoring, truncation and chaining."
---

# Node Processors

Processors are functions that run before (preprocessors) or after (postprocessors) a PromptNode's LLM call. They transform state, parse outputs, and enrich results. The built-in ones are factories in `promptise.engine.processors`: call them to get the processor.

## Usage

```python
from promptise.engine import PromptNode
from promptise.engine.processors import (
    chain_postprocessors, context_enricher, state_writer,
)
from pydantic import BaseModel


class Answer(BaseModel):
    answer: str
    confidence: float


node = PromptNode("analyze",
    output_schema=Answer,
    preprocessor=context_enricher(),
    postprocessor=chain_postprocessors(state_writer(fields={"answer": "final_answer"})),
)
```

!!! info "What a postprocessor receives"
    A postprocessor gets `NodeResult.output`: a dict for a node with an
    `output_schema`, the reply text for a plain-text reply (a reasoning
    node's JSON fields when its reply ends with them), or the extracted answer
    for a node whose `strategy` parsed one (for example `chain_of_thought`
    when the reply has its answer marker). It runs on the model's reply only,
    not on a step that only calls tools. So `json_extractor()` parses the JSON
    out of a plain `PromptNode`'s reply. A postprocessor's non-`None` return
    value becomes the node's `output` (and is written to `output_key`). Before
    1.7.0 a plain-text reply reached the postprocessor as `None`.

!!! warning "A preprocessor that raises stops the node"
    An exception from a preprocessor fails the node before the model is
    called (fail closed): `NodeResult.error` is `"Preprocessor failed in node
    'x': ValueError: ..."`, an [`on_error` edge](engine-edges.md#on_error-error-routing)
    or hook can route it, and otherwise the run raises `GraphExecutionError`
    with the exception as its cause. So `input_validator()` is a gate. An
    exception from a postprocessor is logged on `promptise.engine` and the
    output kept. Before 1.7.0 preprocessor exceptions were logged and the
    node called the model anyway.

## Preprocessors

Preprocessors run before the LLM call and modify `state` in place. A preprocessor can be sync or `async`; the node awaits a coroutine.

**Signature:** `(state: GraphState, config: dict) -> None`

### context_enricher

Adds timestamp and iteration info to `state.context`.

```python
context_enricher(include_timestamp=True, include_iteration=True)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `include_timestamp` | `bool` | `True` | Set `state.context["_timestamp"]` (UTC, ISO 8601) |
| `include_iteration` | `bool` | `True` | Set `state.context["_iteration"]` (`state.iteration`) and `["_tools_called"]` (`state.tool_calls_made`) |

The values are in `state.context`; list them in the node's `input_keys` for the model to see them.

### state_summarizer

Truncates long string values in `state.context` to save tokens.

```python
state_summarizer(max_context_chars=2000)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_context_chars` | `int` | `2000` | Every string value in `state.context` longer than this is cut to this length plus `"... (truncated)"` |

It changes the shared `state.context` for every later node, not only this one's prompt.

### input_validator

Checks that required keys exist in `state.context` and raises `ValueError` naming the missing ones. Inside a node that fails the node before the model is called (see the warning above).

```python
input_validator(required_keys=["query", "data_source"])
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `required_keys` | `list[str]` | *required* | Keys that must be in `state.context` |

## Postprocessors

Postprocessors run after the LLM call (and after guards and strategy parsing) and transform the output. Sync or `async`.

**Signature:** `(output: Any, state: GraphState, config: dict) -> Any`

### json_extractor

Parses the first JSON object out of a string output (nested braces work, e.g. `{"a": {"b": 1}}`). A dict output is passed through, filtered to `keys` when given. Anything else (including `None`, or a string with no JSON object) is returned unchanged.

```python
json_extractor()                        # Extract first JSON object from text
json_extractor(keys=["answer", "confidence"])  # Only keep these keys
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `keys` | `list[str] \| None` | `None` | Keep only these keys (missing ones are left out) |

```python
from promptise.engine import GraphState
from promptise.engine.processors import json_extractor

extract = json_extractor(keys=["answer"])
print(extract('Result: {"answer": 42, "notes": "x"}', GraphState(), {}))   # {'answer': 42}
```

### confidence_scorer

Adds a `_confidence` score from hedging words in the output text: `max(0.1, 1 - 10 * hedges / words)`, rounded to two decimals, where `hedges` counts which of 12 phrases ("might", "maybe", "perhaps", "possibly", "unclear", "uncertain", "not sure", "I think", "could be", "approximately", "roughly", "it seems") appear. A heuristic, not a calibrated probability. A dict output gets a `_confidence` key; any other output becomes `{"text": str(output), "_confidence": ...}`.

```python
from promptise.engine import GraphState
from promptise.engine.processors import confidence_scorer

score = confidence_scorer()
print(score("The total is 42.", GraphState(), {}))   # {'text': 'The total is 42.', '_confidence': 1.0}
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

Combines multiple preprocessors into one. Runs them in order; sync and `async` preprocessors mix. The chain stays synchronous until one returns an awaitable, then it returns a coroutine that awaits it and runs the rest (the node awaits it). An exception stops the chain and fails the node. Before 1.7.0 an `async` preprocessor in a chain never ran.

```python
preprocessor = chain_preprocessors(
    context_enricher(),
    input_validator(required_keys=["query"]),
    state_summarizer(max_context_chars=1000),
)
```

### chain_postprocessors

Combines multiple postprocessors into one. Pipes the output through each function in order (each gets the previous one's return value, `None` included). Sync and `async` postprocessors mix, as in `chain_preprocessors`: each step's awaitable is awaited before the next step gets its value.

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
import re
from typing import Any

from promptise.engine import GraphState, PromptNode
from promptise.engine.processors import chain_preprocessors, context_enricher


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
