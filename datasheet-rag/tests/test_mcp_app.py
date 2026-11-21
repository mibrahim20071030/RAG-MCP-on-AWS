"""Project 3 Layer 4 tests for the HTTP app (datasheet_mcp/app.py). The requests are the by-hand curl checks, sent
in-process with Starlette's TestClient: no network, no database, no AWS. The connection and the retrieval are fakes.

`with TestClient(app)` runs the app's startup (the SDK session manager) and shutdown around the requests."""
import pytest
from starlette.testclient import TestClient

from datasheet_mcp.app import build_app
from datasheet_mcp.tools import Deps

TOOL = "search_datasheets"
NEW = "2026-07-28"  # the stateless protocol revision (mcp.types.LATEST_PROTOCOL_VERSION in mcp 2.3.0)
OLD = "2025-11-25"  # an initialize-handshake revision
LOCAL = "http://127.0.0.1:8000"  # TestClient's default Host, "testserver", is not on the localhost allowed list
ACCEPT = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
DOCUMENTS = {"lm35": {"part": "LM35", "revision": "TEST-REV-A"}}
HITS = [
    {"document": "lm35", "page": 4, "page_label": "3", "section": "6.1", "text": "Supply voltage: MAX: 35 V",
     "score": 0.91},
    {"document": "lm35", "page": 5, "page_label": "5", "section": None, "text": "Operating range", "score": 0.5},
]
ARGS = {"query": "max supply voltage", "datasheet": "LM35", "num_results": 2}


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class Fakes:
    """A fake connect and retrieve; `calls` records each retrieve call."""

    def __init__(self):
        self.calls = []

    def connect(self):
        return FakeConnection()

    def retrieve(self, conn, query, document=None, k=None):
        self.calls.append({"query": query, "document": document, "k": k})
        return {"passages": HITS[:k], "reranked": True}

    def deps(self):
        return Deps(connect=self.connect, retrieve=self.retrieve, documents=DOCUMENTS)


@pytest.fixture
def fakes():
    return Fakes()


@pytest.fixture
def client(fakes):
    with TestClient(build_app(fakes.deps()), base_url=LOCAL) as c:
        yield c


def meta():
    return {"io.modelcontextprotocol/protocolVersion": NEW, "io.modelcontextprotocol/clientCapabilities": {}}


def new_style(method, params, name=None):
    """Headers and body of a 2026-07-28 style request: the version, method and name in headers and in the body."""
    headers = {**ACCEPT, "MCP-Protocol-Version": NEW, "Mcp-Method": method}
    if name is not None:
        headers["Mcp-Name"] = name
    return headers, {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": meta()}}


def call_body(arguments, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": TOOL, "arguments": arguments}}


# 2026-07-28 style


def test_new_style_call_returns_200_with_the_structured_result(client, fakes):
    headers, body = new_style("tools/call", {"name": TOOL, "arguments": ARGS}, name=TOOL)
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    result = response.json()["result"]
    assert result.get("isError") is not True
    assert result["structuredContent"]["count"] == 2
    assert result["structuredContent"]["passages"][0]["datasheet"] == "LM35"
    assert fakes.calls == [{"query": "max supply voltage", "document": "lm35", "k": 2}]


def test_new_style_tools_list_shows_the_one_tool(client):
    headers, body = new_style("tools/list", {})
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 200
    assert [tool["name"] for tool in response.json()["result"]["tools"]] == [TOOL]


def test_new_style_without_meta_is_400_invalid_params(client, fakes):
    headers, body = new_style("tools/call", {"name": TOOL, "arguments": ARGS}, name=TOOL)
    del body["params"]["_meta"]
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602
    assert fakes.calls == []


def test_name_header_not_matching_the_body_is_400_header_mismatch(client, fakes):
    headers, body = new_style("tools/call", {"name": TOOL, "arguments": ARGS}, name="some_other_tool")
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert fakes.calls == []


def test_method_header_not_matching_the_body_is_400_header_mismatch(client, fakes):
    headers, body = new_style("tools/call", {"name": TOOL, "arguments": ARGS}, name=TOOL)
    headers["Mcp-Method"] = "tools/list"
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert fakes.calls == []


def test_version_header_not_matching_meta_is_400_header_mismatch(client, fakes):
    headers, body = new_style("tools/call", {"name": TOOL, "arguments": ARGS}, name=TOOL)
    body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] = "2099-01-01"
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert fakes.calls == []


def test_unexpected_error_over_http_gives_only_the_generic_text(fakes):
    def broken_retrieve(conn, query, document=None, k=None):
        raise KeyError("secret detail")

    deps = Deps(connect=fakes.connect, retrieve=broken_retrieve, documents=DOCUMENTS)
    with TestClient(build_app(deps), base_url=LOCAL) as c:
        response = c.post("/mcp", headers=ACCEPT, json=call_body(ARGS))
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Error executing tool search_datasheets"
    assert "secret detail" not in response.text


def test_tool_error_is_a_200_reply_with_is_error_and_the_readable_text(client):
    headers, body = new_style("tools/call", {"name": TOOL, "arguments": {"query": "   "}}, name=TOOL)
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "The query is empty" in result["content"][0]["text"]


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_get_and_delete_with_the_version_header_are_405(client, method):
    response = client.request(method, "/mcp", headers={**ACCEPT, "MCP-Protocol-Version": NEW})
    assert response.status_code == 405


# Older style (no version header, or an initialize-handshake version)


def test_call_without_a_version_header_is_served_in_the_older_style(client, fakes):
    response = client.post("/mcp", headers=ACCEPT, json=call_body(ARGS))
    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"]["count"] == 2
    assert len(fakes.calls) == 1


def test_initialize_then_notification_then_call_needs_no_session(client):
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": OLD, "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}}
    response = client.post("/mcp", headers=ACCEPT, json=init)
    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == OLD
    assert "mcp-session-id" not in response.headers

    followup = {**ACCEPT, "MCP-Protocol-Version": OLD}
    response = client.post("/mcp", headers=followup, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response.status_code == 202

    response = client.post("/mcp", headers=followup, json=call_body(ARGS, request_id=2))
    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"]["count"] == 2


# Host check


def test_wrong_host_header_is_421(client, fakes):
    response = client.post("/mcp", headers={**ACCEPT, "Host": "evil.example"}, json=call_body(ARGS))
    assert response.status_code == 421
    assert fakes.calls == []


def test_allowed_hosts_replaces_the_localhost_default(fakes):
    app = build_app(fakes.deps(), allowed_hosts=["api.example.com"])
    with TestClient(app, base_url="https://api.example.com") as c:
        assert c.post("/mcp", headers=ACCEPT, json=call_body(ARGS)).status_code == 200
        assert len(fakes.calls) == 1
        response = c.post("/mcp", headers={**ACCEPT, "Host": "localhost:8000"}, json=call_body(ARGS))
        assert response.status_code == 421
        # The allowed Origins list stays empty, so a browser request (it carries Origin) is refused.
        response = c.post("/mcp", headers={**ACCEPT, "Origin": "https://evil.example"}, json=call_body(ARGS))
        assert response.status_code == 403
    assert len(fakes.calls) == 1


def test_empty_allowed_hosts_is_refused(fakes):
    with pytest.raises(ValueError, match="allowed_hosts is empty"):
        build_app(fakes.deps(), allowed_hosts=[])


# Built once per run


def test_each_build_gives_a_new_app_that_serves_requests(fakes):
    """An app starts only once (the SDK session manager refuses a second run); a fresh build_app each time works."""
    for _ in range(2):
        with TestClient(build_app(fakes.deps()), base_url=LOCAL) as c:
            assert c.post("/mcp", headers=ACCEPT, json=call_body(ARGS)).status_code == 200
    assert len(fakes.calls) == 2


def test_the_same_app_cannot_start_twice(fakes):
    """Why build_app runs per call: if this starts passing, an SDK upgrade changed it and the rule can be revisited."""
    app = build_app(fakes.deps())
    with TestClient(app, base_url=LOCAL):
        pass
    with pytest.raises(RuntimeError, match="once per instance"), TestClient(app, base_url=LOCAL):
        pass
