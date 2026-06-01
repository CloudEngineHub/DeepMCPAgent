"""Unit tests for the AWS IAM provider and ``from_aws``.

The STS happy path monkeypatches ``boto3.client`` to return a stub
(boto3 is installed in the dev venv as the ``identity-aws`` extra).
The missing-boto3 path injects ``None`` into ``sys.modules`` so the
lazy ``import boto3`` raises ImportError — the technique the build
plan specifies. The EKS-projected path uses a real temp file. No
network access and no real credentials.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from promptise.identity import (
    AwsEksProjectedProvider,
    AwsStsProvider,
    ProviderConfigError,
    TokenAcquisitionError,
)
from promptise.identity.providers.aws import (
    ENV_ANTHROPIC_IDENTITY_TOKEN_FILE,
    from_aws,
)

FAKE_JWT: str = "header.payload.sig"

_FED_KWARGS: dict[str, str] = {
    "federation_rule_id": "fdrl_test",
    "organization_id": "org_test",
    "service_account_id": "svac_test",
}


class _FakeStsClient:
    """Minimal stand-in for a boto3 STS client."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def get_web_identity_token(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._response


def _mock_boto3_client(
    monkeypatch: pytest.MonkeyPatch, fake_client: _FakeStsClient
) -> None:
    import boto3

    def _client(service_name: str, **kwargs: Any) -> _FakeStsClient:
        assert service_name == "sts"
        return fake_client

    monkeypatch.setattr(boto3, "client", _client)


def _mock_boto3_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Setting the module to None makes `import boto3` raise ImportError.
    monkeypatch.setitem(sys.modules, "boto3", None)


def _mock_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _clear_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)


# -- STS path -------------------------------------------------------------


def test_sts_calls_get_web_identity_token_with_audience_and_algorithm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeStsClient({"WebIdentityToken": FAKE_JWT})
    _mock_boto3_client(monkeypatch, fake)
    provider = AwsStsProvider(region="us-east-1", **_FED_KWARGS)
    assert provider._acquire_upstream_jwt() == FAKE_JWT
    assert provider.provider_name == "aws-sts"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["Audience"] == ["https://api.anthropic.com"]
    assert call["SigningAlgorithm"] == "RS256"


def test_sts_custom_audience_and_algorithm(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeStsClient({"WebIdentityToken": FAKE_JWT})
    _mock_boto3_client(monkeypatch, fake)
    provider = AwsStsProvider(
        region="eu-west-1",
        audience="https://custom.example.com",
        signing_algorithm="ES256",
        **_FED_KWARGS,
    )
    provider._acquire_upstream_jwt()
    assert fake.calls[0]["Audience"] == ["https://custom.example.com"]
    assert fake.calls[0]["SigningAlgorithm"] == "ES256"


def test_sts_missing_web_identity_token_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeStsClient({"SomethingElse": "x"})
    _mock_boto3_client(monkeypatch, fake)
    provider = AwsStsProvider(region="us-east-1", **_FED_KWARGS)
    with pytest.raises(TokenAcquisitionError, match="did not contain a WebIdentityToken"):
        provider._acquire_upstream_jwt()


def test_sts_client_error_raises_token_acquisition_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import boto3

    def _client(service_name: str, **kwargs: Any) -> Any:
        raise RuntimeError("simulated STS AccessDenied")

    monkeypatch.setattr(boto3, "client", _client)
    provider = AwsStsProvider(region="us-east-1", **_FED_KWARGS)
    with pytest.raises(TokenAcquisitionError, match="STS GetWebIdentityToken failed"):
        provider._acquire_upstream_jwt()


def test_sts_missing_boto3_raises_provider_config_error_with_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build-plan phase-4 acceptance: the boto3-not-installed error names
    the exact `pip install promptise[identity-aws]` command, and it
    surfaces as ProviderConfigError (not wrapped in TokenAcquisitionError)."""
    _mock_boto3_missing(monkeypatch)
    provider = AwsStsProvider(region="us-east-1", **_FED_KWARGS)
    with pytest.raises(ProviderConfigError) as exc_info:
        provider._acquire_upstream_jwt()
    assert "pip install promptise[identity-aws]" in str(exc_info.value)


# -- Region resolution ----------------------------------------------------


def test_region_from_explicit_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_region(monkeypatch)
    provider = AwsStsProvider(region="ap-southeast-2", **_FED_KWARGS)
    assert provider._region == "ap-southeast-2"


def test_region_from_aws_region_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_region(monkeypatch)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    provider = AwsStsProvider(**_FED_KWARGS)
    assert provider._region == "us-west-2"


def test_region_from_aws_default_region_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_region(monkeypatch)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    provider = AwsStsProvider(**_FED_KWARGS)
    assert provider._region == "us-east-2"


def test_no_region_raises_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_region(monkeypatch)
    with pytest.raises(ProviderConfigError, match="AWS STS is regional"):
        AwsStsProvider(**_FED_KWARGS)


# -- EKS projected path ---------------------------------------------------


def test_eks_projected_reads_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    monkeypatch.setenv(ENV_ANTHROPIC_IDENTITY_TOKEN_FILE, str(f))
    provider = AwsEksProjectedProvider(**_FED_KWARGS)
    assert provider.token_file == f
    assert provider._acquire_upstream_jwt() == FAKE_JWT
    assert provider.provider_name == "aws-eks-projected"


def test_eks_projected_explicit_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("env.jwt", encoding="utf-8")
    explicit = tmp_path / "explicit"
    explicit.write_text("explicit.jwt", encoding="utf-8")
    monkeypatch.setenv(ENV_ANTHROPIC_IDENTITY_TOKEN_FILE, str(env_file))
    provider = AwsEksProjectedProvider(token_file=explicit, **_FED_KWARGS)
    assert provider._acquire_upstream_jwt() == "explicit.jwt"


def test_eks_projected_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_ANTHROPIC_IDENTITY_TOKEN_FILE, raising=False)
    provider = AwsEksProjectedProvider(**_FED_KWARGS)
    assert str(provider.token_file).endswith("anthropic.com/token")


# -- Factory --------------------------------------------------------------


def test_from_aws_sts_mode_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeStsClient({"WebIdentityToken": FAKE_JWT})
    _mock_boto3_client(monkeypatch, fake)
    _mock_exchange(monkeypatch)
    provider = from_aws(mode="sts", region="us-east-1", **_FED_KWARGS)
    assert isinstance(provider, AwsStsProvider)
    assert provider.get_token() == "sk-ant-oat01-mock"


def test_from_aws_projected_mode_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    _mock_exchange(monkeypatch)
    provider = from_aws(mode="projected", token_file=f, **_FED_KWARGS)
    assert isinstance(provider, AwsEksProjectedProvider)
    assert provider.get_token() == "sk-ant-oat01-mock"


def test_from_aws_auto_picks_projected_when_token_file_env_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    monkeypatch.setenv(ENV_ANTHROPIC_IDENTITY_TOKEN_FILE, str(f))
    provider = from_aws(mode="auto", **_FED_KWARGS)
    assert isinstance(provider, AwsEksProjectedProvider)


def test_from_aws_auto_picks_sts_when_token_file_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_ANTHROPIC_IDENTITY_TOKEN_FILE, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    provider = from_aws(mode="auto", **_FED_KWARGS)
    assert isinstance(provider, AwsStsProvider)


def test_from_aws_unknown_mode_raises() -> None:
    with pytest.raises(ProviderConfigError, match="Unknown AWS mode"):
        from_aws(mode="bogus", **_FED_KWARGS)  # type: ignore[arg-type]


def test_from_aws_missing_federation_id_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Use projected mode so no region/boto3 is needed; the failure must
    # be the missing federation ID.
    f = tmp_path / "token"
    f.write_text(FAKE_JWT, encoding="utf-8")
    monkeypatch.setenv(ENV_ANTHROPIC_IDENTITY_TOKEN_FILE, str(f))
    monkeypatch.delenv("ANTHROPIC_FEDERATION_RULE_ID", raising=False)
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org_env")
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_env")
    with pytest.raises(ProviderConfigError, match="ANTHROPIC_FEDERATION_RULE_ID"):
        from_aws(mode="projected")
