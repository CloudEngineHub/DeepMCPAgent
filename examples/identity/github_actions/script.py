"""GitHub Actions — a verifiable agent identity from the OIDC runner token.

Demonstrates:
- AgentIdentity.from_oidc() giving the agent a verifiable identity, backed
  by the GitHub Actions OIDC token the workflow exported
- get_credential() producing the signed credential the agent would present
  to an MCP server / API so it can verify and attribute the caller

This is the example exercised by .github/workflows/identity-integration.yml
on every pull request. It needs no Anthropic federation rule and no model
key — it only proves the agent's identity and credential.

Run (inside a GitHub Actions job that set GITHUB_OIDC_TOKEN):
    python examples/identity/github_actions/script.py
"""

from __future__ import annotations

import os
import sys

from promptise.identity import AgentIdentity, IdentityError


def main() -> int:
    token = os.environ.get("GITHUB_OIDC_TOKEN")
    if not token:
        print("GITHUB_OIDC_TOKEN is not set — run this inside the workflow.")
        return 2

    identity = AgentIdentity.from_oidc(
        "ci-release-bot",
        issuer="https://token.actions.githubusercontent.com",
        name="CI Release Bot",
        owner="platform-team",
        token_env_var="GITHUB_OIDC_TOKEN",
    )
    print(f"[identity] claims={identity.claims()}")

    try:
        credential = identity.get_credential()
    except IdentityError as exc:
        print(f"[identity] could not produce a credential: {exc}")
        return 1

    # The credential is the runner's OIDC JWT — what the agent would present
    # to a resource (e.g. an MCP server's bearer_token) for verification.
    assert credential == token
    print("[identity] verifiable credential produced — agent can prove who it is")
    return 0


if __name__ == "__main__":
    sys.exit(main())
