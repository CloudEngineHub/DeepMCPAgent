"""Exception types for the ``promptise.identity`` subsystem.

All identity-related exceptions inherit from :class:`IdentityError`,
which inherits directly from :class:`Exception`. The framework's
existing :mod:`promptise.exceptions` is scoped to the SuperAgent file
loader and does not provide a base type that fits federated identity,
so we root a new hierarchy here.

Every message follows the existing Promptise error style: it names the
operation that failed, explains the most common cause in one sentence,
and points at the concrete fix.
"""

from __future__ import annotations


class IdentityError(Exception):
    """Base exception for every Agent Identity failure.

    Catching :class:`IdentityError` catches every error raised by
    ``promptise.identity`` regardless of which provider produced it.
    """


class TokenAcquisitionError(IdentityError):
    """The upstream identity provider could not supply a JWT.

    Most common causes:

    * The platform metadata service (Azure IMDS, GCP metadata server,
      AWS STS) is unreachable from this workload.
    * The federated token file declared by an environment variable does
      not exist or is not readable.
    * A user-supplied callable raised an exception (the original
      exception is chained on ``__cause__``).
    * A required environment variable is unset.
    """


class TokenExchangeError(IdentityError):
    """Anthropic rejected the JWT-bearer exchange.

    Most common causes:

    * The JWT's ``iss`` claim does not match the federation issuer URL
      registered in the Anthropic Console.
    * The federation rule, organization, or service-account ID is
      wrong or has been removed.
    * The audience claim in the JWT does not include
      ``https://api.anthropic.com``.
    * The JWT expired between acquisition and exchange (clock skew).
    """


class ProviderConfigError(IdentityError):
    """An identity provider was misconfigured at construction time.

    Most common causes:

    * A required environment variable is unset and no override was
      passed to the factory.
    * Conflicting arguments were supplied (for example, both
      ``token_file`` and ``token_fn`` to :func:`from_oidc`).
    * A required optional dependency is missing — boto3 for AWS, or
      pyspiffe for SPIFFE SDK mode. The exception message names the
      exact ``pip install`` command that resolves it.
    """


class CredentialPrecedenceError(IdentityError):
    """Both an :class:`AgentIdentity` and a static API key are configured.

    The framework refuses to silently shadow one with the other. Either
    unset ``ANTHROPIC_API_KEY`` or remove the ``identity=`` argument
    from :func:`promptise.build_agent`.
    """


class PlatformDetectionError(IdentityError):
    """:meth:`AgentIdentity.auto` could not detect a supported platform.

    Most common cause: the process is running on a host that does not
    expose a workload identity (a developer laptop, a bare VM without
    managed identity, a container without service-account token
    projection). Use one of the explicit factories — ``from_entra``,
    ``from_aws``, ``from_gcp``, ``from_spiffe``, ``from_oidc`` —
    instead of ``AgentIdentity.auto()``.
    """
