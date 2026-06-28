"""Unit tests for :class:`FileTokenProvider`.

The base file provider reads a projected JWT on every refresh and caches
it only while its ``exp`` claim is comfortably in the future.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import pytest

from promptise.identity import CredentialAcquisitionError, FileTokenProvider


def _jwt(exp: float | None) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    claims: dict[str, object] = {"sub": "agent"}
    if exp is not None:
        claims["exp"] = exp
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    )
    return f"{header}.{payload}."


def test_reads_and_strips_token(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text(f"  {_jwt(None)}\n", encoding="utf-8")
    provider = FileTokenProvider(token_file=f)
    assert provider.provider_name == "file"
    assert provider.token_file == f
    assert provider._acquire_upstream_jwt() == _jwt(None)
    assert provider.get_credential() == _jwt(None)


def test_custom_provider_label() -> None:
    provider = FileTokenProvider(token_file="/nope", provider_label="entra-projected")
    assert provider.provider_name == "entra-projected"


def test_missing_file_raises(tmp_path: Path) -> None:
    provider = FileTokenProvider(token_file=tmp_path / "absent")
    with pytest.raises(CredentialAcquisitionError, match="not found"):
        provider.get_credential()


def test_empty_file_raises(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text("   \n", encoding="utf-8")
    provider = FileTokenProvider(token_file=f)
    with pytest.raises(CredentialAcquisitionError, match="is empty"):
        provider.get_credential()


def test_directory_path_raises_os_error(tmp_path: Path) -> None:
    # Opening a directory for read raises IsADirectoryError (an OSError).
    provider = FileTokenProvider(token_file=tmp_path)
    with pytest.raises(CredentialAcquisitionError, match="could not read"):
        provider.get_credential()


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="permission bits are unenforceable as root or on non-POSIX",
)
def test_unreadable_file_raises_permission_error(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text("header.payload.sig", encoding="utf-8")
    f.chmod(0o000)
    try:
        provider = FileTokenProvider(token_file=f)
        with pytest.raises(CredentialAcquisitionError, match="not readable"):
            provider.get_credential()
    finally:
        f.chmod(0o600)


def test_no_expiry_token_is_reread_on_rotation(tmp_path: Path) -> None:
    """A token with no decodable expiry is re-read every call, so in-place
    rotation (Kubernetes/SPIFFE) is observed."""
    f = tmp_path / "token"
    f.write_text(_jwt(None), encoding="utf-8")
    provider = FileTokenProvider(token_file=f)
    assert provider.get_credential() == _jwt(None)

    rotated = "header.rotated.sig"
    f.write_text(rotated, encoding="utf-8")
    assert provider.get_credential() == rotated


def test_token_with_future_expiry_is_cached(tmp_path: Path) -> None:
    """A token whose exp is far in the future is cached and not re-read."""
    f = tmp_path / "token"
    first = _jwt(time.time() + 3600)
    f.write_text(first, encoding="utf-8")
    provider = FileTokenProvider(token_file=f)
    assert provider.get_credential() == first

    # Overwrite the file; the cached, still-valid token is returned.
    f.write_text(_jwt(time.time() + 7200), encoding="utf-8")
    assert provider.get_credential() == first
