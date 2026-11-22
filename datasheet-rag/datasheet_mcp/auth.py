"""Login check: is the bearer token a valid Cognito access token for this server?

The SDK calls CognitoVerifier.verify_token for every request to /mcp. Returning None makes the SDK answer 401 with
a WWW-Authenticate header that points the client at the protected resource metadata (so it can log in); returning an
AccessToken lets the request through, and the tool can read the user id from get_access_token().subject.

A Cognito access token is a JWT (JSON Web Token): three base64 parts, header.claims.signature. Cognito signs it with
RS256 (an RSA private key it keeps); anyone can check the signature with the public keys it publishes at
<issuer>/.well-known/jwks.json (the JWKS, JSON Web Key Set). The header's `kid` names the key that signed it.

Claims checked here (read in the Cognito docs on access tokens and the PyJWT 2.15 source, 2026-11-21):
iss (must be our user pool), exp (not expired), token_use (must be "access", not an ID token), client_id (one of our
app clients), scope (space-separated; must hold the required scope), and aud only when present: Cognito adds aud to
an access token only with resource binding, so the SDK's own resource check is off (validate_token_resource=False)
and the audience is checked here instead.
"""
import ipaddress
import logging
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

import anyio
import jwt
from mcp.server.auth.provider import AccessToken

logger = logging.getLogger(__name__)

REQUIRED_CLAIMS = ["exp", "iss", "sub", "client_id", "token_use"]
JWKS_TIMEOUT_S = 5  # how long to wait for the key list; well inside the 28 s tool deadline
LOGGED_VALUE_MAX = 64  # a client_id is cut to this length in a log line (it comes from the request)

# Errors that mean this server is broken, not the token: the key list cannot be fetched, is empty or malformed, or a
# key cannot be used. They are raised (the reply is 500, and the error is logged) instead of refused with 401: a 401
# for every token would send every client back to a login that cannot help, with no sign that the server is at fault.
SERVER_FAULTS = (jwt.PyJWKClientConnectionError, jwt.PyJWKSetError, jwt.PyJWKError, jwt.InvalidKeyError)


def _is_local(host: str | None) -> bool:
    """True for localhost, 127.0.0.1 and ::1 (the only hosts where a plain http issuer is allowed, for tests)."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def _check_issuer(issuer: str) -> None:
    """Refuse an issuer that can never match a token's iss exactly, or that would fetch keys over plain http."""
    if not issuer or not issuer.strip():
        raise ValueError("issuer is blank")
    if issuer != issuer.strip():
        raise ValueError("issuer has spaces around it: no token's iss would match")
    if issuer.endswith("/"):
        raise ValueError("issuer ends with '/': Cognito's iss has no trailing slash, so no token would match")
    parts = urlsplit(issuer)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and _is_local(parts.hostname):
        return
    raise ValueError("issuer must be an https URL (http only for localhost)")


class CognitoVerifier:
    """Checks Cognito access tokens. key_for(token) returns the public key for a token (tests pass a fake)."""

    def __init__(self, issuer: str, allowed_client_ids: Iterable[str], required_scope: str, resource: str,
                 key_for: Callable[[str], Any] | None = None, jwks_url: str | None = None):
        if not jwt.algorithms.has_crypto:
            # Without the cryptography package PyJWT has no RS256, and every real token would be refused with 401.
            raise RuntimeError("the cryptography package is not installed: RS256 tokens cannot be checked")
        _check_issuer(issuer)
        clients = frozenset(c.strip() for c in allowed_client_ids if c and c.strip())
        if not clients:
            raise ValueError("allowed_client_ids is empty: give at least one Cognito app client id")
        if not required_scope or not required_scope.strip():
            raise ValueError("required_scope is blank")
        if required_scope != required_scope.strip():
            raise ValueError("required_scope has spaces around it")
        if not resource or not resource.strip():
            raise ValueError("resource is blank: give the public MCP URL")
        if resource != resource.strip():
            raise ValueError("resource has spaces around it")
        self.issuer, self.clients, self.scope, self.resource = issuer, clients, required_scope, resource
        if key_for is None:
            # Built once, so its key cache lasts as long as this verifier. It fetches the key list only on first use.
            jwks = jwt.PyJWKClient(jwks_url or f"{issuer}/.well-known/jwks.json", cache_keys=True,
                                   timeout=JWKS_TIMEOUT_S)
            key_for = lambda token: jwks.get_signing_key_from_jwt(token).key
        self._key_for = key_for

    async def verify_token(self, token: str) -> AccessToken | None:
        """An AccessToken when the token passes every check; None (the SDK answers 401) when any check fails.

        SERVER_FAULTS (for example a key list that cannot be fetched) are raised, not refused: a 401 would send the
        client into a login that cannot help, while a 500 is logged and the client can retry.

        The key lookup may download the key list (blocking network I/O, up to JWKS_TIMEOUT_S), so it runs in a
        worker thread: the server keeps answering other requests meanwhile.
        """
        try:
            key = await anyio.to_thread.run_sync(self._key_for, token)
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=self.issuer,
                                options={"require": REQUIRED_CLAIMS, "verify_aud": False})
        except SERVER_FAULTS:
            raise
        except jwt.PyJWKClientError as exc:
            # Usually a token from another pool (its kid is not in our key list). If every token gets this, the
            # issuer setting names the wrong pool, so it is logged at WARNING.
            return _refuse(f"{type(exc).__name__}: {exc}", level=logging.WARNING)
        except jwt.PyJWTError as exc:
            return _refuse(f"{type(exc).__name__}: {exc}")
        return self._check_claims(token, claims)

    def _check_claims(self, token: str, claims: dict[str, Any]) -> AccessToken | None:
        """The checks PyJWT does not do: token kind, client, scope, audience."""
        if claims["token_use"] != "access":
            return _refuse("token_use is not 'access' (an ID token?)")
        client_id = claims["client_id"]
        if not isinstance(client_id, str) or client_id not in self.clients:
            return _refuse(f"client_id {client_id[:LOGGED_VALUE_MAX]!r} is not allowed" if isinstance(client_id, str)
                           else "client_id is not a string")
        scope = claims.get("scope")
        scopes = scope.split() if isinstance(scope, str) else []
        if self.scope not in scopes:
            return _refuse(f"required scope {self.scope!r} missing")
        aud = claims.get("aud")
        if aud is not None and not _aud_matches(aud, self.resource):
            return _refuse("aud does not match this server's URL")
        return AccessToken(token=token, client_id=client_id, scopes=scopes, expires_at=int(claims["exp"]),
                           resource=self.resource if aud is not None else None, subject=claims["sub"],
                           claims=claims)


def _aud_matches(aud: Any, resource: str) -> bool:
    """aud may be one string or a list of strings; the resource must be it, or one of them. A trailing slash on
    either side does not count (the SDK's own resource check ignores it too)."""
    want = resource.removesuffix("/")
    values = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    return any(isinstance(value, str) and value.removesuffix("/") == want for value in values)


def _refuse(reason: str, level: int = logging.INFO) -> None:
    """Log why a token was refused; callers return its None. The token itself is never logged. The reason may hold
    a short value from the request (a client_id cut to LOGGED_VALUE_MAX characters, or a key id in PyJWT's text)."""
    logger.log(level, "token refused: %s", reason)
