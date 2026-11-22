"""Project 3 Layer 5 tests for login (datasheet_mcp/auth.py and the login part of datasheet_mcp/app.py).

The tokens are signed in the test with an RSA key made here, and the verifier's key lookup is a fake that returns
the matching public key, so nothing reaches Cognito or the network. The HTTP tests use Starlette's TestClient with
fake deps: no database, no AWS."""
import logging
import time
import uuid
from typing import ClassVar

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.middleware.auth_context import get_access_token
from starlette.testclient import TestClient

from datasheet_mcp import auth
from datasheet_mcp.app import build_app, login_from_env
from datasheet_mcp.auth import CognitoVerifier
from datasheet_mcp.limits import Limits
from datasheet_mcp.tools import Deps
from rag import usage

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"
CLIENT = "test-client"
SCOPE = "datasheets/search"
URL = "http://localhost:8000/mcp"
BASE = "http://localhost:8000"  # localhost is on the SDK's default Host list; TestClient's "testserver" is not
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
SUB = "user-1234"
TOOL = "search_datasheets"
ACCEPT = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
DOCUMENTS = {"lm35": {"part": "LM35", "revision": "TEST-REV-A"}}
HITS = [
    {"document": "lm35", "page": 4, "page_label": "3", "section": "6.1", "text": "Supply voltage: MAX: 35 V",
     "score": 0.91},
    {"document": "lm35", "page": 5, "page_label": "5", "section": None, "text": "Operating range", "score": 0.5},
]
ARGS = {"query": "max supply voltage", "datasheet": "LM35", "num_results": 2}
REMOVE = object()  # a make_token override with this value deletes the claim


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def sign(claims, private_key):
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})


def claims_with(**overrides):
    """Claims shaped like a Cognito access token, with `overrides` applied (REMOVE deletes a claim)."""
    now = int(time.time())
    claims = {
        "iss": ISSUER, "sub": SUB, "client_id": CLIENT, "token_use": "access", "scope": f"openid {SCOPE}",
        "exp": now + 3600, "iat": now, "jti": str(uuid.uuid4()),
    }
    for name, value in overrides.items():
        if value is REMOVE:
            claims.pop(name, None)
        else:
            claims[name] = value
    return claims


@pytest.fixture
def make_token(key):
    def make(**overrides):
        return sign(claims_with(**overrides), key)
    return make


@pytest.fixture
def verifier(key):
    public = key.public_key()
    return CognitoVerifier(ISSUER, [CLIENT], SCOPE, URL, key_for=lambda token: public)


# Token table: verify_token directly


@pytest.mark.anyio
async def test_valid_token_gives_an_access_token(verifier, make_token):
    token = make_token()
    result = await verifier.verify_token(token)
    assert result is not None
    assert result.token == token
    assert result.subject == SUB
    assert result.client_id == CLIENT
    assert SCOPE in result.scopes
    assert "openid" in result.scopes
    assert isinstance(result.expires_at, int)
    assert result.expires_at > time.time()
    assert result.resource is None
    assert result.claims["iss"] == ISSUER


@pytest.mark.anyio
@pytest.mark.parametrize("aud", [URL, [URL], ["https://other.example/mcp", URL]])
async def test_aud_naming_this_server_is_accepted_and_sets_resource(verifier, make_token, aud):
    result = await verifier.verify_token(make_token(aud=aud))
    assert result is not None
    assert result.resource == URL


BAD_CLAIMS = {
    "expired": {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200},
    "wrong issuer": {"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OTHER"},
    "id token": {"token_use": "id"},
    "wrong client": {"client_id": "other-client"},
    "no scope claim": {"scope": REMOVE},
    "other scopes only": {"scope": "openid profile"},
    "scope as a prefix only": {"scope": "datasheets/search-extra"},
    "aud for another URL": {"aud": "https://other.example/mcp"},
    "aud list without this URL": {"aud": ["https://other.example/mcp"]},
    "missing sub": {"sub": REMOVE},
    "missing exp": {"exp": REMOVE},
    "missing iss": {"iss": REMOVE},
    "missing client_id": {"client_id": REMOVE},
    "missing token_use": {"token_use": REMOVE},
}


@pytest.mark.anyio
@pytest.mark.parametrize("overrides", BAD_CLAIMS.values(), ids=BAD_CLAIMS.keys())
async def test_bad_claims_give_none(verifier, make_token, overrides):
    assert await verifier.verify_token(make_token(**overrides)) is None


@pytest.mark.anyio
@pytest.mark.parametrize("garbage", ["not-a-jwt", "", "a.b.c", "Bearer x"])
async def test_garbage_text_gives_none(verifier, garbage):
    assert await verifier.verify_token(garbage) is None


@pytest.mark.anyio
async def test_token_signed_with_another_rsa_key_gives_none(verifier, other_key):
    assert await verifier.verify_token(sign(claims_with(), other_key)) is None


@pytest.mark.anyio
async def test_hs256_signed_token_gives_none(verifier):
    token = jwt.encode(claims_with(), "a-shared-secret-that-is-long-enough-for-hs256", algorithm="HS256",
                       headers={"kid": "test"})
    assert await verifier.verify_token(token) is None


@pytest.mark.anyio
async def test_unsigned_alg_none_token_gives_none(verifier):
    token = jwt.encode(claims_with(), None, algorithm="none", headers={"kid": "test"})
    assert await verifier.verify_token(token) is None


@pytest.mark.anyio
async def test_key_lookup_error_gives_none(make_token):
    def key_for(token):
        raise jwt.PyJWKClientError("no key with this kid")

    v = CognitoVerifier(ISSUER, [CLIENT], SCOPE, URL, key_for=key_for)
    assert await v.verify_token(make_token()) is None


@pytest.mark.anyio
async def test_key_lookup_connection_error_is_raised(make_token):
    def key_for(token):
        raise jwt.PyJWKClientConnectionError("cannot reach the JWKS URL")

    v = CognitoVerifier(ISSUER, [CLIENT], SCOPE, URL, key_for=key_for)
    with pytest.raises(jwt.PyJWKClientConnectionError):
        await v.verify_token(make_token())


@pytest.mark.anyio
async def test_any_of_several_allowed_clients_is_accepted(key):
    public = key.public_key()
    v = CognitoVerifier(ISSUER, ["first", CLIENT], SCOPE, URL, key_for=lambda token: public)
    assert (await v.verify_token(sign(claims_with(client_id="first"), key))).client_id == "first"
    assert (await v.verify_token(sign(claims_with(), key))).client_id == CLIENT


# Constructor checks


def test_constructor_keeps_its_settings():
    v = CognitoVerifier(ISSUER, [CLIENT, "second"], SCOPE, URL, key_for=lambda token: None)
    assert v.issuer == ISSUER
    assert v.clients == frozenset({CLIENT, "second"})
    assert isinstance(v.clients, frozenset)
    assert v.scope == SCOPE
    assert v.resource == URL


BAD_SETTINGS = {
    "blank issuer": {"issuer": "  "},
    "empty issuer": {"issuer": ""},
    "issuer with trailing slash": {"issuer": ISSUER + "/"},
    "http issuer not on localhost": {"issuer": "http://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"},
    "no clients": {"allowed_client_ids": []},
    "blank scope": {"required_scope": " "},
    "blank resource": {"resource": ""},
}


@pytest.mark.parametrize("overrides", BAD_SETTINGS.values(), ids=BAD_SETTINGS.keys())
def test_constructor_refuses_bad_settings(overrides):
    settings = {"issuer": ISSUER, "allowed_client_ids": [CLIENT], "required_scope": SCOPE, "resource": URL,
                "key_for": lambda token: None, **overrides}
    with pytest.raises(ValueError):
        CognitoVerifier(**settings)


@pytest.mark.parametrize("issuer", ["http://localhost:9000/pool", "http://127.0.0.1:9000/pool"])
def test_http_issuer_on_localhost_is_accepted(issuer):
    assert CognitoVerifier(issuer, [CLIENT], SCOPE, URL, key_for=lambda token: None).issuer == issuer


# HTTP: build_app with login on


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class Fakes:
    """A fake connect and retrieve; `calls` records each retrieve call and the token subject seen inside it."""

    def __init__(self):
        self.calls = []

    def connect(self):
        return FakeConnection()

    def retrieve(self, conn, query, document=None, k=None, rerank_client=None):
        token = get_access_token()
        self.calls.append({"query": query, "subject": token.subject if token else None})
        return {"passages": HITS[:k], "reranked": True}

    def deps(self):
        return Deps(connect=self.connect, retrieve=self.retrieve, documents=DOCUMENTS)


@pytest.fixture
def fakes():
    return Fakes()


@pytest.fixture
def client(fakes, verifier):
    app = build_app(fakes.deps(), verifier=verifier, public_url=URL, issuer=ISSUER, scope=SCOPE)
    with TestClient(app, base_url=BASE) as c:
        yield c


def call_body(arguments, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": TOOL, "arguments": arguments}}


def bearer(token):
    return {**ACCEPT, "Authorization": f"Bearer {token}"}


def test_no_token_is_401_with_resource_metadata(client, fakes):
    response = client.post("/mcp", headers=ACCEPT, json=call_body(ARGS))
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert "resource_metadata=" in challenge
    assert METADATA_PATH in challenge
    assert fakes.calls == []


def test_metadata_document_is_served(client):
    response = client.get(METADATA_PATH)
    assert response.status_code == 200
    document = response.json()
    assert document["resource"].rstrip("/") == URL
    assert [server.rstrip("/") for server in document["authorization_servers"]] == [ISSUER]
    assert document["scopes_supported"] == [SCOPE]


def test_valid_token_gets_the_tool_result(client, fakes, make_token):
    response = client.post("/mcp", headers=bearer(make_token()), json=call_body(ARGS))
    assert response.status_code == 200
    result = response.json()["result"]
    assert result.get("isError") is not True
    assert result["structuredContent"]["count"] == 2
    assert len(fakes.calls) == 1


def test_token_subject_is_visible_inside_the_tool_call(client, fakes, make_token):
    response = client.post("/mcp", headers=bearer(make_token(sub="someone-else")), json=call_body(ARGS))
    assert response.status_code == 200
    assert [call["subject"] for call in fakes.calls] == ["someone-else"]


def test_each_token_subject_gets_its_own_per_user_limit(fakes, verifier, make_token):
    """Layer 6: the per-user count is keyed by the token's `sub`, read inside the tool's worker thread."""
    windows = {}

    class Counter:  # rag.usage's take_window, in memory
        window_left_s = staticmethod(usage.window_left_s)

        @staticmethod
        def take_window(kind, key, limit, window_s=60, now=None):
            windows[key] = windows.get(key, 0) + 1
            return windows[key] if windows[key] <= limit else None

    deps = Deps(connect=fakes.connect, retrieve=fakes.retrieve, documents=DOCUMENTS,
                limits=Limits(per_user_per_minute=1, counter=Counter, clock=lambda: 1_800_000_000.0))
    app = build_app(deps, verifier=verifier, public_url=URL, issuer=ISSUER, scope=SCOPE)
    with TestClient(app, base_url=BASE) as client:
        results = [client.post("/mcp", headers=bearer(make_token(sub=sub)), json=call_body(ARGS)).json()["result"]
                   for sub in ("user-a", "user-b", "user-a")]
    assert [result.get("isError", False) for result in results] == [False, False, True]
    assert "Too many searches this minute" in results[2]["content"][0]["text"]
    assert set(windows) == {"user-a", "user-b"}


HTTP_BAD_CLAIMS = {name: BAD_CLAIMS[name] for name in
                   ["expired", "wrong client", "id token", "no scope claim", "aud for another URL"]}


@pytest.mark.parametrize("overrides", HTTP_BAD_CLAIMS.values(), ids=HTTP_BAD_CLAIMS.keys())
def test_bad_token_is_401_and_the_tool_is_not_called(client, fakes, make_token, overrides):
    response = client.post("/mcp", headers=bearer(make_token(**overrides)), json=call_body(ARGS))
    assert response.status_code == 401
    assert fakes.calls == []


def test_garbage_token_is_401(client, fakes):
    response = client.post("/mcp", headers=bearer("garbage"), json=call_body(ARGS))
    assert response.status_code == 401
    assert fakes.calls == []


def test_authorization_without_the_bearer_word_is_401(client, fakes, make_token):
    response = client.post("/mcp", headers={**ACCEPT, "Authorization": f"Token {make_token()}"},
                           json=call_body(ARGS))
    assert response.status_code == 401
    assert fakes.calls == []


# build_app settings


def test_build_app_with_a_verifier_needs_all_three_settings(fakes, verifier):
    full = {"public_url": URL, "issuer": ISSUER, "scope": SCOPE}
    for missing in full:
        settings = {name: value for name, value in full.items() if name != missing}
        with pytest.raises(ValueError):
            build_app(fakes.deps(), verifier=verifier, **settings)


@pytest.mark.parametrize("name, value", [("public_url", URL), ("issuer", ISSUER), ("scope", SCOPE)])
def test_build_app_refuses_login_settings_without_a_verifier(fakes, name, value):
    with pytest.raises(ValueError):
        build_app(fakes.deps(), **{name: value})


def test_build_app_refuses_a_verifier_for_another_url(fakes, verifier):
    with pytest.raises(ValueError):
        build_app(fakes.deps(), verifier=verifier, public_url="http://localhost:9999/mcp", issuer=ISSUER,
                  scope=SCOPE)


def test_without_login_a_call_needs_no_token(fakes):
    with TestClient(build_app(fakes.deps()), base_url=BASE) as c:
        response = c.post("/mcp", headers=ACCEPT, json=call_body(ARGS))
    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"]["count"] == 2
    assert fakes.calls == [{"query": "max supply voltage", "subject": None}]


# login_from_env


def test_login_from_env_is_none_without_an_issuer():
    assert login_from_env({}) is None
    assert login_from_env({"MCP_COGNITO_ISSUER": "  ", "MCP_COGNITO_CLIENT_IDS": CLIENT}) is None


def test_login_from_env_reads_the_settings_and_applies_defaults():
    login = login_from_env({"MCP_COGNITO_ISSUER": ISSUER, "MCP_COGNITO_CLIENT_IDS": "a, b"})
    assert set(login) == {"verifier", "public_url", "issuer", "scope"}
    assert isinstance(login["verifier"], CognitoVerifier)
    assert login["verifier"].clients == frozenset({"a", "b"})
    assert login["issuer"] == ISSUER
    assert login["scope"] == "datasheets/search"
    assert login["public_url"] == "http://localhost:8000/mcp"
    assert login["verifier"].resource == login["public_url"]
    assert login["verifier"].scope == login["scope"]


def test_login_from_env_uses_the_given_scope_and_url():
    login = login_from_env({"MCP_COGNITO_ISSUER": ISSUER, "MCP_COGNITO_CLIENT_IDS": CLIENT,
                            "MCP_SCOPE": "other/scope", "MCP_PUBLIC_URL": "https://api.example.com/mcp"})
    assert login["scope"] == "other/scope"
    assert login["public_url"] == "https://api.example.com/mcp"
    assert login["verifier"].resource == "https://api.example.com/mcp"


@pytest.mark.parametrize("clients", [None, "", " , "])
def test_login_from_env_needs_client_ids(clients):
    environ = {"MCP_COGNITO_ISSUER": ISSUER}
    if clients is not None:
        environ["MCP_COGNITO_CLIENT_IDS"] = clients
    with pytest.raises(ValueError):
        login_from_env(environ)


def test_login_from_env_result_works_with_build_app(fakes):
    login = login_from_env({"MCP_COGNITO_ISSUER": ISSUER, "MCP_COGNITO_CLIENT_IDS": CLIENT})
    with TestClient(build_app(fakes.deps(), **login), base_url=BASE) as c:
        response = c.post("/mcp", headers=ACCEPT, json=call_body(ARGS))
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]
    assert fakes.calls == []


# Added after the Layer 5 review (lead): server faults, the real key lookup, deployed-style settings, the logs


@pytest.mark.anyio
@pytest.mark.parametrize("fault", [jwt.PyJWKSetError("The JWKS endpoint did not contain any keys"),
                                   jwt.PyJWKError("bad key in the set"), jwt.InvalidKeyError("unusable key")])
async def test_server_side_key_problems_are_raised_not_refused(make_token, fault):
    def key_for(token):
        raise fault

    v = CognitoVerifier(ISSUER, [CLIENT], SCOPE, URL, key_for=key_for)
    with pytest.raises(type(fault)):
        await v.verify_token(make_token())


def test_key_list_outage_over_http_is_500_not_401(fakes, make_token):
    def key_for(token):
        raise jwt.PyJWKClientConnectionError("cannot reach the JWKS URL")

    v = CognitoVerifier(ISSUER, [CLIENT], SCOPE, URL, key_for=key_for)
    app = build_app(fakes.deps(), verifier=v, public_url=URL, issuer=ISSUER, scope=SCOPE)
    with TestClient(app, base_url=BASE, raise_server_exceptions=False) as c:
        response = c.post("/mcp", headers=bearer(make_token()), json=call_body(ARGS))
    assert response.status_code == 500
    assert "www-authenticate" not in response.headers  # not a login challenge: logging in again cannot help
    assert fakes.calls == []


def test_constructor_refuses_when_cryptography_is_missing(monkeypatch):
    monkeypatch.setattr(jwt.algorithms, "has_crypto", False)
    with pytest.raises(RuntimeError, match="cryptography"):
        CognitoVerifier(ISSUER, [CLIENT], SCOPE, URL, key_for=lambda token: None)


class FakeJWKClient:
    """Stands in for jwt.PyJWKClient: records how it was built and returns the public key made in the test."""

    built: ClassVar[list] = []
    public = None

    def __init__(self, uri, **kwargs):
        FakeJWKClient.built.append({"uri": uri, **kwargs})

    def get_signing_key_from_jwt(self, token):
        return type("SigningKey", (), {"key": FakeJWKClient.public})()


@pytest.fixture
def fake_jwk_client(monkeypatch, key):
    FakeJWKClient.built, FakeJWKClient.public = [], key.public_key()
    monkeypatch.setattr(jwt, "PyJWKClient", FakeJWKClient)
    return FakeJWKClient


@pytest.mark.anyio
async def test_default_key_lookup_uses_the_pool_key_list(fake_jwk_client, make_token):
    v = CognitoVerifier(ISSUER, [CLIENT], SCOPE, URL)
    assert (await v.verify_token(make_token())).subject == SUB
    assert fake_jwk_client.built == [{"uri": f"{ISSUER}/.well-known/jwks.json", "cache_keys": True,
                                      "timeout": auth.JWKS_TIMEOUT_S}]


def test_jwks_url_replaces_the_default(fake_jwk_client):
    CognitoVerifier(ISSUER, [CLIENT], SCOPE, URL, jwks_url="https://keys.example/jwks.json")
    assert fake_jwk_client.built[0]["uri"] == "https://keys.example/jwks.json"


PUBLIC_HOST = "abc123.execute-api.us-east-1.amazonaws.com"
PUBLIC_URL = f"https://{PUBLIC_HOST}/mcp"


@pytest.fixture
def deployed_client(fakes, key):
    """Login with allowed_hosts and an https public URL, as the Lambda will run it (Layer 7)."""
    public = key.public_key()
    v = CognitoVerifier(ISSUER, [CLIENT], SCOPE, PUBLIC_URL, key_for=lambda token: public)
    app = build_app(fakes.deps(), [PUBLIC_HOST], verifier=v, public_url=PUBLIC_URL, issuer=ISSUER, scope=SCOPE)
    with TestClient(app, base_url=f"https://{PUBLIC_HOST}") as c:
        yield c


def test_deployed_style_challenge_and_metadata_name_the_public_url(deployed_client, fakes):
    response = deployed_client.post("/mcp", headers=ACCEPT, json=call_body(ARGS))
    assert response.status_code == 401
    assert f'resource_metadata="https://{PUBLIC_HOST}{METADATA_PATH}"' in response.headers["www-authenticate"]
    document = deployed_client.get(METADATA_PATH).json()
    assert document["resource"].rstrip("/") == PUBLIC_URL
    assert fakes.calls == []


def test_deployed_style_valid_token_bound_to_the_url_gets_the_result(deployed_client, fakes, make_token):
    response = deployed_client.post("/mcp", headers=bearer(make_token(aud=PUBLIC_URL)), json=call_body(ARGS))
    assert response.status_code == 200
    assert [call["subject"] for call in fakes.calls] == [SUB]


def test_deployed_style_wrong_host_is_still_421(deployed_client, make_token):
    response = deployed_client.post("/mcp", headers={**bearer(make_token()), "Host": "evil.example"},
                                    json=call_body(ARGS))
    assert response.status_code == 421


def test_two_calls_each_see_their_own_subject(client, fakes, make_token):
    for sub in ("user-a", "user-b"):
        assert client.post("/mcp", headers=bearer(make_token(sub=sub)), json=call_body(ARGS)).status_code == 200
    assert [call["subject"] for call in fakes.calls] == ["user-a", "user-b"]


@pytest.mark.anyio
async def test_refusal_logs_never_hold_the_token_or_the_subject(verifier, make_token, caplog):
    caplog.set_level(logging.DEBUG, logger="datasheet_mcp")
    tokens = [make_token(client_id="x" * 500, sub="secret-sub-123"),
              make_token(token_use="id", sub="secret-sub-123"), "garbage.token.text"]
    for token in tokens:
        assert await verifier.verify_token(token) is None
    for token in tokens:
        assert token not in caplog.text
    assert "secret-sub-123" not in caplog.text
    assert "x" * (auth.LOGGED_VALUE_MAX + 1) not in caplog.text  # a long client_id is cut
    assert caplog.text.count("token refused") == 3


@pytest.mark.anyio
@pytest.mark.parametrize("overrides", [{"client_id": [CLIENT]}, {"scope": [SCOPE]}],
                         ids=["client_id as a list", "scope as a list"])
async def test_odd_claim_types_are_refused_not_crashing(verifier, make_token, overrides):
    assert await verifier.verify_token(make_token(**overrides)) is None


@pytest.mark.anyio
@pytest.mark.parametrize("aud", [URL + "/", [URL + "/"]])
async def test_aud_with_a_trailing_slash_still_matches(verifier, make_token, aud):
    assert (await verifier.verify_token(make_token(aud=aud))).resource == URL


@pytest.mark.parametrize("settings", [
    {"allowed_client_ids": ["", " "]},
    {"issuer": ISSUER + " "},
    {"required_scope": " " + SCOPE},
    {"resource": URL + " "},
    {"issuer": "https://"},
    {"issuer": "ftp://cognito.example/pool"},
    {"issuer": "http://localhost.evil.example/pool"},
], ids=["only blank clients", "issuer with a space", "scope with a space", "resource with a space",
        "issuer with no host", "issuer not http", "http issuer that only starts with localhost"])
def test_constructor_refuses_more_bad_settings(settings):
    arguments = {"issuer": ISSUER, "allowed_client_ids": [CLIENT], "required_scope": SCOPE, "resource": URL,
                 **settings}
    with pytest.raises(ValueError):
        CognitoVerifier(key_for=lambda token: None, **arguments)


def test_client_ids_are_stripped():
    assert CognitoVerifier(ISSUER, [" a ", "b"], SCOPE, URL, key_for=lambda t: None).clients == {"a", "b"}


def test_ipv6_loopback_http_issuer_is_accepted():
    CognitoVerifier("http://[::1]:9000/pool", [CLIENT], SCOPE, URL, key_for=lambda token: None)


@pytest.mark.parametrize("settings", [{"issuer": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OTHER"},
                                      {"scope": "other/scope"}], ids=["issuer", "scope"])
def test_build_app_refuses_settings_that_disagree_with_the_verifier(fakes, verifier, settings):
    arguments = {"public_url": URL, "issuer": ISSUER, "scope": SCOPE, **settings}
    with pytest.raises(ValueError):
        build_app(fakes.deps(), verifier=verifier, **arguments)


def test_without_login_there_is_no_metadata_document(fakes):
    with TestClient(build_app(fakes.deps()), base_url=BASE) as c:
        assert c.get(METADATA_PATH).status_code == 404
