"""Untrusted values are escaped before they reach a log line
(CodeQL py/log-injection)."""

from __future__ import annotations

import logging

import pytest
from aiohttp.test_utils import make_mocked_request

from promptise._logsafe import log_safe
from promptise.runtime.triggers.webhook import WebhookTrigger


class TestLogSafe:
    def test_line_breaks_and_control_characters_are_escaped(self) -> None:
        out = log_safe("1.2.3.4\r\nCRITICAL forged entry\x1b[31m ")
        assert "\n" not in out and "\r" not in out and "\x1b" not in out
        assert " " not in out
        assert out.startswith("1.2.3.4\\r\\nCRITICAL forged entry")

    def test_length_is_bounded(self) -> None:
        out = log_safe("a" * 10_000, max_len=50)
        assert out == "a" * 50 + "...[truncated]"

    def test_plain_values_pass_through(self) -> None:
        assert log_safe("203.0.113.9") == "203.0.113.9"
        assert log_safe(None) == "None"


class TestWebhookLogging:
    @pytest.mark.parametrize("secret", [None, "s3cret"])
    async def test_forged_remote_cannot_break_the_log_line(
        self, secret: str | None, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Behind a forwarded-header middleware request.remote is whatever the
        # client sent; a CR/LF in it forged a second log entry.
        trigger = WebhookTrigger(
            allowed_sources=["10.0.0.0/8"] if secret is None else None,
            hmac_secret=secret,
        )
        forged = "10.0.0.1\nCRITICAL promptise: forged entry"
        request = make_mocked_request("POST", "/webhook").clone(remote=forged)
        with caplog.at_level(logging.WARNING, logger="promptise.runtime.triggers.webhook"):
            resp = await trigger._handle_request(request)
        assert resp.status in (401, 403)
        [record] = [r for r in caplog.records if "rejected request" in r.getMessage()]
        assert "\n" not in record.getMessage()
        assert "forged entry" in record.getMessage()
