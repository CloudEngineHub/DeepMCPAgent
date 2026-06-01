"""Callable-backed identity provider — invokes a function for the JWT.

Used by:

* AWS STS ``get_web_identity_token`` (wrapped in a callable inside
  :class:`AwsStsProvider`).
* GCP metadata server ``identity`` endpoint.
* SPIFFE Workload API via :mod:`pyspiffe`.
* Generic OIDC callable mode.

Any exception raised by the user-supplied callable is wrapped in
:class:`TokenAcquisitionError` with the provider label in the message
and the underlying exception chained on ``__cause__``.
"""

from __future__ import annotations

from collections.abc import Callable

from .errors import IdentityError, TokenAcquisitionError
from .provider import IdentityProvider


class CallableTokenProvider(IdentityProvider):
    """Invokes a user-supplied callable to acquire the upstream JWT.

    Args:
        token_fn: A zero-argument callable that returns the JWT as a
            string. Called fresh on every refresh — caching the result
            would defeat the upstream's own rotation.
        provider_label: Short string used in log messages and error
            output. Concrete provider subclasses (Entra IMDS, AWS STS,
            GCP metadata, SPIFFE SDK, generic OIDC callable) pass
            their own label.
        federation_rule_id: See :class:`IdentityProvider`.
        organization_id: See :class:`IdentityProvider`.
        service_account_id: See :class:`IdentityProvider`.
        workspace_id: See :class:`IdentityProvider`.
        exchange_timeout: See :class:`IdentityProvider`.
    """

    def __init__(
        self,
        *,
        token_fn: Callable[[], str],
        provider_label: str = "callable",
        federation_rule_id: str,
        organization_id: str,
        service_account_id: str,
        workspace_id: str | None = None,
        exchange_timeout: float = 10.0,
    ) -> None:
        super().__init__(
            federation_rule_id=federation_rule_id,
            organization_id=organization_id,
            service_account_id=service_account_id,
            workspace_id=workspace_id,
            exchange_timeout=exchange_timeout,
        )
        self._token_fn: Callable[[], str] = token_fn
        self._provider_label: str = provider_label

    @property
    def provider_name(self) -> str:
        return self._provider_label

    def _acquire_upstream_jwt(self) -> str:
        try:
            token = self._token_fn()
        except IdentityError:
            # The callable already produced a precise, typed identity
            # error — a TokenAcquisitionError from a metadata-service
            # provider, or a ProviderConfigError from a missing optional
            # dependency. Let it propagate unchanged rather than
            # wrapping it in a second, more generic error.
            raise
        except Exception as exc:
            raise TokenAcquisitionError(
                f"[{self._provider_label}] upstream JWT callable raised "
                f"{type(exc).__name__}: {exc}. Most common cause: the cloud "
                f"SDK or metadata service is unreachable from this workload."
            ) from exc
        if not isinstance(token, str):
            raise TokenAcquisitionError(
                f"[{self._provider_label}] upstream JWT callable returned "
                f"{type(token).__name__}, expected str. Fix the callable to "
                f"return the JWT as a string."
            )
        token = token.strip()
        if not token:
            raise TokenAcquisitionError(
                f"[{self._provider_label}] upstream JWT callable returned an "
                f"empty string. Most common cause: the metadata service "
                f"responded successfully but the JWT body was empty "
                f"(transient issue, or a misconfigured audience parameter)."
            )
        return token
