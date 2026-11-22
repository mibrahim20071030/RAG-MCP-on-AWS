"""Project 3 Layer 7 tests for the Lambda entry point (mcp_handler.py).

The events are shaped like API Gateway HTTP API (payload 2.0) events and go through the real Mangum and the real
app. Tokens are signed with an RSA key made here, and the verifier's key lookup is a fake, as in test_mcp_auth.py.
No AWS, no database, no network."""
import base64
import json
import logging
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import mcp_handler
from datasheet_mcp import tools
from datasheet_mcp.auth import CognitoVerifier
from datasheet_mcp.tools import Deps
from mcp_handler import Setup, lambda_handler, load_setup

HOST = "abc123.execute-api.us-east-1.amazonaws.com"
URL = f"https://{HOST}/mcp"
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"
CLIENT = "test-client"
SCOPE = f"{URL}/search"
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
DOCUMENTS = {"lm35": {"part": "LM35", "revision": "TEST-REV-A"}}
HITS = [{"document": "lm35", "page": 4, "page_label": "3", "section": "6.1", "text": "Supply voltage: MAX: 35 V",
         "score": 0.91}]
ARGS = {"query": "max supply voltage", "datasheet": "LM35", "num_results": 1}
ENV = {"MCP_COGNITO_ISSUER": ISSUER, "MCP_COGNITO_CLIENT_IDS": CLIENT, "MCP_SCOPE": SCOPE, "MCP_PUBLIC_URL": URL}


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token_for(key, **overrides):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": "user-1", "client_id": CLIENT, "token_use": "access", "scope": f"openid {SCOPE}",
              "exp": now + 3600, "iat": now, "jti": str(uuid.uuid4()), **overrides}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class Fakes:
    def __init__(self):
        self.calls = 0

    def retrieve(self, conn, query, document=None, k=None, rerank_client=None):
        self.calls += 1
        return {"passages": HITS[:k], "reranked": True}

    def deps(self):
        return Deps(connect=FakeConnection, retrieve=self.retrieve, documents=DOCUMENTS)


@pytest.fixture
def fakes():
    return Fakes()


@pytest.fixture
def setup(key, fakes):
    public = key.public_key()
    verifier = CognitoVerifier(ISSUER, [CLIENT], SCOPE, URL, key_for=lambda token: public)
    login = {"verifier": verifier, "public_url": URL, "issuer": ISSUER, "scope": SCOPE}
    return Setup(deps=fakes.deps(), login=login, allowed_hosts=[HOST])


def event(method="POST", path="/mcp", headers=None, body=None, base64_body=False):
    """An HTTP API payload 2.0 event, as API Gateway sends it (header names lower case)."""
    all_headers = {"host": HOST, "content-type": "application/json", "accept": "application/json, text/event-stream",
                   "user-agent": "test-agent", **(headers or {})}
    text = json.dumps(body) if body is not None else None
    if text is not None and base64_body:
        text = base64.b64encode(text.encode()).decode()
    return {
        "version": "2.0", "routeKey": f"{method} {path}", "rawPath": path, "rawQueryString": "",
        "headers": all_headers,
        "requestContext": {
            "accountId": "123456789012", "apiId": "abc123", "domainName": HOST, "domainPrefix": "abc123",
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1", "sourceIp": "192.0.2.1",
                     "userAgent": all_headers["user-agent"]},
            "requestId": "req-1", "routeKey": f"{method} {path}", "stage": "$default", "time": "21/Nov/2026:12:00:00 +0000",
            "timeEpoch": 1_795_000_000_000,
        },
        "body": text, "isBase64Encoded": base64_body,
    }


def call_body(arguments=ARGS):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_datasheets",
                                                                          "arguments": arguments}}


def bearer(token):
    return {"authorization": f"Bearer {token}"}


def reply_json(response):
    body = response["body"]
    if response.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    return json.loads(body)


def header(response, name):
    return {key.lower(): value for key, value in response["headers"].items()}.get(name)


# Requests through Mangum


def test_no_token_is_401_with_resource_metadata(setup, fakes):
    response = lambda_handler(event(body=call_body()), None, setup=setup)
    assert response["statusCode"] == 401
    challenge = header(response, "www-authenticate")
    assert "resource_metadata=" in challenge and f"https://{HOST}{METADATA_PATH}" in challenge
    assert fakes.calls == 0


def test_metadata_document_names_the_deployed_url(setup):
    response = lambda_handler(event("GET", METADATA_PATH), None, setup=setup)
    assert response["statusCode"] == 200
    document = reply_json(response)
    assert document["resource"].rstrip("/") == URL
    assert [server.rstrip("/") for server in document["authorization_servers"]] == [ISSUER]
    assert document["scopes_supported"] == [SCOPE]


def test_valid_token_gets_the_tool_result(setup, fakes, key):
    response = lambda_handler(event(headers=bearer(token_for(key)), body=call_body()), None, setup=setup)
    assert response["statusCode"] == 200
    result = reply_json(response)["result"]
    assert result.get("isError") is not True
    assert result["structuredContent"]["passages"][0]["page"] == 4
    assert fakes.calls == 1


def test_a_base64_body_is_read(setup, fakes, key):
    response = lambda_handler(event(headers=bearer(token_for(key)), body=call_body(), base64_body=True), None,
                              setup=setup)
    assert response["statusCode"] == 200
    assert fakes.calls == 1


def test_calls_in_a_row_each_get_a_new_app(setup, fakes, key):
    # An app can start only once, so a second call on the same instance would fail if the app were reused.
    for _ in range(3):
        response = lambda_handler(event(headers=bearer(token_for(key)), body=call_body()), None, setup=setup)
        assert response["statusCode"] == 200
    assert fakes.calls == 3


def test_a_wrong_host_is_421(setup, fakes, key):
    response = lambda_handler(event(headers={"host": "evil.example", **bearer(token_for(key))}, body=call_body()),
                              None, setup=setup)
    assert response["statusCode"] == 421
    assert fakes.calls == 0


def test_an_origin_header_is_403(setup, fakes, key):
    response = lambda_handler(event(headers={"origin": "https://evil.example", **bearer(token_for(key))},
                                    body=call_body()), None, setup=setup)
    assert response["statusCode"] == 403
    assert fakes.calls == 0


def test_a_bad_input_reaches_the_client_as_a_tool_error(setup, fakes, key):
    response = lambda_handler(event(headers=bearer(token_for(key)), body=call_body({**ARGS, "num_results": 11})),
                              None, setup=setup)
    assert response["statusCode"] == 200
    assert reply_json(response)["result"]["isError"] is True
    assert fakes.calls == 0


# The log line


def last_log_line(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_each_call_prints_one_json_line_without_the_token(setup, key, capsys):
    # A 2026-07-28 style request: the version header, Mcp-Method and Mcp-Name, and `_meta` in the body.
    token = token_for(key)
    headers = {**bearer(token), "mcp-method": "tools/call", "mcp-name": "search_datasheets",
               "mcp-protocol-version": "2026-07-28"}
    body = call_body()
    body["params"]["_meta"] = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                               "io.modelcontextprotocol/clientCapabilities": {}}
    lambda_handler(event(headers=headers, body=body), None, setup=setup)
    output = capsys.readouterr().out
    assert token not in output and "max supply voltage" not in output
    line = json.loads(output.strip().splitlines()[-1])
    assert line["status"] == 200
    assert line["method"] == "POST" and line["path"] == "/mcp"
    assert line["rpc_method"] == "tools/call"
    assert line["mcp_method"] == "tools/call"
    assert line["protocol_version"] == "2026-07-28"
    assert line["origin_present"] is False
    assert line["user_agent"] == "test-agent"
    assert line["total_ms"] >= 0


def test_the_log_line_shows_an_origin_and_an_initialize(setup, capsys):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    lambda_handler(event(headers={"origin": "https://claude.ai"}, body=body), None, setup=setup)
    line = last_log_line(capsys)
    assert line["origin_present"] is True
    assert line["rpc_method"] == "initialize"
    assert line["mcp_method"] is None and line["protocol_version"] is None


@pytest.mark.parametrize("body", ["not json", "[1, 2]", json.dumps({"method": "x" * 65}), json.dumps({"method": 3})])
def test_an_unreadable_or_odd_body_logs_no_method(body):
    assert mcp_handler.request_summary({**event(), "body": body})["rpc_method"] is None


def test_a_base64_body_logs_its_method():
    assert mcp_handler.request_summary(event(body=call_body(), base64_body=True))["rpc_method"] == "tools/call"


def test_an_initialize_logs_the_protocol_version_and_client():
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "claude-ai", "version": "0.1.0"}}}
    summary = mcp_handler.request_summary(event(body=body))
    assert summary["init_protocol_version"] == "2025-11-25"
    assert summary["client_name"] == "claude-ai" and summary["client_version"] == "0.1.0"


def test_initialize_fields_are_cut_and_odd_values_dropped():
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "v" * 500, "clientInfo": {"name": 7, "version": ""}}}
    summary = mcp_handler.request_summary(event(body=body))
    assert summary["init_protocol_version"] == "v" * mcp_handler.LOG_TEXT_MAX
    assert summary["client_name"] is None and summary["client_version"] is None


@pytest.mark.parametrize("params", [None, "text", {"clientInfo": "text"}])
def test_an_initialize_with_odd_params_still_logs(params):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
    summary = mcp_handler.request_summary(event(body=body))
    assert summary["rpc_method"] == "initialize"
    assert summary["init_protocol_version"] is None and summary["client_name"] is None


def test_the_printed_line_carries_the_initialize_fields(setup, capsys):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-ai", "version": "0.1.0"}}}
    for base64_body in (False, True):
        lambda_handler(event(body=body, base64_body=base64_body), None, setup=setup)
        line = last_log_line(capsys)
        assert line["init_protocol_version"] == "2025-06-18"
        assert line["client_name"] == "claude-ai" and line["client_version"] == "0.1.0"


def test_only_the_three_initialize_fields_are_added():
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {"q": "query text"},
                       "clientInfo": {"name": "a", "version": "1", "secret": "hidden-value"},
                       "access_token": "token-like-value"}}
    summary = mcp_handler.request_summary(event(body=body))
    base = set(mcp_handler.request_summary(event(body=call_body())))
    assert set(summary) == base | {"init_protocol_version", "client_name", "client_version"}
    text = json.dumps(summary)
    assert "hidden-value" not in text and "query text" not in text and "token-like-value" not in text


class Context:
    aws_request_id = "req-123"


def log_lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.strip().splitlines() if line.startswith("{")]


def test_a_start_line_comes_before_the_app_runs(setup, key, capsys, monkeypatch):
    # A call the Lambda timeout kills never reaches the end line; the start line is already out.
    seen = []

    def hanging_app(app, lifespan):
        def run(event, context):
            seen.extend(log_lines(capsys))
            raise TimeoutError("stands in for the Lambda timeout")
        return run

    monkeypatch.setattr(mcp_handler, "Mangum", hanging_app)
    headers = {**bearer(token_for(key)), "accept": "application/json, text/event-stream"}
    with pytest.raises(TimeoutError):
        lambda_handler(event(headers=headers, body=call_body()), Context(), setup=setup)
    assert len(seen) == 1 and seen[0]["phase"] == "start" and seen[0]["request_id"] == "req-123"
    assert seen[0]["rpc_method"] == "tools/call" and seen[0]["accept"] == "application/json, text/event-stream"
    assert "status" not in seen[0]
    end = log_lines(capsys)[-1]
    assert end["phase"] == "end" and end["request_id"] == "req-123" and end["status"] is None


def test_a_call_prints_a_start_and_an_end_line_without_the_token(setup, key, capsys):
    token = token_for(key)
    lambda_handler(event(headers=bearer(token), body=call_body()), Context(), setup=setup)
    output = capsys.readouterr().out
    assert token not in output and "max supply voltage" not in output
    lines = [json.loads(line) for line in output.strip().splitlines() if line.startswith('{"phase"')]
    assert [line["phase"] for line in lines] == ["start", "end"]
    assert lines[1]["status"] == 200 and lines[0]["request_id"] == lines[1]["request_id"] == "req-123"


def test_no_context_gives_no_request_id(setup, capsys):
    lambda_handler(event(body=call_body()), None, setup=setup)
    assert last_log_line(capsys)["request_id"] is None


def test_a_tool_call_has_no_initialize_fields():
    assert "client_name" not in mcp_handler.request_summary(event(body=call_body()))


def test_a_deeply_nested_body_keeps_the_real_response_and_the_log_line(setup, capsys):
    response = lambda_handler({**event(), "body": "[" * 100_000}, None, setup=setup)
    assert response["statusCode"] == 401  # the login check answers first; the log step must not replace it
    line = last_log_line(capsys)
    assert line["status"] == 401 and line["rpc_method"] is None


def test_odd_event_parts_still_give_a_log_line(setup, capsys):
    odd = {**event(body=call_body()), "requestContext": {**event()["requestContext"], "http": "not a dict"}}
    summary = mcp_handler.request_summary({**odd, "headers": ["not", "a", "dict"]})
    assert summary["method"] is None and summary["user_agent"] is None


def test_a_refused_token_is_not_in_any_log(setup, key, capsys, caplog):
    token = token_for(key, client_id="someone-else")
    with caplog.at_level(logging.INFO, logger="datasheet_mcp"):
        response = lambda_handler(event(headers=bearer(token), body=call_body()), None, setup=setup)
    assert response["statusCode"] == 401
    captured = capsys.readouterr()
    assert "client_id" in caplog.text or caplog.records  # the refusal reason is logged...
    assert token not in caplog.text and token not in captured.out and token not in captured.err  # ...not the token


def test_a_failing_setup_still_prints_the_log_line(capsys, monkeypatch):
    monkeypatch.setattr(mcp_handler, "_setup", None)
    monkeypatch.setattr(mcp_handler, "load_setup", lambda: (_ for _ in ()).throw(RuntimeError("no settings")))
    with pytest.raises(RuntimeError, match="no settings"):
        lambda_handler(event(body=call_body()), None)
    assert last_log_line(capsys)["status"] is None


# The setup


def test_load_setup_reads_the_settings(fakes):
    made = load_setup(ENV, deps=fakes.deps())
    assert made.allowed_hosts == [HOST]
    assert made.login["public_url"] == URL
    assert made.login["scope"] == SCOPE
    assert set(made.login["verifier"].clients) == {CLIENT}


def test_load_setup_refuses_login_off(fakes):
    with pytest.raises(RuntimeError, match="login on"):
        load_setup({}, deps=fakes.deps())


@pytest.mark.parametrize("url", ["", "http://abc.example/mcp", f"https://{HOST}/", f"https://{HOST}/mcp/",
                                 f"https://{HOST}/mcp?x=1", f"https://{HOST}/other", f"https://{HOST}:443/mcp",
                                 f"https://user@{HOST}/mcp", f"https://{HOST.upper()}/mcp"])
def test_load_setup_refuses_a_public_url_that_is_not_https_host_mcp(fakes, url):
    # "" falls back to the localhost default, which must not be deployed.
    with pytest.raises(RuntimeError, match="MCP_PUBLIC_URL"):
        load_setup({**ENV, "MCP_PUBLIC_URL": url}, deps=fakes.deps())


def test_the_shared_setup_is_made_once(monkeypatch, fakes):
    made = []
    monkeypatch.setattr(mcp_handler, "_setup", None)
    monkeypatch.setattr(mcp_handler, "load_setup", lambda: made.append(1) or Setup(fakes.deps(), {}, [HOST]))
    first, second = mcp_handler._shared_setup(), mcp_handler._shared_setup()
    assert first is second and made == [1]


def test_real_deps_get_the_deadline(monkeypatch):
    captured = {}
    monkeypatch.setattr(mcp_handler.server, "real_deps", lambda budget_s=None: captured.setdefault("b", budget_s))
    load_setup(ENV)
    assert captured["b"] == tools.DEADLINE_S == 25.0
