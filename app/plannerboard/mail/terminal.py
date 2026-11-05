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


def covered(store, account_id: str, folder: dict) -> bool:
    """The store answers for inbox and sent (the capture path keeps them) and,
    once the backfill has walked the whole mailbox, for every folder."""
    kind = (folder.get("kind") or "other").lower()
    if kind in ("inbox", "sent") and not str(folder.get("id") or "").startswith("Label_"):
        return True
    from .sync import state
    return bool(state(store, account_id).get("done"))


def list_rows(store, account_id: str, folder: dict, offset: int = 0, limit: int = 100) -> dict | None:
    f = folder_filter(folder)
    if f is None or not covered(store, account_id, folder):
        return None
    cond, args = f
    offset, limit = max(0, int(offset or 0)), max(1, min(int(limit or 100), ROW_LIMIT))
    cols = ("SELECT m.rowid, m.provider_id, m.subject, m.from_email, m.from_name, m.to_addrs, m.received_ts,"
            " m.snippet, m.has_attachments, m.is_read, m.is_starred, m.folder, m.thread_key, m.message_id,"
            " m.body_state, m.local_labels, m.list_unsub FROM mail_messages m")
    base = f" WHERE m.account_id = ? AND m.removed = 0 AND {cond}"
    # Thread state is a handful of threads: read it once and keep the main
    # query on the date index (an ORDER BY through a join would not be).
    states = {r["thread_key"]: r for r in store.query(
        "SELECT thread_key, pinned, muted, snooze_until, waiting_until, project_id FROM mail_threads"
        " WHERE account_id = ? AND (pinned = 1 OR muted = 1 OR snooze_until IS NOT NULL"
        " OR waiting_until IS NOT NULL OR project_id != '')", [account_id])}
    now = time.time()
    hidden, pinned = [], []
    if (folder.get("kind") or "").lower() == "inbox":
        # The inbox hides muted threads and snoozed ones until the snooze ends.
        hidden = [k for k, t in states.items() if t["muted"] or (t["snooze_until"] or 0) > now]
    pinned = [k for k, t in states.items() if t["pinned"] and k not in hidden]
    excl, eargs = "", []
    if hidden or pinned:
        keys = hidden + pinned
        excl = f" AND m.thread_key NOT IN ({','.join('?' * len(keys))})"
        eargs = keys
    top = []
    if pinned:
        top = store.query(cols + base + f" AND m.thread_key IN ({','.join('?' * len(pinned))})"
                          " ORDER BY m.received_ts DESC LIMIT 200", [account_id] + args + pinned)
    rows = top[offset:offset + limit]
    rest_off = max(0, offset - len(top))
    if len(rows) < limit:
        rows = rows + store.query(cols + base + excl + " ORDER BY m.received_ts DESC LIMIT ? OFFSET ?",
                                  [account_id] + args + eargs + [limit - len(rows), rest_off])
    total = len(top) + store.query("SELECT COUNT(*) AS n FROM mail_messages m" + base + excl,
                                   [account_id] + args + eargs)[0]["n"]
    out = []
    for r in rows:
        t = states.get(r["thread_key"])
        out.append({
            "ref": r["provider_id"], "id": r["message_id"] or r["provider_id"], "subject": r["subject"] or "",
            "from": r["from_name"] or r["from_email"] or "", "from_email": r["from_email"] or "",
            "to": r["to_addrs"] or "", "date": _iso(r["received_ts"]), "preview": r["snippet"] or "",
            "is_read": bool(r["is_read"]), "flagged": bool(r["is_starred"]),
            "has_attachments": bool(r["has_attachments"]), "folder": r["folder"],
            "conversation_id": r["thread_key"], "internet_message_id": r["message_id"],
            "body_local": r["body_state"] == "full",
            "labels_local": json.loads(r["local_labels"] or "[]"), "pinned": bool(t and t["pinned"]),
            "muted": bool(t and t["muted"]), "snoozed_until": _iso(t["snooze_until"]) if t else None,
            "waiting_until": _iso(t["waiting_until"]) if t else None,
            "project_id": (t["project_id"] if t else "") or None,
            "can_unsubscribe": bool(r["list_unsub"]),
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


def apply_move(store, account_id: str, ref: str, dest_kind: str, new_ref: str | None, provider=None) -> str | None:
    """The message left its folder; keep the row in step with the server.

    - Gmail: re-read the labels (a move adds and drops several, user labels
      included) and derive the folder from them.
    - Graph and IMAP give the message a new id on move: the row takes the new
      id, so it stays listed and an undo can find it.
    Returns the message's id in the store after the move."""
    row = store.get(account_id, ref)
    if not row:
        return None
    kind = dest_kind if dest_kind in STANDARD_KINDS else "other"
    if provider is not None and callable(getattr(provider, "labels_of", None)):
        try:
            labels = provider.labels_of(ref)
        except Exception:
            labels = None
        if labels is not None:
            from .gmail import folder_for_labels
            set_labels(store, account_id, ref, labels, folder_for_labels(labels))
            return ref
    sid = new_ref
    if provider is not None and callable(getattr(provider, "store_id", None)):
        sid = provider.store_id(new_ref)
    if sid and sid != ref:
        if store.has(account_id, sid, live_only=False):
            store.mark_removed(account_id, ref, f"moved to {kind}")
            return sid
        with store._conn() as c:
            c.execute("UPDATE mail_messages SET provider_id=?, folder=?, updated_at=? WHERE account_id=? AND provider_id=?",
                      (sid, kind, time.time(), account_id, ref))
        return sid
    with store._conn() as c:
        c.execute("UPDATE mail_messages SET folder=?, updated_at=? WHERE account_id=? AND provider_id=?",
                  (kind, time.time(), account_id, ref))
    return ref


def set_labels(store, account_id: str, ref: str, labels, folder: str) -> None:
    row = store.get(account_id, ref)
    if not row:
        return
    new = set(labels or ())
    old = set(row["labels"])
    store.apply_labels(account_id, ref, added=new - old, removed=old - new, folder_fn=lambda _l: folder)


def undo_data(store, account_id: str, ref: str, provider) -> dict | None:
    """What a move needs to be reversed, taken from the message's own row (not
    from the folder on screen: a message opened from search, the palette or
    the timeline does not live there)."""
    row = store.get(account_id, ref)
    if not row:
        return None
    back = row.get("folder") or "inbox"
    if callable(getattr(provider, "undo_folder", None)):
        back = provider.undo_folder(row)
    return {"from_kind": row.get("folder") or "inbox", "from_folder": back, "prior_labels": row.get("labels") or []}


# ── undo ─────────────────────────────────────────────────────────────────────

def record(store, account_id: str, kind: str, ref: str, data: dict, label: str) -> str:
    aid = _secrets.token_urlsafe(10)
    with store._conn() as c:
        c.execute("INSERT INTO mail_undo (action_id, account_id, kind, ref, data, label, created_at)"
                  " VALUES (?,?,?,?,?,?,?)", (aid, account_id, kind, ref, json.dumps(data), label, time.time()))
        # Keep the last 500 of your own actions and, separately, the last 2000
        # rule runs: a busy rule never pushes your own undo history out.
        c.execute("DELETE FROM mail_undo WHERE kind != 'rule_run' AND action_id NOT IN"
                  " (SELECT action_id FROM mail_undo WHERE kind != 'rule_run' ORDER BY created_at DESC LIMIT 500)")
        c.execute("DELETE FROM mail_undo WHERE kind = 'rule_run' AND action_id NOT IN"
                  " (SELECT action_id FROM mail_undo WHERE kind = 'rule_run' ORDER BY created_at DESC LIMIT 2000)")
    return aid


def undo(store, provider, account_id: str, action_id: str, board=None) -> dict:
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
        if data.get("prior_labels") and callable(getattr(provider, "restore_labels", None)):
            labels = provider.restore_labels(at, data["prior_labels"])
            from .gmail import folder_for_labels
            set_labels(store, account_id, at, labels, folder_for_labels(labels))
            out = {"ref": at}
        else:
            back = data.get("from_folder") or "inbox"
            nr = provider.move_message(at, back)
            sid = apply_move(store, account_id, at, data.get("from_kind") or back, nr, provider)
            out = {"ref": sid or nr or at}
    elif a["kind"] == "thread":
        from . import threading as thr
        thr.set_state(store, account_id, data.get("thread_key") or "", **(data.get("prior") or {}))
        tu = data.get("task_undo") or {}
        if tu and board is not None:
            if tu["op"] == "delete":
                board.delete_subtask(tu["project_id"], tu["task"])
            elif tu["op"] == "reopen":
                board.reopen_subtask(tu["project_id"], tu["task"])
            elif tu["op"] == "date":
                board.update_subtask_fields(tu["project_id"], tu["task"], follow_up_date=tu.get("date"))
        out = {"ref": a["ref"]}
    elif a["kind"] == "override":
        from . import threading as thr
        thr.undo_overrides(store, account_id, data)
        out = {"ref": a["ref"]}
    elif a["kind"] == "rule_run":
        from . import rules
        out = rules.undo_run(store, provider, board, account_id, int(data.get("run_id") or 0))
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
