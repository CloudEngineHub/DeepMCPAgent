# Agent Identity API Reference

Federated, zero-static-credential authentication — every public class, factory,
and exception across `promptise.identity`.

For concepts and setup, start with the [Agent Identity overview](../identity/overview.md).

## Public class

### AgentIdentity

The entire user-facing surface. Construct one with a factory classmethod
(`from_entra`, `from_aws`, `from_gcp`, `from_spiffe`, `from_oidc`) or
`auto()`, then call `get_token()` / `get_auth_header()` or pass it to
`build_agent(identity=...)`.

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
        - get_token
        - get_auth_header
        - get_upstream_jwt
        - provider
        - provider_name
        - federation_rule_id
        - organization_id
        - service_account_id
        - workspace_id

---

## Token model

### MintedToken

An immutable minted Anthropic access token with monotonic expiry and the
two-tier refresh helpers. The module also exposes
`ADVISORY_REFRESH_BUFFER_SECONDS` (120) and
`MANDATORY_REFRESH_BUFFER_SECONDS` (30).

::: promptise.identity.MintedToken
    options:
      show_source: false
      heading_level: 4

---

## Provider base classes

For advanced use and custom subclassing.

### IdentityProvider

::: promptise.identity.IdentityProvider
    options:
      show_source: false
      heading_level: 4
      members:
        - get_token
        - get_auth_header
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

---

## Concrete providers

### Microsoft Entra ID

::: promptise.identity.EntraManagedIdentityProvider
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.EntraProjectedTokenProvider
    options:
      show_source: false
      heading_level: 4

### AWS IAM

::: promptise.identity.AwsStsProvider
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.AwsEksProjectedProvider
    options:
      show_source: false
      heading_level: 4

### Google Cloud

::: promptise.identity.GcpMetadataProvider
    options:
      show_source: false
      heading_level: 4

### SPIFFE / SPIRE

::: promptise.identity.SpiffeFileProvider
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.SpiffeSdkProvider
    options:
      show_source: false
      heading_level: 4

### Generic OIDC

::: promptise.identity.OidcFileProvider
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.OidcCallableProvider
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

::: promptise.identity.TokenAcquisitionError
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.TokenExchangeError
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.ProviderConfigError
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.CredentialPrecedenceError
    options:
      show_source: false
      heading_level: 4

::: promptise.identity.PlatformDetectionError
    options:
      show_source: false
      heading_level: 4

---

## Internals

The exchange engine and platform detection — useful when debugging or building
a custom provider.

### exchange_jwt_for_anthropic_token

The RFC 7523 `jwt-bearer` exchange every provider funnels through.

::: promptise.identity._core.exchange.exchange_jwt_for_anthropic_token
    options:
      show_source: false
      heading_level: 4

### detect_platform

The environment-marker detection behind `AgentIdentity.auto()`.

::: promptise.identity._internal.detect.detect_platform
    options:
      show_source: false
      heading_level: 4
