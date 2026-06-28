"""Unit tests for the SPIFFE / SPIRE credential provider.

pyspiffe is an optional dependency that is intentionally not installed in
the dev venv, so SDK-mode tests inject a fake module tree into
``sys.modules``; the missing-pyspiffe path sets the package to ``None``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from promptise.identity import (
    CredentialAcquisitionError,
    ProviderConfigError,
    SpiffeFileProvider,
    SpiffeSdkProvider,
)
from promptise.identity.providers.spiffe import (
    DEFAULT_SPIFFE_ENDPOINT_SOCKET,
    ENV_SPIFFE_ENDPOINT_SOCKET,
    from_spiffe,
)

FAKE_JWT = "header.payload.sig"


class _FakeJwtSvid:
    def __init__(self, value: Any, *, accessor: str = "token") -> None:
        setattr(self, accessor, value)


class _FakeClient:
    last_socket: str | None = None
    last_audiences: set[str] | None = None

    def __init__(self, *, spiffe_socket_path: str) -> None:
        type(self).last_socket = spiffe_socket_path

    def fetch_jwt_svid(self, *, audiences: set[str]) -> Any:
        type(self).last_audiences = audiences
        return _FakeJwtSvid(FAKE_JWT)

    def close(self) -> None:
        pass


def _install_fake_pyspiffe(monkeypatch: pytest.MonkeyPatch, client_cls: type) -> None:
    pyspiffe_mod = types.ModuleType("pyspiffe")
    wl_mod = types.ModuleType("pyspiffe.workloadapi")
    client_mod = types.ModuleType("pyspiffe.workloadapi.workload_api_client")
    client_mod.WorkloadApiClient = client_cls  # type: ignore[attr-defined]
    wl_mod.workload_api_client = client_mod  # type: ignore[attr-defined]
    pyspiffe_mod.workloadapi = wl_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyspiffe", pyspiffe_mod)
    monkeypatch.setitem(sys.modules, "pyspiffe.workloadapi", wl_mod)
    monkeypatch.setitem(
        sys.modules, "pyspiffe.workloadapi.workload_api_client", client_mod
    )


# -- File mode ------------------------------------------------------------


def test_file_mode_reads_token(tmp_path: Path) -> None:
    f = tmp_path / "svid.jwt"
    f.write_text(FAKE_JWT, encoding="utf-8")
    provider = SpiffeFileProvider(token_file=f)
    assert provider.provider_name == "spiffe-file"
    assert provider._acquire_upstream_jwt() == FAKE_JWT


# -- SDK mode -------------------------------------------------------------


def test_socket_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SPIFFE_ENDPOINT_SOCKET, "unix:///env/sock")
    assert SpiffeSdkProvider(socket_path="unix:///explicit")._socket_path == (
        "unix:///explicit"
    )
    assert SpiffeSdkProvider()._socket_path == "unix:///env/sock"
    monkeypatch.delenv(ENV_SPIFFE_ENDPOINT_SOCKET, raising=False)
    assert SpiffeSdkProvider()._socket_path == DEFAULT_SPIFFE_ENDPOINT_SOCKET


def test_sdk_fetches_and_extracts(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.last_socket = None
    _FakeClient.last_audiences = None
    _install_fake_pyspiffe(monkeypatch, _FakeClient)
    provider = SpiffeSdkProvider(socket_path="unix:///tmp/a.sock", audience="api://m")
    assert provider.provider_name == "spiffe-sdk"
    assert provider._acquire_upstream_jwt() == FAKE_JWT
    assert _FakeClient.last_socket == "unix:///tmp/a.sock"
    assert _FakeClient.last_audiences == {"api://m"}


def test_sdk_token_via_method_accessor(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client(_FakeClient):
        def fetch_jwt_svid(self, *, audiences: set[str]) -> Any:
            svid = types.SimpleNamespace(marshal=lambda: FAKE_JWT)
            return svid

    _install_fake_pyspiffe(monkeypatch, _Client)
    assert SpiffeSdkProvider()._acquire_upstream_jwt() == FAKE_JWT


def test_sdk_accessor_that_raises_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Svid:
        def token(self) -> str:  # first accessor — raises, must be skipped
            raise RuntimeError("not this one")

        serialize = FAKE_JWT  # later accessor — a plain attribute

    class _Client(_FakeClient):
        def fetch_jwt_svid(self, *, audiences: set[str]) -> Any:
            return _Svid()

    _install_fake_pyspiffe(monkeypatch, _Client)
    assert SpiffeSdkProvider()._acquire_upstream_jwt() == FAKE_JWT


def test_sdk_unextractable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client(_FakeClient):
        def fetch_jwt_svid(self, *, audiences: set[str]) -> Any:
            return types.SimpleNamespace(token=None, token_str="")

    _install_fake_pyspiffe(monkeypatch, _Client)
    with pytest.raises(CredentialAcquisitionError, match="could not be extracted"):
        SpiffeSdkProvider()._acquire_upstream_jwt()


def test_sdk_fetch_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client(_FakeClient):
        def fetch_jwt_svid(self, *, audiences: set[str]) -> Any:
            raise RuntimeError("no agent")

    _install_fake_pyspiffe(monkeypatch, _Client)
    with pytest.raises(CredentialAcquisitionError, match="fetching a JWT-SVID"):
        SpiffeSdkProvider()._acquire_upstream_jwt()


def test_sdk_close_error_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client(_FakeClient):
        def close(self) -> None:
            raise RuntimeError("teardown")

    _install_fake_pyspiffe(monkeypatch, _Client)
    assert SpiffeSdkProvider()._acquire_upstream_jwt() == FAKE_JWT


def test_sdk_client_without_close(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        def __init__(self, *, spiffe_socket_path: str) -> None:
            pass

        def fetch_jwt_svid(self, *, audiences: set[str]) -> Any:
            return _FakeJwtSvid(FAKE_JWT)

    _install_fake_pyspiffe(monkeypatch, _Client)
    assert SpiffeSdkProvider()._acquire_upstream_jwt() == FAKE_JWT


def test_sdk_missing_pyspiffe_raises_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pyspiffe", None)
    with pytest.raises(ProviderConfigError) as exc:
        SpiffeSdkProvider()._acquire_upstream_jwt()
    assert "pip install promptise[identity-spiffe]" in str(exc.value)


# -- Factory --------------------------------------------------------------


def test_from_spiffe_auto_file_when_token_file(tmp_path: Path) -> None:
    f = tmp_path / "svid.jwt"
    f.write_text(FAKE_JWT, encoding="utf-8")
    assert isinstance(from_spiffe(token_file=f), SpiffeFileProvider)


def test_from_spiffe_auto_sdk_when_no_token_file() -> None:
    assert isinstance(from_spiffe(), SpiffeSdkProvider)


def test_from_spiffe_file_requires_token_file() -> None:
    with pytest.raises(ProviderConfigError, match="requires token_file"):
        from_spiffe(mode="file")


def test_from_spiffe_unknown_mode_raises() -> None:
    with pytest.raises(ProviderConfigError, match="Unknown SPIFFE mode"):
        from_spiffe(mode="bogus")  # type: ignore[arg-type]
