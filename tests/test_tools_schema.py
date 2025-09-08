from __future__ import annotations

import pytest

pytest.importorskip("fastmcp")

from deepmcpagent.tools import _jsonschema_to_pydantic  # type: ignore


def test_jsonschema_to_pydantic_basic_types() -> None:
    schema = {
        "type": "object",
        "properties": {
            "s": {"type": "string", "description": "a"},
            "i": {"type": "integer"},
            "n": {"type": "number"},
            "b": {"type": "boolean"},
        },
        "required": ["s", "i"],
    }
    model = _jsonschema_to_pydantic(schema, model_name="Args_test")
    # required fields have Ellipsis default (pydantic Required)
    fields = model.model_fields
    assert fields["s"].is_required()
    assert fields["i"].is_required()
    assert fields["n"].is_required() is False
    assert fields["b"].is_required() is False


def test_jsonschema_to_pydantic_empty_schema() -> None:
    model = _jsonschema_to_pydantic({}, model_name="Args_empty")
    # Fallback field exists
    assert "payload" in model.model_fields
