# Agent Identity API Reference

Identity, tracing, and attribution for agents — every public class,
factory, and exception across `promptise.identity`.

For concepts and setup, start with the [Agent Identity overview](../identity/overview.md).

## Public class

### AgentIdentity

The entire user-facing surface. Construct a local identity directly, or
use a credential factory (`from_entra`, `from_aws`, `from_gcp`,
`from_spiffe`, `from_oidc`, or `auto`) to make it verifiable. Pass it to
`build_agent(identity=...)` to attribute the agent's actions to it.

::: promptise.identity.AgentIdentity
    options:
      show_source: false
      heading_level: 4
      members:
        - from_entra
        - from_aws
        - from_gcp
        - from_spiffe
        - from_oidc
        - auto
        - subject
        - idp_claims
        - resolve_identifier
        - claims
        - get_credential
        - auth_header
        - agent_id
        - name
        - owner
        - labels
        - is_verifiable
        - credential_provider
        - credential

---

## Credential providers

The verifiable backing of an identity. Use the factory classmethods on
`AgentIdentity`; these classes are exported for advanced use.

### IdentityProvider

::: promptise.identity.IdentityProvider
    options:
      show_source: false
      heading_level: 4
      members:
        - get_credential
        - auth_header
        - provider_name

### FileTokenProvider

::: promptise.identity.FileTokenProvider
    options:
      show_source: false
      heading_level: 4

### CallableTokenProvider

::: promptise.identity.CallableTokenProvider
    options:
      show_source: false
      heading_level: 4

### Concrete providers

::: promptise.identity.EntraManagedIdentityProvider
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.EntraProjectedTokenProvider
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.AwsStsProvider
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.AwsEksProjectedProvider
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.GcpMetadataProvider
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.SpiffeFileProvider
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.SpiffeSdkProvider
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.OidcFileProvider
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.OidcCallableProvider
    options:
      show_source: false
      heading_level: 4

---

## Credential cache

### CachedCredential

::: promptise.identity.CachedCredential
    options:
      show_source: false
      heading_level: 4

### decode_jwt_claims

::: promptise.identity.decode_jwt_claims
    options:
      show_source: false
      heading_level: 4

### decode_jwt_expiry

::: promptise.identity.decode_jwt_expiry
    options:
      show_source: false
      heading_level: 4

---

## Exceptions

All derive from `IdentityError`.

::: promptise.identity.IdentityError
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.CredentialAcquisitionError
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.ProviderConfigError
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.PlatformDetectionError
    options:
      show_source: false
      heading_level: 4
