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


_WANT = {"in-reply-to", "references", "list-id", "list-unsubscribe", "auto-submitted", "precedence",
         "x-autoreply", "x-autorespond"}


def thread_headers(msg) -> dict:
    """The linkage, list and auto-reply headers the store keeps (bounded).
    One pass over the header list (this runs for every stored message)."""
    h = {}
    for x in getattr(msg, "headers", None) or ():
        if isinstance(x, dict):
            n = str(x.get("name", "")).lower()
            if n in _WANT and n not in h:
                h[n] = str(x.get("value") or "")
    auto = h.get("auto-submitted", "").strip().lower()
    prec = h.get("precedence", "").strip().lower()
    return {
        "in_reply_to": " ".join(_ID_RE.findall(h.get("in-reply-to", "")))[:1000],
        "refs": " ".join(_ID_RE.findall(h.get("references", ""))[-50:])[:8000],
        "list_id": h.get("list-id", "").strip()[:500],
        "list_unsub": h.get("list-unsubscribe", "").strip()[:2000],
        "auto_reply": bool((auto and auto != "no") or prec in ("auto_reply", "bulk", "junk")
                           or h.get("x-autoreply") or h.get("x-autorespond")),
    }


def _parents(in_reply_to: str, refs: str, own: str) -> list[str]:
    """Candidate parent Message-IDs, nearest first."""
    out: list[str] = []
    for mid in _ID_RE.findall(in_reply_to or "") + list(reversed(_ID_RE.findall(refs or ""))):
        if mid != own and mid not in out:
            out.append(mid)
    return out[:51]


_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|sv|wg|tr)\s*(\[\d+\])?\s*:\s*)+", re.I)
_ADDR = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def norm_subject(subject: str) -> str:
    return re.sub(r"\s+", " ", _PREFIX.sub("", subject or "")).strip().lower()


def _addrs(*parts) -> set:
    return {a.lower() for p in parts for a in _ADDR.findall(p or "")}


def _plausible(child: dict, parent, me: str = "") -> bool:
    """Quoting a Message-ID is not enough to join a thread (anyone the mail
    was ever forwarded to has it): the reply must share the subject, or the
    two messages must share a participant other than you (every message in
    your mailbox has you on it)."""
    if norm_subject(child["subject"]) and norm_subject(child["subject"]) == norm_subject(parent["subject"]):
        return True
    a = _addrs(child["from_email"], child["to"], child["cc"])
    b = _addrs(parent["from_email"], parent["to_addrs"], parent["cc_addrs"])
    return bool((a & b) - {me})


def _own_key(own: str, sid: str) -> str:
    from .message import thread_key_from_ids
    return thread_key_from_ids("", "", own) if own else "own:" + sid


def _key(c, account_id: str, sid: str, child: dict) -> str:
    o = c.execute("SELECT thread_key FROM mail_thread_overrides WHERE account_id=? AND provider_id=?",
                  (account_id, sid)).fetchone()
    if o:
        return o[0]
    parents = _parents(child["in_reply_to"], child["refs"], child["own"])
    if parents:
        found = {}
        for r in c.execute("SELECT message_id, thread_key, subject, from_email, to_addrs, cc_addrs FROM mail_messages"
                           " INDEXED BY mail_messages_msgid"
                           f" WHERE account_id=? AND message_id IN ({','.join('?' * len(parents))})"
                           " AND provider_id != ? AND thread_key != ''", [account_id] + parents + [sid]):
            found.setdefault(r["message_id"], r)
        for mid in parents:
            r = found.get(mid)
            if r is not None:
                # The nearest stored parent decides: join it, or stand alone.
                me = c.execute("SELECT email FROM mail_accounts WHERE account_id=?", (account_id,)).fetchone()
                ok = _plausible(child, r, ((me[0] if me else "") or "").lower())
                return r["thread_key"] if ok else _own_key(child["own"], sid)
    return child["conv_key"] or ""


def resolve_key(c, account_id: str, sid: str, msg, hdr: dict) -> str:
    """The thread key for one message being ingested (inside the store's
    transaction, ``c`` is its connection)."""
    sender = getattr(msg, "sender", None)
    return _key(c, account_id, sid, {
        "own": (getattr(msg, "internet_message_id", "") or "").strip(),
        "in_reply_to": hdr.get("in_reply_to") or "", "refs": hdr.get("refs") or "",
        "subject": getattr(msg, "subject", "") or "",
        "from_email": (sender.email if sender else "") or "",
        "to": " ".join(a.email for a in getattr(msg, "to", []) or []),
        "cc": " ".join(a.email for a in getattr(msg, "cc", []) or []),
        "conv_key": getattr(msg, "conversation_id", "") or ""})


def reresolve(c, account_id: str, pids) -> set:
    """Re-derive the thread of stored messages without an override (after an
    override is undone), oldest first so parents settle before replies."""
    touched = set()
    rows = []
    for i, pid in enumerate(list(pids)):
        r = c.execute("SELECT provider_id, message_id, in_reply_to, refs, subject, from_email, to_addrs, cc_addrs,"
                      " conv_key, thread_key, received_ts FROM mail_messages WHERE account_id=? AND provider_id=?",
                      (account_id, pid)).fetchone()
        if r:
            rows.append(r)
    for r in sorted(rows, key=lambda x: x["received_ts"] or 0):
        k = _key(c, account_id, r["provider_id"], {
            "own": r["message_id"] or "", "in_reply_to": r["in_reply_to"], "refs": r["refs"], "subject": r["subject"],
            "from_email": r["from_email"], "to": r["to_addrs"], "cc": r["cc_addrs"], "conv_key": r["conv_key"]})
        if k and k != r["thread_key"]:
            c.execute("UPDATE mail_messages SET thread_key=? WHERE account_id=? AND provider_id=?",
                      (k, account_id, r["provider_id"]))
            touched |= {k, r["thread_key"]}
    return touched


def recount(c, account_id: str, keys) -> None:
    """Re-derive count and last date for threads a message left or joined;
    an empty thread with no state of its own is dropped."""
    for key in {k for k in keys if k}:
        n, last = c.execute("SELECT COUNT(*), MAX(received_ts) FROM mail_messages WHERE account_id=? AND thread_key=?"
                            " AND removed=0", (account_id, key)).fetchone()
        if not n:
            c.execute("DELETE FROM mail_threads WHERE account_id=? AND thread_key=? AND pinned=0 AND muted=0"
                      " AND snooze_until IS NULL AND waiting_until IS NULL AND project_id=''", (account_id, key))
            c.execute("UPDATE mail_threads SET message_count=0 WHERE account_id=? AND thread_key=?", (account_id, key))
            continue
        subj = c.execute("SELECT subject FROM mail_messages WHERE account_id=? AND thread_key=? AND removed=0"
                         " ORDER BY received_ts LIMIT 1", (account_id, key)).fetchone()[0]
        c.execute("INSERT INTO mail_threads (account_id, thread_key, subject, message_count, last_ts) VALUES (?,?,?,?,?)"
                  " ON CONFLICT(account_id, thread_key) DO UPDATE SET message_count=excluded.message_count,"
                  " last_ts=excluded.last_ts", (account_id, key, subj or "", int(n), last))


# ── split / merge (persisted overrides) ──────────────────────────────────────

def _key_of(store, account_id: str, ref: str) -> str:
    row = store.get(account_id, ref)
    if not row:
        raise LookupError("That message is not in the local store.")
    return row.get("thread_key") or ""


def _members(c, account_id: str, key: str) -> list[str]:
    return [r[0] for r in c.execute("SELECT provider_id FROM mail_messages WHERE account_id=? AND thread_key=?",
                                    (account_id, key))] if key else []


def _set_overrides(c, account_id: str, items) -> set:
    """items: (provider_id, key, kind_or_None). None clears the override (the
    message then re-derives its thread). Returns the thread keys touched."""
    touched, cleared = set(), []
    for pid, key, kind in items:
        old = c.execute("SELECT thread_key FROM mail_messages WHERE account_id=? AND provider_id=?",
                        (account_id, pid)).fetchone()
        if old:
            touched.add(old[0])
        if kind is None:
            c.execute("DELETE FROM mail_thread_overrides WHERE account_id=? AND provider_id=?", (account_id, pid))
            cleared.append(pid)
        else:
            c.execute("INSERT INTO mail_thread_overrides (account_id, provider_id, thread_key, kind, created_at)"
                      " VALUES (?,?,?,?,?) ON CONFLICT(account_id, provider_id) DO UPDATE SET"
                      " thread_key=excluded.thread_key, kind=excluded.kind, created_at=excluded.created_at",
                      (account_id, pid, key, kind, time.time()))
        if key:
            c.execute("UPDATE mail_messages SET thread_key=? WHERE account_id=? AND provider_id=?", (key, account_id, pid))
            touched.add(key)
    return touched


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
    """Take one message out of its thread into a thread of its own. Every
    other message of the old thread is pinned where it is, so a re-sync
    cannot drag the split message's replies (or the rest of the thread)
    along with it."""
    old = _key_of(store, account_id, ref)
    new_key = "split:" + _secrets.token_urlsafe(9)
    with store._conn() as c:
        members = _members(c, account_id, old) or [ref]
        before = _snapshot(c, account_id, members)
        items = [(p, old, "pin") for p in members if p != ref] + [(ref, new_key, "split")]
        recount(c, account_id, _set_overrides(c, account_id, items))
    return {"thread_key": new_key, "undo": {"items": before, "keys": [old, new_key]}}


def merge(store, account_id: str, ref: str, into_ref: str) -> dict:
    """Put every message of ref's thread into into_ref's thread. The target
    takes over the source's state where it has none of its own."""
    src, dst = _key_of(store, account_id, ref), _key_of(store, account_id, into_ref)
    if not dst:
        raise LookupError("The target message has no thread.")
    if src == dst:
        return {"thread_key": dst, "undo": {"items": [], "keys": []}}
    with store._conn() as c:
        pids = _members(c, account_id, src) or [ref]
        before = _snapshot(c, account_id, pids)
        recount(c, account_id, _set_overrides(c, account_id, [(p, dst, "merge") for p in pids]))
    s_state, d_state = get_state(store, account_id, src), get_state(store, account_id, dst)
    carry = {k: s_state[k] for k in STATE_FIELDS if s_state.get(k) and not d_state.get(k)}
    prior = set_state(store, account_id, dst, **carry) if carry else {}
    return {"thread_key": dst, "undo": {"items": before, "keys": [src, dst], "target": dst, "target_prior": prior}}


def undo_overrides(store, account_id: str, data: dict) -> None:
    """Put the overrides back as they were, then let every message in the
    threads involved that has no override re-derive its thread (a reply that
    arrived after a merge goes back with its parent)."""
    with store._conn() as c:
        touched = _set_overrides(c, account_id, [(pid, key if kind else "", kind)
                                                 for pid, key, kind in data.get("items") or []])
        touched |= set(data.get("keys") or [])
        free = []
        for k in list(touched):
            for pid in _members(c, account_id, k):
                if not c.execute("SELECT 1 FROM mail_thread_overrides WHERE account_id=? AND provider_id=?",
                                 (account_id, pid)).fetchone():
                    free.append(pid)
        touched |= reresolve(c, account_id, free)
        recount(c, account_id, touched)
    if data.get("target") and data.get("target_prior"):
        set_state(store, account_id, data["target"], **data["target_prior"])


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

def link_correspondence(store, board, account_id: str, message_ids=None, flag: str = "corr_link") -> int:
    """Point threads at the project their mail is filed under, by Message-ID.

    With ``message_ids`` None this is a full pass over every existing
    ``project_correspondence`` row, done once per ``flag`` (the runner runs
    it on the first capture and again when the backfill has stored the whole
    mailbox, so history the backfill brings in is linked too). With a list
    it links the messages just stored. A thread already filed somewhere
    keeps its project."""
    conn = getattr(board, "conn", None) if board is not None else None
    if conn is None:
        return 0
    key = f"{flag}:{account_id}"
    if message_ids is None:
        if store.meta_get(key):
            return 0
        pairs = conn.execute("SELECT message_id, project_id FROM project_correspondence"
                             " WHERE message_id IS NOT NULL AND message_id != '' ORDER BY id").fetchall()
    else:
        ids = sorted({m for m in message_ids if m})
        pairs = []
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            pairs += conn.execute(f"SELECT message_id, project_id FROM project_correspondence WHERE message_id IN"
                                  f" ({','.join('?' * len(chunk))}) ORDER BY id", chunk).fetchall()
    n = 0
    for i in range(0, len(pairs), 500):         # short transactions: capture and search keep going
        with store._conn() as c:
            for mid, pid in pairs[i:i + 500]:
                for (tk,) in c.execute("SELECT DISTINCT thread_key FROM mail_messages INDEXED BY mail_messages_msgid"
                                       " WHERE account_id=? AND message_id=? AND thread_key != ''",
                                       (account_id, mid)).fetchall():
                    c.execute("INSERT INTO mail_threads (account_id, thread_key) VALUES (?,?)"
                              " ON CONFLICT(account_id, thread_key) DO NOTHING", (account_id, tk))
                    n += c.execute("UPDATE mail_threads SET project_id=? WHERE account_id=? AND thread_key=?"
                                   " AND project_id=''", (pid, account_id, tk)).rowcount
    if message_ids is None:
        store.meta_set(key, str(time.time()))
    return n


# ── waiting-on ───────────────────────────────────────────────────────────────

def replies_arrived(store, board, account_id: str, rows, me: str = "") -> list[str]:
    """New inbound mail in a thread marked waiting-on clears the wait and
    completes its nudge (a subtask completion; never a status change). An
    auto-reply (out of office, bounce) is not a reply."""
    waiting = {r["thread_key"]: r for r in store.query(
        "SELECT thread_key, waiting_since, waiting_task, project_id FROM mail_threads"
        " WHERE account_id=? AND waiting_since IS NOT NULL", [account_id])}
    if not waiting:
        return []
    cleared = []
    me = (me or "").lower()
    for r in rows:
        key = r.get("thread_key") or ""
        st = waiting.get(key)
        if st is None or key in cleared or r.get("folder") == "sent" or r.get("auto_reply"):
            continue
        if me and (r.get("from_email") or "").lower() == me:
            continue
        if (r.get("received_ts") or 0) <= (st["waiting_since"] or 0):
            continue
        set_state(store, account_id, key, waiting_until=None, waiting_since=None, waiting_task="")
        if st["waiting_task"] and board is not None and st["project_id"]:
            try:
                board.complete_subtask(st["project_id"], st["waiting_task"], completed_by="mail_reply")
            except Exception:
                pass
        cleared.append(key)
    return cleared


def nudge_text(subject: str) -> str:
    subj = re.sub(r"\s+", " ", subject or "").strip()[:80] or "(no subject)"
    return f"Nudge: no reply yet on “{subj}”"


def state_json(st: dict) -> str:
    return json.dumps(st, default=str)
