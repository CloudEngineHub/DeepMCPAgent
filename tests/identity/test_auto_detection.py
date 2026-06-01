"""Unit tests for platform auto-detection and ``AgentIdentity.auto``.

``detect_platform`` is environment-variable driven, so every test
starts from a clean slate (all detection markers removed) and sets only
the markers under test. Covers each detection branch, the precedence
ordering (Entra beats AWS beats GCP beats SPIFFE), empty-string markers
counting as unset, the no-platform error, and the ``auto`` dispatch
wiring each detected platform to the right provider. No network access
and no real credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from promptise.identity import (
    AgentIdentity,
    AwsEksProjectedProvider,
    EntraProjectedTokenProvider,
    GcpMetadataProvider,
    PlatformDetectionError,
    SpiffeSdkProvider,
)
from promptise.identity._internal.detect import detect_platform

# Synthetic federation IDs — identifiers, not secrets (build plan 4.1).
_FED_KWARGS: dict[str, str] = {
    "federation_rule_id": "fdrl_test",
    "organization_id": "org_test",
    "service_account_id": "svac_test",
}

# Every environment marker the detector inspects, across all platforms.
_ALL_MARKERS: tuple[str, ...] = (
    "AZURE_FEDERATED_TOKEN_FILE",
    "AZURE_CLIENT_ID",
    "AWS_LAMBDA_FUNCTION_NAME",
    "AWS_EXECUTION_ENV",
    "EKS_POD_NAME",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "GOOGLE_CLOUD_PROJECT",
    "K_SERVICE",
    "GCE_METADATA_IP",
    "SPIFFE_ENDPOINT_SOCKET",
)


@pytest.fixture(autouse=True)
def _clean_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every detection marker so each test controls the env."""
    for name in _ALL_MARKERS:
        monkeypatch.delenv(name, raising=False)


# -- Single-platform detection --------------------------------------------


@pytest.mark.parametrize("marker", ["AZURE_FEDERATED_TOKEN_FILE", "AZURE_CLIENT_ID"])
def test_detects_entra(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    monkeypatch.setenv(marker, "value")
    assert detect_platform() == "entra"


@pytest.mark.parametrize(
    "marker",
    [
        "AWS_LAMBDA_FUNCTION_NAME",
        "AWS_EXECUTION_ENV",
        "EKS_POD_NAME",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    ],
)
def test_detects_aws(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    monkeypatch.setenv(marker, "value")
    assert detect_platform() == "aws"


@pytest.mark.parametrize(
    "marker", ["GOOGLE_CLOUD_PROJECT", "K_SERVICE", "GCE_METADATA_IP"]
)
def test_detects_gcp(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    monkeypatch.setenv(marker, "value")
    assert detect_platform() == "gcp"


def test_detects_spiffe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPIFFE_ENDPOINT_SOCKET", "unix:///tmp/agent.sock")
    assert detect_platform() == "spiffe"


# -- Precedence -----------------------------------------------------------


def test_entra_beats_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "x")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "y")
    assert detect_platform() == "entra"


def test_aws_beats_gcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_EXECUTION_ENV", "x")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "y")
    assert detect_platform() == "aws"


def test_gcp_beats_spiffe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K_SERVICE", "x")
    monkeypatch.setenv("SPIFFE_ENDPOINT_SOCKET", "unix:///tmp/agent.sock")
    assert detect_platform() == "gcp"


# -- Empty / missing markers ----------------------------------------------


def test_empty_marker_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "")  # declared but empty
    with pytest.raises(PlatformDetectionError):
        detect_platform()


def test_no_markers_raises_platform_detection_error() -> None:
    with pytest.raises(PlatformDetectionError, match="could not auto-detect"):
        detect_platform()


# -- AgentIdentity.auto dispatch ------------------------------------------


def test_auto_dispatches_to_entra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "token"
    f.write_text("header.payload.sig", encoding="utf-8")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(f))
    identity = AgentIdentity.auto(**_FED_KWARGS)
    # mode="auto" + a projected token file present => projected provider.
    assert isinstance(identity.provider, EntraProjectedTokenProvider)
    assert identity.federation_rule_id == "fdrl_test"


def test_auto_dispatches_to_aws(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "token"
    f.write_text("header.payload.sig", encoding="utf-8")
    monkeypatch.setenv("EKS_POD_NAME", "my-pod")
    # Make from_aws(mode="auto") pick projected mode (no region/boto3 needed).
    monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN_FILE", str(f))
    identity = AgentIdentity.auto(**_FED_KWARGS)
    assert isinstance(identity.provider, AwsEksProjectedProvider)


def test_auto_dispatches_to_gcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    identity = AgentIdentity.auto(**_FED_KWARGS)
    assert isinstance(identity.provider, GcpMetadataProvider)


def test_auto_dispatches_to_spiffe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPIFFE_ENDPOINT_SOCKET", "unix:///tmp/agent.sock")
    identity = AgentIdentity.auto(**_FED_KWARGS)
    # mode="auto" + no token_file => SDK mode.
    assert isinstance(identity.provider, SpiffeSdkProvider)


def test_auto_raises_when_no_platform() -> None:
    with pytest.raises(PlatformDetectionError):
        AgentIdentity.auto(**_FED_KWARGS)
