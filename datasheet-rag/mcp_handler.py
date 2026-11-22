"""The Lambda entry point of the MCP server (Project 3 Layer 7): function `datasheet-mcp`, handler
`mcp_handler.lambda_handler`, behind the HTTP API routes POST /mcp and GET /.well-known/oauth-protected-resource/mcp.

Mangum turns the API Gateway event (HTTP API, payload 2.0) into an ASGI request for the app from
datasheet_mcp.app.build_app, and the app's ASGI reply back into the response API Gateway expects.

- The setup (settings, the token verifier, the deps) is made once per Lambda instance and kept, so the verifier's
  cache of Cognito's keys lasts between calls. Missing login settings refuse every call: the public server never
  runs with login off.
- The app is built on every call. An app can be started only once (the SDK's session manager refuses a second run),
  and Mangum runs the app's startup and shutdown around each call (lifespan="auto").
- One URL setting, MCP_PUBLIC_URL, gives the metadata document's URL and the one Host header accepted.
- Each call prints a JSON start line (request id, the MCP headers, whether an Origin header came, the user agent,
  Accept) and an end line (the same, plus status and time), so a call the Lambda timeout kills still shows up. No
  token, query or body is logged; from the body only the JSON-RPC method name is read, and for an `initialize`
  request the protocol version and the client's name and version (Layer 8: which protocol era Claude uses).
"""
import base64
import binascii
import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from mangum import Mangum

from datasheet_mcp import server, tools
from datasheet_mcp.app import build_app, login_from_env
from datasheet_mcp.tools import Deps

# The Lambda runtime's root logger prints to CloudWatch; without this, this package's INFO lines (token refusals,
# limit refusals) would be dropped at the default WARNING level.
logging.getLogger("datasheet_mcp").setLevel(logging.INFO)

MCP_PATH = "/mcp"
LOG_TEXT_MAX = 100  # characters kept of a logged header value
METHOD_MAX = 64  # a JSON-RPC method name longer than this is not logged


@dataclass(frozen=True)
class Setup:
    """What every call of one Lambda instance shares: the deps, the build_app login arguments, the Host list."""
    deps: Deps
    login: dict
    allowed_hosts: list[str]


def load_setup(environ: Mapping[str, str] = os.environ, deps: Deps | None = None) -> Setup:
    """The setup from environment settings (see datasheet_mcp.app.login_from_env). `deps` replaces the real ones
    (the database, retrieval and limits, with the DEADLINE_S budget) in tests.

    Refuses (RuntimeError) a missing login setting, and an MCP_PUBLIC_URL that is not https://<host>/mcp: the
    default is localhost, which would give a metadata document pointing at localhost and refuse every real Host."""
    login = login_from_env(environ)
    if login is None:
        raise RuntimeError("MCP_COGNITO_ISSUER is not set: the deployed MCP server must have login on")
    url = urlparse(login["public_url"])
    # The host must be written exactly as API Gateway sends the Host header: lower case, no port, no user info.
    # Otherwise the Host check would refuse every call with 421.
    if (url.scheme != "https" or not url.hostname or url.netloc != url.hostname or url.path != MCP_PATH
            or url.query or url.params or url.fragment):
        raise RuntimeError(f"MCP_PUBLIC_URL must be https://<API host, lower case, no port>{MCP_PATH}, "
                           f"got {login['public_url']!r}")
    deps = deps or server.real_deps(budget_s=tools.DEADLINE_S)
    return Setup(deps=deps, login=login, allowed_hosts=[url.hostname])


_setup: Setup | None = None


def _shared_setup() -> Setup:
    """The setup of this Lambda instance, made on its first call. A failure is raised again on every call."""
    global _setup
    if _setup is None:
        _setup = load_setup()
    return _setup


def lambda_handler(event, context, setup: Setup | None = None) -> dict:
    """One API Gateway request. `setup` replaces this instance's setup in tests."""
    start = time.perf_counter()
    response = None
    try:
        line = request_summary(event if isinstance(event, dict) else {})
    except Exception as error:  # noqa: BLE001 (the summary is best effort; the reply comes first)
        line = {"summary_error": type(error).__name__}
    request_id = getattr(context, "aws_request_id", None)
    line = {"request_id": request_id if isinstance(request_id, str) else None, **line}
    # A start line too: a request the Lambda timeout kills never reaches the end line (Layer 8 saw one hang 60 s).
    print(json.dumps({"phase": "start", **line}), flush=True)
    try:
        setup = setup or _shared_setup()
        app = build_app(setup.deps, setup.allowed_hosts, **setup.login)
        response = Mangum(app, lifespan="auto")(event, context)
        return response
    finally:
        # Printed on failures too (status null), so every call leaves an end line in CloudWatch. Never raises: an
        # error here would replace the response, or the real error.
        status = response.get("statusCode") if isinstance(response, dict) else None
        total_ms = round((time.perf_counter() - start) * 1000, 1)
        print(json.dumps({"phase": "end", **line, "status": status, "total_ms": total_ms}), flush=True)


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def request_summary(event: dict) -> dict:
    """The parts of a request that are logged: no token, query or body text."""
    http = _mapping(_mapping(event.get("requestContext")).get("http"))
    headers = {str(name).lower(): str(value) for name, value in _mapping(event.get("headers")).items()}
    message = _body_message(event)
    method = _rpc_method(message)
    line = {
        "method": http.get("method"),
        "path": event.get("rawPath"),
        "rpc_method": method,
        "mcp_method": headers.get("mcp-method", "")[:LOG_TEXT_MAX] or None,
        "protocol_version": headers.get("mcp-protocol-version", "")[:LOG_TEXT_MAX] or None,
        "origin_present": "origin" in headers,
        "user_agent": headers.get("user-agent", "")[:LOG_TEXT_MAX] or None,
        "accept": headers.get("accept", "")[:LOG_TEXT_MAX] or None,  # text/event-stream: the client takes a stream
    }
    if method == "initialize":
        line.update(_initialize_summary(message))
    return line


def _body_message(event: dict) -> dict | None:
    """The JSON-RPC message of the body, or None when there is none or it is not a JSON object."""
    body = event.get("body")
    if not body:
        return None
    try:
        raw = base64.b64decode(body, validate=True) if event.get("isBase64Encoded") else body
        message = json.loads(raw)
    except (binascii.Error, ValueError, TypeError, RecursionError):  # RecursionError: a deeply nested body
        return None
    return message if isinstance(message, dict) else None


def _rpc_method(message: dict | None) -> str | None:
    """The JSON-RPC `method` (for example "initialize" or "tools/call"), or None. Older-style clients send no
    Mcp-Method header, so this is how Layer 8 sees which handshake Claude uses."""
    method = message.get("method") if message else None
    return method if isinstance(method, str) and len(method) <= METHOD_MAX else None


def _short(value) -> str | None:
    return value[:LOG_TEXT_MAX] if isinstance(value, str) and value else None


def _initialize_summary(message: dict) -> dict:
    """From an older-style `initialize` request: the protocol version the client asks for, and the client's name and
    version (its clientInfo). These name the client and its protocol era; they carry no user data."""
    params = _mapping(message.get("params"))
    client = _mapping(params.get("clientInfo"))
    return {
        "init_protocol_version": _short(params.get("protocolVersion")),
        "client_name": _short(client.get("name")),
        "client_version": _short(client.get("version")),
    }
