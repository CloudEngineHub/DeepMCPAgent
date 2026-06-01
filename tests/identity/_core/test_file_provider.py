"""Unit tests for :class:`FileTokenProvider`.

Covers the file-reading contract: strip trailing whitespace, raise on
missing or empty files, and — critically — re-read on every refresh
so projected-token rotation works (build plan section 4.6).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from promptise.identity import FileTokenProvider, TokenAcquisitionError


def _make_provider(token_file: Path) -> FileTokenProvider:
    """Construct a provider with synthetic federation IDs."""
    return FileTokenProvider(
        token_file=token_file,
        provider_label="test-file",
        federation_rule_id="fdrl_test",
        organization_id="org_test",
        service_account_id="svac_test",
    )


def test_reads_file_and_strips_whitespace(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text("header.payload.sig\n", encoding="utf-8")
    provider = _make_provider(f)
    assert provider._acquire_upstream_jwt() == "header.payload.sig"


def test_missing_file_raises_with_path_in_message(tmp_path: Path) -> None:
    f = tmp_path / "absent"
    provider = _make_provider(f)
    with pytest.raises(TokenAcquisitionError, match=str(f)):
        provider._acquire_upstream_jwt()


def test_empty_file_raises(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text("   \n", encoding="utf-8")
    provider = _make_provider(f)
    with pytest.raises(TokenAcquisitionError, match="empty"):
        provider._acquire_upstream_jwt()


def test_file_is_reread_every_call(tmp_path: Path) -> None:
    """Section 4.6: the projection mechanism rewrites the file in
    place. The provider must observe each new write, not cache the
    first read."""
    f = tmp_path / "token"
    f.write_text("first.jwt.value", encoding="utf-8")
    provider = _make_provider(f)
    assert provider._acquire_upstream_jwt() == "first.jwt.value"

    # Platform rewrites the file in place — simulate rotation.
    f.write_text("second.jwt.value", encoding="utf-8")
    assert provider._acquire_upstream_jwt() == "second.jwt.value"

    f.write_text("third.jwt.value", encoding="utf-8")
    assert provider._acquire_upstream_jwt() == "third.jwt.value"


def test_token_file_property_exposes_path(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text("anything", encoding="utf-8")
    provider = _make_provider(f)
    assert provider.token_file == f


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission test")
def test_unreadable_file_raises_permission_error(tmp_path: Path) -> None:
    """If the file exists but the process cannot read it, the provider
    raises :class:`TokenAcquisitionError` (not the bare OSError)."""
    f = tmp_path / "token"
    f.write_text("anything", encoding="utf-8")
    # Strip every read permission. Running tests as root would bypass
    # this; the CI environment is non-root.
    f.chmod(stat.S_IWUSR)
    provider = _make_provider(f)
    try:
        if os.geteuid() == 0:
            pytest.skip("running as root; cannot test PermissionError")
        with pytest.raises(TokenAcquisitionError, match="not readable"):
            provider._acquire_upstream_jwt()
    finally:
        f.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_provider_name_returns_label(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text("x.y.z", encoding="utf-8")
    provider = _make_provider(f)
    assert provider.provider_name == "test-file"


def test_path_pointing_at_directory_raises_token_acquisition_error(
    tmp_path: Path,
) -> None:
    """A generic OSError (here IsADirectoryError, from pointing the
    token path at a directory) is surfaced as TokenAcquisitionError,
    not leaked as a bare OSError."""
    directory = tmp_path / "a_directory"
    directory.mkdir()
    provider = _make_provider(directory)
    with pytest.raises(TokenAcquisitionError, match="could not read projected token"):
        provider._acquire_upstream_jwt()
