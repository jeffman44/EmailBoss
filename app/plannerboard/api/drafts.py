"""
/api/drafts: the draft outbox. NOTHING SENDS. A draft is a record the user
copies into their own mail client (or that a mail provider's create_draft
mirrors into the provider's Drafts folder); `status` moves to 'sent' only
when the user says so.

Replaces the reference file-based drafts (server.py 5973-6036) with the
`drafts` table.
"""

from flask import Blueprint, jsonify, request

from ._common import body, error_response, store

bp = Blueprint("drafts", __name__)


@bp.route("/api/drafts", methods=["GET"])
def api_list_drafts():
    """?project_id=&status=draft|sent|discarded|all"""
    drafts = store().list_drafts(pid=request.args.get("project_id") or None,
                                 status=request.args.get("status", "draft"))
    return jsonify({"ok": True, "drafts": drafts, "count": len(drafts)})


@bp.route("/api/drafts", methods=["POST"])
def api_create_draft():
    """Body: {project_id, to, cc, subject, body, reply_to_corr_id, created_by}."""
    db = store()
    data = body()
    if not (data.get("subject") or data.get("body") or data.get("note")):
        return error_response(400, "a subject or body is required")
    did = db.add_draft(data.get("project_id", ""), data.get("to", ""), data.get("cc", ""),
                       data.get("subject", ""), data.get("body") or data.get("note") or "",
                       reply_to_corr_id=data.get("reply_to_corr_id"),
                       created_by=data.get("created_by") or "user")
    pid = data.get("project_id")
    if pid and db.project_exists(pid):
        db.append_note(pid, f"Draft created: {(data.get('subject') or '')[:60]}", db._note_time("draft"))
    return jsonify({"ok": True, "id": did, "draft": db.get_draft(did)}), 201


@bp.route("/api/drafts/<draft_id>", methods=["GET"])
def api_get_draft(draft_id):
    d = store().get_draft(draft_id)
    if not d:
        return error_response(404, f"Draft {draft_id} not found")
    return jsonify({"ok": True, "draft": d})


@bp.route("/api/drafts/<draft_id>", methods=["PUT"])
def api_update_draft(draft_id):
    db = store()
    if not db.get_draft(draft_id):
        return error_response(404, f"Draft {draft_id} not found")
    try:
        d = db.update_draft(draft_id, **body())
    except ValueError as e:
        return error_response(400, str(e))
    return jsonify({"ok": True, "draft": d})


@bp.route("/api/drafts/<draft_id>", methods=["DELETE"])
def api_delete_draft(draft_id):
    n = store().delete_draft(draft_id)
    return jsonify({"ok": True, "deleted": draft_id, "removed": n})
