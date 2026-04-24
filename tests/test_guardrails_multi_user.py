"""Multi-user attribution tests for :mod:`promptise.guardrails`.

:class:`ScanReport` now captures the caller's ``user_id`` / ``session_id``
/ roles at scan time so audit logs can attribute findings to a tenant
even after the CallerContext contextvar has been reset.
"""

from __future__ import annotations

import asyncio

import pytest

from promptise.agent import CallerContext, _caller_ctx_var
from promptise.guardrails import PromptiseSecurityScanner


def _make_scanner() -> PromptiseSecurityScanner:
    """A deterministic scanner: regex-only, no ML models.

    Using ``detectors=[]`` short-circuits every scanning head so the
    tests focus purely on caller-attribution — which is populated by
    ``scan_text`` regardless of which detectors ran.
    """
    return PromptiseSecurityScanner(detectors=[])


class TestScanReportCallerAttribution:
    @pytest.mark.asyncio
    async def test_report_carries_user_id_from_caller(self) -> None:
        scanner = _make_scanner()
        token = _caller_ctx_var.set(CallerContext(user_id="alice"))
        try:
            report = await scanner.scan_text("Card: 4532015112830366")
        finally:
            _caller_ctx_var.reset(token)

        assert report.user_id == "alice"

    @pytest.mark.asyncio
    async def test_report_session_id_from_caller_metadata(self) -> None:
        scanner = _make_scanner()
        token = _caller_ctx_var.set(
            CallerContext(user_id="alice", metadata={"session_id": "sess-9"})
        )
        try:
            report = await scanner.scan_text("ssn 123-45-6789")
        finally:
            _caller_ctx_var.reset(token)

        assert report.user_id == "alice"
        assert report.session_id == "sess-9"

    @pytest.mark.asyncio
    async def test_report_caller_roles_captured(self) -> None:
        scanner = _make_scanner()
        token = _caller_ctx_var.set(CallerContext(user_id="alice", roles={"analyst", "admin"}))
        try:
            report = await scanner.scan_text("hello")
        finally:
            _caller_ctx_var.reset(token)

        # Sorted deterministically so assertions are stable.
        assert report.caller_roles == ("admin", "analyst")

    @pytest.mark.asyncio
    async def test_report_without_caller_has_null_attribution(self) -> None:
        scanner = _make_scanner()
        report = await scanner.scan_text("no caller here")
        assert report.user_id is None
        assert report.session_id is None
        assert report.caller_roles == ()

    @pytest.mark.asyncio
    async def test_attribution_survives_contextvar_reset(self) -> None:
        """Capture-time attribution — not a live reference."""
        scanner = _make_scanner()
        token = _caller_ctx_var.set(CallerContext(user_id="alice"))
        try:
            report = await scanner.scan_text("some text")
        finally:
            _caller_ctx_var.reset(token)

        # Context gone, report still holds the snapshot.
        assert _caller_ctx_var.get() is None
        assert report.user_id == "alice"


class TestConcurrentScanAttribution:
    @pytest.mark.asyncio
    async def test_parallel_scans_stamp_distinct_users(self) -> None:
        scanner = _make_scanner()

        async def scan_as(user: str) -> str | None:
            token = _caller_ctx_var.set(CallerContext(user_id=user))
            try:
                await asyncio.sleep(0)
                report = await scanner.scan_text("benign content")
                return report.user_id
            finally:
                _caller_ctx_var.reset(token)

        users = await asyncio.gather(scan_as("alice"), scan_as("bob"), scan_as("carol"))
        assert sorted(users) == ["alice", "bob", "carol"]
