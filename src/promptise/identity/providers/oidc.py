"""Generic OIDC identity provider.

For any standards-compliant OIDC issuer that Promptise does not ship a
dedicated provider for — GitLab CI, CircleCI, Azure DevOps, Keycloak,
Authentik, Dex, and others. The upstream JWT reaches the framework via
exactly one of three mutually-exclusive sources:

* a **file** on disk (``token_file``) — for systems that project the
  OIDC token to a path;
* a **callable** (``token_fn``) — for programmatic acquisition;
* an **environment variable** (``token_env_var``) — the simplest path,
  used by CI systems that expose the token directly in the environment.

The cloud providers shipped in later phases (Entra, AWS, GCP, SPIFFE)
all reduce to one of these two base mechanisms; this module is the
foundation they build on.

Example::

    from promptise.identity.providers.oidc import from_oidc

    # GitLab CI exposes its OIDC token in an environment variable.
    provider = from_oidc(
        issuer="https://gitlab.com",
        token_env_var="CI_JOB_JWT_V2",
        # federation IDs fall back to the ANTHROPIC_* env vars when
        # omitted.
    )
    access_token = provider.get_token()
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import cast

from .._core.callable_provider import CallableTokenProvider
from .._core.errors import ProviderConfigError, TokenAcquisitionError
from .._core.file_provider import FileTokenProvider
from .._internal.env import _resolve_anthropic_credentials


class OidcFileProvider(FileTokenProvider):
    """Generic OIDC provider that reads the JWT from a file path.

    A thin specialisation of :class:`FileTokenProvider` that records
    the issuer and fixes the provider label to ``"oidc:<issuer>"``.
    Construct via :func:`from_oidc` rather than directly.

    Args:
        issuer: The OIDC issuer URL — the ``iss`` claim your JWTs carry.
            Recorded for diagnostics and embedded in the provider label.
        token_file: See :class:`FileTokenProvider`.
        federation_rule_id: See :class:`FileTokenProvider`.
        organization_id: See :class:`FileTokenProvider`.
        service_account_id: See :class:`FileTokenProvider`.
        workspace_id: See :class:`FileTokenProvider`.
        exchange_timeout: See :class:`FileTokenProvider`.
    """

    def __init__(
        self,
        *,
        issuer: str,
        token_file: str | Path,
        federation_rule_id: str,
        organization_id: str,
        service_account_id: str,
        workspace_id: str | None = None,
        exchange_timeout: float = 10.0,
    ) -> None:
        super().__init__(
            token_file=token_file,
            provider_label=f"oidc:{issuer}",
            federation_rule_id=federation_rule_id,
            organization_id=organization_id,
            service_account_id=service_account_id,
            workspace_id=workspace_id,
            exchange_timeout=exchange_timeout,
        )
        self.issuer: str = issuer


class OidcCallableProvider(CallableTokenProvider):
    """Generic OIDC provider that invokes a callable for the JWT.

    A thin specialisation of :class:`CallableTokenProvider` that records
    the issuer and fixes the provider label to ``"oidc:<issuer>"``.
    Construct via :func:`from_oidc` rather than directly. Environment-
    variable mode is implemented on top of this class with a callable
    that reads the variable fresh on every refresh.

    Args:
        issuer: The OIDC issuer URL — the ``iss`` claim your JWTs carry.
        token_fn: See :class:`CallableTokenProvider`.
        federation_rule_id: See :class:`CallableTokenProvider`.
        organization_id: See :class:`CallableTokenProvider`.
        service_account_id: See :class:`CallableTokenProvider`.
        workspace_id: See :class:`CallableTokenProvider`.
        exchange_timeout: See :class:`CallableTokenProvider`.
    """

    def __init__(
        self,
        *,
        issuer: str,
        token_fn: Callable[[], str],
        federation_rule_id: str,
        organization_id: str,
        service_account_id: str,
        workspace_id: str | None = None,
        exchange_timeout: float = 10.0,
    ) -> None:
        super().__init__(
            token_fn=token_fn,
            provider_label=f"oidc:{issuer}",
            federation_rule_id=federation_rule_id,
            organization_id=organization_id,
            service_account_id=service_account_id,
            workspace_id=workspace_id,
            exchange_timeout=exchange_timeout,
        )
        self.issuer: str = issuer


def _make_env_var_reader(var_name: str) -> Callable[[], str]:
    """Return a callable that reads the JWT from ``var_name`` each call.

    The variable is read fresh on every refresh — some CI systems
    rotate the projected OIDC token in place, just as Kubernetes
    rotates projected files (build plan section 4.6).
    """

    def _read() -> str:
        value = os.environ.get(var_name)
        if value is None or not value.strip():
            raise TokenAcquisitionError(
                f"OIDC token environment variable {var_name!r} is not set "
                f"(or is empty) at refresh time. Most common cause: the CI "
                f"system that injects the OIDC token did not run for this "
                f"job, or the variable name is misspelled."
            )
        return value

    return _read


def from_oidc(
    issuer: str,
    *,
    token_file: str | Path | None = None,
    token_fn: Callable[[], str] | None = None,
    token_env_var: str | None = None,
    federation_rule_id: str | None = None,
    organization_id: str | None = None,
    service_account_id: str | None = None,
    workspace_id: str | None = None,
    exchange_timeout: float = 10.0,
) -> OidcFileProvider | OidcCallableProvider:
    """Build a generic OIDC identity provider.

    Exactly one of ``token_file``, ``token_fn``, or ``token_env_var``
    must be supplied — they are the three mutually-exclusive ways the
    issuer's JWT reaches the framework.

    Args:
        issuer: The OIDC issuer URL (the ``iss`` claim your JWTs carry).
            Used in the provider label and recorded for diagnostics.
        token_file: Path to a file the issuer projects the JWT into.
        token_fn: A zero-argument callable returning the JWT string.
        token_env_var: Name of an environment variable holding the JWT.
            Read fresh on every refresh.
        federation_rule_id: The ``fdrl_*`` identifier. Falls back to
            ``ANTHROPIC_FEDERATION_RULE_ID`` when omitted.
        organization_id: The organization UUID. Falls back to
            ``ANTHROPIC_ORGANIZATION_ID``.
        service_account_id: The ``svac_*`` identifier. Falls back to
            ``ANTHROPIC_SERVICE_ACCOUNT_ID``.
        workspace_id: Optional ``wrkspc_*`` identifier. Falls back to
            ``ANTHROPIC_WORKSPACE_ID``.
        exchange_timeout: Seconds to wait for the Anthropic exchange.

    Returns:
        An :class:`OidcFileProvider` for ``token_file`` mode, or an
        :class:`OidcCallableProvider` for ``token_fn`` and
        ``token_env_var`` modes.

    Raises:
        ProviderConfigError: If zero or more than one token source is
            supplied, or if a required federation identifier is unset.
    """
    supplied = [
        name
        for name, value in (
            ("token_file", token_file),
            ("token_fn", token_fn),
            ("token_env_var", token_env_var),
        )
        if value is not None
    ]
    if len(supplied) == 0:
        raise ProviderConfigError(
            "from_oidc requires exactly one token source but none was "
            "supplied. Pass one of token_file=..., token_fn=..., or "
            "token_env_var=.... Most common cause: the JWT source for your "
            "CI system was not wired up — e.g. GitLab CI exposes the token "
            "in CI_JOB_JWT_V2, so pass token_env_var='CI_JOB_JWT_V2'."
        )
    if len(supplied) > 1:
        raise ProviderConfigError(
            f"from_oidc requires exactly one token source but "
            f"{len(supplied)} were supplied ({', '.join(supplied)}). These "
            f"are mutually exclusive — choose the single way the issuer's "
            f"JWT reaches this workload and remove the others."
        )

    creds = _resolve_anthropic_credentials(
        federation_rule_id=federation_rule_id,
        organization_id=organization_id,
        service_account_id=service_account_id,
        workspace_id=workspace_id,
    )
    # _resolve_anthropic_credentials raises ProviderConfigError if any
    # of the three required identifiers is missing, so by this point
    # they are guaranteed non-None. The cast documents that invariant
    # for the type checker; workspace_id remains legitimately optional.
    fed = cast(str, creds["federation_rule_id"])
    org = cast(str, creds["organization_id"])
    svc = cast(str, creds["service_account_id"])
    ws = creds["workspace_id"]

    if token_file is not None:
        return OidcFileProvider(
            issuer=issuer,
            token_file=token_file,
            federation_rule_id=fed,
            organization_id=org,
            service_account_id=svc,
            workspace_id=ws,
            exchange_timeout=exchange_timeout,
        )

    resolved_fn = token_fn if token_fn is not None else _make_env_var_reader(
        cast(str, token_env_var)
    )
    return OidcCallableProvider(
        issuer=issuer,
        token_fn=resolved_fn,
        federation_rule_id=fed,
        organization_id=org,
        service_account_id=svc,
        workspace_id=ws,
        exchange_timeout=exchange_timeout,
    )
