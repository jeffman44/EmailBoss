"""Shared web plumbing: templates, session lookup, CSRF, flash, rate limit."""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import Settings
from .models import Database

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

SESSION_COOKIE = "lic_session"
CSRF_COOKIE = "lic_csrf"


def db_of(request: Request) -> Database:
    return request.app.state.db


def settings_of(request: Request) -> Settings:
    return request.app.state.settings


# ------------------------------------------------------------------ sessions

def current_account(request: Request) -> Optional[dict]:
    if hasattr(request.state, "account"):
        return request.state.account
    token = request.cookies.get(SESSION_COOKIE, "")
    acct = None
    sess = db_of(request).get_session(token) if token else None
    if sess:
        acct = db_of(request).get_account(sess["account_id"])
    request.state.account = acct
    request.state.session = sess
    return acct


def require_account(request: Request) -> dict:
    acct = current_account(request)
    if not acct:
        nxt = quote(str(request.url.path))
        raise HTTPException(status_code=303, headers={"Location": f"/login?next={nxt}"})
    return acct


def require_admin(request: Request) -> dict:
    acct = require_account(request)
    if not acct.get("is_admin"):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "admin only"})
    return acct


def set_session_cookie(response, token: str, settings: Settings, max_age: int = 14 * 86400) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True,
                        secure=settings.secure_cookies, samesite="lax", path="/")


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


# ------------------------------------------------------------------ csrf (double-submit cookie)

def csrf_token(request: Request) -> str:
    tok = request.cookies.get(CSRF_COOKIE, "")
    if not tok or len(tok) < 16:
        tok = getattr(request.state, "csrf_new", None) or secrets.token_urlsafe(24)
        request.state.csrf_new = tok
    return tok


def check_csrf(request: Request, form_token: str | None) -> None:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not cookie or not form_token or not hmac.compare_digest(cookie, form_token):
        raise HTTPException(status_code=400, detail={"code": "csrf", "message": "form expired, reload and try again"})


def _apply_csrf_cookie(request: Request, response) -> None:
    new = getattr(request.state, "csrf_new", None)
    if new:
        response.set_cookie(CSRF_COOKIE, new, max_age=7 * 86400, httponly=True,
                            secure=settings_of(request).secure_cookies, samesite="lax", path="/")


# ------------------------------------------------------------------ render / redirect

def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    s = settings_of(request)
    base = {
        "request": request,
        "account": current_account(request),
        "csrf": csrf_token(request),
        "settings": s,
        "product": s.product_name,
        "notice": request.query_params.get("notice", ""),
        "error": request.query_params.get("error", ""),
    }
    base.update(ctx)
    resp = templates.TemplateResponse(request, name, base, status_code=status_code)
    _apply_csrf_cookie(request, resp)
    return resp


def redirect(url: str, notice: str = "", error: str = "", status_code: int = 303) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if notice:
        url = f"{url}{sep}notice={quote(notice)}"
        sep = "&"
    if error:
        url = f"{url}{sep}error={quote(error)}"
    return RedirectResponse(url, status_code=status_code)


# ------------------------------------------------------------------ in-memory rate limit

class RateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Record a hit; return True when the caller is within the limit."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


def client_ip(request: Request) -> str:
    """X-Forwarded-For is honoured only behind a proxy we trust
    (LIC_TRUSTED_PROXY=1); otherwise the header is attacker-controlled and
    would let anyone dodge the rate limiter by rotating it."""
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and getattr(settings, "trusted_proxy", False):
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            # The LAST hop is the one our proxy appended; earlier ones are client-supplied.
            return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "?"
