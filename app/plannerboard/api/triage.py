"""
/api/triage: list, add, edit, approve, dismiss, dismissed list, revive,
status, run.

Ported from server.py 1554-1717 (list/add/update/approve dispatch),
5062-5137 (dismiss with filing, dismissed list, revive) and 8251-8319
(status, run). Build-config, acknowledgment and other install-specific card
types are gone: a card is an email card unless the triage layer says
otherwise, and approve routes everything through Store.approve_triage.
"""

from datetime import timedelta

from flask import Blueprint, jsonify, request

from ._common import body, error_response, rt, store

bp = Blueprint("triage", __name__)


@bp.route("/api/triage", methods=["GET"])
def api_list_triage():
    """Pending cards plus auto-committed cards from the last 24h (audit)."""
    db = store()
    items = db.list_triage("pending")
    cutoff = (db.hours.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    auto = [i for i in db.list_triage("auto_committed") if (i.get("staged_at") or "") >= cutoff]
    return jsonify({"ok": True, "items": items + auto})


@bp.route("/api/triage", methods=["POST"])
def api_add_triage():
    """Add one or more cards. An email card whose thread already routes to an
    OPEN project is auto-committed instead of staged (1574-1643)."""
    db = store()
    data = request.get_json(force=True, silent=True)
    items = data if isinstance(data, list) else [data] if isinstance(data, dict) else None
    if items is None:
        return error_response(400, "Expected JSON object or array")
    staged = auto_committed = deduped = 0
    ids = []
    for item in items:
        if not isinstance(item, dict):
            continue
        msg_id = item.get("message_id")
        if msg_id and db.message_seen(msg_id) and not item.get("force"):
            deduped += 1
            continue
        if item.get("card_type", "email") == "email":
            pid, reason = db.should_auto_commit(item)
            if pid:
                result = db.auto_commit_email(item, pid, reason=reason)
                item["status"] = "auto_committed"
                item["matched_project"] = pid
                item["match_reason"] = reason
                if isinstance(result, dict):
                    item.setdefault("signals", {})
                    if isinstance(item["signals"], dict):
                        item["signals"]["proposal_detected"] = result
                tid = db.add_triage_card(item)
                db.message_mark(msg_id, "auto_committed", pid=pid, triage_id=tid, subject=item.get("subject"),
                                received_at=item.get("received_at"))
                try:
                    db.file_correspondence(pid, item, [], source="auto_commit", triage_id=tid)
                except Exception:
                    pass
                auto_committed += 1
                ids.append(tid)
                continue
        tid = db.add_triage_card(item)
        if msg_id:
            db.message_mark(msg_id, "staged", triage_id=tid, subject=item.get("subject"),
                            received_at=item.get("received_at"))
        staged += 1
        ids.append(tid)
    db.meta_set("last_triage", db._now())
    return jsonify({"ok": True, "staged": staged, "auto_committed": auto_committed, "deduped": deduped,
                    "ids": ids, "total": db.count_pending_triage()})


@bp.route("/api/triage/<triage_id>", methods=["PUT"])
def api_update_triage(triage_id):
    db = store()
    data = dict(body())
    if "new_project_site" in data:
        data["new_project_location"] = data.pop("new_project_site")
    data["edited"] = 1
    data["edited_at"] = db._now()
    db.update_triage_card(triage_id, **data)
    card = db.get_triage_card(triage_id)
    if not card:
        return error_response(404, f"Triage item {triage_id} not found")
    return jsonify({"ok": True, "item": card})


@bp.route("/api/triage/<triage_id>/approve", methods=["POST"])
def api_approve_triage(triage_id):
    """Body: {matched_project | '__new__', workflow_id, status, work_type,
    new_project_name, new_project_location, quote_request, subtask_completions,
    dollar_amount}. Idempotent on a resolved card."""
    db = store()
    data = body()
    if not db.get_triage_card(triage_id):
        return error_response(404, f"Triage item {triage_id} not found")
    new_loc = data.get("new_project_location", data.get("new_project_site"))
    try:
        result = db.approve_triage(
            triage_id, data.get("matched_project"), workflow_id=data.get("workflow_id"),
            status=data.get("status"), work_type=data.get("work_type"),
            new_name=data.get("new_project_name"), new_location=new_loc, actor=data.get("actor") or "user",
            subtask_completions=data.get("subtask_completions") or [], quote_request=data.get("quote_request"),
            dollar_amount=data.get("dollar_amount") if rt().config.feature("money") else None)
    except LookupError as e:
        return error_response(404, str(e))
    except ValueError as e:
        return error_response(400, str(e))
    # Thread registration + subtask effects live in the triage layer.
    pid = (result.get("project") or {}).get("id") if isinstance(result, dict) else None
    if pid and not result.get("already_approved"):
        try:
            from ..triage.pipeline import after_approve
            card = db.get_triage_card(triage_id) or {}
            r = rt()
            result["effects"] = after_approve(db, r.config.doc, r.profession(), card, pid)
        except Exception as e:
            result["effects"] = {"error": f"{type(e).__name__}: {e}"}
    return jsonify(result)


@bp.route("/api/triage/approve-all", methods=["POST"])
def api_approve_all_triage():
    """Approve every pending card that already names an existing project.
    Unmatched cards are skipped (never guessed). Body (optional): {ids: [...]}
    limits the batch. Returns {ok, approved, skipped_unmatched, failed,
    failures: [{id, error}], remaining}."""
    db = store()
    data = body()
    only = set(data.get("ids") or []) if isinstance(data.get("ids"), list) else None
    r = rt()
    approved = skipped = failed = 0
    failures = []
    for card in db.list_triage("pending"):
        tid = card.get("triage_id")
        if only is not None and tid not in only:
            continue
        pid = (card.get("matched_project") or "").strip()
        if not pid or not db.project_exists(pid):
            skipped += 1
            continue
        try:
            result = db.approve_triage(tid, pid, actor="user")
            if isinstance(result, dict) and not result.get("already_approved"):
                try:
                    from ..triage.pipeline import after_approve
                    after_approve(db, r.config.doc, r.profession(), db.get_triage_card(tid) or {}, pid)
                except Exception:
                    pass
            approved += 1
        except Exception as e:
            failed += 1
            failures.append({"id": tid, "error": f"{type(e).__name__}: {e}"[:200]})
    return jsonify({"ok": failed == 0, "approved": approved, "skipped_unmatched": skipped, "failed": failed,
                    "failures": failures, "remaining": db.count_pending_triage()})


@bp.route("/api/triage/<triage_id>", methods=["DELETE"])
def api_delete_triage(triage_id):
    """Dismiss WITHOUT throwing away correspondence on an already-routed thread."""
    db = store()
    item = db.get_triage_card(triage_id)
    if not item:
        return error_response(404, f"Triage item {triage_id} not found")
    filed_to = None
    try:
        filed_to = db.file_before_dismiss(item, triage_id)
    except Exception:
        filed_to = None
    reason = (request.get_json(silent=True) or {}).get("reason", "") if request.data else ""
    db.dismiss_triage(triage_id, reason=reason or "", actor="user")
    if item.get("message_id"):
        db.message_mark(item["message_id"], "dismissed", pid=filed_to, triage_id=triage_id,
                        subject=item.get("subject"), received_at=item.get("received_at"))
    db.append_event("dismiss_triage", {"triage_id": triage_id, "subject": item.get("subject", ""),
                                       "filed_to": filed_to or ""})
    return jsonify({"ok": True, "dismissed": triage_id, "filed_to": filed_to,
                    "remaining": db.count_pending_triage()})


@bp.route("/api/triage/dismissed", methods=["GET"])
def api_list_dismissed_triage():
    card_type = request.args.get("card_type")
    items = store().list_triage("dismissed")
    if card_type:
        items = [i for i in items if i.get("card_type") == card_type]
    return jsonify({"ok": True, "items": items})


@bp.route("/api/triage/<triage_id>/revive", methods=["POST"])
def api_revive_triage(triage_id):
    db = store()
    item = db.get_triage_card(triage_id)
    if not item:
        return error_response(404, f"Triage item {triage_id} not found")
    if item.get("status") != "dismissed":
        return error_response(400, f"Can only revive dismissed cards (current: {item.get('status')})")
    db.revive_triage(triage_id)
    db.append_event("revive_triage", {"triage_id": triage_id, "subject": item.get("subject", "")})
    return jsonify({"ok": True, "revived": triage_id, "remaining": db.count_pending_triage()})


@bp.route("/api/triage/status", methods=["GET"])
def api_triage_status():
    r = rt()
    cache = r.paths.mail_cache
    info = {"path": str(cache), "exists": cache.exists()}
    for sub in ("inbox", "sent"):
        d = cache / sub
        info[f"{sub}_count"] = len(list(d.glob("*.json"))) if d.exists() else "missing"
    att = cache / "attachments"
    info["attachment_folders"] = len([p for p in att.iterdir() if p.is_dir()]) if att.exists() else "missing"
    st = r.triage_state
    return jsonify({"pipeline_available": r.run_triage_cycle is not None, "last_run": st["last_run"],
                    "run_count": st["run_count"], "error_count": st["error_count"],
                    "last_error": st["last_error"], "interval_seconds": r.config.get("mail.poll_seconds", 300),
                    "mail_cache": info, "pending": store().count_pending_triage()})


@bp.route("/api/triage/run", methods=["POST"])
def api_triage_run():
    """Run one triage cycle synchronously (the pipeline is supplied by the
    triage layer through Runtime.run_triage_cycle)."""
    r = rt()
    if r.run_triage_cycle is None:
        return jsonify({"ok": False, "error": "triage pipeline not installed"}), 501
    try:
        summary = r.run_triage_cycle()
        r.triage_state["last_run"] = r.hours.now_iso()
        r.triage_state["last_error"] = None
        r.triage_state["run_count"] += 1
        r.heartbeat("triage_loop")
        return jsonify({"ok": True, "message": f"Triage cycle #{r.triage_state['run_count']} complete",
                        "summary": summary})
    except Exception as e:
        r.triage_state["last_error"] = f"{type(e).__name__}: {e}"
        r.triage_state["error_count"] += 1
        return jsonify({"ok": False, "error": r.triage_state["last_error"]}), 500


@bp.route("/api/triage/re-enrich", methods=["POST"])
def api_re_enrich_triage():
    """Re-run classification over pending cards. Supplied by the triage layer
    through Runtime.run_triage_cycle(reenrich=True) when available."""
    r = rt()
    if r.run_triage_cycle is None:
        return jsonify({"ok": False, "error": "triage pipeline not installed"}), 501
    try:
        summary = r.run_triage_cycle(reenrich=True)
    except TypeError:
        return jsonify({"ok": False, "error": "pipeline does not support re-enrich"}), 501
    return jsonify({"ok": True, "summary": summary})
