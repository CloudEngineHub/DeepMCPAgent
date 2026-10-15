"""Redaction keeps JSON (and other quoted structure) intact.

Regression (v1.3.3): the connection-string patterns ended in ``[^\\s]{10,}``
and the password/secret patterns in ``\\S{4,}`` / ``\\S{8,}``, so on JSON text
a match ran through the closing quote, brace and comma up to the next
whitespace. Redacting a JSON tool result produced invalid JSON and dropped
the punctuation of neighbouring keys::

    '{"ok": true, "config": {"warehouse_db": "[POSTGRES_CONNECTION] "region": "eu"}'

Now a redaction replaces text inside a string value only, never the quotes,
escapes or punctuation around it, so a redacted JSON document parses to the
same structure with the secrets replaced.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from promptise.guardrails import (
    Action,
    CredentialDetector,
    CustomRule,
    PIIDetector,
    PromptiseSecurityScanner,
    SecurityFinding,
    Severity,
)

PG = "postgres://app:S3cr3t-pw@db.internal:5432/orders"
MYSQL = "mysql://root:Hunter2-pw@mysql.internal:3306/shop"
MONGO = "mongodb+srv://svc:M0ngo-pw@cluster0.example.net/app"
REDIS = "redis://:R3dis-pw@cache.internal:6379/0"


def _scanner(**kwargs: Any) -> PromptiseSecurityScanner:
    return PromptiseSecurityScanner(detectors=[CredentialDetector(), PIIDetector()], **kwargs)


def _leaves(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _leaves(v)]
    if isinstance(value, list):
        return [s for v in value for s in _leaves(v)]
    return [value] if isinstance(value, str) else []


def _no_secret(text: str) -> None:
    for secret in ("S3cr3t-pw", "Hunter2-pw", "M0ngo-pw", "R3dis-pw", "hunter22", "topsecret-key"):
        assert secret not in text, text


@pytest.mark.asyncio
async def test_bug_report_example_stays_valid_json() -> None:
    text = json.dumps({"ok": True, "config": {"warehouse_db": PG}, "region": "eu"})
    out = await _scanner().check_output(text)
    doc = json.loads(out)
    assert doc == {"ok": True, "config": {"warehouse_db": "[POSTGRES_CONNECTION]"}, "region": "eu"}


@pytest.mark.asyncio
async def test_every_connection_string_in_a_nested_document() -> None:
    original = {
        "services": [
            {"name": "orders", "dsn": PG, "replicas": 2},
            {"name": "shop", "dsn": MYSQL},
            {"name": "events", "dsn": {"primary": MONGO, "cache": [REDIS, "local"]}},
        ],
        "region": "eu-west-1",
    }
    for indent in (None, 2):
        text = json.dumps(original, indent=indent)
        out = await _scanner().check_tool_result("get_config", text)
        _no_secret(out)
        doc = json.loads(out)
        assert doc["region"] == "eu-west-1"
        assert doc["services"][0] == {
            "name": "orders",
            "dsn": "[POSTGRES_CONNECTION]",
            "replicas": 2,
        }
        assert doc["services"][1]["dsn"] == "[MYSQL_CONNECTION]"
        assert doc["services"][2]["dsn"]["primary"] == "[MONGODB_CONNECTION]"
        assert doc["services"][2]["dsn"]["cache"] == ["[REDIS_CONNECTION]", "local"]
        # Formatting is kept: only the secrets changed
        assert out == text.replace(PG, "[POSTGRES_CONNECTION]").replace(
            MYSQL, "[MYSQL_CONNECTION]"
        ).replace(MONGO, "[MONGODB_CONNECTION]").replace(REDIS, "[REDIS_CONNECTION]")


@pytest.mark.asyncio
async def test_json_encoded_inside_a_json_string() -> None:
    inner = json.dumps({"db": PG, "region": "eu", "auth": "password=hunter22"})
    text = json.dumps({"payload": inner, "status": "ok"})
    out = await _scanner().check_tool_result("fetch", text)
    _no_secret(out)
    outer = json.loads(out)
    assert outer["status"] == "ok"
    doc = json.loads(outer["payload"])
    assert doc["db"] == "[POSTGRES_CONNECTION]"
    assert doc["region"] == "eu"
    assert doc["auth"] == "[PASSWORD]"


@pytest.mark.asyncio
async def test_escape_sequences_bound_a_redaction() -> None:
    text = json.dumps(
        {"note": "password=hunter22\nnext line", "path": 'C:\\data "quoted" ' + PG, "x": "y"}
    )
    out = await _scanner().check_tool_result("notes", text)
    _no_secret(out)
    doc = json.loads(out)
    assert doc["note"] == "[PASSWORD]\nnext line"
    assert doc["path"] == 'C:\\data "quoted" [POSTGRES_CONNECTION]'
    assert doc["x"] == "y"


@pytest.mark.asyncio
async def test_password_and_secret_fields_inside_values() -> None:
    text = json.dumps(
        {"url": "https://h/?password=hunter22", "env": "API_SECRET=topsecret-key", "n": 1}
    )
    out = await _scanner().check_tool_result("env", text)
    _no_secret(out)
    doc = json.loads(out)
    assert doc["n"] == 1 and set(doc) == {"url", "env", "n"}


@pytest.mark.asyncio
async def test_python_repr_and_prose_keep_their_quotes() -> None:
    out = await _scanner().check_output(str({"db": PG, "region": "eu"}))
    assert out == "{'db': '[POSTGRES_CONNECTION]', 'region': 'eu'}"
    out = await _scanner().check_output(f"db is {PG} and region eu")
    assert out == "db is [POSTGRES_CONNECTION] and region eu"
    out = await _scanner().check_output(f'set DSN="{PG}" then restart')
    assert out == 'set DSN="[POSTGRES_CONNECTION]" then restart'


@pytest.mark.asyncio
async def test_custom_rule_spanning_keys_keeps_structure() -> None:
    scanner = PromptiseSecurityScanner(
        detectors=[],
        custom_rules=[CustomRule(name="api_key", pattern=r'"api_key":\s*"[^"]+"')],
    )
    text = json.dumps({"api_key": "abc123", "region": "eu"})
    out = await scanner.check_tool_result("t", text)
    doc = json.loads(out)
    assert "abc123" not in out
    assert doc["region"] == "eu"
    assert len(doc) == 2


def _finding(text: str, needle: str, length: int | None = None) -> SecurityFinding:
    start = text.index(needle)
    return SecurityFinding(
        detector="custom",
        category="thing",
        severity=Severity.HIGH,
        confidence=1.0,
        matched_text=needle,
        start=start,
        end=start + (length if length is not None else len(needle)),
        action=Action.REDACT,
        description="",
    )


def test_unicode_escape_is_never_split() -> None:
    text = '{"name": "Jos\\u00e9 1234", "k": 1}'
    # A span that starts in the middle of the \\u00e9 escape
    out = PromptiseSecurityScanner._apply_redactions(text, [_finding(text, "00e9 12")])
    doc = json.loads(out)
    assert doc == {"name": "Jos[THING]34", "k": 1}


def test_a_number_value_becomes_a_string() -> None:
    text = '{"phone": 4155552671, "k": [1, 2]}'
    out = PromptiseSecurityScanner._apply_redactions(text, [_finding(text, "4155552671")])
    assert json.loads(out) == {"phone": "[THING]", "k": [1, 2]}


def test_a_span_running_past_its_string_is_clipped() -> None:
    text = '{"a": "secret-value", "b": "keep"}'
    out = PromptiseSecurityScanner._apply_redactions(
        text, [_finding(text, "secret-value", length=len('secret-value", "b": "ke'))]
    )
    assert json.loads(out) == {"a": "[THING]", "b": "keep"}


def test_non_json_text_is_unchanged_behaviour() -> None:
    text = 'not json: "secret-value", tail'
    out = PromptiseSecurityScanner._apply_redactions(text, [_finding(text, "secret-value")])
    assert out == 'not json: "[THING]", tail'
