"""File-backed identity provider — reads a projected JWT on every refresh.

Used by:

* Kubernetes service-account token projection.
* AKS Workload Identity (``$AZURE_FEDERATED_TOKEN_FILE``).
* SPIFFE helper output (``spiffe-helper`` writes the JWT-SVID to a
  file that the workload reads).
* Generic OIDC file mode.

The file is re-opened on every call to :meth:`_acquire_upstream_jwt`.
Kubernetes and SPIRE rotate projected tokens **in place**; caching
the contents in memory would defeat the rotation mechanism. This is
architectural, not a tunable (build plan section 4.6).
"""

from __future__ import annotations

from pathlib import Path

from .errors import TokenAcquisitionError
from .provider import IdentityProvider


class FileTokenProvider(IdentityProvider):
    """Reads the upstream JWT from a file on disk on every refresh.

    Args:
        token_file: Filesystem path to the JWT file. The framework
            re-opens this file on every refresh; the projection
            mechanism (Kubernetes, AKS, ``spiffe-helper``) keeps it
            up to date.
        provider_label: Short string used in log messages and error
            output. Concrete provider subclasses (Entra projected,
            AWS EKS projected, SPIFFE file, generic OIDC file) pass
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
        token_file: str | Path,
        provider_label: str = "file",
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
        self._token_file: Path = Path(token_file)
        self._provider_label: str = provider_label

    @property
    def provider_name(self) -> str:
        return self._provider_label

    @property
    def token_file(self) -> Path:
        """Path of the projected JWT file (read-only access)."""
        return self._token_file

    def _acquire_upstream_jwt(self) -> str:
        try:
            with self._token_file.open("r", encoding="utf-8") as fh:
                contents = fh.read()
        except FileNotFoundError as exc:
            raise TokenAcquisitionError(
                f"[{self._provider_label}] projected token file not found at "
                f"{self._token_file}. Most common cause: the workload is not "
                f"running with federated token projection enabled. Verify the "
                f"platform's projection mechanism (Kubernetes service-account "
                f"token volume, AKS Workload Identity admission webhook, or "
                f"spiffe-helper) writes to this path."
            ) from exc
        except PermissionError as exc:
            raise TokenAcquisitionError(
                f"[{self._provider_label}] projected token file at "
                f"{self._token_file} is not readable. Most common cause: the "
                f"process user does not match the file owner of the projected "
                f"volume. Underlying error: {exc}"
            ) from exc
        except OSError as exc:
            raise TokenAcquisitionError(
                f"[{self._provider_label}] could not read projected token "
                f"file at {self._token_file} ({type(exc).__name__}): {exc}"
            ) from exc

        token = contents.strip()
        if not token:
            raise TokenAcquisitionError(
                f"[{self._provider_label}] projected token file at "
                f"{self._token_file} is empty. Most common cause: the "
                f"projection volume was just created and the platform has "
                f"not yet written the first token. Wait a few seconds and "
                f"retry."
            )
        return token
