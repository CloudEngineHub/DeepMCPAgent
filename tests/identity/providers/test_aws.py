"""Unit tests for the AWS IAM credential provider."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from promptise.identity import (
    AwsEksProjectedProvider,
    AwsStsProvider,
    CredentialAcquisitionError,
    ProviderConfigError,
)
from promptise.identity.providers.aws import (
    ENV_PROMPTISE_IDENTITY_TOKEN_FILE,
    from_aws,
)

FAKE_JWT = "header.payload.sig"


class _FakeStsClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def get_web_identity_token(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._response


def _mock_boto3(monkeypatch: pytest.MonkeyPatch, fake: _FakeStsClient) -> None:
    import boto3

    def _client(service_name: str, **kwargs: Any) -> _FakeStsClient:
        assert service_name == "sts"
        return fake

    monkeypatch.setattr(boto3, "client", _client)


def _clear_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)


# -- STS ------------------------------------------------------------------


def test_sts_returns_web_identity_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeStsClient({"WebIdentityToken": FAKE_JWT})
    _mock_boto3(monkeypatch, fake)
    provider = AwsStsProvider(region="us-east-1", audience="api://my-mcp")
    assert provider.provider_name == "aws-sts"
    assert provider._acquire_upstream_jwt() == FAKE_JWT
    assert fake.calls[0]["Audience"] == ["api://my-mcp"]
    assert fake.calls[0]["SigningAlgorithm"] == "RS256"


def test_sts_audience_override_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeStsClient({"WebIdentityToken": FAKE_JWT})
    _mock_boto3(monkeypatch, fake)
    provider = AwsStsProvider(region="us-east-1", audience="api://default")
    provider._acquire_upstream_jwt("api://override")
    assert fake.calls[0]["Audience"] == ["api://override"]


def test_sts_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeStsClient({"Other": "x"})
    _mock_boto3(monkeypatch, fake)
    provider = AwsStsProvider(region="us-east-1")
    with pytest.raises(CredentialAcquisitionError, match="WebIdentityToken"):
        provider._acquire_upstream_jwt()


def test_sts_client_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    def _client(service_name: str, **kwargs: Any) -> Any:
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(boto3, "client", _client)
    provider = AwsStsProvider(region="us-east-1")
    with pytest.raises(CredentialAcquisitionError, match="GetWebIdentityToken failed"):
        provider._acquire_upstream_jwt()


def test_sts_missing_boto3_raises_with_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "boto3", None)
    provider = AwsStsProvider(region="us-east-1")
    with pytest.raises(ProviderConfigError) as exc:
        provider._acquire_upstream_jwt()
    assert "pip install promptise[identity-aws]" in str(exc.value)


def test_region_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_region(monkeypatch)
    assert AwsStsProvider(region="ap-southeast-2")._region == "ap-southeast-2"
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert AwsStsProvider()._region == "us-west-2"


def test_no_region_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_region(monkeypatch)
    with pytest.raises(ProviderConfigError, match="AWS STS is regional"):
        AwsStsProvider()


# -- EKS projected --------------------------------------------------------


def test_eks_reads_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    monkeypatch.setenv(ENV_PROMPTISE_IDENTITY_TOKEN_FILE, str(f))
    provider = AwsEksProjectedProvider()
    assert provider.provider_name == "aws-eks-projected"
    assert provider._acquire_upstream_jwt() == FAKE_JWT


def test_eks_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_PROMPTISE_IDENTITY_TOKEN_FILE, raising=False)
    assert str(AwsEksProjectedProvider().token_file).endswith("promptise/token")


# -- Factory --------------------------------------------------------------


def test_from_aws_auto_picks_projected_when_env_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    monkeypatch.setenv(ENV_PROMPTISE_IDENTITY_TOKEN_FILE, str(f))
    assert isinstance(from_aws(), AwsEksProjectedProvider)


def test_from_aws_auto_picks_sts_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_PROMPTISE_IDENTITY_TOKEN_FILE, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert isinstance(from_aws(), AwsStsProvider)


def test_from_aws_unknown_mode_raises() -> None:
    with pytest.raises(ProviderConfigError, match="Unknown AWS mode"):
        from_aws(mode="bogus")  # type: ignore[arg-type]
