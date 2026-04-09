"""Built-in pre/post processors for PromptNode pipelines.

Preprocessors run before the LLM call (modify state).
Postprocessors run after (transform output).

Usage::

    from promptise.engine.processors import (
        context_enricher, json_extractor,
        chain_preprocessors, chain_postprocessors,
    )

    node = PromptNode("analyze",
        preprocessor=context_enricher(),
        postprocessor=chain_postprocessors(json_extractor(), confidence_scorer()),
    )
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .state import GraphState


def _extract_first_json(text: str) -> dict | None:
    """Extract the first valid JSON object from text, handling nested braces."""
    depth = 0
    start = None
    for i, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = i
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start : i + 1])
                except (json.JSONDecodeError, ValueError):
                    start = None
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Preprocessors — signature: (state: GraphState, config: dict) -> None
# ═══════════════════════════════════════════════════════════════════════════════


def context_enricher(*, include_timestamp: bool = True, include_iteration: bool = True):
    """Add timestamp and iteration info to state.context."""

    def _enrich(state: GraphState, config: dict) -> None:
        if include_timestamp:
            state.context["_timestamp"] = datetime.now(timezone.utc).isoformat()
        if include_iteration:
            state.context["_iteration"] = state.iteration
            state.context["_tools_called"] = state.tool_calls_made

    return _enrich


def state_summarizer(*, max_context_chars: int = 2000):
    """Truncate long string values in state.context to save tokens."""

    def _summarize(state: GraphState, config: dict) -> None:
        for key, val in list(state.context.items()):
            if isinstance(val, str) and len(val) > max_context_chars:
                state.context[key] = val[:max_context_chars] + "... (truncated)"

    return _summarize


def input_validator(*, required_keys: list[str]):
    """Validate that required keys exist in state.context."""

    def _validate(state: GraphState, config: dict) -> None:
        missing = [k for k in required_keys if k not in state.context]
        if missing:
            raise ValueError(f"Missing required context keys: {missing}")

    return _validate


# ═══════════════════════════════════════════════════════════════════════════════
# Postprocessors — signature: (output: Any, state: GraphState, config: dict) -> Any
# ═══════════════════════════════════════════════════════════════════════════════


def json_extractor(*, keys: list[str] | None = None):
    """Parse JSON from LLM output string, optionally filter to specific keys."""

    def _extract(output: Any, state: GraphState, config: dict) -> Any:
        if isinstance(output, dict):
            if keys:
                return {k: output.get(k) for k in keys if k in output}
            return output

        if isinstance(output, str):
            # Extract first valid JSON object (handles nested braces)
            parsed = _extract_first_json(output)
            if parsed is not None:
                if keys:
                    return {k: parsed.get(k) for k in keys if k in parsed}
                return parsed
        return output

    return _extract


def confidence_scorer():
    """Add a confidence score based on hedging language in text output."""

    _HEDGING = [
        "might",
        "maybe",
        "perhaps",
        "possibly",
        "unclear",
        "uncertain",
        "not sure",
        "I think",
        "could be",
        "approximately",
        "roughly",
        "it seems",
    ]

    def _score(output: Any, state: GraphState, config: dict) -> Any:
        text = output if isinstance(output, str) else str(output)
        text_lower = text.lower()
        hedge_count = sum(1 for h in _HEDGING if h in text_lower)
        word_count = max(len(text.split()), 1)
        confidence = max(0.1, 1.0 - (hedge_count / word_count * 10))

        if isinstance(output, dict):
            output["_confidence"] = round(confidence, 2)
            return output
        return {"text": text, "_confidence": round(confidence, 2)}

    return _score


def state_writer(*, fields: dict[str, str]):
    """Write specific output fields to state.context keys.

    Args:
        fields: Mapping of output_key → state.context_key.
            E.g. ``{"answer": "final_answer", "confidence": "score"}``
    """

    def _write(output: Any, state: GraphState, config: dict) -> Any:
        if isinstance(output, dict):
            for out_key, ctx_key in fields.items():
                if out_key in output:
                    state.context[ctx_key] = output[out_key]
        return output

    return _write


def output_truncator(*, max_chars: int = 4000):
    """Truncate output text to a maximum length."""

    def _truncate(output: Any, state: GraphState, config: dict) -> Any:
        if isinstance(output, str) and len(output) > max_chars:
            return output[:max_chars] + "..."
        return output

    return _truncate


# ═══════════════════════════════════════════════════════════════════════════════
# Combinators — chain multiple processors
# ═══════════════════════════════════════════════════════════════════════════════


def chain_preprocessors(*fns):
    """Combine multiple preprocessors into one. Runs in order."""

    def _chained(state: GraphState, config: dict) -> None:
        for fn in fns:
            fn(state, config)

    return _chained


def chain_postprocessors(*fns):
    """Combine multiple postprocessors into one. Pipes output through each."""

    def _chained(output: Any, state: GraphState, config: dict) -> Any:
        for fn in fns:
            output = fn(output, state, config)
        return output

    return _chained
