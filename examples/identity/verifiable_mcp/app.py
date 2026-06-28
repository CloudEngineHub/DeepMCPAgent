"""Agent Identity — the headline value, end to end, on your laptop.

A *verifiable* agent identity presents a signed JWT to the resources it calls so
they can **cryptographically verify and attribute the caller** — not just trust a
self-asserted id. This script demonstrates the full trust loop with the real
APIs, with no cloud and no IdP:

    1. (stand-in for your IdP) mint a short-lived JWT for the agent, signed with
       an RSA key, and publish the matching public key at a local JWKS endpoint.
    2. AGENT side: build an AgentIdentity.from_oidc(...) — the agent presents the
       JWT as its bearer token to the MCP server.
    3. SERVER side: the MCP server's JwksAuth fetches the JWKS, verifies the
       signature + audience + expiry, and surfaces the verified subject — so the
       server knows *which agent* called and can authorize it.

In production, step 1 is your cloud/IdP (Entra, AWS, GCP, SPIFFE, any OIDC) and
the agent uses AgentIdentity.from_entra(...)/from_aws(...)/auto(); steps 2-3 are
identical. Only requirement here: `pip install promptise` (cryptography + PyJWT
are already deps).

Run:

    python examples/identity/verifiable_mcp/app.py
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from promptise import AgentIdentity
from promptise.mcp.server import JwksAuth
from promptise.mcp.server._auth import RequestContext  # the server's per-request context

AUDIENCE = "api://billing-mcp"  # the resource the MCP server represents
ISSUER = "https://demo-idp.local"
KID = "demo-key"

GREEN = "\033[32m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
RED = "\033[31m"
RESET = "\033[0m"


# ── 1. Stand-in IdP: an RSA key, a JWKS endpoint, and a token minter ─────────
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = json.loads(RSAAlgorithm.to_jwk(_KEY.public_key()))
_JWK["kid"] = KID
_JWKS = {"keys": [_JWK]}


class _JwksHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(_JWKS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence the dev server
        pass


def _serve_jwks() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _JwksHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/jwks"


def _mint_agent_token() -> str:
    """What your IdP returns to the agent — a short-lived, signed JWT."""
    now = int(time.time())
    return jwt.encode(
        {"sub": "billing-bot", "aud": AUDIENCE, "iss": ISSUER, "iat": now, "exp": now + 300},
        _KEY,
        algorithm="RS256",
        headers={"kid": KID},
    )


async def main() -> None:
    jwks_server, jwks_url = _serve_jwks()
    try:
        print(f"\n{BOLD}Verifiable agent identity → MCP server verifies the caller{RESET}\n")

        # ── 2. AGENT side ────────────────────────────────────────────────────
        # In production: AgentIdentity.from_entra(...)/from_aws(...)/auto().
        # Here: from_oidc with a token_fn standing in for the IdP.
        identity = AgentIdentity.from_oidc(
            "billing-bot",
            issuer=ISSUER,
            name="Billing Bot",
            token_fn=_mint_agent_token,
        )
        print(f"  {CYAN}agent{RESET}   is_verifiable={identity.is_verifiable}")
        header = identity.auth_header(AUDIENCE)  # the agent presents this to the server
        print(f"  {CYAN}agent{RESET}   presents Authorization: Bearer <jwt>  "
              f"{DIM}({len(header['Authorization'])} chars){RESET}")

        # ── 3. SERVER side ───────────────────────────────────────────────────
        # The MCP server is configured with JwksAuth for the resource it
        # represents. It fetches the JWKS, verifies signature+audience+expiry,
        # and surfaces the verified subject.
        auth = JwksAuth(jwks_url=jwks_url, audience=AUDIENCE, issuer=ISSUER)
        ctx = RequestContext(server_name="billing-mcp", meta={"authorization": header["Authorization"]})
        subject = await auth.authenticate(ctx)
        print(f"  {CYAN}server{RESET}  JwksAuth verified the token → subject = {BOLD}{subject}{RESET}")

        # The server now knows *which agent* called and can authorize it. In a
        # real MCPServer you wire JwksAuth + a RequireClientId / role guard and
        # the middleware enforces it per tool; here we authorize the verified
        # subject directly against an allow-list.
        allow_list = {"billing-bot", "reporting-bot"}
        print(f"  {CYAN}server{RESET}  authorize subject against {allow_list} → "
              f"{GREEN + 'allowed' + RESET if subject in allow_list else RED + 'denied' + RESET}")

        # ── Show the negative case: a forged/wrong-audience token is rejected ─
        bad = jwt.encode(
            {"sub": "evil-bot", "aud": "api://something-else", "iss": ISSUER,
             "exp": int(time.time()) + 300},
            _KEY, algorithm="RS256", headers={"kid": KID},
        )
        bad_ctx = RequestContext(server_name="billing-mcp", meta={"authorization": f"Bearer {bad}"})
        try:
            await auth.authenticate(bad_ctx)
            print(f"  {RED}server  wrong-audience token was accepted (unexpected!){RESET}")
        except Exception as exc:  # noqa: BLE001 — demo
            print(f"  {CYAN}server{RESET}  wrong-audience token {GREEN}rejected{RESET} "
                  f"{DIM}({type(exc).__name__}){RESET}")

        print(f"\n{GREEN}✓{RESET} The MCP server cryptographically verified and attributed the agent — "
              f"no shared secret, no self-asserted id.\n")
    finally:
        jwks_server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
