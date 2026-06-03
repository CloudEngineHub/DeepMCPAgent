# Security

Agent Identity exists to remove static credentials from agent code. This page
states what it guarantees, what it deliberately does not, and how it handles
secrets.

## Secret-handling guarantees

- **Tokens live in memory only.** A `MintedToken` is cached on the provider
  instance and never written to disk, never serialized, and never persisted.
- **No credential is ever logged.** Not at `DEBUG`, not at any level. The
  upstream JWT and the minted `sk-ant-oat01-…` access token are never emitted
  to logs. The test suite asserts that no log record contains either token
  shape.
- **`repr()` is identifier-only.** `repr(AgentIdentity(...))` shows the provider
  name, the service account id, and the optional workspace id — never a token,
  even after one has been minted and cached.
- **Bearer, not API key.** Minted tokens are OAuth bearer tokens; they are sent
  in the `Authorization` header, never as `x-api-key`.
- **No static key shadowing.** `build_agent(identity=…)` refuses to start if
  `ANTHROPIC_API_KEY` is also set, so a stray environment key cannot silently
  override a federated identity.

## Threat model

### What the framework protects against

- **Long-lived key leakage.** There is no `ANTHROPIC_API_KEY` to commit, print,
  or copy into a ticket. Tokens are short-lived and rotated automatically.
- **Credential sprawl across tools.** MCP tools call
  `agent.identity.get_auth_header()` for a fresh bearer token instead of each
  carrying its own secret.
- **Clock-skew refresh bugs.** Expiry is tracked with `time.monotonic()`, so a
  wall-clock jump cannot cause a stale token to be treated as valid (or a valid
  one as expired).
- **Thundering-herd refresh.** A `threading.Lock` collapses concurrent
  `get_token()` callers into a single exchange.

### What it does *not* protect against

- **A compromised host.** If an attacker can read process memory or the
  platform's projected token file, they can impersonate the workload. That is
  the platform's trust boundary, not the framework's.
- **Cross-process token theft.** The cache is process-local **by design**.
  Sharing minted tokens across processes is a security problem the framework
  does not attempt to solve at this layer — each process performs its own
  exchange.
- **Misconfigured federation rules.** If the Anthropic Console rule trusts the
  wrong issuer or audience, the framework will faithfully exchange whatever the
  platform issues. Federation-rule correctness is an operator responsibility.
- **Optional-SDK supply chain.** `boto3` (AWS STS mode) and `pyspiffe` (SPIFFE
  SDK mode) are optional dependencies you opt into; their supply-chain posture
  is yours to manage.

## Optional dependencies and isolation

The cloud SDKs are never imported at module load. `boto3` and `pyspiffe` are
imported *inside* the acquisition method, so a workload that uses file-based
modes (EKS-projected, `spiffe-helper`, AKS Workload Identity) needs neither
installed. When an SDK is missing, the provider raises
[`ProviderConfigError`](#errors) naming the exact install command
(`pip install promptise[identity-aws]` or `promptise[identity-spiffe]`).

## Errors

All identity errors derive from `IdentityError`, so a single `except
IdentityError` catches the whole subsystem.

| Exception | Raised when |
| --- | --- |
| `IdentityError` | Base class for every error below. |
| `TokenAcquisitionError` | The upstream platform could not supply a JWT (metadata unreachable, STS denied, token file missing, Workload API down). |
| `TokenExchangeError` | Anthropic rejected the RFC 7523 exchange (bad rule, audience, or expired JWT). |
| `ProviderConfigError` | Misconfiguration or a missing optional SDK — names the fix in the message. |
| `CredentialPrecedenceError` | `build_agent(identity=…)` was called while `ANTHROPIC_API_KEY` is set. |
| `PlatformDetectionError` | `AgentIdentity.auto()` found no platform marker. |

Every message names the provider, the operation that failed, and the most
likely fix. See the [API reference](../api/identity.md) for the exact classes.

## Logging

The subsystem uses one logger, `promptise.identity`, with `NullHandler`
installed by default (library best practice). Per-provider sub-loggers
(`promptise.identity.aws`, …) inherit from it. Configure the parent to control
the whole subsystem:

```python
import logging

logging.getLogger("promptise.identity").setLevel(logging.INFO)
```

Levels: `INFO` for successful mints (provider, scope, `expires_in` — never the
token), `WARNING` for advisory-refresh failures (cached token still valid),
`ERROR` for mandatory-refresh and runtime configuration failures.
