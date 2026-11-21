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
"""
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from datasheet_mcp import server
from datasheet_mcp.tools import Deps

HOST, PORT = "127.0.0.1", 8000


def build_app(deps: Deps, allowed_hosts: list[str] | None = None) -> Starlette:
    """A new HTTP app serving search_datasheets at /mcp, calling the given deps.

    Each app can be started only once: its startup runs the SDK's session manager, which refuses a second run. So
    build a new app for each run (the Lambda handler in Layer 7 builds one per call).

    allowed_hosts: the Host header values to accept, for example ["abc123.execute-api.us-east-1.amazonaws.com"].
    None keeps the SDK default: localhost and 127.0.0.1 on any port. An empty list is refused (ValueError), so a
    blank setting cannot quietly fall back to localhost-only. When a list is given, the allowed Origins list is
    empty, so a request that carries an Origin header (a browser) gets 403; server-to-server clients send none.
    """
    if allowed_hosts is not None and not allowed_hosts:
        raise ValueError("allowed_hosts is empty: give the Host values to accept, or None for localhost only")
    security = TransportSecuritySettings(allowed_hosts=allowed_hosts) if allowed_hosts else None
    mcp = server.build_server(deps)
    return mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=security)


def main() -> None:
    import uvicorn  # here, not at the top: the Lambda (Layer 7) imports build_app and never needs uvicorn

    # Bound to localhost only: there is no login until Layer 5.
    uvicorn.run(build_app(server.real_deps()), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
