"""SMTP submission for the IMAP provider.

Port 587 with STARTTLS by default (RFC 6409 submission), 465 implicit TLS
when ``ssl`` is set, app password or XOAUTH2. Presets mirror the IMAP presets
so a Gmail / Outlook.com / iCloud mailbox needs no extra typing.

This module only runs when ``mail.send_enabled`` is true; the API refuses
before it gets here. Nothing in the capture path imports it.
"""
from __future__ import annotations

import base64
import smtplib
import ssl

SMTP_PRESETS = {
    "gmail": {"host": "smtp.gmail.com", "port": 587, "starttls": True, "ssl": False},
    "outlook": {"host": "smtp.office365.com", "port": 587, "starttls": True, "ssl": False},
    "icloud": {"host": "smtp.mail.me.com", "port": 587, "starttls": True, "ssl": False},
    "custom": {"host": "", "port": 587, "starttls": True, "ssl": False},
}


def smtp_settings(mail_cfg: dict | None, imap_settings: dict | None = None) -> dict:
    """Effective SMTP settings: explicit ``mail.smtp`` over the preset that
    matches the IMAP preset, user defaulting to the IMAP user."""
    raw = dict((mail_cfg or {}).get("smtp") or {})
    imap_settings = imap_settings or {}
    preset_id = (raw.get("preset") or imap_settings.get("preset") or "custom").strip().lower()
    preset = SMTP_PRESETS.get(preset_id, SMTP_PRESETS["custom"])
    host = (raw.get("host") or preset["host"] or "").strip()
    if not host and imap_settings.get("host", "").startswith("imap."):
        host = "smtp." + imap_settings["host"][5:]       # imap.example.com -> smtp.example.com
    return {
        "preset": preset_id if preset_id in SMTP_PRESETS else "custom",
        "host": host,
        "port": int(raw.get("port") or preset["port"] or 587),
        "starttls": bool(raw.get("starttls", preset["starttls"])),
        "ssl": bool(raw.get("ssl", preset["ssl"])),
        "user": (raw.get("user") or imap_settings.get("user") or "").strip(),
        "auth_mode": (raw.get("auth_mode") or imap_settings.get("auth_mode") or "password").strip().lower(),
        "timeout": int(raw.get("timeout") or 45),
    }


def _xoauth2(user: str, token: str) -> str:
    return base64.b64encode(f"user={user}\x01auth=Bearer {token}\x01\x01".encode()).decode()


def connect(settings: dict, password: str | None = None, oauth_token: str | None = None,
            smtp_factory=None):
    """Logged-in SMTP connection. Raises on failure; callers wrap it.
    ``smtp_factory(host, port, timeout, ssl_ctx)`` lets tests inject a fake."""
    host, port, timeout = settings["host"], int(settings["port"]), int(settings.get("timeout") or 45)
    if not host:
        raise ValueError("no SMTP host configured")
    ctx = ssl.create_default_context()
    if smtp_factory is not None:
        conn = smtp_factory(host, port, timeout, ctx, bool(settings.get("ssl")))
    elif settings.get("ssl"):
        conn = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
    else:
        conn = smtplib.SMTP(host, port, timeout=timeout)
    conn.ehlo()
    if settings.get("starttls") and not settings.get("ssl"):
        conn.starttls(context=ctx)
        conn.ehlo()
    user = settings.get("user") or ""
    if settings.get("auth_mode") == "xoauth2":
        if not oauth_token:
            raise ValueError("auth_mode is xoauth2 but no access token is available")
        code, resp = conn.docmd("AUTH", "XOAUTH2 " + _xoauth2(user, oauth_token))
        if code != 235:
            raise smtplib.SMTPAuthenticationError(code, resp)
    elif user:
        if not password:
            raise ValueError("no SMTP password available")
        conn.login(user, password)
    return conn


def send(settings: dict, from_addr: str, rcpts: list[str], raw: bytes, password=None,
         oauth_token=None, smtp_factory=None) -> dict:
    """One submission. Returns {accepted, refused}. Raises on a transport or
    auth failure so the API can report it."""
    if not rcpts:
        raise ValueError("no recipients")
    conn = connect(settings, password=password, oauth_token=oauth_token, smtp_factory=smtp_factory)
    try:
        refused = conn.sendmail(from_addr, rcpts, raw)
    finally:
        try:
            conn.quit()
        except Exception:
            pass
    return {"accepted": [r for r in rcpts if r not in (refused or {})],
            "refused": {k: str(v) for k, v in (refused or {}).items()}}
