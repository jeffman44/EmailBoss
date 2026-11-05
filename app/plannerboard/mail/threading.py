"""Threads (2.0 Phase 3, plan 5.3).

A message joins a thread by RFC 5322 linkage, per account:

1. A manual override for this message (split / merge) always wins, and
   survives every re-sync: it lives in ``mail_thread_overrides``, not on the
   message row the capture path rewrites.
2. Otherwise the thread of a message it replies to (In-Reply-To, then the
   References chain newest first) when that message is in the store.
3. Otherwise the provider's conversation key (Gmail and IMAP compute it from
   the References root with a subject fallback; Graph sends its own).

Thread state (pin, mute, snooze, waiting-on, the project it is filed to)
lives on ``mail_threads``. Every change here returns what is needed to undo
it; ``terminal.undo`` runs the inverse. Nothing here sends mail or touches a
project's status, stage or money.
"""
from __future__ import annotations

import json
import re
import secrets as _secrets
import time

_ID_RE = re.compile(r"<[^<>\s]+>")

STATE_FIELDS = ("pinned", "muted", "snooze_until", "waiting_until", "waiting_since", "waiting_task", "project_id")


# ── headers ──────────────────────────────────────────────────────────────────

def _header(msg, name: str) -> str:
    want = name.lower()
    for h in getattr(msg, "headers", None) or []:
        if isinstance(h, dict) and str(h.get("name", "")).lower() == want:
            return str(h.get("value") or "")
    return ""


def thread_headers(msg) -> dict:
    """The linkage and list headers the store keeps (bounded)."""
    return {
        "in_reply_to": " ".join(_ID_RE.findall(_header(msg, "In-Reply-To")))[:1000],
        "refs": " ".join(_ID_RE.findall(_header(msg, "References"))[-50:])[:8000],
        "list_id": _header(msg, "List-Id").strip()[:500],
        "list_unsub": _header(msg, "List-Unsubscribe").strip()[:2000],
    }


def _parents(hdr: dict) -> list[str]:
    """Candidate parent Message-IDs, nearest first."""
    out: list[str] = []
    for mid in _ID_RE.findall(hdr.get("in_reply_to") or ""):
        if mid not in out:
            out.append(mid)
    for mid in reversed(_ID_RE.findall(hdr.get("refs") or "")):
        if mid not in out:
            out.append(mid)
    return out


def resolve_key(c, account_id: str, sid: str, msg, hdr: dict) -> str:
    """The thread key for one message being ingested (inside the store's
    transaction, ``c`` is its connection)."""
    o = c.execute("SELECT thread_key FROM mail_thread_overrides WHERE account_id=? AND provider_id=?",
                  (account_id, sid)).fetchone()
    if o:
        return o[0]
    own = (getattr(msg, "internet_message_id", "") or "").strip()
    for mid in _parents(hdr):
        if mid == own:
            continue
        row = c.execute("SELECT thread_key FROM mail_messages WHERE account_id=? AND message_id=? AND thread_key != ''"
                        " AND provider_id != ? ORDER BY received_ts DESC LIMIT 1", (account_id, mid, sid)).fetchone()
        if row:
            return row[0]
    return getattr(msg, "conversation_id", "") or ""


def recount(c, account_id: str, keys) -> None:
    """Re-derive count and last date for threads a message left or joined."""
    for key in {k for k in keys if k}:
        n, last, subj = c.execute(
            "SELECT COUNT(*), MAX(received_ts), (SELECT subject FROM mail_messages WHERE account_id=? AND thread_key=?"
            " ORDER BY received_ts LIMIT 1) FROM mail_messages WHERE account_id=? AND thread_key=? AND removed=0",
            (account_id, key, account_id, key)).fetchone()
        c.execute("INSERT INTO mail_threads (account_id, thread_key, subject, message_count, last_ts) VALUES (?,?,?,?,?)"
                  " ON CONFLICT(account_id, thread_key) DO UPDATE SET message_count=excluded.message_count,"
                  " last_ts=excluded.last_ts", (account_id, key, subj or "", int(n or 0), last))


# ── split / merge (persisted overrides) ──────────────────────────────────────

def _key_of(store, account_id: str, ref: str) -> str:
    row = store.get(account_id, ref)
    if not row:
        raise LookupError("That message is not in the local store.")
    return row.get("thread_key") or ""


def _set_overrides(c, account_id: str, items) -> None:
    """items: (provider_id, new_key, kind_or_None). None clears the override."""
    touched = set()
    for pid, key, kind in items:
        old = c.execute("SELECT thread_key FROM mail_messages WHERE account_id=? AND provider_id=?",
                        (account_id, pid)).fetchone()
        if kind is None:
            c.execute("DELETE FROM mail_thread_overrides WHERE account_id=? AND provider_id=?", (account_id, pid))
        else:
            c.execute("INSERT INTO mail_thread_overrides (account_id, provider_id, thread_key, kind, created_at)"
                      " VALUES (?,?,?,?,?) ON CONFLICT(account_id, provider_id) DO UPDATE SET"
                      " thread_key=excluded.thread_key, kind=excluded.kind, created_at=excluded.created_at",
                      (account_id, pid, key, kind, time.time()))
        c.execute("UPDATE mail_messages SET thread_key=? WHERE account_id=? AND provider_id=?", (key, account_id, pid))
        touched.add(key)
        if old:
            touched.add(old[0])
    recount(c, account_id, touched)


def _snapshot(c, account_id: str, pids) -> list:
    out = []
    for pid in pids:
        m = c.execute("SELECT thread_key FROM mail_messages WHERE account_id=? AND provider_id=?",
                      (account_id, pid)).fetchone()
        o = c.execute("SELECT kind FROM mail_thread_overrides WHERE account_id=? AND provider_id=?",
                      (account_id, pid)).fetchone()
        out.append([pid, m[0] if m else "", o[0] if o else None])
    return out


def split(store, account_id: str, ref: str) -> dict:
    """Take one message out of its thread into a thread of its own."""
    _key_of(store, account_id, ref)
    new_key = "split:" + _secrets.token_urlsafe(9)
    with store._conn() as c:
        before = _snapshot(c, account_id, [ref])
        _set_overrides(c, account_id, [(ref, new_key, "split")])
    return {"thread_key": new_key, "undo": {"items": before}}


def merge(store, account_id: str, ref: str, into_ref: str) -> dict:
    """Put every message of ref's thread into into_ref's thread."""
    src, dst = _key_of(store, account_id, ref), _key_of(store, account_id, into_ref)
    if not dst:
        raise LookupError("The target message has no thread.")
    if src == dst:
        return {"thread_key": dst, "undo": {"items": []}}
    pids = [r["provider_id"] for r in store.query(
        "SELECT provider_id FROM mail_messages WHERE account_id=? AND thread_key=?", [account_id, src])] if src else [ref]
    with store._conn() as c:
        before = _snapshot(c, account_id, pids)
        _set_overrides(c, account_id, [(p, dst, "merge") for p in pids])
    return {"thread_key": dst, "undo": {"items": before}}


def undo_overrides(store, account_id: str, data: dict) -> None:
    with store._conn() as c:
        _set_overrides(c, account_id, [(pid, key, kind) for pid, key, kind in data.get("items") or []])


# ── thread state: pin, mute, snooze, waiting-on, project ─────────────────────

def get_state(store, account_id: str, key: str) -> dict:
    rows = store.query(f"SELECT {', '.join(STATE_FIELDS)} FROM mail_threads WHERE account_id=? AND thread_key=?",
                       [account_id, key])
    if rows:
        return {k: rows[0][k] for k in STATE_FIELDS}
    return {"pinned": 0, "muted": 0, "snooze_until": None, "waiting_until": None, "waiting_since": None,
            "waiting_task": "", "project_id": ""}


def set_state(store, account_id: str, key: str, **fields) -> dict:
    """Change thread state; returns the prior values of the fields changed."""
    fields = {k: v for k, v in fields.items() if k in STATE_FIELDS}
    if not key or not fields:
        return {}
    prior = get_state(store, account_id, key)
    with store._conn() as c:
        c.execute("INSERT INTO mail_threads (account_id, thread_key) VALUES (?,?)"
                  " ON CONFLICT(account_id, thread_key) DO NOTHING", (account_id, key))
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE mail_threads SET {sets} WHERE account_id=? AND thread_key=?",
                  list(fields.values()) + [account_id, key])
    return {k: prior.get(k) for k in fields}


def thread_of(store, account_id: str, ref: str) -> str:
    return _key_of(store, account_id, ref)


# ── project links (correspondence backfill, filing) ──────────────────────────

def link_correspondence(store, board, account_id: str, message_ids=None) -> int:
    """Point threads at the project their mail is filed under, by Message-ID.

    With ``message_ids`` None this is the one-time pass over every existing
    ``project_correspondence`` row (marked done in mail_meta); with a list it
    is the incremental link for messages just ingested. A thread already
    filed somewhere keeps its project."""
    conn = getattr(board, "conn", None) if board is not None else None
    if conn is None:
        return 0
    flag = f"corr_link:{account_id}"
    if message_ids is None:
        if store.meta_get(flag):
            return 0
        pairs = conn.execute("SELECT message_id, project_id FROM project_correspondence"
                             " WHERE message_id IS NOT NULL AND message_id != '' ORDER BY id").fetchall()
    else:
        ids = [m for m in message_ids if m]
        pairs = []
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            pairs += conn.execute(f"SELECT message_id, project_id FROM project_correspondence WHERE message_id IN"
                                  f" ({','.join('?' * len(chunk))}) ORDER BY id", chunk).fetchall()
    n = 0
    with store._conn() as c:
        for mid, pid in pairs:
            for (key,) in c.execute("SELECT DISTINCT thread_key FROM mail_messages WHERE account_id=? AND message_id=?"
                                    " AND thread_key != ''", (account_id, mid)).fetchall():
                c.execute("INSERT INTO mail_threads (account_id, thread_key) VALUES (?,?)"
                          " ON CONFLICT(account_id, thread_key) DO NOTHING", (account_id, key))
                cur = c.execute("UPDATE mail_threads SET project_id=? WHERE account_id=? AND thread_key=?"
                                " AND project_id=''", (pid, account_id, key))
                n += cur.rowcount
    if message_ids is None:
        store.meta_set(flag, str(time.time()))
    return n


# ── waiting-on ───────────────────────────────────────────────────────────────

def replies_arrived(store, board, account_id: str, rows, me: str = "") -> list[str]:
    """New inbound mail in a thread marked waiting-on clears the wait and
    completes its nudge (a subtask completion; never a status change)."""
    cleared = []
    me = (me or "").lower()
    for r in rows:
        key = r.get("thread_key") or ""
        if not key or r.get("folder") == "sent" or (me and (r.get("from_email") or "").lower() == me):
            continue
        st = get_state(store, account_id, key)
        since = st.get("waiting_since")
        if not since or (r.get("received_ts") or 0) <= since:
            continue
        set_state(store, account_id, key, waiting_until=None, waiting_since=None, waiting_task="")
        task = st.get("waiting_task") or ""
        if task and board is not None and st.get("project_id"):
            try:
                board.complete_subtask(st["project_id"], task, completed_by="mail_reply")
            except Exception:
                pass
        cleared.append(key)
    return cleared


def nudge_text(subject: str) -> str:
    subj = re.sub(r"\s+", " ", subject or "").strip()[:80] or "(no subject)"
    return f"Nudge: no reply yet on “{subj}”"


def state_json(st: dict) -> str:
    return json.dumps(st, default=str)
