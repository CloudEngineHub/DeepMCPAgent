"""AWS IAM identity provider.

Two acquisition modes:

* **STS GetWebIdentityToken** — for Lambda, EC2, ECS, and EKS workloads
  with an IAM role. Lazily imports boto3 and calls STS to mint an OIDC
  web-identity token scoped to the Anthropic audience.
* **EKS projected token** — reads a projected federated token from
  ``$ANTHROPIC_IDENTITY_TOKEN_FILE`` (default
  ``/var/run/secrets/anthropic.com/token``).

boto3 is an *optional* dependency (``pip install promptise[identity-aws]``).
The STS provider imports it inside the acquisition method, never at
module top, so a workload that only uses the EKS-projected mode — or
only uses a different cloud — does not need boto3 installed. When the
import fails the provider raises :class:`ProviderConfigError` naming the
exact install command.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, cast

from .._core.callable_provider import CallableTokenProvider
from .._core.errors import ProviderConfigError, TokenAcquisitionError
from .._core.file_provider import FileTokenProvider
from .._internal.env import _resolve_anthropic_credentials

#: Environment variable naming the projected federated-token file.
ENV_ANTHROPIC_IDENTITY_TOKEN_FILE: str = "ANTHROPIC_IDENTITY_TOKEN_FILE"

#: Default projected-token path when the env var is unset.
DEFAULT_ANTHROPIC_IDENTITY_TOKEN_FILE: str = "/var/run/secrets/anthropic.com/token"

#: Environment variables AWS uses to expose the active region.
ENV_AWS_REGION: str = "AWS_REGION"
ENV_AWS_DEFAULT_REGION: str = "AWS_DEFAULT_REGION"

#: Default audience requested from STS.
_DEFAULT_AUDIENCE: str = "https://api.anthropic.com"

#: Default JWT signing algorithm requested from STS.
_DEFAULT_SIGNING_ALGORITHM: str = "RS256"

#: The exact install command surfaced when boto3 is missing.
_INSTALL_HINT: str = "pip install promptise[identity-aws]"


class AwsStsProvider(CallableTokenProvider):
    """AWS STS web-identity-token provider (lazily imports boto3).

    STS is regional, so a region is required. It is resolved from the
    ``region`` argument, then ``AWS_REGION``, then ``AWS_DEFAULT_REGION``.

    Args:
        region: AWS region for the STS client. Falls back to
            ``AWS_REGION`` / ``AWS_DEFAULT_REGION``.
        audience: Audience to request in the web-identity token.
            Defaults to ``https://api.anthropic.com``.
        signing_algorithm: JWT signing algorithm STS should use.
            Defaults to ``RS256``.
        federation_rule_id: See :class:`IdentityProvider`.
        organization_id: See :class:`IdentityProvider`.
        service_account_id: See :class:`IdentityProvider`.
        workspace_id: See :class:`IdentityProvider`.
        exchange_timeout: See :class:`IdentityProvider`.

    Raises:
        ProviderConfigError: At construction, if no region can be
            resolved.
    """

    def __init__(
        self,
        *,
        region: str | None = None,
        audience: str = _DEFAULT_AUDIENCE,
        signing_algorithm: str = _DEFAULT_SIGNING_ALGORITHM,
        federation_rule_id: str,
        organization_id: str,
        service_account_id: str,
        workspace_id: str | None = None,
        exchange_timeout: float = 10.0,
    ) -> None:
        resolved_region = (
            region
            or os.environ.get(ENV_AWS_REGION)
            or os.environ.get(ENV_AWS_DEFAULT_REGION)
        )
        if not resolved_region:
            raise ProviderConfigError(
                "AWS STS is regional but no region was supplied. Pass "
                "region='us-east-1' (or your region) or set AWS_REGION / "
                "AWS_DEFAULT_REGION in the environment. Most common cause: "
                "constructing the provider outside a configured AWS runtime."
            )
        self._region: str = resolved_region
        self._audience: str = audience
        self._signing_algorithm: str = signing_algorithm
        super().__init__(
            token_fn=self._fetch_sts_web_identity_token,
            provider_label="aws-sts",
            federation_rule_id=federation_rule_id,
            organization_id=organization_id,
            service_account_id=service_account_id,
            workspace_id=workspace_id,
            exchange_timeout=exchange_timeout,
        )

    def _fetch_sts_web_identity_token(self) -> str:
        """Mint an OIDC web-identity token via STS.

        Lazily imports boto3 (principle 4.2). Raises
        :class:`ProviderConfigError` when boto3 is missing and
        :class:`TokenAcquisitionError` when the STS call itself fails;
        both propagate through the base unchanged.
        """
        try:
            import boto3  # noqa: E402  -- lazy import is intentional (4.2)
        except ImportError as exc:
            raise ProviderConfigError(
                f"[aws-sts] AWS STS mode requires boto3, which is not "
                f"installed. Install it with: {_INSTALL_HINT}. A workload "
                f"that only uses the EKS-projected mode does not need boto3."
            ) from exc

        try:
            client = boto3.client("sts", region_name=self._region)
            response: dict[str, Any] = client.get_web_identity_token(
                Audience=[self._audience],
                SigningAlgorithm=self._signing_algorithm,
            )
        except Exception as exc:
            raise TokenAcquisitionError(
                f"[aws-sts] STS GetWebIdentityToken failed "
                f"({type(exc).__name__}: {exc}). Most common cause: this "
                f"workload has no IAM role attached, the role lacks "
                f"sts:GetWebIdentityToken permission, or the region "
                f"{self._region!r} is wrong."
            ) from exc

        token = response.get("WebIdentityToken")
        if not isinstance(token, str) or not token:
            raise TokenAcquisitionError(
                f"[aws-sts] STS response did not contain a WebIdentityToken "
                f"string. Most common cause: AWS changed the "
                f"GetWebIdentityToken response schema. Response keys: "
                f"{list(response.keys())}"
            )
        return token


class AwsEksProjectedProvider(FileTokenProvider):
    """AWS EKS projected-token provider.

    Reads a projected federated token from
    ``$ANTHROPIC_IDENTITY_TOKEN_FILE`` (default
    ``/var/run/secrets/anthropic.com/token``). Requires no boto3.

    Args:
        token_file: Projected-token path. Defaults to
            ``$ANTHROPIC_IDENTITY_TOKEN_FILE`` or, if unset,
            :data:`DEFAULT_ANTHROPIC_IDENTITY_TOKEN_FILE`.
        federation_rule_id: See :class:`IdentityProvider`.
        organization_id: See :class:`IdentityProvider`.
        service_account_id: See :class:`IdentityProvider`.
        workspace_id: See :class:`IdentityProvider`.
        exchange_timeout: See :class:`IdentityProvider`.
    """

    def __init__(
        self,
        *,
        token_file: str | Path | None = None,
        federation_rule_id: str,
        organization_id: str,
        service_account_id: str,
        workspace_id: str | None = None,
        exchange_timeout: float = 10.0,
    ) -> None:
        resolved_path: str | Path
        if token_file is not None:
            resolved_path = token_file
        else:
            resolved_path = os.environ.get(
                ENV_ANTHROPIC_IDENTITY_TOKEN_FILE,
                DEFAULT_ANTHROPIC_IDENTITY_TOKEN_FILE,
            )
        super().__init__(
            token_file=resolved_path,
            provider_label="aws-eks-projected",
            federation_rule_id=federation_rule_id,
            organization_id=organization_id,
            service_account_id=service_account_id,
            workspace_id=workspace_id,
            exchange_timeout=exchange_timeout,
        )


def from_aws(
    *,
    mode: Literal["auto", "sts", "projected"] = "auto",
    region: str | None = None,
    token_file: str | Path | None = None,
    audience: str = _DEFAULT_AUDIENCE,
    signing_algorithm: str = _DEFAULT_SIGNING_ALGORITHM,
    federation_rule_id: str | None = None,
    organization_id: str | None = None,
    service_account_id: str | None = None,
    workspace_id: str | None = None,
    exchange_timeout: float = 10.0,
) -> AwsStsProvider | AwsEksProjectedProvider:
    """Build an AWS IAM identity provider.

    Args:
        mode: ``"auto"`` (default) picks projected when
            ``ANTHROPIC_IDENTITY_TOKEN_FILE`` is set, otherwise STS.
            Force a mode with ``"sts"`` or ``"projected"``.
        region: AWS region for STS mode. Falls back to ``AWS_REGION`` /
            ``AWS_DEFAULT_REGION``.
        token_file: Projected-token path for projected mode. Falls back
            to ``$ANTHROPIC_IDENTITY_TOKEN_FILE``.
        audience: Audience for the STS web-identity token (STS mode).
        signing_algorithm: JWT signing algorithm for STS (STS mode).
        federation_rule_id: The ``fdrl_*`` identifier. Falls back to
            ``ANTHROPIC_FEDERATION_RULE_ID``.
        organization_id: The organization UUID. Falls back to
            ``ANTHROPIC_ORGANIZATION_ID``.
        service_account_id: The ``svac_*`` identifier. Falls back to
            ``ANTHROPIC_SERVICE_ACCOUNT_ID``.
        workspace_id: Optional ``wrkspc_*`` identifier. Falls back to
            ``ANTHROPIC_WORKSPACE_ID``.
        exchange_timeout: Seconds to wait for the Anthropic exchange.

    Returns:
        An :class:`AwsStsProvider` for STS mode or an
        :class:`AwsEksProjectedProvider` for projected mode.

    Raises:
        ProviderConfigError: If ``mode`` is not ``auto``/``sts``/
            ``projected``, if STS mode cannot resolve a region, or if a
            required federation identifier is unset.
    """
    resolved_mode = mode
    if resolved_mode == "auto":
        resolved_mode = (
            "projected"
            if os.environ.get(ENV_ANTHROPIC_IDENTITY_TOKEN_FILE)
            else "sts"
        )

    creds = _resolve_anthropic_credentials(
        federation_rule_id=federation_rule_id,
        organization_id=organization_id,
        service_account_id=service_account_id,
        workspace_id=workspace_id,
    )
    fed = cast(str, creds["federation_rule_id"])
    org = cast(str, creds["organization_id"])
    svc = cast(str, creds["service_account_id"])
    ws = creds["workspace_id"]

    if resolved_mode == "projected":
        return AwsEksProjectedProvider(
            token_file=token_file,
            federation_rule_id=fed,
            organization_id=org,
            service_account_id=svc,
            workspace_id=ws,
            exchange_timeout=exchange_timeout,
        )
    if resolved_mode == "sts":
        return AwsStsProvider(
            region=region,
            audience=audience,
            signing_algorithm=signing_algorithm,
            federation_rule_id=fed,
            organization_id=org,
            service_account_id=svc,
            workspace_id=ws,
            exchange_timeout=exchange_timeout,
        )
    raise ProviderConfigError(
        f"Unknown AWS mode {mode!r}. Valid modes are 'auto', 'sts', and "
        f"'projected'."
    )
