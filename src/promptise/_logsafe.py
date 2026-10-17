"""Make untrusted values safe to put in a log line."""

from __future__ import annotations

from typing import Any

__all__ = ["log_safe"]

_DEFAULT_MAX = 200


def log_safe(value: Any, max_len: int = _DEFAULT_MAX) -> str:
    """Render *value* for a log line without letting it forge one.

    A value that came from the network (a header, a peer address, a path)
    can carry ``CR``/``LF`` or other control characters, which would let
    it start a fake log entry or corrupt a terminal.  Every non-printable
    character is escaped (``\\n`` becomes the two characters ``\\`` and
    ``n``), and the result is capped at *max_len* characters so one
    request cannot flood the log.

    Args:
        value: Anything; it is passed through ``str()`` first.
        max_len: The longest result before truncation (a marker is added).
    """
    text = str(value)
    if len(text) > max_len:
        text = text[:max_len] + "...[truncated]"
    text = text.replace("\r", "\\r")
    text = "".join(
        ch if ch == "\n" or ch.isprintable() else ch.encode("unicode_escape").decode("ascii")
        for ch in text
    )
    return text.replace("\n", "\\n")
