"""Rules and thread actions for the MAIL view (2.0 Phase 3).

GET    /api/mail/rules                    rules + what a rule may do
POST   /api/mail/rules                    {rule, dry_run_ack}
PUT    /api/mail/rules/<id>               {rule, dry_run_ack}
DELETE /api/mail/rules/<id>
POST   /api/mail/rules/dry-run            {rule}: would match N in the last 30 days
GET    /api/mail/rules/runs?since=        recent runs (each carries an action_id for undo)
POST   /api/mail/thread/<op>              pin, mute, snooze, waiting, split, merge, unsubscribe

Every thread op returns an ``action_id``; POST /api/mail/undo/<id> reverses
it. Nothing here sends mail or changes a project's status, stage or money.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from flask import Blueprint, jsonify, request

from ..mail import rules, terminal
from ..mail import threading as thr
from ._common import body, error_response, store as board_store
from .mail_client_api import _fail, _guard, _mail_store_for, _sid

bp = Blueprint("rules_api", __name__)


def _ctx():
    prov, err = _guard()
    if err:
        return None, None, None, err
    st = _mail_store_for(prov)
    if st is None:
        return None, None, None, error_response(409, "Rules need the mail store on this computer.")
    return prov, st, getattr(prov, "account_id", prov.id), None


@bp.route("/api/mail/rules", methods=["GET"])
def api_rules():
    prov, st, acct, err = _ctx()
    if err:
        return err
    return jsonify({"ok": True, "rules": rules.list_rules(st), "conditions": list(rules.CONDITION_FIELDS),
                    "actions": list(rules.ACTIONS), "mailbox_actions": sorted(rules.MAILBOX_ACTIONS),
                    "dry_run_days": rules.DRY_RUN_DAYS})


def _save(rule_id=None):
    prov, st, acct, err = _ctx()
    if err:
        return err
    data = body({}) or {}
    try:
        r = rules.save_rule(st, data.get("rule") or {}, rule_id=rule_id, dry_run_ack=bool(data.get("dry_run_ack")))
    except rules.RuleError as e:
        return error_response(400, str(e))
    except LookupError as e:
        return error_response(404, str(e))
    return jsonify({"ok": True, "rule": r})


@bp.route("/api/mail/rules", methods=["POST"])
def api_rule_create():
    return _save()


@bp.route("/api/mail/rules/<int:rule_id>", methods=["PUT"])
def api_rule_update(rule_id):
    return _save(rule_id)


@bp.route("/api/mail/rules/<int:rule_id>", methods=["DELETE"])
def api_rule_delete(rule_id):
    prov, st, acct, err = _ctx()
    if err:
        return err
    if not rules.delete_rule(st, rule_id):
        return error_response(404, "No such rule.")
    return jsonify({"ok": True})


@bp.route("/api/mail/rules/dry-run", methods=["POST"])
def api_rule_dry_run():
    prov, st, acct, err = _ctx()
    if err:
        return err
    try:
        out = rules.dry_run(st, acct, (body({}) or {}).get("rule") or {})
    except rules.RuleError as e:
        return error_response(400, str(e))
    out["ok"] = True
    return jsonify(out)


@bp.route("/api/mail/rules/runs", methods=["GET"])
def api_rule_runs():
    prov, st, acct, err = _ctx()
    if err:
        return err
    try:
        since = float(request.args.get("since") or 0)
    except ValueError:
        since = 0
    return jsonify({"ok": True, "runs": rules.recent_runs(st, acct, since=since), "now": time.time()})


# ── thread ops ───────────────────────────────────────────────────────────────

def _until(data, default_hours=None):
    """An absolute time from {until: ISO} or {hours: n} or {days: n}."""
    if data.get("until"):
        try:
            dt = datetime.fromisoformat(str(data["until"]).replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            raise ValueError("That date is not readable.")
    for k, mult in (("hours", 3600), ("days", 86400)):
        if data.get(k) is not None:
            return time.time() + float(data[k]) * mult
    if default_hours is not None:
        return time.time() + default_hours * 3600
    raise ValueError("Pick a time.")


def _unsub_options(raw: str) -> dict:
    links = re.findall(r"<([^<>]+)>", raw or "")
    web = next((u for u in links if u.lower().startswith("https://")), "")
    mail = next((u for u in links if u.lower().startswith("mailto:")), "")
    return {"web": web, "mailto": mail}


@bp.route("/api/mail/thread/<op>", methods=["POST"])
def api_thread_op(op):
    prov, st, acct, err = _ctx()
    if err:
        return err
    data = body({}) or {}
    ref = _sid(prov, data.get("ref") or "")
    if not ref:
        return error_response(400, "Pick a message first.")
    row = st.get(acct, ref)
    if not row:
        return error_response(404, "That message is not in the local store yet. Refresh and try again.")
    key = row.get("thread_key") or ""
    subj = (row.get("subject") or "(no subject)")[:60]
    board = board_store()
    try:
        if op == "unsubscribe":
            opts = _unsub_options(row.get("list_unsub") or "")
            if not opts["web"] and not opts["mailto"]:
                return error_response(404, "This sender did not include an unsubscribe link.")
            # Nothing is sent from here: the page opens in the browser, or a
            # mailto becomes a draft the user sends.
            return jsonify({"ok": True, **opts})
        if op in ("split", "merge"):
            if op == "split":
                res = thr.split(st, acct, ref)
                label = f"Split “{subj}” into its own thread"
            else:
                into = _sid(prov, data.get("into") or "")
                if not into:
                    return error_response(400, "Pick the thread to merge into.")
                res = thr.merge(st, acct, ref, into)
                label = f"Merge “{subj}” into another thread"
            aid = terminal.record(st, acct, "override", ref, res["undo"], label)
            return jsonify({"ok": True, "thread_key": res["thread_key"], "action_id": aid, "label": label})
        if not key:
            return error_response(409, "This message has no thread yet.")
        extra = {}
        if op == "pin":
            on = bool(data.get("on", not thr.get_state(st, acct, key)["pinned"]))
            prior = thr.set_state(st, acct, key, pinned=int(on))
            label = ("Pin" if on else "Unpin") + f" “{subj}”"
        elif op == "mute":
            on = bool(data.get("on", not thr.get_state(st, acct, key)["muted"]))
            prior = thr.set_state(st, acct, key, muted=int(on))
            label = ("Mute" if on else "Unmute") + f" “{subj}”"
        elif op == "snooze":
            if data.get("off"):
                prior = thr.set_state(st, acct, key, snooze_until=None)
                label = f"Wake “{subj}”"
            else:
                until = _until(data)
                prior = thr.set_state(st, acct, key, snooze_until=until)
                label = f"Snooze “{subj}”"
                extra["until"] = until
        elif op == "waiting":
            state = thr.get_state(st, acct, key)
            if data.get("off"):
                prior = thr.set_state(st, acct, key, waiting_until=None, waiting_since=None, waiting_task="")
                label = f"Stop waiting on “{subj}”"
            else:
                until = _until(data, default_hours=72)
                pid = state.get("project_id") or (terminal.context(st, acct, ref, board=board).get("project") or {}).get("id")
                task = ""
                if pid and board is not None:
                    day = datetime.fromtimestamp(until).strftime("%Y-%m-%d")
                    task = board.add_subtask(pid, thr.nudge_text(row.get("subject")), date_type="follow_up",
                                             follow_up_date=day, source="mail", allow_if_completed=True) or ""
                prior = thr.set_state(st, acct, key, waiting_until=until, waiting_since=time.time(), waiting_task=task)
                label = f"Wait for a reply on “{subj}”"
                extra.update(until=until, project_id=pid, task=task)
        else:
            return error_response(404, f"Unknown thread action: {op}.")
        undo = {"thread_key": key, "prior": prior}
        if extra.get("task"):
            undo.update(task=extra["task"], project_id=extra.get("project_id"))
        aid = terminal.record(st, acct, "thread", ref, undo, label)
        return jsonify({"ok": True, "thread_key": key, "action_id": aid, "label": label,
                        "state": thr.get_state(st, acct, key), **extra})
    except ValueError as e:
        return error_response(400, str(e))
    except LookupError as e:
        return error_response(404, str(e))
    except Exception as e:  # noqa: BLE001
        return _fail(e)
