"""Per-account mail configuration (2.0 plan section 5.1, Phase 1).

1.x kept one slot per provider type: ``mail.gmail``, ``mail.imap``, one
``gmail_token`` keyring entry, one ``state_gmail.json``. 2.0 keys all of it
by account:

``mail.accounts`` (optional) is a list of
``{"id": "work", "provider": "gmail", "email": "...", "label": "...", "settings": {...}}``.
``settings`` overlays ``mail.<provider>`` for that account.

Without ``mail.accounts`` the 1.x slots become one account per configured
provider whose id IS the provider id (``gmail``, ``imap`` ...), so an
upgrade keeps its cursor file, its retry rows and its keyring entry.

Per account:

- OAuth token: keyring key ``mail_token_<account_id>`` (environment
  fallback ``PB_MAIL_TOKEN_<ACCOUNT_ID>``). A 1.x-shaped account keeps its
  ``gmail_token`` / ``graph_token`` entry.
- Cursor and dedup state: ``<mail_cache>/state_<account_id>.json``.
- Store rows, retry rows and reconcile rows: keyed by ``account_id``.
"""
from __future__ import annotations

import copy
import re

PROVIDER_TYPES = ("pa_folder", "imap", "gmail", "graph")
LEGACY_TOKEN_KEYS = {"gmail": "gmail_token", "graph": "graph_token"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def valid_id(account_id: str) -> bool:
    return bool(_ID_RE.match(account_id or ""))


def token_key(account: dict) -> str:
    """Keyring key of an account's OAuth token. A 1.x-shaped account keeps
    the 1.x key, so an upgrade stays signed in."""
    if account.get("legacy") and account["provider"] in LEGACY_TOKEN_KEYS:
        return LEGACY_TOKEN_KEYS[account["provider"]]
    return f"mail_token_{account['id']}"


def accounts(cfg: dict) -> list[dict]:
    """Every configured account, normalized: id, provider, email, label,
    settings, legacy (True for an account synthesized from 1.x slots)."""
    mail = (cfg or {}).get("mail") or {}
    out: list[dict] = []
    seen: set[str] = set()
    listed = mail.get("accounts")
    if isinstance(listed, list) and listed:
        for a in listed:
            if not isinstance(a, dict):
                continue
            aid = str(a.get("id") or "").strip().lower()
            prov = str(a.get("provider") or "").strip().lower()
            if not valid_id(aid) or prov not in PROVIDER_TYPES or aid in seen:
                continue
            seen.add(aid)
            out.append({"id": aid, "provider": prov, "email": str(a.get("email") or ""),
                        "label": str(a.get("label") or aid), "settings": dict(a.get("settings") or {}),
                        "legacy": False})
        return out
    ids = list(mail.get("providers") or [])
    p = (mail.get("provider") or "").strip().lower()
    if p and p not in ids:
        ids.insert(0, p)
    for pid in ids:
        if pid in PROVIDER_TYPES and pid not in seen:
            seen.add(pid)
            out.append({"id": pid, "provider": pid, "email": "", "label": pid,
                        "settings": {}, "legacy": True})
    return out


def account_cfg(cfg: dict, account: dict) -> dict:
    """The config a provider class reads, with this account's settings laid
    over ``mail.<provider>``."""
    doc = copy.deepcopy(cfg or {})
    mail = doc.setdefault("mail", {})
    prov = account["provider"]
    base = dict(mail.get(prov) or {})
    base.update(account.get("settings") or {})
    mail[prov] = base
    return doc


def find(cfg: dict, account_id: str) -> dict | None:
    for a in accounts(cfg):
        if a["id"] == account_id:
            return a
    return None
