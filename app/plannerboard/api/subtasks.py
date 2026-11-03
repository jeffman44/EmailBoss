"""
/api/projects/<id>/subtasks (GET open, POST) and /api/subtasks/<id>/complete.

Ported from server.py 1046-1049 and 6046-6138. The ids are not unique across
projects (PRIMARY KEY (id, project_id)), so complete takes project_id in the
body and only falls back to a lookup when it is missing.
"""

from flask import Blueprint, jsonify

from ._common import body, error_response, store

bp = Blueprint("subtasks", __name__)

_ALLOWED_SOURCES = {"manual", "system", "email_response", "assistant", "triage", "ladder"}


@bp.route("/api/projects/<proj_id>/subtasks", methods=["GET"])
def api_get_subtasks(proj_id):
    """OPEN subtasks only."""
    return jsonify({"project_id": proj_id, "subtasks": store().open_subtasks(proj_id)})


@bp.route("/api/projects/<proj_id>/subtasks", methods=["POST"])
def api_create_subtask(proj_id):
    db = store()
    data = body()
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    if not db.project_exists(proj_id):
        return jsonify({"error": f"project {proj_id} not found"}), 404
    source = data.get("source", "manual") or "manual"
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    if source not in _ALLOWED_SOURCES:
        metadata = dict(metadata)
        metadata.setdefault("origin_source", source)
        source = "system"
    date_type = data.get("date_type") or ("sla" if data.get("sla_hours") else "follow_up")
    follow_up_date = data.get("follow_up_date")
    if not follow_up_date and date_type == "follow_up":
        # Default to today's LOCAL date (config timezone), never UTC.
        follow_up_date = db.hours.today()
    st_id = db.add_subtask(
        proj_id, text, date_type=date_type, follow_up_date=follow_up_date,
        sla_hours=data.get("sla_hours"), priority=data.get("priority") or 0, source=source,
        category=data.get("category") or "", created=data.get("created"), sla_type=data.get("sla_type"),
        sla_created=data.get("sla_created"), metadata=metadata,
        allow_if_completed=bool(data.get("allow_if_completed")))
    return jsonify({"subtask_id": st_id, "project_id": proj_id,
                    "deduped": st_id is None})


@bp.route("/api/subtasks/<subtask_id>/complete", methods=["POST"])
def api_complete_subtask(subtask_id):
    """Body: {project_id, completed_by}. Does NOT run the status cascade; that
    belongs to the board's PUT with the full subtask array."""
    db = store()
    data = body()
    proj_id = (data.get("project_id") or "").strip()
    if not proj_id:
        proj_id = db.find_subtask_project(subtask_id)
        if not proj_id:
            return jsonify({"error": "subtask not found"}), 404
    ok = db.complete_subtask(proj_id, subtask_id, completed_by=data.get("completed_by") or "manual")
    if not ok:
        row = db.conn.execute("SELECT 1 FROM subtasks WHERE id = ? AND project_id = ?",
                              (subtask_id, proj_id)).fetchone()
        if not row:
            return jsonify({"error": "subtask not found", "subtask_id": subtask_id,
                            "project_id": proj_id}), 404
        return jsonify({"status": "refused", "subtask_id": subtask_id, "project_id": proj_id,
                        "reason": "send-type task needs a trusted actor"}), 403
    return jsonify({"status": "completed", "subtask_id": subtask_id, "project_id": proj_id})


@bp.route("/api/projects/<proj_id>/subtasks/<subtask_id>", methods=["DELETE"])
def api_delete_subtask(proj_id, subtask_id):
    db = store()
    if not db.project_exists(proj_id):
        return error_response(404, f"project {proj_id} not found")
    db.delete_subtask(proj_id, subtask_id)
    return jsonify({"ok": True, "deleted": subtask_id, "project_id": proj_id})
