"""Environment-variable helpers for federation configuration.

Every provider factory needs the four Anthropic federation identifiers
(rule, organization, service account, optional workspace). They are
read from environment variables by convention, with explicit
constructor arguments overriding any environment value.

This module centralises the resolution so each factory contains
exactly one line of credential plumbing.
"""

from __future__ import annotations

import os

from .._core.errors import ProviderConfigError

#: Environment variable holding the ``fdrl_*`` federation rule ID.
ENV_FEDERATION_RULE_ID: str = "ANTHROPIC_FEDERATION_RULE_ID"

#: Environment variable holding the Anthropic organization UUID.
ENV_ORGANIZATION_ID: str = "ANTHROPIC_ORGANIZATION_ID"

#: Environment variable holding the ``svac_*`` service-account ID.
ENV_SERVICE_ACCOUNT_ID: str = "ANTHROPIC_SERVICE_ACCOUNT_ID"

#: Environment variable holding the optional ``wrkspc_*`` workspace ID.
ENV_WORKSPACE_ID: str = "ANTHROPIC_WORKSPACE_ID"


def _require_env(name: str) -> str:
    """Return the value of ``name`` or raise :class:`ProviderConfigError`.

    Args:
        name: The environment variable to read.

    Returns:
        The whitespace-stripped value.

    Raises:
        ProviderConfigError: If ``name`` is unset or contains only
            whitespace. The exception message names the variable and
            points at the matching factory argument.
    """
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ProviderConfigError(
            f"Required environment variable {name} is not set. Either set it "
            f"in the workload's environment or pass the equivalent argument "
            f"to the factory (federation_rule_id=..., organization_id=..., "
            f"service_account_id=...). Most common cause: the variable was "
            f"set in the developer's shell but not in the deployed "
            f"environment."
        )
    return value.strip()


def _resolve_anthropic_credentials(
    *,
    federation_rule_id: str | None = None,
    organization_id: str | None = None,
    service_account_id: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, str | None]:
    """Resolve the four Anthropic federation identifiers.

    Explicit overrides win. Each missing identifier (except the
    optional ``workspace_id``) is read from its environment variable
    via :func:`_require_env`, raising :class:`ProviderConfigError`
    when absent.

    Args:
        federation_rule_id: Override for
            :data:`ENV_FEDERATION_RULE_ID`.
        organization_id: Override for :data:`ENV_ORGANIZATION_ID`.
        service_account_id: Override for
            :data:`ENV_SERVICE_ACCOUNT_ID`.
        workspace_id: Override for :data:`ENV_WORKSPACE_ID`. Optional
            in every code path.

    Returns:
        A dict with keys ``federation_rule_id``, ``organization_id``,
        ``service_account_id``, ``workspace_id``. The first three are
        always non-empty strings; the fourth is ``None`` when neither
        the override nor the environment variable supplies a value.
    """
    fed = federation_rule_id if federation_rule_id else _require_env(ENV_FEDERATION_RULE_ID)
    org = organization_id if organization_id else _require_env(ENV_ORGANIZATION_ID)
    svc = service_account_id if service_account_id else _require_env(ENV_SERVICE_ACCOUNT_ID)
    if workspace_id is None:
        env_value = os.environ.get(ENV_WORKSPACE_ID)
        if env_value is not None and env_value.strip():
            workspace_id = env_value.strip()
    return {
        "federation_rule_id": fed,
        "organization_id": org,
        "service_account_id": svc,
        "workspace_id": workspace_id,
    }
