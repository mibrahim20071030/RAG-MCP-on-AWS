"""Project 3 Layer 5: a real Cognito login, then calls to the local MCP server with the token. Free.

First start the server with login on, in another terminal (from datasheet-rag/, with the MCP_COGNITO_* values in
.env; see runs/mcp_cognito_setup.md):
    .venv\\Scripts\\python.exe -m datasheet_mcp.app
Then:
    .venv\\Scripts\\python.exe scripts\\mcp_login.py                                     login without resource binding
    .venv\\Scripts\\python.exe scripts\\mcp_login.py --resource http://localhost:8000/mcp  ask Cognito for `aud`

What it does:
1. Makes a PKCE pair: a random secret (the verifier) and its SHA-256 hash (the challenge). The login page gets only
   the hash; the token endpoint later gets the secret, so a stolen code alone cannot be swapped for a token.
2. Opens Cognito's managed login page in the browser. After you sign in, Cognito sends the browser to
   http://localhost:8765/callback with a one-time code. A small server here catches it and checks `state`.
3. Swaps the code (plus the PKCE secret) for tokens at /oauth2/token. The access token stays in memory: it is never
   printed or written, because it works as a login for an hour.
4. Prints the token's claims (not the token), then checks the local server: no token gives 401 with
   resource_metadata; the metadata document gives 200; a search for the LM35 with the token gives 200.

The deployed server (Layer 7; runs/mcp_deploy.md), with the deployed scope:
    .venv\\Scripts\\python.exe scripts\\mcp_login.py --server https://<api-id>.execute-api.us-east-1.amazonaws.com
        --scope https://<api-id>.execute-api.us-east-1.amazonaws.com/mcp/search --deployed [--resource <url>/mcp]
--deployed adds: the search answers in under 30 s; GET /mcp gets the gateway's 404 (no route); quick calls in a
row get the readable per-user limit text by the 4th search of the minute. Each search is the same cached question.

Standard library only. The claims are printed without checking the signature; the server does the real check.
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag import config  # noqa: F401  (reads .env into the environment)

CALLBACK_HOST, CALLBACK_PORT = "localhost", 8765
CALLBACK = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"
SERVER = "http://localhost:8000"
NEW = "2026-07-28"
QUESTION = "What is the absolute maximum supply voltage of the LM35?"
LOGIN_WAIT_S = 300
GATEWAY_LIMIT_S = 30  # API Gateway's integration timeout
PER_USER_LIMIT = 3  # MCP_USER_PER_MINUTE on the deployed Lambda
LIMIT_TEXT = "Too many searches this minute"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def claims_of(token: str) -> dict:
    """The token's middle part (the claims), decoded without checking the signature."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def wait_for_code(state: str) -> str:
    """Serve http://localhost:8765/callback until the browser brings the code back."""
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            result.update({key: values[0] for key, values in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Login received. You can close this tab and go back to the terminal.")

        def log_message(self, *args):  # keep the code out of the terminal log
            pass

    server = http.server.HTTPServer((CALLBACK_HOST, CALLBACK_PORT), Handler)
    server.timeout = 5
    end = time.monotonic() + LOGIN_WAIT_S
    try:
        while not result and time.monotonic() < end:
            server.handle_request()
    finally:
        server.server_close()
    if not result:
        sys.exit(f"No login came back within {LOGIN_WAIT_S} s.")
    if "error" in result:
        sys.exit(f"Cognito refused the login: {result['error']} {result.get('error_description', '')}")
    if result.get("state") != state:
        sys.exit("The state value did not match: this callback was not from our login request.")
    if not result.get("code"):
        sys.exit("The callback came back without a code. Run the script again.")
    return result["code"]


def login(domain: str, client_id: str, scope: str, resource: str | None) -> str:
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = b64url(secrets.token_bytes(16))
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "scope": f"openid {scope}",
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    if resource:
        params["resource"] = resource
    url = f"{domain}/oauth2/authorize?{urllib.parse.urlencode(params)}"
    print("Opening the Cognito login page in your browser. If it does not open, paste this address:\n" + url)
    webbrowser.open(url)
    code = wait_for_code(state)

    body = urllib.parse.urlencode({"grant_type": "authorization_code", "client_id": client_id, "code": code,
                                   "redirect_uri": CALLBACK, "code_verifier": verifier}).encode()
    request = urllib.request.Request(f"{domain}/oauth2/token", data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=15) as reply:
            tokens = json.loads(reply.read())
    except urllib.error.HTTPError as err:
        sys.exit(f"Token endpoint answered {err.code}: {err.read().decode(errors='replace')}")
    except urllib.error.URLError as err:
        sys.exit(f"Could not reach the token endpoint ({domain}): {err.reason}")
    if "access_token" not in tokens:
        sys.exit(f"The token reply had no access_token; its keys: {sorted(tokens)}")
    return tokens["access_token"]


def post_mcp(server: str, arguments: dict, token: str | None) -> tuple[int, dict, str]:
    meta = {"io.modelcontextprotocol/protocolVersion": NEW, "io.modelcontextprotocol/clientCapabilities": {}}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search_datasheets", "arguments": arguments, "_meta": meta}}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": NEW, "Mcp-Method": "tools/call", "Mcp-Name": "search_datasheets"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return send(urllib.request.Request(f"{server}/mcp", data=json.dumps(body).encode(), headers=headers))


def send(request) -> tuple[int, dict, str]:
    try:
        with urllib.request.urlopen(request, timeout=60) as reply:
            return reply.status, dict(reply.headers), reply.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read().decode(errors="replace")
    except urllib.error.URLError as err:
        sys.exit(f"Could not reach {request.full_url}: {err.reason}. Is the server running (python -m datasheet_mcp.app)?")


def check(name: str, ok: bool, detail: str) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok


def tool_reply(status: int, text: str) -> tuple[dict, str | None]:
    """(the tool result, its error text or None) of a tools/call reply. A JSON-RPC error (a 200 with `error` and
    no `result`) is an error too."""
    reply = json.loads(text) if status == 200 else {}
    if "error" in reply:
        return {}, f"JSON-RPC error: {reply['error']}"
    result = reply.get("result", {})
    error = (result.get("content") or [{}])[0].get("text") if result.get("isError") else None
    return result, error


def deployed_checks(server: str, token: str) -> list[bool]:
    """The Layer 7 checks that only the deployed gateway can pass."""
    results = []
    status, _, text = send(urllib.request.Request(f"{server}/mcp", method="GET"))
    results.append(check("GET /mcp has no route", status == 404, f"{status}, {text[:80]}"))

    # The search in main() was the first of this minute (if the minute did not just turn over), so the limit text
    # should come by the 4th search. Up to PER_USER_LIMIT + 1 more are sent, and sending stops at the first refusal.
    outcomes = []
    for _ in range(PER_USER_LIMIT + 1):
        status, _, text = post_mcp(server, {"query": QUESTION, "datasheet": "LM35", "num_results": 3}, token)
        _, error = tool_reply(status, text)
        outcomes.append("200" if status == 200 and error is None else (f"refused: {error}" if error else str(status)))
        if outcomes[-1] != "200":
            break
    refused = outcomes[-1].startswith("refused") and LIMIT_TEXT in outcomes[-1]
    results.append(check("per-user limit", refused and all(o == "200" for o in outcomes[:-1]),
                         f"searches 2 to {len(outcomes) + 1}: {outcomes}"))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--resource", help="ask Cognito to bind the token to this URL (it then carries `aud`)")
    parser.add_argument("--server", default=SERVER, help=f"the server's base URL, without /mcp (default {SERVER})")
    parser.add_argument("--scope", help="the scope to ask for (default MCP_SCOPE, or datasheets/search)")
    parser.add_argument("--deployed", action="store_true", help="add the checks of the deployed gateway (Layer 7)")
    args = parser.parse_args()
    server = args.server.rstrip("/")

    domain = os.environ.get("MCP_COGNITO_DOMAIN", "").rstrip("/")
    client_ids = [c.strip() for c in os.environ.get("MCP_COGNITO_CLIENT_IDS", "").split(",") if c.strip()]
    scope = args.scope or os.environ.get("MCP_SCOPE") or "datasheets/search"
    if not domain or not client_ids:
        sys.exit("Set MCP_COGNITO_DOMAIN and MCP_COGNITO_CLIENT_IDS in .env (runs/mcp_cognito_setup.md).")

    token = login(domain, client_ids[0], scope, args.resource)
    claims = claims_of(token)
    print("\nAccess token claims (the token itself is not shown):")
    for key in ("iss", "client_id", "token_use", "scope", "aud", "sub"):
        print(f"  {key}: {claims.get(key, '(absent)')}")
    print(f"  exp: {claims.get('exp')} ({int(claims.get('exp', 0) - time.time())} s from now)")

    print(f"\nChecks against {server}:")
    results = []
    status, headers, _ = post_mcp(server, {"query": QUESTION, "datasheet": "LM35", "num_results": 3}, None)
    www = next((v for k, v in headers.items() if k.lower() == "www-authenticate"), "")
    results.append(check("no token", status == 401 and "resource_metadata=" in www, f"{status}, {www}"))

    status, _, text = send(urllib.request.Request(f"{server}/.well-known/oauth-protected-resource/mcp"))
    metadata = json.loads(text) if status == 200 else {}
    issuer = os.environ.get("MCP_COGNITO_ISSUER", "")
    names_us = (metadata.get("resource", "").rstrip("/") == f"{server}/mcp"
                and [a.rstrip("/") for a in metadata.get("authorization_servers", [])] == [issuer]
                and scope in metadata.get("scopes_supported", []))
    results.append(check("metadata document", status == 200 and names_us, f"{status}, {text}"))

    start = time.monotonic()
    status, headers, text = post_mcp(server, {"query": QUESTION, "datasheet": "LM35", "num_results": 3}, token)
    seconds = time.monotonic() - start
    result, error = tool_reply(status, text)
    structured = result.get("structuredContent") or {}
    pages = [p.get("page") for p in structured.get("passages", [])]
    if status != 200:
        detail = f"{status}, {next((v for k, v in headers.items() if k.lower() == 'www-authenticate'), text)}"
    elif error:
        detail = f"200 with a tool error: {error}"
    else:
        detail = f"200, count {structured.get('count')}, pages {pages}"
    results.append(check("search with the token", status == 200 and not error and bool(pages),
                         f"{detail}, {seconds:.1f} s"))
    if args.deployed:
        results.append(check("search within the gateway limit", seconds < GATEWAY_LIMIT_S, f"{seconds:.1f} s"))
        results.extend(deployed_checks(server, token))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
