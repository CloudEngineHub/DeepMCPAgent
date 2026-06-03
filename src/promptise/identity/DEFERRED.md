# Agent Identity — deferred items

Everything shipped in this subsystem is fully implemented; nothing here is a
stub. This file records work intentionally left for a follow-up PR, with the
reason it was deferred and the contract a future change should honor.

## 1. Audit-log enrichment in Security Guardrails

**What:** Stamp every Guardrails audit-log entry with two fields when an
`AgentIdentity` is attached to an agent:

- `identity.provider` — `agent.identity.provider_name` (e.g. `aws-sts`)
- `identity.service_account_id` — `agent.identity.service_account_id`

**Why deferred:** the audit-log writer lives in the Guardrails subsystem
(`src/promptise/guardrails.py` / the `AuditMiddleware`), outside this
subsystem's allowed change surface. Editing it cleanly is a cross-subsystem
change that should be owned/reviewed by Guardrails.

**Contract:** the two values are already exposed as public attributes on
`AgentIdentity` (and reachable via `agent.identity`), so the follow-up is purely
additive on the writer side. Documented in
[`docs/identity/architecture.md`](../../../docs/identity/architecture.md)
under "Audit-log enrichment".

## 2. First-class Anthropic-SDK workload-identity credential

**What:** Authenticate the agent's own Anthropic LLM calls through a dedicated
Anthropic-SDK credential type instead of the current header injection.

**Why deferred:** the installed `anthropic` SDK (0.96.0) exposes no
`WorkloadIdentityCredentials` (or equivalent), and `build_agent()` constructs
models through LangChain `init_chat_model`, not a raw `anthropic.Anthropic`
client. Phase 8 therefore injects the minted bearer token at the
model-construction layer via `default_headers={"Authorization": "Bearer …"}`
(the same header shape the SDK's `auth_token` produces) — which works on the
SDK versions Promptise targets today.

**Contract:** when a future Anthropic SDK ships a workload-identity credential
(or `langchain_anthropic.ChatAnthropic` exposes `auth_token`), migrate
`_normalize_model()` in `src/promptise/agent.py` onto it. The `AgentIdentity`
public surface (`get_token`, `get_upstream_jwt`, the federation identifiers)
already provides everything such a credential needs, so the change is internal
to `build_agent()` and requires no public-API change.

## 3. Per-request token refresh for long-lived attached agents

**What:** Refresh the federated token automatically for an agent whose process
outlives the token lifetime, without rebuilding the agent.

**Why deferred:** today the token is resolved when the model is built (see
Phase 8). `AgentIdentity.get_token()` already performs the two-tier refresh, so
the cache is sound; what is missing is a hook that re-reads it per request at
the model layer. This is naturally subsumed by item 2 — an SDK credential that
calls back into the provider gets refresh for free — so it should be addressed
together with that migration rather than as a separate header-rewriting shim.
