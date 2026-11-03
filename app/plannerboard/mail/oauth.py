"""OAuth2 plumbing shared by the Gmail and Graph providers: urllib HTTP,
PKCE pairs, the pending-flow registry, a token cache in the keyring, and a
tiny standalone loopback listener for command-line use.

The token-refresh dance is a generalization of v2/graph_auth.py lines 21-34
(``post``) and 82-95 (``access_token`` with rotating refresh token).

Nothing here depends on a vendor SDK; it is stdlib only.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets as _rand
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from . import secrets

HttpFn = Callable[..., tuple[int, dict | bytes]]


def http_request(method: str, url: str, *, data=None, headers=None, form=False,
                 timeout=30, raw=False) -> tuple[int, dict | bytes]:
    """(status, json_or_bytes). HTTP errors come back as a status + body
    instead of an exception, so callers can read the OAuth error field."""
    headers = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, (bytes, bytearray)):
            body = bytes(data)
        else:
            body = json.dumps(data).encode()
            headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        payload = e.read()
        status = e.code
    if raw:
        return status, payload
    if not payload:
        return status, {}
    try:
        return status, json.loads(payload)
    except Exception:
        return status, {"error": f"http_{status}", "raw": payload[:400].decode("utf-8", "replace")}


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(_rand.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class PendingFlows:
    """state -> {provider, verifier, redirect_uri, created}. Process-local;
    a flow that is not finished within ``ttl`` seconds is forgotten."""

    def __init__(self, ttl: int = 900):
        self._lock = threading.Lock()
        self._flows: dict[str, dict] = {}
        self.ttl = ttl

    def start(self, provider: str, redirect_uri: str, extra: dict | None = None) -> tuple[str, str, str]:
        verifier, challenge = pkce_pair()
        state = _rand.token_urlsafe(24)
        with self._lock:
            self._gc()
            self._flows[state] = {"provider": provider, "verifier": verifier,
                                  "redirect_uri": redirect_uri, "created": time.time(),
                                  **(extra or {})}
        return state, verifier, challenge

    def pop(self, state: str) -> dict | None:
        with self._lock:
            self._gc()
            return self._flows.pop(state, None)

    def _gc(self):
        cut = time.time() - self.ttl
        for k in [k for k, v in self._flows.items() if v["created"] < cut]:
            self._flows.pop(k, None)


PENDING = PendingFlows()


class TokenCache:
    """Refresh/access token JSON kept in the keyring under one key
    (``gmail_token`` / ``graph_token``)."""

    def __init__(self, key: str, account: str | None = None):
        self.key = key
        self.account = account

    def load(self) -> dict:
        raw, _src = secrets.get_secret(self.key, account=self.account)
        if not raw:
            return {}
        try:
            doc = json.loads(raw)
            return doc if isinstance(doc, dict) else {}
        except Exception:
            return {}

    def save(self, doc: dict) -> bool:
        ok, _where, _err = secrets.set_secret(self.key, json.dumps(doc), account=self.account)
        return ok

    def clear(self) -> None:
        secrets.delete_secret(self.key, account=self.account)

    def access_token(self, refresh_fn: Callable[[dict], dict | None]) -> str | None:
        """A valid access token, refreshing through ``refresh_fn(doc)`` when
        the cached one is within 60 s of expiry. ``refresh_fn`` returns the
        new token response (access_token, expires_in, optional refresh_token)
        or None on failure."""
        doc = self.load()
        if not doc:
            return None
        if doc.get("access_token") and float(doc.get("expires_at") or 0) - 60 > time.time():
            return doc["access_token"]
        if not doc.get("refresh_token"):
            return doc.get("access_token")
        fresh = refresh_fn(doc)
        if not fresh or not fresh.get("access_token"):
            return None
        self.store_response(fresh, prior=doc)
        return fresh["access_token"]

    def store_response(self, resp: dict, prior: dict | None = None) -> dict:
        doc = dict(prior or {})
        doc["access_token"] = resp.get("access_token")
        doc["expires_at"] = time.time() + int(resp.get("expires_in") or 3600)
        # Rotating refresh tokens: keep the newest one we were given.
        if resp.get("refresh_token"):
            doc["refresh_token"] = resp["refresh_token"]
        if resp.get("scope"):
            doc["scope"] = resp["scope"]
        doc["obtained"] = time.time()
        self.save(doc)
        return doc


def loopback_redirect_uri(port: int, path: str = "/api/mail/oauth/callback") -> str:
    return f"http://127.0.0.1:{int(port)}{path}"


def wait_for_code(port: int, path: str = "/oauth/cb", timeout: int = 300) -> dict:
    """Standalone loopback listener (CLI use). The app itself receives the
    callback on its own board port through api/mail_api.py."""
    result: dict = {}
    done = threading.Event()

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            if u.path != path:
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(u.query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>Sign-in complete. You can close this tab.</body></html>")
            done.set()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", int(port)), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    done.wait(timeout)
    srv.shutdown()
    return result
