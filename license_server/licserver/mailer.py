"""Pluggable outbound email. Default implementation logs to the console.

Swap in a real sender with:

    from licserver import mailer
    mailer.SENDER = my_callable  # (to: str, subject: str, body: str) -> None

or subclass/replace `ConsoleMailer`. Nothing here talks to a network by default.
"""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger("licserver.mail")

SENT: list[dict] = []  # tests inspect this; capped so it never grows unbounded
_SENT_CAP = 200


def console_sender(to: str, subject: str, body: str) -> None:
    log.info("MAIL to=%s subject=%s\n%s", to, subject, body)
    SENT.append({"to": to, "subject": subject, "body": body})
    if len(SENT) > _SENT_CAP:
        del SENT[: len(SENT) - _SENT_CAP]


SENDER: Callable[[str, str, str], None] = console_sender


def send(to: str, subject: str, body: str) -> None:
    SENDER(to, subject, body)


def send_license_key(to: str, product: str, plan: str, key: str, portal_url: str) -> None:
    body = (
        f"Your {product} {plan} license key:\n\n{key}\n\n"
        f"Paste it into Settings > License inside the app, or sign in at {portal_url} to manage devices.\n"
    )
    send(to, f"Your {product} license key", body)


def send_password_reset(to: str, product: str, reset_url: str) -> None:
    send(to, f"{product} password reset",
         f"Use this link within two hours to set a new password:\n\n{reset_url}\n\nIf you did not ask for this, ignore it.\n")


def send_welcome(to: str, product: str, portal_url: str, temp_password: str | None = None) -> None:
    body = f"An account was created for you at {portal_url}.\n"
    if temp_password:
        body += f"\nTemporary password: {temp_password}\nChange it after your first sign-in.\n"
    send(to, f"Welcome to {product}", body)


def send_verification(to: str, product: str, verify_url: str) -> None:
    send(to, f"Confirm your {product} email",
         f"Confirm this address by opening:\n\n{verify_url}\n\nIf you did not sign up, ignore this.\n")


def send_trial(to: str, product: str, key: str, portal_url: str, *, verify_url: str, set_password_url: str) -> None:
    """The one mail a new trial gets: key, email confirmation (funds the AI
    trial credits) and a set-password link. No password is ever mailed."""
    body = (
        f"Your {product} free trial license key:\n\n{key}\n\n"
        f"The app on the computer you started the trial from is already activated.\n\n"
        f"1. Confirm this address (this also adds your free AI trial credits):\n{verify_url}\n\n"
        f"2. Set a password for your account at {portal_url} (link valid for two hours):\n{set_password_url}\n\n"
        f"If you did not ask for this, ignore it.\n"
    )
    send(to, f"Your {product} free trial", body)
