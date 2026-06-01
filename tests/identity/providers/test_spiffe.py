"""Unit tests for the SPIFFE / SPIRE provider and ``from_spiffe``.

pyspiffe is an *optional* dependency and is intentionally **not**
installed in the dev venv (it has no extra to introspect). The SDK-mode
tests therefore inject a fake ``pyspiffe.workloadapi.workload_api_client``
module tree into ``sys.modules`` so the lazy import inside
``_fetch_jwt_svid`` resolves to a stub. The missing-pyspiffe path sets
the package to ``None`` in ``sys.modules`` so the lazy import raises
ImportError — the same technique the AWS tests use for boto3. File mode
uses a real temp file. No network access and no real credentials.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

from promptise.identity import (
    ProviderConfigError,
    SpiffeFileProvider,
    SpiffeSdkProvider,
    TokenAcquisitionError,
)
from promptise.identity.providers.spiffe import (
    DEFAULT_SPIFFE_ENDPOINT_SOCKET,
    ENV_SPIFFE_ENDPOINT_SOCKET,
    from_spiffe,
)

FAKE_JWT: str = "header.payload.sig"

# Synthetic federation IDs reused across tests — identifiers, not
# secrets (build plan section 4.1).
_FED_KWARGS: dict[str, str] = {
    "federation_rule_id": "fdrl_test",
    "organization_id": "org_test",
    "service_account_id": "svac_test",
}


# -- Fakes ----------------------------------------------------------------


class _FakeJwtSvid:
    """Stand-in for a pyspiffe JwtSvid with a configurable accessor.

    The real serialized-token accessor varies across pyspiffe releases,
    so the production code probes several. ``token_value`` is exposed
    under the accessor named by ``accessor`` — either as a plain
    attribute or as a zero-arg method, per ``as_method``.
    """

    def __init__(
        self,
        token_value: Any,
        *,
        accessor: str = "token",
        as_method: bool = False,
    ) -> None:
        if as_method:
            setattr(self, accessor, lambda: token_value)
        else:
            setattr(self, accessor, token_value)


class _FakeWorkloadApiClient:
    """Minimal stand-in for a pyspiffe WorkloadApiClient.

    Records construction and call arguments on the class so tests can
    assert on them, and returns a preconfigured JwtSvid (or raises).
    """

    last_socket_path: str | None = None
    last_audiences: set[str] | None = None
    close_calls: int = 0

    def __init__(self, *, spiffe_socket_path: str) -> None:
        type(self).last_socket_path = spiffe_socket_path

    def fetch_jwt_svid(self, *, audiences: set[str]) -> Any:
        type(self).last_audiences = audiences
        return _FakeJwtSvid(FAKE_JWT)

    def close(self) -> None:
        type(self).close_calls += 1


def _install_fake_pyspiffe(
    monkeypatch: pytest.MonkeyPatch, client_cls: type
) -> None:
    """Inject a fake ``pyspiffe.workloadapi.workload_api_client`` tree.

    Makes ``from pyspiffe.workloadapi.workload_api_client import
    WorkloadApiClient`` resolve to ``client_cls``.
    """
    pyspiffe_mod = types.ModuleType("pyspiffe")
    workloadapi_mod = types.ModuleType("pyspiffe.workloadapi")
    client_mod = types.ModuleType("pyspiffe.workloadapi.workload_api_client")
    client_mod.WorkloadApiClient = client_cls  # type: ignore[attr-defined]
    workloadapi_mod.workload_api_client = client_mod  # type: ignore[attr-defined]
    pyspiffe_mod.workloadapi = workloadapi_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyspiffe", pyspiffe_mod)
    monkeypatch.setitem(sys.modules, "pyspiffe.workloadapi", workloadapi_mod)
    monkeypatch.setitem(
        sys.modules, "pyspiffe.workloadapi.workload_api_client", client_mod
    )


def _install_mock_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "sk-ant-oat01-mock", "expires_in": 3600},
        )

    transport = httpx.MockTransport(handler)

    def mocked_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", mocked_post)


# -- File mode ------------------------------------------------------------


def test_file_mode_reads_token(tmp_path: Path) -> None:
    f = tmp_path / "svid.jwt"
    f.write_text(FAKE_JWT, encoding="utf-8")
    provider = SpiffeFileProvider(token_file=f, **_FED_KWARGS)
    assert provider.provider_name == "spiffe-file"
    assert provider.token_file == f
    assert provider._acquire_upstream_jwt() == FAKE_JWT


def test_file_mode_get_token_smoke(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "svid.jwt"
    f.write_text(FAKE_JWT, encoding="utf-8")
    _install_mock_exchange(monkeypatch)
    provider = SpiffeFileProvider(token_file=f, **_FED_KWARGS)
    assert provider.get_token() == "sk-ant-oat01-mock"


# -- SDK mode: socket resolution ------------------------------------------


def test_socket_path_explicit_argument_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SPIFFE_ENDPOINT_SOCKET, "unix:///env/sock")
    provider = SpiffeSdkProvider(socket_path="unix:///explicit/sock", **_FED_KWARGS)
    assert provider._socket_path == "unix:///explicit/sock"


def test_socket_path_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SPIFFE_ENDPOINT_SOCKET, "unix:///env/sock")
    provider = SpiffeSdkProvider(**_FED_KWARGS)
    assert provider._socket_path == "unix:///env/sock"


def test_socket_path_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_SPIFFE_ENDPOINT_SOCKET, raising=False)
    provider = SpiffeSdkProvider(**_FED_KWARGS)
    assert provider._socket_path == DEFAULT_SPIFFE_ENDPOINT_SOCKET


# -- SDK mode: fetch + extraction -----------------------------------------


def test_sdk_fetches_and_extracts_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeWorkloadApiClient.last_socket_path = None
    _FakeWorkloadApiClient.last_audiences = None
    _FakeWorkloadApiClient.close_calls = 0
    _install_fake_pyspiffe(monkeypatch, _FakeWorkloadApiClient)
    provider = SpiffeSdkProvider(socket_path="unix:///tmp/agent.sock", **_FED_KWARGS)
    assert provider.provider_name == "spiffe-sdk"
    assert provider._fetch_jwt_svid() == FAKE_JWT
    assert _FakeWorkloadApiClient.last_socket_path == "unix:///tmp/agent.sock"
    assert _FakeWorkloadApiClient.last_audiences == {"https://api.anthropic.com"}
    # The client is closed after a successful fetch.
    assert _FakeWorkloadApiClient.close_calls == 1


def test_sdk_custom_audience_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeWorkloadApiClient.last_audiences = None
    _install_fake_pyspiffe(monkeypatch, _FakeWorkloadApiClient)
    provider = SpiffeSdkProvider(audience="https://custom.example.com", **_FED_KWARGS)
    provider._fetch_jwt_svid()
    assert _FakeWorkloadApiClient.last_audiences == {"https://custom.example.com"}


def test_sdk_token_via_method_accessor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some pyspiffe versions expose the serialized token under a method
    (e.g. ``marshal()``) rather than an attribute."""

    class _Client(_FakeWorkloadApiClient):
        def fetch_jwt_svid(self, *, audiences: set[str]) -> Any:
            return _FakeJwtSvid(FAKE_JWT, accessor="marshal", as_method=True)

    _install_fake_pyspiffe(monkeypatch, _Client)
    provider = SpiffeSdkProvider(**_FED_KWARGS)
    assert provider._fetch_jwt_svid() == FAKE_JWT


def test_sdk_token_whitespace_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client(_FakeWorkloadApiClient):
        def fetch_jwt_svid(self, *, audiences: set[str]) -> Any:
            return _FakeJwtSvid(f"  {FAKE_JWT}\n", accessor="token_str")

    _install_fake_pyspiffe(monkeypatch, _Client)
    provider = SpiffeSdkProvider(**_FED_KWARGS)
    assert provider._fetch_jwt_svid() == FAKE_JWT


def test_sdk_accessor_that_raises_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A callable accessor that raises must be skipped, falling through to
    the next candidate accessor."""

    class _Svid:
        def token(self) -> str:  # first accessor — raises
            raise RuntimeError("not this one")

        serialize = FAKE_JWT  # later accessor — a plain attribute

    class _Client(_FakeWorkloadApiClient):
        def fetch_jwt_svid(self, *, audiences: set[str]) -> Any:
            return _Svid()

    _install_fake_pyspiffe(monkeypatch, _Client)
    provider = SpiffeSdkProvider(**_FED_KWARGS)
    assert provider._fetch_jwt_svid() == FAKE_JWT


def test_sdk_unextractable_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Svid:
        token = None
        token_str = ""

    class _Client(_FakeWorkloadApiClient):
        def fetch_jwt_svid(self, *, audiences: set[str]) -> Any:
            return _Svid()

    _install_fake_pyspiffe(monkeypatch, _Client)
    provider = SpiffeSdkProvider(socket_path="unix:///tmp/agent.sock", **_FED_KWARGS)
    with pytest.raises(TokenAcquisitionError, match="serialized token could not be"):
        provider._fetch_jwt_svid()


def test_sdk_fetch_failure_raises_and_still_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client(_FakeWorkloadApiClient):
        def fetch_jwt_svid(self, *, audiences: set[str]) -> Any:
            raise RuntimeError("no agent listening")

    _Client.close_calls = 0
    _install_fake_pyspiffe(monkeypatch, _Client)
    provider = SpiffeSdkProvider(socket_path="unix:///tmp/agent.sock", **_FED_KWARGS)
    with pytest.raises(TokenAcquisitionError, match="fetching a JWT-SVID"):
        provider._fetch_jwt_svid()
    # The finally clause closes the client even when the fetch failed.
    assert _Client.close_calls == 1


def test_sdk_close_error_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A teardown failure must not mask a successful fetch result."""

    class _Client(_FakeWorkloadApiClient):
        def close(self) -> None:
            raise RuntimeError("teardown blew up")

    _install_fake_pyspiffe(monkeypatch, _Client)
    provider = SpiffeSdkProvider(**_FED_KWARGS)
    assert provider._fetch_jwt_svid() == FAKE_JWT


def test_sdk_client_without_close_method(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client lacking a ``close`` method must not trip the teardown."""

    class _Client:
        def __init__(self, *, spiffe_socket_path: str) -> None:
            pass

        def fetch_jwt_svid(self, *, audiences: set[str]) -> Any:
            return _FakeJwtSvid(FAKE_JWT)

    _install_fake_pyspiffe(monkeypatch, _Client)
    provider = SpiffeSdkProvider(**_FED_KWARGS)
    assert provider._fetch_jwt_svid() == FAKE_JWT


def test_sdk_missing_pyspiffe_raises_provider_config_error_with_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build-plan phase-6 acceptance: the pyspiffe-not-installed error
    names the exact ``pip install promptise[identity-spiffe]`` command and
    surfaces as ProviderConfigError (not TokenAcquisitionError)."""
    # Setting the package to None makes the lazy import raise ImportError.
    monkeypatch.setitem(sys.modules, "pyspiffe", None)
    provider = SpiffeSdkProvider(**_FED_KWARGS)
    with pytest.raises(ProviderConfigError) as exc_info:
        provider._fetch_jwt_svid()
    assert "pip install promptise[identity-spiffe]" in str(exc_info.value)


def test_sdk_get_token_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_pyspiffe(monkeypatch, _FakeWorkloadApiClient)
    _install_mock_exchange(monkeypatch)
    provider = SpiffeSdkProvider(**_FED_KWARGS)
    assert provider.get_token() == "sk-ant-oat01-mock"


# -- Factory --------------------------------------------------------------


def test_from_spiffe_auto_picks_file_when_token_file_given(tmp_path: Path) -> None:
    f = tmp_path / "svid.jwt"
    f.write_text(FAKE_JWT, encoding="utf-8")
    provider = from_spiffe(token_file=f, **_FED_KWARGS)
    assert isinstance(provider, SpiffeFileProvider)


def test_from_spiffe_auto_picks_sdk_when_no_token_file() -> None:
    provider = from_spiffe(**_FED_KWARGS)
    assert isinstance(provider, SpiffeSdkProvider)


def test_from_spiffe_file_mode_requires_token_file() -> None:
    with pytest.raises(ProviderConfigError, match="requires token_file"):
        from_spiffe(mode="file", **_FED_KWARGS)


def test_from_spiffe_file_mode_explicit(tmp_path: Path) -> None:
    f = tmp_path / "svid.jwt"
    f.write_text(FAKE_JWT, encoding="utf-8")
    provider = from_spiffe(mode="file", token_file=f, **_FED_KWARGS)
    assert isinstance(provider, SpiffeFileProvider)


def test_from_spiffe_sdk_mode_passes_options() -> None:
    provider = from_spiffe(
        mode="sdk",
        socket_path="unix:///tmp/custom.sock",
        audience="https://aud.example.com",
        **_FED_KWARGS,
    )
    assert isinstance(provider, SpiffeSdkProvider)
    assert provider._socket_path == "unix:///tmp/custom.sock"
    assert provider._audience == "https://aud.example.com"


def test_from_spiffe_unknown_mode_raises() -> None:
    with pytest.raises(ProviderConfigError, match="Unknown SPIFFE mode"):
        from_spiffe(mode="bogus", **_FED_KWARGS)  # type: ignore[arg-type]


def test_from_spiffe_workspace_id_passed_through() -> None:
    provider = from_spiffe(workspace_id="wrkspc_explicit", **_FED_KWARGS)
    assert provider.workspace_id == "wrkspc_explicit"


def test_from_spiffe_missing_federation_id_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_FEDERATION_RULE_ID", raising=False)
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org_env")
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_env")
    with pytest.raises(ProviderConfigError, match="ANTHROPIC_FEDERATION_RULE_ID"):
        from_spiffe()
