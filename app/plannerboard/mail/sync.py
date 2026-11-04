"""Store backfill: the full mailbox into the local store, never into triage.

2.0 plan section 5.1: "Store sync and triage are separate jobs." The
capture path (``fetch_new``) follows new mail from a cursor and feeds
triage. This job walks the whole mailbox once, newest first, a page per
call, and only ever writes the store:

- headers for every message in every folder the provider exposes
- body text for messages inside the body window (``mail.store.body_days``,
  default 90; 0 = all)
- ``historical = 1`` for anything received before ``install_cutoff``

It never writes a mail-cache record, never runs triage, rules or AI. The
cursor lives in the store (``mail_meta['backfill:<account_id>']``) so it
resumes across restarts and stops when the provider reports the end.

Providers opt in with ``backfill_page(cursor, limit, body_since_ts)``
returning ``(messages, next_cursor, done)``.
"""
from __future__ import annotations

import json
import time

DEFAULT_BODY_DAYS = 90
DEFAULT_PAGE = 200


def store_settings(cfg: dict) -> dict:
    s = (((cfg or {}).get("mail") or {}).get("store") or {})
    try:
        body_days = int(s.get("body_days", DEFAULT_BODY_DAYS))
    except (TypeError, ValueError):
        body_days = DEFAULT_BODY_DAYS
    return {"body_days": max(0, body_days), "backfill": s.get("backfill", True) is not False,
            "page": max(10, min(500, int(s.get("backfill_page") or DEFAULT_PAGE)))}


def body_since(cfg: dict, now: float | None = None) -> float | None:
    """Oldest receive time that keeps its body; None = every body."""
    days = store_settings(cfg)["body_days"]
    if days <= 0:
        return None
    return (now or time.time()) - days * 86400


def supports_backfill(provider) -> bool:
    return callable(getattr(provider, "backfill_page", None))


def state(store, account_id: str) -> dict:
    raw = store.meta_get(f"backfill:{account_id}")
    try:
        doc = json.loads(raw) if raw else {}
    except Exception:
        doc = {}
    doc.setdefault("cursor", None)
    doc.setdefault("done", False)
    doc.setdefault("stored", 0)
    return doc


def _save(store, account_id: str, doc: dict) -> None:
    store.meta_set(f"backfill:{account_id}", json.dumps(doc))


def step(provider, store, cfg: dict, cutoff_ts: float | None, pages: int = 1,
         now: float | None = None) -> dict:
    """Run up to ``pages`` backfill pages for one provider. Never raises."""
    aid = getattr(provider, "account_id", None) or provider.id
    doc = state(store, aid)
    out = {"account": aid, "stored": 0, "done": doc["done"], "error": None}
    if doc["done"] or not supports_backfill(provider):
        return out
    settings = store_settings(cfg)
    since = body_since(cfg, now)
    try:
        for _ in range(max(1, pages)):
            msgs, cursor, done = provider.backfill_page(doc["cursor"], settings["page"], since)
            for msg in msgs:
                rts = _ts(msg)
                historical = cutoff_ts is None or rts is None or rts < cutoff_ts
                with_body = since is None or (rts is not None and rts >= since)
                store.ingest(aid, msg, historical=historical, with_body=with_body)
                out["stored"] += 1
            doc["cursor"] = cursor
            doc["stored"] = int(doc.get("stored") or 0) + len(msgs)
            doc.setdefault("started_at", time.time())
            if done:
                doc["done"] = True
                doc["finished_at"] = time.time()
                _save(store, aid, doc)
                break
            _save(store, aid, doc)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"[:300]
        _save(store, aid, doc)
    out["done"] = doc["done"]
    out["total_stored"] = doc["stored"]
    return out


def reset(store, account_id: str) -> None:
    """Start the walk over (after a re-index request or a purge)."""
    store.meta_delete(f"backfill:{account_id}")


def _ts(msg) -> float | None:
    from .store import _ts as conv
    return conv(getattr(msg, "received_at", None))
