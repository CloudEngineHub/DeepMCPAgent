"""Unit tests for :class:`MintedToken` and the two-tier refresh windows."""

from __future__ import annotations

import time

from promptise.identity import (
    ADVISORY_REFRESH_BUFFER_SECONDS,
    MANDATORY_REFRESH_BUFFER_SECONDS,
    MintedToken,
)


def test_buffer_constants_match_build_plan() -> None:
    """The two refresh buffers must match section 4.5 of the build plan exactly."""
    assert ADVISORY_REFRESH_BUFFER_SECONDS == 120
    assert MANDATORY_REFRESH_BUFFER_SECONDS == 30


def _mint(expires_in: int) -> MintedToken:
    """Create a token whose nominal expiry is ``expires_in`` seconds from now.

    The test JWT prefix here is the legitimate Anthropic format; this is
    not a real credential — it is a literal made of the prefix plus the
    word ``test``.
    """
    return MintedToken(
        access_token="sk-ant-oat01-test",
        token_type="Bearer",
        expires_at_monotonic=time.monotonic() + expires_in,
        expires_in_seconds=expires_in,
    )


def test_fresh_token_needs_no_refresh() -> None:
    token = _mint(expires_in=3600)
    assert not token.needs_advisory_refresh()
    assert not token.needs_mandatory_refresh()
    assert token.time_until_expiry() > 3500


def test_token_inside_advisory_window_only() -> None:
    # 90 s remaining: inside the 120 s advisory window but outside the
    # 30 s mandatory window.
    token = _mint(expires_in=90)
    assert token.needs_advisory_refresh()
    assert not token.needs_mandatory_refresh()


def test_token_inside_mandatory_window_triggers_both() -> None:
    # 15 s remaining: inside both windows.
    token = _mint(expires_in=15)
    assert token.needs_advisory_refresh()
    assert token.needs_mandatory_refresh()


def test_expired_token_triggers_both_windows() -> None:
    token = _mint(expires_in=-1)
    assert token.needs_advisory_refresh()
    assert token.needs_mandatory_refresh()
    assert token.time_until_expiry() < 0


def test_time_until_expiry_uses_monotonic_clock() -> None:
    """Section 4.5: wall-clock skew must not affect token validity.

    Asserts the clock used is monotonic by observing that consecutive
    calls always show a strictly decreasing remaining lifetime.
    """
    token = _mint(expires_in=600)
    remaining_before = token.time_until_expiry()
    time.sleep(0.05)
    remaining_after = token.time_until_expiry()
    assert remaining_before > remaining_after
    assert remaining_after > 0


def test_minted_token_is_frozen() -> None:
    """Tokens are immutable — refresh replaces, never mutates."""
    token = _mint(expires_in=3600)
    try:
        token.access_token = "tampered"  # type: ignore[misc]
    except (AttributeError, Exception):  # noqa: BLE001 — covers both FrozenInstanceError and AttributeError
        return
    raise AssertionError("MintedToken should be frozen but allowed mutation")
