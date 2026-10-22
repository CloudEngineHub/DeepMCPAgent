"""Log, warning and exception messages use plain punctuation (no em dashes)."""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

import pytest

import promptise
from promptise.runtime.budget import BudgetEnforcer, BudgetViolation
from promptise.runtime.config import BudgetConfig
from promptise.runtime.triggers.webhook import WebhookTrigger

EM_DASH = "—"
PACKAGE = Path(promptise.__file__).parent
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _is_message_call(call: ast.Call) -> bool:
    """``logger.<level>(...)``, ``logging.getLogger(...).<level>(...)`` or ``warnings.warn(...)``."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    target = func.value
    if func.attr == "warn" and isinstance(target, ast.Name) and target.id == "warnings":
        return True
    if func.attr not in _LOG_METHODS:
        return False
    if isinstance(target, ast.Name):
        return "log" in target.id.lower()
    if isinstance(target, ast.Attribute):
        return "log" in target.attr.lower()
    if isinstance(target, ast.Call) and isinstance(target.func, ast.Attribute):
        return target.func.attr == "getLogger"
    return False


def _em_dash_messages(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    if EM_DASH not in source:
        return []
    found = []
    for node in ast.walk(ast.parse(source)):
        call: Any = None
        if isinstance(node, ast.Call) and _is_message_call(node):
            call = node
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            call = node.exc
        if call is None:
            continue
        for sub in ast.walk(call):
            if (
                isinstance(sub, ast.Constant)
                and isinstance(sub.value, str)
                and EM_DASH in sub.value
            ):
                found.append(f"{path.relative_to(PACKAGE)}:{sub.lineno}")
    return found


@pytest.mark.parametrize(
    "subpackage", ["", "runtime", "engine", "identity", "mcpcast", "prompts", "run", "mcp"]
)
def test_no_em_dash_in_log_warning_or_exception_messages(subpackage: str) -> None:
    root = PACKAGE / subpackage
    files = root.glob("*.py") if not subpackage else root.rglob("*.py")
    offenders = [hit for path in sorted(files) for hit in _em_dash_messages(path)]
    assert offenders == []


def test_webhook_without_secret_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="promptise.runtime.triggers.webhook"):
        WebhookTrigger(port=9191)
    [message] = [r.getMessage() for r in caplog.records if "HMAC" in r.getMessage()]
    assert message == (
        "WebhookTrigger on port 9191 has no HMAC secret: any HTTP client can trigger "
        "this webhook. Set hmac_secret for production use."
    )


async def test_budget_violation_warning(caplog: pytest.LogCaptureFixture) -> None:
    class _Process:
        async def suspend(self) -> None:
            return None

    enforcer = BudgetEnforcer(BudgetConfig(enabled=True, on_exceeded="pause"))
    violation = BudgetViolation("max_irreversible_per_run", 3, 4)
    with caplog.at_level(logging.WARNING, logger="promptise.runtime.budget"):
        await enforcer.handle_violation(violation, _Process())
    [message] = [r.getMessage() for r in caplog.records if "Budget violation" in r.getMessage()]
    assert message == (
        "Budget violation: max_irreversible_per_run (limit=3, current=4), action=pause"
    )
