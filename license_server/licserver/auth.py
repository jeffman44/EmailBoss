"""Accounts: signup, login, logout, password reset, admin gate.

Passwords: hashlib.scrypt, stored as  scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>.
Sessions: random token in an HttpOnly cookie, row in `sessions`.
Login is rate-limited per client IP in memory (LIC_LOGIN_RATE_LIMIT per window).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import mailer
from .web import (RateLimiter, check_csrf, clear_session_cookie, client_ip, current_account, db_of, redirect,
                  render, require_account, set_session_cookie, settings_of)

log = logging.getLogger("licserver.auth")
router = APIRouter()

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8


# ------------------------------------------------------------------ passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii"))


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(EMAIL_RE.match(email))


# ------------------------------------------------------------------ account creation (shared with billing/admin)

def create_account(request_or_app, email: str, password: str | None = None, *, verified: bool = False,
                   actor: str = "self") -> tuple[dict, Optional[str]]:
    """Create an account; returns (account, temp_password_or_None).

    Nobody becomes admin at creation. An address in LIC_ADMIN_EMAILS is
    promoted only once the email is VERIFIED (verification link or a
    completed password reset, both of which prove inbox access), see
    promote_if_listed(); the alternative is the CLI
    `python -m licserver.admin_seed <email>`.
    """
    app = getattr(request_or_app, "app", request_or_app)
    db, settings = app.state.db, app.state.settings
    email = email.lower().strip()
    temp = None
    if not password:
        temp = password = secrets.token_urlsafe(12)
    acct = db.create_account(email, hash_password(password), is_admin=False, verified=verified)
    db.audit("account.create", actor=actor, target=acct["id"], detail={"email": email, "verified": verified})
    if verified:
        acct = promote_if_listed(app, acct)
    return acct, temp


def promote_if_listed(app, acct: dict) -> dict:
    """Grant admin to a VERIFIED account whose email is in LIC_ADMIN_EMAILS."""
    db, settings = app.state.db, app.state.settings
    if acct and acct.get("verified") and not acct.get("is_admin") and acct["email"] in settings.admin_emails:
        db.set_admin(acct["id"], True)
        db.audit("account.admin_granted", actor="verified_email", target=acct["id"], detail={"email": acct["email"]})
        acct = db.get_account(acct["id"])
    return acct


def mark_email_verified(app, aid: str) -> dict:
    db = app.state.db
    db.mark_verified(aid)
    db.audit("account.verified", actor=aid, target=aid)
    return promote_if_listed(app, db.get_account(aid))


def send_verification(app, acct: dict) -> None:
    settings = app.state.settings
    token = app.state.db.create_verification(acct["id"])
    mailer.send_verification(acct["email"], settings.product_name, f"{settings.base_url}/verify/{token}")


def safe_next(target: str, default: str = "/dashboard") -> str:
    """Only a plain in-site path survives: starts with one '/', no second
    slash or backslash after it, no scheme, no control characters."""
    t = (target or "").strip()
    if (not t.startswith("/") or t.startswith("//") or "\\" in t or ":" in t.split("?", 1)[0]
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in t) or t.startswith("/\\")):
        return default
    return t


def _limiter(request: Request) -> RateLimiter:
    return request.app.state.login_limiter


# ------------------------------------------------------------------ routes

@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    if current_account(request):
        return redirect("/dashboard")
    return render(request, "signup.html")


@router.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...),
           password2: str = Form(""), csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    email = email.lower().strip()
    if not valid_email(email):
        return render(request, "signup.html", status_code=400, error="Enter a valid email address.", email=email)
    if len(password) < MIN_PASSWORD:
        return render(request, "signup.html", status_code=400, error=f"Password needs at least {MIN_PASSWORD} characters.", email=email)
    if password2 and password2 != password:
        return render(request, "signup.html", status_code=400, error="Passwords do not match.", email=email)
    db, settings = db_of(request), settings_of(request)
    if not _limiter(request).hit("signup:" + client_ip(request)):
        return render(request, "signup.html", status_code=429, error="Too many attempts. Try again in a few minutes.", email=email)
    if db.get_account_by_email(email):
        return render(request, "signup.html", status_code=400, error="That email already has an account. Sign in instead.", email=email)
    acct, _ = create_account(request, email, password)
    send_verification(request.app, acct)
    notice = "Account created. Check your email for a link to confirm the address."
    if settings.trial_enabled:
        from .licenses import issue_license
        lic = issue_license(request.app, acct, plan="trial", days=settings.trial_days, actor="signup")
        mailer.send_license_key(email, settings.product_name, "trial", lic["key"], settings.base_url)
        notice = f"Account created. A {settings.trial_days}-day trial license is on your dashboard."
    sess = db.create_session(acct["id"])
    resp = redirect("/dashboard", notice=notice)
    set_session_cookie(resp, sess["token"], settings)
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_account(request):
        return redirect("/dashboard")
    return render(request, "login.html", next=safe_next(request.query_params.get("next", "/dashboard")))


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          next: str = Form("/dashboard"), csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    email = email.lower().strip()
    ip = client_ip(request)
    if not _limiter(request).hit("login:" + ip):
        db_of(request).audit("login.rate_limited", actor=ip, target=email)
        return render(request, "login.html", status_code=429, error="Too many sign-in attempts. Wait a few minutes.", email=email, next=next)
    db = db_of(request)
    acct = db.get_account_by_email(email)
    if not acct or not verify_password(password, acct["pw_hash"]):
        db.audit("login.failed", actor=ip, target=email)
        return render(request, "login.html", status_code=401, error="Email or password is wrong.", email=email, next=next)
    _limiter(request).reset("login:" + ip)
    sess = db.create_session(acct["id"])
    db.audit("login.ok", actor=acct["id"], target=email, detail={"ip": ip})
    resp = redirect(safe_next(next))
    set_session_cookie(resp, sess["token"], settings_of(request))
    return resp


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    current_account(request)
    sess = getattr(request.state, "session", None)
    if sess:
        db_of(request).delete_session(sess["token"])
    resp = redirect("/login", notice="Signed out.")
    clear_session_cookie(resp)
    return resp


@router.get("/forgot", response_class=HTMLResponse)
def forgot_page(request: Request):
    return render(request, "forgot.html")


@router.post("/forgot")
def forgot(request: Request, email: str = Form(...), csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    email = email.lower().strip()
    db, settings = db_of(request), settings_of(request)
    if _limiter(request).hit("forgot:" + client_ip(request)):
        acct = db.get_account_by_email(email)
        if acct:
            token = db.create_reset(acct["id"])
            mailer.send_password_reset(email, settings.product_name, f"{settings.base_url}/reset/{token}")
            db.audit("password.reset_requested", actor=acct["id"], target=email)
    # Same answer whether or not the account exists.
    return redirect("/login", notice="If that email has an account, a reset link has been sent.")


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_page(request: Request, token: str):
    if not db_of(request).peek_reset(token):
        return render(request, "message.html", status_code=400, title="Link expired",
                      body="That reset link is no longer valid. Request a new one.")
    return render(request, "reset.html", token=token)


@router.post("/reset/{token}")
def reset(request: Request, token: str, password: str = Form(...), password2: str = Form(""),
          csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    db = db_of(request)
    if len(password) < MIN_PASSWORD:
        return render(request, "reset.html", status_code=400, token=token,
                      error=f"Password needs at least {MIN_PASSWORD} characters.")
    if password2 and password2 != password:
        return render(request, "reset.html", status_code=400, token=token, error="Passwords do not match.")
    aid = db.consume_reset(token)
    if not aid:
        return render(request, "message.html", status_code=400, title="Link expired",
                      body="That reset link is no longer valid. Request a new one.")
    db.set_password(aid, hash_password(password))
    db.audit("password.reset", actor=aid, target=aid)
    # The reset link reached the inbox, which proves the address.
    if not (db.get_account(aid) or {}).get("verified"):
        mark_email_verified(request.app, aid)
    return redirect("/login", notice="Password updated. Sign in with the new one.")


@router.get("/verify/{token}", response_class=HTMLResponse)
def verify_email(request: Request, token: str):
    aid = db_of(request).consume_verification(token)
    if not aid:
        return render(request, "message.html", status_code=400, title="Link expired",
                      body="That confirmation link is no longer valid.")
    mark_email_verified(request.app, aid)
    return redirect("/dashboard", notice="Email confirmed.")


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, account: dict = Depends(require_account)):
    return render(request, "account.html")


@router.post("/account/password")
def change_password(request: Request, account: dict = Depends(require_account),
                    current: str = Form(...), password: str = Form(...), password2: str = Form(""),
                    csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    db = db_of(request)
    if not verify_password(current, account["pw_hash"]):
        return render(request, "account.html", status_code=400, error="Current password is wrong.")
    if len(password) < MIN_PASSWORD:
        return render(request, "account.html", status_code=400, error=f"Password needs at least {MIN_PASSWORD} characters.")
    if password2 and password2 != password:
        return render(request, "account.html", status_code=400, error="Passwords do not match.")
    db.set_password(account["id"], hash_password(password))
    db.audit("password.change", actor=account["id"], target=account["id"])
    sess = db.create_session(account["id"])
    resp = redirect("/account", notice="Password changed. Other sessions were signed out.")
    set_session_cookie(resp, sess["token"], settings_of(request))
    return resp
