"""Tests for environment variable resolution."""

from __future__ import annotations

import pytest

from promptise.env_resolver import (
    resolve_env_in_dict,
    resolve_env_var,
    validate_all_env_vars_available,
)
from promptise.exceptions import EnvVarNotFoundError


def test_resolve_simple_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test basic environment variable resolution."""
    monkeypatch.setenv("TEST_VAR", "resolved_value")
    result = resolve_env_var("prefix ${TEST_VAR} suffix")
    assert result == "prefix resolved_value suffix"


def test_resolve_multiple_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test multiple environment variables in one string."""
    monkeypatch.setenv("VAR1", "value1")
    monkeypatch.setenv("VAR2", "value2")
    result = resolve_env_var("${VAR1} and ${VAR2}")
    assert result == "value1 and value2"


def test_resolve_with_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test default value syntax."""
    result = resolve_env_var("${NONEXISTENT:-default_value}")
    assert result == "default_value"


def test_resolve_with_default_containing_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test default value with spaces."""
    result = resolve_env_var("${NONEXISTENT:-default with spaces}")
    assert result == "default with spaces"


def test_resolve_env_var_takes_precedence_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that environment variable value takes precedence over default."""
    monkeypatch.setenv("TEST_VAR", "env_value")
    result = resolve_env_var("${TEST_VAR:-default_value}")
    assert result == "env_value"


def test_missing_required_var() -> None:
    """Test error on missing required variable."""
    with pytest.raises(EnvVarNotFoundError) as exc_info:
        resolve_env_var("${MISSING_VAR}")
    assert exc_info.value.var_name == "MISSING_VAR"
    assert exc_info.value.context is None


def test_missing_required_var_with_context() -> None:
    """Test error message includes context when provided."""
    with pytest.raises(EnvVarNotFoundError) as exc_info:
        resolve_env_var("${MISSING_VAR}", context="test.field")
    assert exc_info.value.var_name == "MISSING_VAR"
    assert exc_info.value.context == "test.field"
    assert "test.field" in str(exc_info.value)


def test_allow_missing_flag() -> None:
    """Test allow_missing flag leaves unresolved vars as-is."""
    result = resolve_env_var("${MISSING_VAR}", allow_missing=True)
    assert result == "${MISSING_VAR}"


def test_no_env_vars_in_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test string without environment variables is returned unchanged."""
    result = resolve_env_var("plain string without vars")
    assert result == "plain string without vars"


def test_empty_string() -> None:
    """Test empty string is handled correctly."""
    result = resolve_env_var("")
    assert result == ""


def test_resolve_in_dict_simple(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test basic dict resolution."""
    monkeypatch.setenv("API_KEY", "secret123")
    data = {"auth": "${API_KEY}"}
    result = resolve_env_in_dict(data)
    assert result == {"auth": "secret123"}


def test_resolve_in_dict_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test recursive resolution in nested dicts."""
    monkeypatch.setenv("API_KEY", "secret123")
    monkeypatch.setenv("URL", "http://api.example.com")
    data = {
        "server": {
            "url": "${URL}",
            "headers": {
                "Authorization": "Bearer ${API_KEY}",
            },
        },
    }
    result = resolve_env_in_dict(data)
    assert result["server"]["url"] == "http://api.example.com"
    assert result["server"]["headers"]["Authorization"] == "Bearer secret123"


def test_resolve_in_dict_with_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test resolution in list values."""
    monkeypatch.setenv("ARG", "value")
    data = {"args": ["--key", "${ARG}", "--flag"]}
    result = resolve_env_in_dict(data)
    assert result["args"] == ["--key", "value", "--flag"]


def test_resolve_in_dict_preserves_non_string_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that non-string values are preserved."""
    data = {
        "string": "test",
        "int": 42,
        "float": 3.14,
        "bool": True,
        "null": None,
        "list": [1, 2, 3],
    }
    result = resolve_env_in_dict(data)
    assert result == data


def test_resolve_in_dict_with_context_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test context prefix is used in error messages."""
    data = {"nested": {"field": "${MISSING}"}}
    with pytest.raises(EnvVarNotFoundError) as exc_info:
        resolve_env_in_dict(data, context_prefix="root")
    assert "root.nested.field" in str(exc_info.value)


def test_resolve_in_dict_list_with_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test context includes list index in error messages."""
    data = {"args": ["${MISSING}"]}
    with pytest.raises(EnvVarNotFoundError) as exc_info:
        resolve_env_in_dict(data)
    assert "args[0]" in str(exc_info.value)


def test_validate_all_env_vars_available_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test validation when all env vars are available."""
    monkeypatch.setenv("VAR1", "value1")
    monkeypatch.setenv("VAR2", "value2")
    data = {
        "field1": "${VAR1}",
        "field2": "${VAR2}",
    }
    missing = validate_all_env_vars_available(data)
    assert missing == []


def test_validate_all_env_vars_available_some_missing() -> None:
    """Test validation identifies missing vars."""
    data = {
        "field1": "${MISSING1}",
        "field2": "${MISSING2}",
    }
    missing = validate_all_env_vars_available(data)
    assert set(missing) == {"MISSING1", "MISSING2"}


def test_validate_all_env_vars_available_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that vars with defaults are not reported as missing."""
    data = {
        "field1": "${MISSING:-default}",
        "field2": "${REQUIRED}",
    }
    missing = validate_all_env_vars_available(data)
    assert missing == ["REQUIRED"]


def test_validate_all_env_vars_available_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test validation in nested structures."""
    data = {
        "server": {
            "url": "${API_URL}",
            "headers": {
                "Authorization": "Bearer ${API_TOKEN}",
            },
        },
        "args": ["${ARG1}", "${ARG2}"],
    }
    missing = validate_all_env_vars_available(data)
    assert set(missing) == {"API_URL", "API_TOKEN", "ARG1", "ARG2"}


def test_validate_all_env_vars_available_returns_sorted() -> None:
    """Test that missing vars are returned in sorted order."""
    data = {
        "c": "${VAR_C}",
        "a": "${VAR_A}",
        "b": "${VAR_B}",
    }
    missing = validate_all_env_vars_available(data)
    assert missing == ["VAR_A", "VAR_B", "VAR_C"]


def test_validate_all_env_vars_available_no_vars() -> None:
    """Test validation with no env var references."""
    data = {
        "field1": "plain value",
        "field2": 42,
        "field3": {"nested": "value"},
    }
    missing = validate_all_env_vars_available(data)
    assert missing == []


def test_env_var_pattern_valid_var_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that valid variable names are matched correctly."""
    monkeypatch.setenv("VAR_NAME_123", "value")
    monkeypatch.setenv("_UNDERSCORE", "value")
    monkeypatch.setenv("ALLCAPS", "value")

    assert resolve_env_var("${VAR_NAME_123}") == "value"
    assert resolve_env_var("${_UNDERSCORE}") == "value"
    assert resolve_env_var("${ALLCAPS}") == "value"


def test_env_var_pattern_does_not_match_invalid_names() -> None:
    """Test that invalid variable names are not matched."""
    # These should not be matched by the pattern, so they're left as-is
    result = resolve_env_var("${123INVALID}", allow_missing=True)
    assert result == "${123INVALID}"  # Not matched, left unchanged


def test_resolve_env_var_empty_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that empty default value is handled correctly."""
    result = resolve_env_var("${MISSING:-}")
    assert result == ""


def test_resolve_env_in_dict_empty_dict() -> None:
    """Test empty dict is handled correctly."""
    result = resolve_env_in_dict({})
    assert result == {}


def test_exception_str_formatting() -> None:
    """Test EnvVarNotFoundError string formatting."""
    err = EnvVarNotFoundError("MY_VAR", context="some.field")
    err_str = str(err)
    assert "MY_VAR" in err_str
    assert "some.field" in err_str

    err_no_context = EnvVarNotFoundError("MY_VAR")
    err_no_context_str = str(err_no_context)
    assert "MY_VAR" in err_no_context_str
