# Security

Agent Identity is about knowing *who acted*. This page states what it
guarantees, what it does not, and how it handles the one secret it
touches — a verifiable identity's short-lived credential.

## Guarantees

- **Local identities hold no secrets.** An `agent_id`, name, owner, and
  labels are identifiers, not credentials.
- **Credentials live in memory only.** A verifiable identity's JWT is
  cached on the provider instance and never written to disk, never
  persisted, never serialized.
- **No credential is ever logged.** Not at `DEBUG`, not at any level.
- **`repr()` is identifier-only.** `repr(AgentIdentity(...))` shows the
  `agent_id`, name, owner, and a `verifiable` flag — never a credential.
- **`claims()` carries no token.** The dict stamped onto traces and
  audit contains only identifiers.

## Threat model

### What it protects against

- **Unattributable actions.** Without identity, a shared key or an
  anonymous process leaves no trace of which agent did what. Identity
  stamps every recorded action with the acting agent.
- **Spoofed callers (verifiable mode).** A resource that validates the
  signed credential knows the caller really is the claimed agent, rather
  than trusting a self-asserted id.
- **Credential staleness bugs.** Credentials are re-acquired before they
  expire, and file-projected tokens are re-read on every use so rotation
  is observed.

### What it does *not* protect against

- **A compromised host.** An attacker who can read process memory or a
  projected token file can impersonate the workload. That is the
  platform's trust boundary.
- **Trusting a local identity as proof.** A *local* identity is a label,
  not a credential — anything in the process can set it. Use a
  verifiable identity when a resource must *authenticate* the agent.
- **`subject()` from an untrusted credential source.** `subject()` reads
  the `sub`/`oid` claim **without verifying the credential's signature**
  (the holder trusts its own IdP-issued token; the *resource* verifies on
  presentation). It is authoritative for attribution only insofar as the
  credential source is trusted — a hand-wired `token_fn`/env var/file can
  return any subject. Cross-system trust comes from the resource verifying
  the signature (e.g. [`JwksAuth`](../mcp/server/auth-security.md#jwksauth),
  which **requires** the audience to prevent token substitution).
- **Authorization policy.** Identity establishes *who*; what that agent
  is allowed to do is enforced by MCP guards, capability policies, and
  your own checks.

## Optional dependencies

The cloud SDKs are never imported at module load. `boto3` (AWS STS) and
`pyspiffe` (SPIFFE SDK) are imported *inside* the acquisition method, so
file-based modes need neither installed. A missing SDK raises
[`ProviderConfigError`](#errors) naming the exact install command.

## Errors

All identity errors derive from `IdentityError`.

| Exception | Raised when |
| --- | --- |
| `IdentityError` | Base class for every error below. |
| `CredentialAcquisitionError` | A verifiable identity's provider could not mint a JWT (metadata unreachable, STS denied, token file missing, Workload API down). |
| `ProviderConfigError` | Misconfiguration, a missing optional SDK, an empty `agent_id`, or `get_credential()` on a local identity. |
| `PlatformDetectionError` | `AgentIdentity.auto()` found no platform marker. |

See the [API reference](../api/identity.md) for the exact classes.

## Logging

The subsystem uses one logger, `promptise.identity`, with a
`NullHandler` installed by default. Configure the parent to control the
whole subsystem:

```python
import logging

logging.getLogger("promptise.identity").setLevel(logging.INFO)
```

## Verification status

In the spirit of honest production claims, here is exactly what is verified:

- **Unit-verified (extensive):** every provider's request construction, token
  extraction, per-audience caching, refresh/expiry math, error classification,
  transient-retry behaviour, and concurrency (N threads collapse to one
  acquisition) are covered by the offline test suite. The cloud calls are mocked
  (monkeypatched boto3 / `httpx` / `pyspiffe`), so the **logic** is proven.
- **Live-smoke-tested:** the generic OIDC path is exercised end-to-end against a
  **real token** in CI (`.github/workflows/identity-integration.yml`). The other
  providers ship opt-in live smoke tests (`tests/identity/integration/`, marked
  `@pytest.mark.integration`) that run inside the actual platform (Azure IMDS,
  GCP metadata, AWS STS, a SPIRE socket) — run them in your environment to
  confirm the live round-trip before you depend on it.
- **Resilience:** active providers retry transient metadata/STS failures
  (timeout / connection / 429 / 5xx) with jittered backoff, and never retry an
  auth (4xx) failure. Server-side JWT verification tolerates a configurable
  clock-skew `leeway` (default 60s) for `exp`/`nbf`/`iat`.

The honest summary: the **library logic is thoroughly tested**; the **live cloud
round-trip is confirmable in your own environment** via the gated integration
tests rather than asserted blindly.
