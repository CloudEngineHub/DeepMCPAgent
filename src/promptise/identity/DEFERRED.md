# Agent Identity — follow-up log

Everything shipped is fully implemented; nothing here is a stub. The four
attribution follow-ups originally tracked here are now **all done** — the
framework is end-to-end aware of the acting agent. The three "optional
future ideas" that followed (OIDC discovery, per-resource credentials,
structured cross-agent identity) are now **also all done**. This file is
kept as a record of what landed and where.

**Verification caveat (honest):** the provider *logic* is thoroughly unit-tested
(cloud calls mocked), and resilience — retry/backoff, clock-skew leeway,
concurrency — is covered. The *live* round-trip against each real cloud is
confirmed via the opt-in, platform-gated tests in `tests/identity/integration/`
(and the OIDC path in CI), not asserted blindly. Run them in your environment
rather than reading "fully implemented" as "proven against your cloud." See
`docs/identity/security.md#verification-status`.

## 1. Audit-log enrichment in Security Guardrails  *(done)*

**Done:** `AuditMiddleware._build_entry` now records the acting agent's
verified identity — `subject` / `issuer` / `audience` / `roles` from
`ctx.client` — under an `identity` block on each tamper-evident audit
entry (inside the HMAC chain). It is populated from the validated token
(JWT / JWKS auth), so server-side audit answers *which agent did what*,
not just a `client_id` string. Only identity *descriptors* are recorded —
never the token or the full claim set, which may carry sensitive data.

## 2. Attribute event notifications to the agent, not the model  *(done)*

**Done:** `PromptiseAgent._actor()` returns the resolved attribution id
(the agent's `agent_id` / IdP subject, computed once by `build_agent`)
when an identity is attached, and the model name otherwise. Every
`emit_event(...)` call now attributes to `self._actor()`, so the events
subsystem (invocation timeout/error, etc.) names the acting agent.
Agents with no identity are unaffected (still the model name).

## 3. MCP credential presentation + server-side verification  *(done)*

**Outbound:** `build_agent(identity=…)` presents a *verifiable* identity's
credential to MCP servers that have no bearer of their own — the
credential becomes the client's `bearer_token` automatically (best-effort;
respects an explicit per-server bearer; resolved at build time, scoped to
each server's `HTTPServerSpec.audience` — see *Per-resource credentials*
below).

**Inbound:** `promptise.mcp.server.JwksAuth` verifies an agent's
IdP-issued token against the IdP's JWKS endpoint (handling key rotation)
and the existing `AuthMiddleware` surfaces the validated `sub` / claims on
`ClientContext`, so guards (`RequireClientId`, `HasRole`) and server-side
audit see *which agent* called — the same `sub`/`oid` that
`AgentIdentity.subject()` reads on the client side.

**Done (was a follow-up):** OIDC discovery —
`JwksAuth.from_discovery(issuer=…, audience=…)` derives `jwks_url` from the
issuer's `.well-known/openid-configuration` document (verifying the
document's own `issuer` matches), so you no longer have to pass the JWKS
URL explicitly.

## 4. Cross-agent identity propagation  *(done)*

**Done:** `make_cross_agent_tools(..., caller_identity=…)` (wired from
`build_agent`) announces the delegating agent's identity to the peer. The
ask and broadcast tools prepend a system message — *"Delegated by agent:
… Caller identity: {claims()}"* — so a peer knows *who is asking* and can
attribute (or authorize) the delegation. Cheap identity descriptors only;
no credential token is forwarded.

## 5. OIDC discovery for `JwksAuth`  *(done)*

**Done:** `JwksAuth.from_discovery(*, issuer, audience, …)` fetches
`{issuer}/.well-known/openid-configuration`, verifies the document's own
`issuer` matches the one supplied (no open redirect to an attacker's keys),
and resolves `jwks_uri` lazily. `audience` is **required** — there is no
fail-open default. See `tests/test_server_auth.py::TestJwksDiscovery`.

## 6. Per-resource credentials  *(done)*

**Done:** `IdentityProvider.get_credential(audience=…)` (and
`AgentIdentity.get_credential` / `auth_header`) take an optional audience.
*Active* providers that re-mint (Entra IMDS, AWS STS, GCP metadata, SPIFFE
SDK) issue a credential scoped to the requested audience; *passive*
providers (projected-token files, OIDC file/env/callable) have a fixed
audience and ignore it. Credentials are cached **per audience** (keyed by
`audience`, each entry expiring on its own `exp`). `build_agent` forwards
each MCP server's `HTTPServerSpec.audience`, so **one identity presents a
resource-scoped token to each server**.

## 7. Structured cross-agent identity  *(done)*

**Done:** the delegating agent's identity is propagated to the peer via the
`_delegation_ctx_var` contextvar set around the peer `ainvoke`, so the
peer's observability stamps `delegated_by` on every event it records during
the delegated call — a first-class field, not just the system-message hint
from item 4. Cheap identity descriptors only; no credential is forwarded.

## 8. Declarative configuration (SuperAgent + runtime manifests)  *(done)*

**Done:** identity is no longer programmatic-only. Both declarative
surfaces now carry an `identity:` block:

* **`.superagent` files** — `SuperAgentSchema.identity` (an
  `IdentityConfig`); `SuperAgentLoader.to_identity()` builds the
  `AgentIdentity` and `to_build_kwargs()` passes it to `build_agent`. The
  CLI (`promptise agent` / `serve`) inherits this for free.
* **`.agent` runtime manifests** — `AgentManifestSchema.identity` →
  `manifest_to_process_config` builds the identity onto `ProcessConfig`;
  `AgentProcess._build_agent` attributes the process to it and presents its
  per-audience credential to the MCP servers the process calls.

Construction is shared via `IdentityConfig.to_identity()`, so all surfaces
build identity identically. `${ENV_VAR}` resolution applies to every
identity field. The dict→`HTTPServerSpec` resolvers in both surfaces now
also carry `audience`, so per-resource credentials work declaratively.

## Optional future ideas

- *(none currently tracked — the subsystem is feature-complete for its
  stated scope, on both the programmatic and declarative surfaces.)*
