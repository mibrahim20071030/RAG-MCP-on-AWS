"""The MCP server over HTTP: one endpoint, POST /mcp. Run locally from datasheet-rag/:

    .venv\\Scripts\\python.exe -m datasheet_mcp.app        (serves http://127.0.0.1:8000/mcp)

Stateless: each request stands alone. The server keeps nothing between requests and sends no session id, so any
copy of the server (any Lambda instance, Layer 7) can answer any request. Replies are plain JSON, not an event stream.

SDK behavior below was read in the installed mcp 2.3.0 source (streamable_http_manager.py, _streamable_http_modern.py,
shared/inbound.py, transport_security.py) and checked with curl on 2026-11-21:
- One endpoint serves both protocol styles; the SDK picks the style from the MCP-Protocol-Version header.
  2026-07-28 style: every request carries its protocol version and client capabilities in `params._meta`, and the
  headers MCP-Protocol-Version, Mcp-Method and Mcp-Name repeat what the body says (a mismatch is refused with 400).
  Older style: `initialize` first, then the calls; with no session kept, the later calls work without it too.
- The Host header is checked against an allowed list (a wrong one gets 421). This stops a web page from reaching the
  server through a hostile domain name that points at it (DNS rebinding).
- A GET or DELETE with a 2026-07-28 version header gets 405. A GET with no version header and
  `Accept: text/event-stream` opens an event stream that stays open. The app does not refuse it; the deployed
  gateway (Layer 7) routes only POST /mcp, so such a GET never reaches the Lambda.

Login (Layer 5; read in mcp 2.3.0 server/auth/ and mcpserver/server.py, 2026-11-21), on when a verifier is given:
- Every request to /mcp needs `Authorization: Bearer <token>`. The SDK hands the token to the verifier; when it says
  no (or there is no token), the reply is 401 with `WWW-Authenticate: Bearer ... resource_metadata="<url>"`.
- The SDK serves that URL, GET /.well-known/oauth-protected-resource/mcp: a small JSON document (the protected
  resource metadata) naming this server's URL, the login server (Cognito, the issuer) and the scope. A client such
  as Claude reads it to find where to log in.
- validate_token_resource=False: the SDK's own check that a token was issued for this server's URL is off, because
  Cognito puts `aud` in an access token only with resource binding. The verifier checks `aud` itself when present.
"""
import logging
import os
import sys
from collections.abc import Mapping

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from datasheet_mcp import server
from datasheet_mcp.auth import CognitoVerifier
from datasheet_mcp.tools import Deps

HOST, PORT = "127.0.0.1", 8000
DEFAULT_SCOPE = "datasheets/search"
DEFAULT_PUBLIC_URL = "http://localhost:8000/mcp"


def build_app(deps: Deps, allowed_hosts: list[str] | None = None, *, verifier: CognitoVerifier | None = None,
              public_url: str | None = None, issuer: str | None = None, scope: str | None = None) -> Starlette:
    """A new HTTP app serving search_datasheets at /mcp, calling the given deps.

    Each app can be started only once: its startup runs the SDK's session manager, which refuses a second run. So
    build a new app for each run (the Lambda handler in Layer 7 builds one per call).

    allowed_hosts: the Host header values to accept, for example ["abc123.execute-api.us-east-1.amazonaws.com"].
    None keeps the SDK default: localhost and 127.0.0.1 on any port. An empty list is refused (ValueError), so a
    blank setting cannot quietly fall back to localhost-only. When a list is given, the allowed Origins list is
    empty, so a request that carries an Origin header (a browser) gets 403; server-to-server clients send none.

    verifier: turns login on. Then public_url (this server's /mcp URL, which must equal verifier.resource), issuer
    and scope are all required. Without a verifier none of them may be given (ValueError), so a half-set login
    configuration cannot quietly run with login off.
    """
    if allowed_hosts is not None and not allowed_hosts:
        raise ValueError("allowed_hosts is empty: give the Host values to accept, or None for localhost only")
    security = TransportSecuritySettings(allowed_hosts=allowed_hosts) if allowed_hosts else None
    mcp = server.build_server(deps, **_login_kwargs(verifier, public_url, issuer, scope))
    return mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=security)


def _login_kwargs(verifier: CognitoVerifier | None, public_url: str | None, issuer: str | None,
                  scope: str | None) -> dict:
    """The MCPServer arguments that turn login on, or {} for no login. Refuses a mixed or half-given setting."""
    if verifier is None:
        if public_url or issuer or scope:
            raise ValueError("public_url, issuer and scope need a verifier (login is off without one)")
        return {}
    if not (public_url and issuer and scope):
        raise ValueError("login needs public_url, issuer and scope")
    if verifier.resource != public_url:
        raise ValueError("verifier.resource must equal public_url (the aud check and the metadata would disagree)")
    if verifier.issuer != issuer:
        raise ValueError("verifier.issuer must equal issuer (clients would log in at one pool, tokens checked at another)")
    if verifier.scope != scope:
        raise ValueError("verifier.scope must equal scope (the SDK and the verifier would ask for different scopes)")
    auth = AuthSettings(issuer_url=issuer, resource_server_url=public_url, required_scopes=[scope],
                        validate_token_resource=False)
    return {"token_verifier": verifier, "auth": auth}


def login_from_env(environ: Mapping[str, str] = os.environ) -> dict | None:
    """The build_app login arguments from environment settings, or None when MCP_COGNITO_ISSUER is not set.

    MCP_COGNITO_CLIENT_IDS (comma-separated) is then required; MCP_SCOPE and MCP_PUBLIC_URL have defaults.
    Makes no network call: the verifier fetches Cognito's keys on the first token it checks.
    """
    issuer = environ.get("MCP_COGNITO_ISSUER", "").strip()
    if not issuer:
        return None
    clients = [c.strip() for c in environ.get("MCP_COGNITO_CLIENT_IDS", "").split(",") if c.strip()]
    if not clients:
        raise ValueError("MCP_COGNITO_ISSUER is set but MCP_COGNITO_CLIENT_IDS is empty")
    scope = environ.get("MCP_SCOPE", "").strip() or DEFAULT_SCOPE
    public_url = environ.get("MCP_PUBLIC_URL", "").strip() or DEFAULT_PUBLIC_URL
    verifier = CognitoVerifier(issuer, clients, scope, public_url)
    return {"verifier": verifier, "public_url": public_url, "issuer": issuer, "scope": scope}


def main() -> None:
    import uvicorn  # here, not at the top: the Lambda (Layer 7) imports build_app and never needs uvicorn

    # Show this package's INFO lines (for example why a token was refused); nothing else sets up logging locally.
    logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("datasheet_mcp").setLevel(logging.INFO)
    login = login_from_env()
    if login is None:
        print("Login is off (MCP_COGNITO_ISSUER not set): serving localhost only.", file=sys.stderr)
    else:
        verifier = login["verifier"]
        print(f"Login is on: issuer {login['issuer']}, URL {login['public_url']}, scope {login['scope']}, "
              f"{len(verifier.clients)} app client(s).", file=sys.stderr)
    # Bound to localhost in both cases: the public endpoint is the deployed gateway (Layer 7).
    uvicorn.run(build_app(server.real_deps(), **(login or {})), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
