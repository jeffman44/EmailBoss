"""Grant admin from the server's shell, the alternative to LIC_ADMIN_EMAILS.

    python -m licserver.admin_seed <email> [--create]

Runs against LIC_DB_PATH (same settings as the server). The account must
exist unless --create is given, in which case it is created with a random
temporary password (printed once) and marked verified, since whoever has a
shell on the server is trusted by definition.
"""
from __future__ import annotations

import argparse
import secrets
import sys

from .auth import hash_password, valid_email
from .config import Settings
from .models import Database


def seed(db: Database, email: str, create: bool = False) -> tuple[dict, str | None]:
    email = email.lower().strip()
    if not valid_email(email):
        raise ValueError(f"not a valid email: {email!r}")
    acct = db.get_account_by_email(email)
    temp = None
    if not acct:
        if not create:
            raise LookupError(f"no account for {email}; sign up first or pass --create")
        temp = secrets.token_urlsafe(12)
        acct = db.create_account(email, hash_password(temp), is_admin=False, verified=True)
    db.mark_verified(acct["id"])
    db.set_admin(acct["id"], True)
    db.audit("account.admin_granted", actor="cli", target=acct["id"], detail={"email": email})
    return db.get_account(acct["id"]), temp  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m licserver.admin_seed", description="Grant admin to an account.")
    ap.add_argument("email")
    ap.add_argument("--create", action="store_true", help="create the account if it does not exist")
    args = ap.parse_args(argv)
    db = Database(Settings().db_path)
    try:
        acct, temp = seed(db, args.email, args.create)
    except (ValueError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"admin granted: {acct['email']} ({acct['id']})")
    if temp:
        print(f"temporary password: {temp}  (change it after signing in)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
