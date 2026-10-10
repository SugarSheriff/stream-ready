# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign in with Miris: the OAuth device-authorization flow (RFC 8628).

This is how Miris's own CLI and Substance plugin sign in. The add-on asks Miris for a
short code, the artist approves it in their browser, and the add-on receives a token.
No password or key is ever typed into Blender, the grant shows up (and can be revoked)
under Account > OAuth tokens in the Miris portal, and the tokens live in the OS vault.

    POST /v1/oauth/device/start    {scopes, client_label} -> device_code, user_code, verification_uri
    POST /v1/oauth/device/poll     {device_code}          -> access_token + refresh_token, or error
    POST /v1/oauth/token/refresh   {refresh_token}        -> new tokens
    POST /v1/oauth/token/revoke    {refresh_token}        -> sign out

Scopes are the permission names Miris's OpenAPI spec attaches to each endpoint; the
add-on asks for only the four it uses.
"""

import json
import os
import time

from . import keystore
from .http import HttpError
from .miris import MirisError

SCOPES = ["content:submit", "asset:read", "assetviewerkey:write", "customer:readself"]
CLIENT_LABEL = "Stream Ready (Blender add-on)"
# Tests point this elsewhere so they can never overwrite a real sign-in.
TOKEN_SERVICE = os.environ.get("STREAM_READY_TOKEN_SERVICE") or "stream-ready/miris-oauth"
REFRESH_MARGIN = 120  # refresh this many seconds before the access token expires
# Verified live (tools/miris_login.py): OAuth access tokens go in a standard
# "Authorization: Bearer" header. The public spec only documents Miris-API-Key.


class OAuthError(MirisError):
    pass


def start(transport, base_url):
    resp = _post(transport, base_url, "/v1/oauth/device/start", {"scopes": SCOPES, "client_label": CLIENT_LABEL})
    for field in ("device_code", "user_code", "verification_uri"):
        if not resp.get(field):
            raise OAuthError(f"device/start response is missing {field}")
    return resp


def wait_for_approval(transport, base_url, grant, clock=time.monotonic):
    """Poll until the artist approves, denies, or the code expires. Returns token dict
    tagged with the server that issued it."""
    interval = max(int(grant.get("interval") or 5), 1)
    deadline = clock() + int(grant.get("expires_in") or 600)
    while clock() < deadline:
        transport.wait(interval)
        resp = _post(transport, base_url, "/v1/oauth/device/poll", {"device_code": grant["device_code"]},
                     ok_errors=True)
        error = (resp.get("error") or "").lower()
        if resp.get("access_token"):
            return dict(_tokens(resp), base_url=base_url.rstrip("/"))
        if error in ("", "authorization_pending"):
            continue
        if error == "slow_down":  # RFC 8628: back off by 5 seconds and keep going
            interval += 5
            continue
        if error in ("access_denied", "denied"):
            raise OAuthError("sign-in was denied in the browser")
        if error in ("expired_token", "expired"):
            break
        raise OAuthError(f"sign-in failed: {error}")
    raise OAuthError("the sign-in code expired before it was approved; start again")


def refresh(transport, base_url, tokens):
    resp = _post(transport, base_url, "/v1/oauth/token/refresh", {"refresh_token": tokens["refresh_token"]})
    if not resp.get("access_token"):
        raise OAuthError("refresh did not return a new access token; sign in again")
    fresh = _tokens(resp)
    fresh["refresh_token"] = fresh["refresh_token"] or tokens["refresh_token"]
    fresh["base_url"] = tokens.get("base_url", base_url.rstrip("/"))
    return fresh


def revoke(transport, base_url, tokens):
    _post(transport, base_url, "/v1/oauth/token/revoke", {"refresh_token": tokens["refresh_token"]})


# --- vault --------------------------------------------------------------------------------

def save_tokens(tokens, service=TOKEN_SERVICE):
    keystore.save(json.dumps(tokens, separators=(",", ":")), service)


def load_tokens(service=TOKEN_SERVICE, base_url=None):
    """Saved tokens, or None. With base_url, only tokens issued by that server: a token
    for app.miris.com must never be sent to a mock server or anywhere else."""
    raw = keystore.load(service)
    try:
        tokens = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not tokens or not tokens.get("access_token"):
        return None
    if base_url and tokens.get("base_url", "").rstrip("/") != base_url.rstrip("/"):
        return None
    return tokens


def forget_tokens(service=TOKEN_SERVICE):
    keystore.delete(service)


def credential(base_url, env=None, service=TOKEN_SERVICE):
    """What to authenticate with: ("api_key", key, label), ("oauth", tokens, label), or
    ("", None, ""). $MIRIS_API_KEY wins, so a studio or CI can inject a key explicitly."""
    env = os.environ if env is None else env
    key = (env.get("MIRIS_API_KEY") or "").strip()
    if key:
        return "api_key", key, "$MIRIS_API_KEY"
    tokens = load_tokens(service, base_url)
    if tokens:
        return "oauth", tokens, "Signed in with Miris"
    return "", None, ""


# --- the credential MirisClient uses ------------------------------------------------------

class OAuthSession:
    """Supplies auth headers, refreshes before expiry, and persists rotated tokens."""

    def __init__(self, transport, base_url, tokens, persist=save_tokens, clock=time.time):
        self.transport = transport
        self.base_url = base_url.rstrip("/")
        self._tokens = dict(tokens)
        self._persist = persist
        self._clock = clock

    def __repr__(self):
        return "OAuthSession(<tokens hidden>)"

    def headers(self):
        if self._tokens.get("expires_at", 0) - REFRESH_MARGIN < self._clock():
            self.refresh()
        return {"Authorization": f"Bearer {self._tokens['access_token']}"}

    def refresh(self):
        self._tokens = refresh(self.transport, self.base_url, self._tokens)
        self._persist(self._tokens)


def _tokens(resp):
    expires_in = int(resp.get("expires_in") or 3600)
    return {
        "access_token": resp["access_token"],
        "refresh_token": resp.get("refresh_token") or "",
        "expires_at": time.time() + expires_in,
    }


def _post(transport, base_url, path, body, ok_errors=False):
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        resp = transport.send(f"POST {path}", lambda _: ("POST", url, headers, data))
    except HttpError as exc:
        # RFC 8628 servers answer "authorization_pending" etc. with a 400 and an error body.
        if ok_errors and exc.status == 400 and exc.problem:
            return {"error": exc.problem.get("error") or exc.problem.get("reason") or exc.problem.get("code") or "error"}
        raise OAuthError(str(exc)) from None
    try:
        return resp.json() or {}
    except ValueError:
        raise OAuthError(f"POST {path}: response was not JSON") from None
