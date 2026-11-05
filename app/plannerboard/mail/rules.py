"""Mail rules (2.0 Phase 3, plan 5.2).

A rule is a set of conditions (all must hold) and a list of actions. Rules
run locally right after the capture path stores new mail, and only on a
message that:

- arrived at or after the moment the rule was created (``received_ts >=
  created_at``): a rule never reaches back into mail you already had;
- is not historical (before the install cutoff) and was taken by the capture
  path (the backfill job never runs rules);
- was not sent by you.

What a rule can do is the frozen ``ACTIONS`` tuple below and nothing else.
Status, stage, money and sending are absent on purpose: inbound mail must
never move a project, and nothing sends by itself. Filing a message to a
project only adds the email to the project's correspondence.

Mailbox-changing actions (star, skip inbox, archive) need ``dry_run_ack``:
the user saw "would match N in the last 30 days" and agreed. Every run is
written to ``mail_rule_runs`` with what it did, and is undoable.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from .message import text_from_html

CONDITION_FIELDS = ("from", "to", "subject", "body", "has_attachment", "list_id", "project")
ACTIONS = ("file_to_project", "label", "star", "snooze", "skip_inbox", "notify", "archive")
MAILBOX_ACTIONS = frozenset({"star", "skip_inbox", "archive"})
DRY_RUN_DAYS = 30


class RuleError(ValueError):
    pass


# ── validation and storage ───────────────────────────────────────────────────

def validate(rule: dict) -> dict:
    name = str(rule.get("name") or "").strip()[:120]
    conds, acts = [], []
    for c in rule.get("conditions") or []:
        f = str((c or {}).get("field") or "")
        if f not in CONDITION_FIELDS:
            raise RuleError(f"Unknown condition: {f or '(blank)'}.")
        v = (c or {}).get("value")
        if f == "has_attachment":
            v = bool(v) if not isinstance(v, str) else v.lower() not in ("", "0", "false", "no")
        else:
            v = str(v or "").strip()[:500]
            if not v:
                raise RuleError(f"The {f} condition needs a value.")
        conds.append({"field": f, "value": v})
    if not conds:
        raise RuleError("A rule needs at least one condition.")
    for a in rule.get("actions") or []:
        t = str((a or {}).get("type") or "")
        if t not in ACTIONS:
            raise RuleError(f"A rule cannot do that: {t or '(blank)'}.")
        v = (a or {}).get("value")
        if t in ("file_to_project", "label") and not str(v or "").strip():
            raise RuleError(f"The {t.replace('_', ' ')} action needs a value.")
        if t == "snooze":
            try:
                v = max(1, min(int(v or 24), 24 * 60))
            except (TypeError, ValueError):
                raise RuleError("Snooze takes a number of hours.")
        else:
            v = str(v or "").strip()[:200]
        acts.append({"type": t, "value": v})
    if not acts:
        raise RuleError("A rule needs at least one action.")
    return {"name": name or "Rule", "conditions": conds, "actions": acts, "enabled": bool(rule.get("enabled", True))}


def changes_mailbox(rule: dict) -> bool:
    return any(a["type"] in MAILBOX_ACTIONS for a in rule.get("actions") or [])


def _out(r: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "enabled": bool(r["enabled"]),
            "conditions": json.loads(r["conditions"] or "[]"), "actions": json.loads(r["actions"] or "[]"),
            "dry_run_ack": r["dry_run_ack"], "created_at": r["created_at"], "updated_at": r["updated_at"]}


def list_rules(store) -> list[dict]:
    return [_out(r) for r in store.query("SELECT * FROM mail_rules ORDER BY id")]


def get_rule(store, rule_id: int) -> dict | None:
    rows = store.query("SELECT * FROM mail_rules WHERE id = ?", [int(rule_id)])
    return _out(rows[0]) if rows else None


def save_rule(store, rule: dict, rule_id: int | None = None, dry_run_ack: bool = False) -> dict:
    """Create or update. A rule that changes the mailbox is refused unless the
    caller passes ``dry_run_ack`` (the user saw the dry run). Editing a rule
    never moves its ``created_at``: mail before it still never matches."""
    v = validate(rule)
    if changes_mailbox(v) and not dry_run_ack:
        raise RuleError("This rule changes your mailbox. Run the dry run and confirm it first.")
    now = time.time()
    ack = now if changes_mailbox(v) else None
    with store._conn() as c:
        if rule_id is None:
            cur = c.execute("INSERT INTO mail_rules (name, enabled, conditions, actions, dry_run_ack, created_at,"
                            " updated_at) VALUES (?,?,?,?,?,?,?)",
                            (v["name"], int(v["enabled"]), json.dumps(v["conditions"]), json.dumps(v["actions"]),
                             ack, now, now))
            rule_id = cur.lastrowid
        else:
            n = c.execute("UPDATE mail_rules SET name=?, enabled=?, conditions=?, actions=?, dry_run_ack=?,"
                          " updated_at=? WHERE id=?",
                          (v["name"], int(v["enabled"]), json.dumps(v["conditions"]), json.dumps(v["actions"]),
                           ack, now, int(rule_id))).rowcount
            if not n:
                raise LookupError("No such rule.")
    return get_rule(store, rule_id)


def delete_rule(store, rule_id: int) -> bool:
    with store._conn() as c:
        return bool(c.execute("DELETE FROM mail_rules WHERE id=?", (int(rule_id),)).rowcount)


# ── matching ─────────────────────────────────────────────────────────────────

def _body_text(row: dict) -> str:
    b = row.get("body") or ""
    if isinstance(b, (bytes, bytearray)):
        b = b.decode("utf-8", "replace")
    return text_from_html(b) if row.get("is_html") else b


def matches(rule: dict, row: dict) -> bool:
    for c in rule["conditions"]:
        f, v = c["field"], c["value"]
        low = v.lower() if isinstance(v, str) else v
        if f == "from":
            ok = low in f"{row.get('from_name') or ''} <{row.get('from_email') or ''}>".lower()
        elif f == "to":
            ok = low in f"{row.get('to_addrs') or ''} {row.get('cc_addrs') or ''}".lower()
        elif f == "subject":
            ok = low in (row.get("subject") or "").lower()
        elif f == "body":
            ok = low in _body_text(row).lower()
        elif f == "has_attachment":
            ok = bool(row.get("has_attachments")) == bool(v)
        elif f == "list_id":
            ok = low in (row.get("list_id") or "").lower()
        elif f == "project":
            pid = row.get("thread_project") or ""
            ok = bool(pid) if low == "any" else (not pid) if low == "none" else pid == v
        else:
            ok = False
        if not ok:
            return False
    return True


_ROW_SQL = ("SELECT m.*, b.body, b.is_html, COALESCE(t.project_id, '') AS thread_project FROM mail_messages m"
            " LEFT JOIN mail_bodies b ON b.msg_rowid = m.rowid"
            " LEFT JOIN mail_threads t ON t.account_id = m.account_id AND t.thread_key = m.thread_key")


def dry_run(store, account_id: str, rule: dict, days: int = DRY_RUN_DAYS, sample: int = 5) -> dict:
    """How many stored messages from the last ``days`` the rule would match
    (a preview only: the rule itself never runs on them)."""
    v = validate(rule)
    since = time.time() - days * 86400
    n, examples = 0, []
    for r in store.query(_ROW_SQL + " WHERE m.account_id=? AND m.removed=0 AND m.folder != 'sent'"
                         " AND m.received_ts >= ? ORDER BY m.received_ts DESC", [account_id, since]):
        if matches(v, r):
            n += 1
            if len(examples) < sample:
                examples.append({"subject": r["subject"], "from": r["from_name"] or r["from_email"]})
    return {"matches": n, "days": days, "examples": examples, "changes_mailbox": changes_mailbox(v)}


# ── running ──────────────────────────────────────────────────────────────────

def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else ""


def run_on_ingest(store, board, provider, account_id: str, provider_ids, me: str = "") -> list[dict]:
    """Run every enabled rule over messages the capture path just stored."""
    rules = [r for r in list_rules(store) if r["enabled"]]
    ids = [p for p in provider_ids if p]
    if not rules or not ids:
        return []
    rows = []
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        rows += store.query(_ROW_SQL + f" WHERE m.account_id=? AND m.provider_id IN ({','.join('?' * len(chunk))})"
                            " AND m.historical=0 AND m.captured=1 AND m.removed=0 AND m.folder != 'sent'",
                            [account_id] + chunk)
    me = (me or "").lower()
    runs = []
    for row in sorted(rows, key=lambda r: r["received_ts"] or 0):
        if me and (row["from_email"] or "").lower() == me:
            continue
        for rule in rules:
            if row["received_ts"] is None or row["received_ts"] < rule["created_at"]:
                continue
            if not matches(rule, row):
                continue
            run = _run(store, board, provider, account_id, rule, row)
            if run:
                runs.append(run)
                row = store.query(_ROW_SQL + " WHERE m.account_id=? AND m.provider_id=?",
                                  [account_id, run["ref"]])[0] if run.get("ref") else row
    return runs


def _run(store, board, provider, account_id, rule, row) -> dict | None:
    from . import terminal, threading as thr
    ref = row["provider_id"]
    with store._conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO mail_rule_runs (rule_id, account_id, provider_id, msg_rowid, ran_at)"
                        " VALUES (?,?,?,?,?)", (rule["id"], account_id, ref, row["rowid"], time.time()))
        if not cur.rowcount:
            return None                            # this rule already ran on this message
        run_id = cur.lastrowid
    done = []
    allow_mailbox = bool(rule.get("dry_run_ack"))
    for a in rule["actions"]:
        t, v = a["type"], a["value"]
        if t not in ACTIONS:
            continue
        if t in MAILBOX_ACTIONS and not allow_mailbox:
            done.append({"type": t, "skipped": "not confirmed"})
            continue
        try:
            if t == "file_to_project":
                done.append(_file(store, board, account_id, row, v))
            elif t == "label":
                labels = json.loads(row.get("local_labels") or "[]")
                added = v not in labels
                if added:
                    with store._conn() as c:
                        c.execute("UPDATE mail_messages SET local_labels=? WHERE rowid=?",
                                  (json.dumps(sorted(set(labels) | {v})), row["rowid"]))
                row = dict(row, local_labels=json.dumps(sorted(set(labels) | {v})))
                done.append({"type": t, "label": v, "added": added})
            elif t == "star":
                prior = bool(row.get("is_starred"))
                if not prior:
                    provider.set_flags(ref, flagged=True)
                    terminal.apply_flags(store, account_id, ref, flagged=True)
                    row = dict(row, is_starred=1)
                done.append({"type": t, "prior": prior})
            elif t == "snooze":
                key = row.get("thread_key") or ""
                prior = thr.set_state(store, account_id, key, snooze_until=time.time() + int(v) * 3600)
                done.append({"type": t, "thread_key": key, "prior": prior})
            elif t in ("skip_inbox", "archive"):
                if row.get("folder") != "inbox":
                    done.append({"type": t, "noop": "already out of the inbox"})
                    continue
                prior_read = bool(row.get("is_read"))
                if t == "skip_inbox" and not prior_read:
                    provider.set_flags(ref, read=True)
                    terminal.apply_flags(store, account_id, ref, read=True)
                data = terminal.undo_data(store, account_id, ref, provider) or {}
                new_ref = provider.move_message(ref, "archive")
                sid = terminal.apply_move(store, account_id, ref, "archive", new_ref, provider)
                data["new_ref"] = sid or new_ref or ref
                if not callable(getattr(provider, "restore_labels", None)):
                    data.pop("prior_labels", None)
                done.append({"type": t, "move": data, "prior_read": prior_read})
                ref = data["new_ref"]
                row = dict(row, folder="archive", provider_id=ref, is_read=1 if t == "skip_inbox" else row.get("is_read"))
            elif t == "notify":
                aid = None
                if board is not None:
                    aid = board.add_alert(f"mail_rule:{rule['id']}:{row['rowid']}",
                                          f"{rule['name']}: {row.get('subject') or '(no subject)'}"[:200],
                                          severity="info", detail=f"From {row.get('from_name') or row.get('from_email')}",
                                          source="rules")
                done.append({"type": t, "alert_id": aid})
        except Exception as e:  # noqa: BLE001 - one failed action never stops the rest
            done.append({"type": t, "error": f"{type(e).__name__}: {str(e)[:160]}"})
    label = f"Rule “{rule['name']}” on “{(row.get('subject') or '(no subject)')[:60]}”"
    action_id = terminal.record(store, account_id, "rule_run", ref, {"run_id": run_id}, label)
    with store._conn() as c:
        c.execute("UPDATE mail_rule_runs SET done=?, action_id=?, provider_id=? WHERE id=?",
                  (json.dumps(done), action_id, ref, run_id))
    return {"run_id": run_id, "rule_id": rule["id"], "rule": rule["name"], "ref": ref, "done": done,
            "action_id": action_id, "label": label}


def _file(store, board, account_id, row, pid) -> dict:
    from . import threading as thr
    conn = getattr(board, "conn", None) if board is not None else None
    if conn is None or not conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
        return {"type": "file_to_project", "skipped": "no such project"}
    mid = row.get("message_id") or ""
    existed = bool(mid) and conn.execute("SELECT 1 FROM project_correspondence WHERE project_id=? AND message_id=?",
                                         (pid, mid)).fetchone() is not None
    corr_id = None
    if not existed:
        corr_id = board.save_correspondence(pid, {
            "message_id": mid, "conversation_id": row.get("thread_key") or "", "subject": row.get("subject") or "",
            "from_name": row.get("from_name") or "", "from_email": row.get("from_email") or "",
            "to": row.get("to_addrs") or "", "cc": row.get("cc_addrs") or "", "direction": "inbound",
            "received_at": _iso(row.get("received_ts")), "body_preview": row.get("snippet") or "",
            "body_html": _body(row)}, source="auto_commit")
    prior = {}
    key = row.get("thread_key") or ""
    if key and not (row.get("thread_project") or ""):
        prior = thr.set_state(store, account_id, key, project_id=pid)
    return {"type": "file_to_project", "project_id": pid, "corr_id": corr_id, "thread_key": key, "prior": prior}


def _body(row) -> str:
    b = row.get("body") or ""
    return b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b


def undo_run(store, provider, board, account_id: str, run_id: int) -> dict:
    from . import terminal, threading as thr
    rows = store.query("SELECT * FROM mail_rule_runs WHERE id=? AND account_id=?", [int(run_id), account_id])
    if not rows:
        raise LookupError("That rule run is gone.")
    run = rows[0]
    if run["undone"]:
        return {"ref": run["provider_id"]}
    ref = run["provider_id"]
    for d in reversed(json.loads(run["done"] or "[]")):
        if d.get("skipped") or d.get("error") or d.get("noop"):
            continue
        t = d.get("type")
        if t == "file_to_project":
            if d.get("corr_id") and board is not None:
                board.delete_correspondence(d["project_id"], int(d["corr_id"]))
            if d.get("prior") and d.get("thread_key"):
                thr.set_state(store, account_id, d["thread_key"], **d["prior"])
        elif t == "label" and d.get("added"):
            r = store.query("SELECT rowid, local_labels FROM mail_messages WHERE account_id=? AND provider_id=?",
                            [account_id, ref])
            if r:
                labels = [x for x in json.loads(r[0]["local_labels"] or "[]") if x != d["label"]]
                with store._conn() as c:
                    c.execute("UPDATE mail_messages SET local_labels=? WHERE rowid=?", (json.dumps(labels), r[0]["rowid"]))
        elif t == "star" and not d.get("prior"):
            provider.set_flags(ref, flagged=False)
            terminal.apply_flags(store, account_id, ref, flagged=False)
        elif t == "snooze":
            thr.set_state(store, account_id, d.get("thread_key") or "", **(d.get("prior") or {}))
        elif t in ("skip_inbox", "archive"):
            mv = d.get("move") or {}
            at = mv.get("new_ref") or ref
            if mv.get("prior_labels") and callable(getattr(provider, "restore_labels", None)):
                labels = provider.restore_labels(at, mv["prior_labels"])
                from .gmail import folder_for_labels
                terminal.set_labels(store, account_id, at, labels, folder_for_labels(labels))
                ref = at
            else:
                back = mv.get("from_folder") or "inbox"
                nr = provider.move_message(at, back)
                ref = terminal.apply_move(store, account_id, at, mv.get("from_kind") or back, nr, provider) or nr or at
            if t == "skip_inbox" and not d.get("prior_read"):
                provider.set_flags(ref, read=False)
                terminal.apply_flags(store, account_id, ref, read=False)
        elif t == "notify" and d.get("alert_id") and board is not None:
            board.ack_alert(d["alert_id"], by="undo")
    with store._conn() as c:
        c.execute("UPDATE mail_rule_runs SET undone=1 WHERE id=?", (int(run_id),))
    return {"ref": ref}


def recent_runs(store, account_id: str, since: float = 0, limit: int = 100) -> list[dict]:
    out = []
    for r in store.query("SELECT rr.*, mr.name FROM mail_rule_runs rr LEFT JOIN mail_rules mr ON mr.id = rr.rule_id"
                         " WHERE rr.account_id=? AND rr.ran_at > ? ORDER BY rr.ran_at DESC LIMIT ?",
                         [account_id, float(since or 0), int(limit)]):
        out.append({"run_id": r["id"], "rule_id": r["rule_id"], "rule": r["name"] or "(deleted rule)",
                    "ref": r["provider_id"], "ran_at": r["ran_at"], "done": json.loads(r["done"] or "[]"),
                    "action_id": r["action_id"], "undone": bool(r["undone"])})
    return out
