"""Unit tests for the SPIFFE / SPIRE credential provider.

py-spiffe (the ``spiffe`` package) is an optional dependency that is
intentionally not installed in the dev venv, so SDK-mode tests inject a
fake ``spiffe`` module into ``sys.modules`` whose client has py-spiffe's
signatures (checked against the real package when it is installed); the
missing-library path sets the package to ``None``.
"""

from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path
from typing import Any

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

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
    """``spiffe.WorkloadApiClient`` with py-spiffe's signatures (0.2 - 0.3)."""

    last_socket: str | None = None
    last_audiences: set[str] | None = None

    def __init__(
        self, socket_path: str | None = None, default_timeout: float | None = None
    ) -> None:
        type(self).last_socket = socket_path

    def fetch_jwt_svid(
        self, audience: set[str], subject: Any = None, timeout: float | None = None
    ) -> Any:
        type(self).last_audiences = audience
        return _FakeJwtSvid(FAKE_JWT)

    def close(self) -> None:
        pass


def _install_fake_spiffe(monkeypatch: pytest.MonkeyPatch, client_cls: type) -> None:
    spiffe_mod = types.ModuleType("spiffe")
    spiffe_mod.WorkloadApiClient = client_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "spiffe", spiffe_mod)


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
    assert SpiffeSdkProvider(socket_path="unix:///explicit")._socket_path == ("unix:///explicit")
    assert SpiffeSdkProvider()._socket_path == "unix:///env/sock"
    monkeypatch.delenv(ENV_SPIFFE_ENDPOINT_SOCKET, raising=False)
    assert SpiffeSdkProvider()._socket_path == DEFAULT_SPIFFE_ENDPOINT_SOCKET


def test_sdk_fetches_and_extracts(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.last_socket = None
    _FakeClient.last_audiences = None
    _install_fake_spiffe(monkeypatch, _FakeClient)
    provider = SpiffeSdkProvider(socket_path="unix:///tmp/a.sock", audience="api://m")
    assert provider.provider_name == "spiffe-sdk"
    assert provider._acquire_upstream_jwt() == FAKE_JWT
    assert _FakeClient.last_socket == "unix:///tmp/a.sock"
    assert _FakeClient.last_audiences == {"api://m"}


def test_sdk_token_via_method_accessor(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client(_FakeClient):
        def fetch_jwt_svid(
            self, audience: set[str], subject: Any = None, timeout: Any = None
        ) -> Any:
            svid = types.SimpleNamespace(marshal=lambda: FAKE_JWT)
            return svid

    _install_fake_spiffe(monkeypatch, _Client)
    assert SpiffeSdkProvider()._acquire_upstream_jwt() == FAKE_JWT


def test_sdk_accessor_that_raises_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Svid:
        def token(self) -> str:  # first accessor — raises, must be skipped
            raise RuntimeError("not this one")

        serialize = FAKE_JWT  # later accessor — a plain attribute

    class _Client(_FakeClient):
        def fetch_jwt_svid(
            self, audience: set[str], subject: Any = None, timeout: Any = None
        ) -> Any:
            return _Svid()

    _install_fake_spiffe(monkeypatch, _Client)
    assert SpiffeSdkProvider()._acquire_upstream_jwt() == FAKE_JWT


def test_sdk_unextractable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client(_FakeClient):
        def fetch_jwt_svid(
            self, audience: set[str], subject: Any = None, timeout: Any = None
        ) -> Any:
            return types.SimpleNamespace(token=None, token_str="")

    _install_fake_spiffe(monkeypatch, _Client)
    with pytest.raises(CredentialAcquisitionError, match="could not be extracted"):
        SpiffeSdkProvider()._acquire_upstream_jwt()


def test_sdk_fetch_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client(_FakeClient):
        def fetch_jwt_svid(
            self, audience: set[str], subject: Any = None, timeout: Any = None
        ) -> Any:
            raise RuntimeError("no agent")

    _install_fake_spiffe(monkeypatch, _Client)
    with pytest.raises(CredentialAcquisitionError, match="fetching a JWT-SVID"):
        SpiffeSdkProvider()._acquire_upstream_jwt()


def test_sdk_close_error_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client(_FakeClient):
        def close(self) -> None:
            raise RuntimeError("teardown")

    _install_fake_spiffe(monkeypatch, _Client)
    assert SpiffeSdkProvider()._acquire_upstream_jwt() == FAKE_JWT


def test_sdk_client_without_close(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        def __init__(self, socket_path: str | None = None) -> None:
            pass

        def fetch_jwt_svid(
            self, audience: set[str], subject: Any = None, timeout: Any = None
        ) -> Any:
            return _FakeJwtSvid(FAKE_JWT)

    _install_fake_spiffe(monkeypatch, _Client)
    assert SpiffeSdkProvider()._acquire_upstream_jwt() == FAKE_JWT


def test_sdk_missing_spiffe_raises_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "spiffe", None)
    with pytest.raises(ProviderConfigError) as exc:
        SpiffeSdkProvider()._acquire_upstream_jwt()
    assert "pip install promptise[identity-spiffe]" in str(exc.value)


def test_sdk_client_construction_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """py-spiffe checks the socket when the client is built, not on fetch."""

    class _Client(_FakeClient):
        def __init__(self, socket_path: str | None = None, default_timeout: Any = None) -> None:
            raise ValueError("Invalid WorkloadApiClient configuration: socket not found")

    _install_fake_spiffe(monkeypatch, _Client)
    with pytest.raises(CredentialAcquisitionError, match="fetching a JWT-SVID"):
        SpiffeSdkProvider()._acquire_upstream_jwt()


# -- py-spiffe compatibility (1.5.0 imported a non-existent `pyspiffe`) ----


def test_sdk_uses_py_spiffe_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK mode imports `spiffe` and calls it with py-spiffe's argument names."""

    class _StrictClient:
        """Only py-spiffe's keywords: `spiffe_socket_path=` / `audiences=` fail."""

        socket: str | None = None
        audience: set[str] | None = None

        def __init__(self, socket_path: str | None = None, default_timeout: Any = None) -> None:
            type(self).socket = socket_path

        def fetch_jwt_svid(self, audience: set[str], subject: Any = None, timeout: Any = None):
            type(self).audience = audience
            svid = types.SimpleNamespace()
            svid.token = FAKE_JWT
            return svid

        def close(self) -> None:
            pass

    _install_fake_spiffe(monkeypatch, _StrictClient)
    monkeypatch.setitem(sys.modules, "pyspiffe", None)  # never the non-existent package
    provider = SpiffeSdkProvider(socket_path="unix:///run/spire/api.sock", audience="api://mcp")
    assert provider._acquire_upstream_jwt() == FAKE_JWT
    assert _StrictClient.socket == "unix:///run/spire/api.sock"
    assert _StrictClient.audience == {"api://mcp"}


def test_auto_identity_with_spiffe_socket_gets_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`AgentIdentity.auto()` on a SPIFFE workload (only the socket env var) works."""
    from promptise.identity import AgentIdentity
    from promptise.identity._internal import detect

    for _platform, markers in detect._DETECTION_ORDER:
        for var in markers:
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(ENV_SPIFFE_ENDPOINT_SOCKET, "unix:///run/spire/api.sock")
    _FakeClient.last_socket = None
    _install_fake_spiffe(monkeypatch, _FakeClient)
    monkeypatch.setitem(sys.modules, "pyspiffe", None)

    identity = AgentIdentity.auto("billing-bot")
    assert isinstance(identity.credential, SpiffeSdkProvider)
    assert identity.credential._acquire_upstream_jwt() == FAKE_JWT
    assert _FakeClient.last_socket == "unix:///run/spire/api.sock"


def test_identity_spiffe_extra_depends_on_published_package() -> None:
    """The extra names `spiffe` (py-spiffe on PyPI), not the non-existent `pyspiffe`."""
    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip("not running from a source checkout")
    extras = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    names = [req.split(";")[0].split("[")[0] for req in extras["identity-spiffe"]]
    names = [n.split(">")[0].split("<")[0].split("=")[0].split("~")[0].strip() for n in names]
    assert names == ["spiffe"]
    assert not any("pyspiffe" in req for reqs in extras.values() for req in reqs)


def test_fake_client_matches_real_py_spiffe() -> None:
    """The fake above keeps py-spiffe's signatures (runs where `spiffe` is installed)."""
    spiffe = pytest.importorskip("spiffe")
    real = spiffe.WorkloadApiClient

    def params(func: Any) -> list[str]:
        return [p for p in inspect.signature(func).parameters if p != "self"]

    assert "socket_path" in params(real.__init__)
    assert params(real.fetch_jwt_svid)[0] == "audience"
    assert isinstance(inspect.getattr_static(spiffe.JwtSvid, "token"), property)


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
