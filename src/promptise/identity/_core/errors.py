"""Exception types for the ``promptise.identity`` subsystem.

All identity-related exceptions inherit from :class:`IdentityError`,
which inherits directly from :class:`Exception`. The framework's
existing :mod:`promptise.exceptions` is scoped to the SuperAgent file
loader and does not provide a base type that fits agent identity, so we
root a new hierarchy here.

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


class CredentialAcquisitionError(IdentityError):
    """A verifiable credential provider could not produce an identity JWT.

    Most common causes:

    * The platform metadata service (Azure IMDS, GCP metadata server,
      AWS STS) is unreachable from this workload.
    * The federated token file declared by an environment variable does
      not exist or is not readable.
    * A user-supplied callable raised an exception (the original
      exception is chained on ``__cause__``).
    * A required environment variable is unset.
    """


class ProviderConfigError(IdentityError):
    """A credential provider was misconfigured at construction time.

    Most common causes:

    * A required argument is missing and no environment fallback was set.
    * Conflicting arguments were supplied (for example, both
      ``token_file`` and ``token_fn`` to the OIDC credential).
    * A required optional dependency is missing — boto3 for AWS, or
      pyspiffe for SPIFFE SDK mode. The exception message names the
      exact ``pip install`` command that resolves it.
    """


class PlatformDetectionError(IdentityError):
    """:meth:`AgentIdentity.auto` could not detect a supported platform.

    Most common cause: the process is running on a host that does not
    expose a workload identity (a developer laptop, a bare VM without
    managed identity, a container without service-account token
    projection). Either construct a local :class:`AgentIdentity` (just
    an ``agent_id``) or use an explicit credential factory —
    ``from_entra``, ``from_aws``, ``from_gcp``, ``from_spiffe``,
    ``from_oidc`` — instead of ``AgentIdentity.auto()``.
    """
