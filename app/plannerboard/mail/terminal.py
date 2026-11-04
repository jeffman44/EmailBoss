"""Store-backed reads and write bookkeeping for the MAIL view (2.0 Phase 2).

The list and the reading pane read from the local store; the provider is
used for writes (flags, move, trash, send), for an explicit refresh and for
a body the store does not hold (outside the body window). After every
write the store row is updated at once, so the list never waits for the
next capture cycle to reflect what the user just did.

Undo: every mutating list action is recorded in ``mail_undo`` with what is
needed to reverse it; ``undo(action_id)`` runs the inverse through the
provider. Nothing here sends mail.
"""
from __future__ import annotations

import json
import secrets as _secrets
import time
from datetime import datetime, timezone

STANDARD_KINDS = ("inbox", "sent", "drafts", "trash", "junk", "archive")
ROW_LIMIT = 500


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def client_ref(provider_id: str, row: dict) -> str:
    """The ref the client API's provider understands for a store row."""
    pid = row.get("provider_id") or ""
    if provider_id == "gmail":
        return pid                        # gid; get_message splits on '|'
    return pid                            # Graph id; IMAP capture or x: ids (imap resolves both)


def folder_filter(folder: dict) -> tuple[str, list] | None:
    """SQL for one rail folder, or None when the store cannot answer for it
    (a provider-only folder the store does not distinguish)."""
    kind = (folder.get("kind") or "other").lower()
    fid = str(folder.get("id") or "")
    if fid == "__all__":
        return "m.folder NOT IN ('trash', 'junk')", []
    if kind in STANDARD_KINDS:
        return "m.folder = ?", [kind]
    if fid.startswith("Label_"):
        return "m.labels LIKE ?", [f'%"{fid}"%']
    if folder.get("provider") == "imap" or folder.get("path"):
        esc = fid.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return "m.provider_id LIKE ? ESCAPE '\\'", [f"x:{esc}:%"]
    return None


def list_rows(store, account_id: str, folder: dict, offset: int = 0, limit: int = 100) -> dict | None:
    f = folder_filter(folder)
    if f is None:
        return None
    cond, args = f
    limit = max(1, min(int(limit or 100), ROW_LIMIT))
    rows = store.query(
        "SELECT m.rowid, m.provider_id, m.subject, m.from_email, m.from_name, m.to_addrs, m.received_ts,"
        " m.snippet, m.has_attachments, m.is_read, m.is_starred, m.folder, m.thread_key, m.message_id, m.body_state"
        f" FROM mail_messages m WHERE m.account_id = ? AND m.removed = 0 AND {cond}"
        " ORDER BY m.received_ts DESC LIMIT ? OFFSET ?", [account_id] + args + [limit, max(0, int(offset or 0))])
    total = store.query(f"SELECT COUNT(*) AS n FROM mail_messages m WHERE m.account_id = ? AND m.removed = 0 AND {cond}",
                        [account_id] + args)[0]["n"]
    out = []
    for r in rows:
        out.append({
            "ref": r["provider_id"], "id": r["message_id"] or r["provider_id"], "subject": r["subject"] or "",
            "from": r["from_name"] or r["from_email"] or "", "from_email": r["from_email"] or "",
            "to": r["to_addrs"] or "", "date": _iso(r["received_ts"]), "preview": r["snippet"] or "",
            "is_read": bool(r["is_read"]), "flagged": bool(r["is_starred"]),
            "has_attachments": bool(r["has_attachments"]), "folder": r["folder"],
            "conversation_id": r["thread_key"], "internet_message_id": r["message_id"],
            "body_local": r["body_state"] == "full",
        })
    return {"messages": out, "total": int(total), "offset": int(offset or 0), "source": "store"}


def message(store, account_id: str, ref: str) -> dict | None:
    """The reading-pane payload from the store, or None when the body is not
    on this computer (the caller asks the provider)."""
    rows = store.query("SELECT m.*, b.body, b.is_html, b.reply_to, b.to_list, b.cc_list FROM mail_messages m"
                       " JOIN mail_bodies b ON b.msg_rowid = m.rowid"
                       " WHERE m.account_id = ? AND m.provider_id = ? AND m.removed = 0", [account_id, ref])
    if not rows:
        return None
    r = rows[0]
    atts = store.query("SELECT idx, name, content_type, size, is_inline, content_id FROM mail_attachments"
                       " WHERE msg_rowid = ? ORDER BY idx", [r["rowid"]])
    return {
        "ref": ref, "id": r["message_id"] or ref, "subject": r["subject"] or "",
        "from": (f"{r['from_name']} <{r['from_email']}>" if r["from_name"] else r["from_email"]) or "",
        "from_email": r["from_email"] or "", "to": r["to_addrs"] or "", "cc": r["cc_addrs"] or "",
        "reply_to": r["reply_to"] or "", "date": _iso(r["received_ts"]), "preview": r["snippet"] or "",
        "is_read": bool(r["is_read"]), "flagged": bool(r["is_starred"]),
        "has_attachments": bool(r["has_attachments"]), "importance": "normal",
        "conversation_id": r["thread_key"], "internet_message_id": r["message_id"], "folder": r["folder"],
        "body": r["body"] or "", "is_html": bool(r["is_html"]),
        "to_list": json.loads(r["to_list"] or "[]"), "cc_list": json.loads(r["cc_list"] or "[]"),
        "attachments": [{"index": a["idx"], "name": a["name"], "content_type": a["content_type"], "size": a["size"],
                         "is_inline": bool(a["is_inline"]), "content_id": a["content_id"] or None} for a in atts],
        "source": "store",
    }


# ── writes: keep the store in step ───────────────────────────────────────────

def apply_flags(store, account_id: str, ref: str, read=None, flagged=None) -> None:
    row = store.get(account_id, ref)
    if not row:
        return
    flags = set(row["flags"])
    labels = set(row["labels"])
    if read is True:
        flags.add("\\Seen")
        labels.discard("UNREAD")
    elif read is False:
        flags.discard("\\Seen")
        if labels:
            labels.add("UNREAD")
    if flagged is True:
        flags.add("\\Flagged")
        if labels:
            labels.add("STARRED")
    elif flagged is False:
        flags.discard("\\Flagged")
        labels.discard("STARRED")
    if labels != set(row["labels"]):
        store.apply_labels(account_id, ref, added=labels - set(row["labels"]), removed=set(row["labels"]) - labels)
    store.set_flags(account_id, ref, flags)


def apply_move(store, account_id: str, ref: str, dest_kind: str, new_ref: str | None) -> None:
    """The message left its folder. Same id (Gmail, Graph): the folder changes.
    New id (IMAP MOVE/COPY): the old row is gone; the new one arrives with the
    next backfill or capture."""
    row = store.get(account_id, ref)
    if not row:
        return
    kind = dest_kind if dest_kind in STANDARD_KINDS else "other"
    if new_ref and new_ref != ref:
        store.mark_removed(account_id, ref, f"moved to {kind}")
        return
    with store._conn() as c:
        c.execute("UPDATE mail_messages SET folder=?, updated_at=? WHERE account_id=? AND provider_id=?",
                  (kind, time.time(), account_id, ref))


# ── undo ─────────────────────────────────────────────────────────────────────

def record(store, account_id: str, kind: str, ref: str, data: dict, label: str) -> str:
    aid = _secrets.token_urlsafe(10)
    with store._conn() as c:
        c.execute("INSERT INTO mail_undo (action_id, account_id, kind, ref, data, label, created_at)"
                  " VALUES (?,?,?,?,?,?,?)", (aid, account_id, kind, ref, json.dumps(data), label, time.time()))
        # Keep the last 500; undo is a short-term safety net, not a history.
        c.execute("DELETE FROM mail_undo WHERE action_id NOT IN"
                  " (SELECT action_id FROM mail_undo ORDER BY created_at DESC LIMIT 500)")
    return aid


def undo(store, provider, account_id: str, action_id: str) -> dict:
    rows = store.query("SELECT * FROM mail_undo WHERE action_id = ? AND account_id = ?", [action_id, account_id])
    if not rows:
        raise LookupError("That action can no longer be undone.")
    a = rows[0]
    if a["undone"]:
        return {"already": True, "label": a["label"]}
    data = json.loads(a["data"] or "{}")
    if a["kind"] == "flags":
        provider.set_flags(a["ref"], read=data.get("read"), flagged=data.get("flagged"))
        apply_flags(store, account_id, a["ref"], read=data.get("read"), flagged=data.get("flagged"))
        out = {"ref": a["ref"]}
    elif a["kind"] == "move":
        at = data.get("new_ref") or a["ref"]
        back = data.get("from_folder") or "inbox"
        nr = provider.move_message(at, back)
        apply_move(store, account_id, at, data.get("from_kind") or back, nr)
        out = {"ref": nr or at}
    else:
        raise LookupError(f"Email Boss cannot undo a {a['kind']} action.")
    with store._conn() as c:
        c.execute("UPDATE mail_undo SET undone=1 WHERE action_id=?", (action_id,))
    out.update(ok=True, label=a["label"])
    return out


# ── the context pane ─────────────────────────────────────────────────────────

def context(store, account_id: str, ref: str, board=None) -> dict:
    """Project, People, Money and Timeline for one open message."""
    row = store.get(account_id, ref)
    if not row:
        return {"thread": [], "people": [], "project": None}
    thread = []
    if row.get("thread_key"):
        thread = [{"ref": t["provider_id"], "subject": t["subject"], "from": t["from_name"] or t["from_email"],
                   "date": _iso(t["received_ts"]), "folder": t["folder"]}
                  for t in store.query("SELECT provider_id, subject, from_name, from_email, received_ts, folder"
                                       " FROM mail_messages WHERE account_id=? AND thread_key=? AND removed=0"
                                       " ORDER BY received_ts", [account_id, row["thread_key"]])]
    emails = {row.get("from_email") or ""}
    for part in (row.get("to_addrs") or "").split(","):
        if "<" in part:
            part = part.split("<", 1)[1].rstrip("> ")
        if "@" in part:
            emails.add(part.strip().lower())
    emails.discard("")
    people = []
    if emails:
        marks = ",".join("?" * len(emails))
        people = [{"email": p["email"], "name": p["name"], "sent": p["sent_count"], "received": p["recv_count"]}
                  for p in store.query(f"SELECT * FROM mail_people WHERE email IN ({marks})"
                                       " ORDER BY sent_count DESC, recv_count DESC", list(emails))]
    project = None
    conn = getattr(board, "conn", None) if board is not None else None
    if conn is not None:
        keys = [k for k in (row.get("message_id"), row.get("thread_key")) if k]
        pid = None
        if keys:
            marks = ",".join("?" * len(keys))
            hit = conn.execute(f"SELECT project_id FROM project_correspondence WHERE message_id IN ({marks})"
                               f" OR conversation_id IN ({marks}) ORDER BY id DESC LIMIT 1", keys + keys).fetchone()
            pid = hit[0] if hit else None
            if not pid and row.get("thread_key"):
                hit = conn.execute("SELECT project_id FROM conversation_routing WHERE conversation_id = ?",
                                   (row["thread_key"],)).fetchone()
                pid = hit[0] if hit else None
        if pid:
            p = conn.execute("SELECT id, name, status, quote_amount, approved_amount, follow_up_date"
                             " FROM projects WHERE id = ?", (pid,)).fetchone()
            if p:
                project = {"id": p[0], "name": p[1], "status": p[2], "quote_amount": p[3],
                           "approved_amount": p[4], "follow_up_date": p[5]}
    return {"thread": thread, "people": people, "project": project}
