"""
Correspondence, correspondence files, intake and reassignment.

Ported from server.py 5427-5433 (intake), 5692-5744 (correspondence),
5794-5854 (unfile duplicate), 5916-5966 (reassign). Disk copies live under
<DATA_ROOT>/project_files/<project>/{attachments,emails}/ and are served by
files.py.
"""

from io import BytesIO

from flask import Blueprint, jsonify, request, send_file

from ._common import body, error_response, rt, store

bp = Blueprint("correspondence", __name__)


@bp.route("/api/projects/<proj_id>/correspondence", methods=["GET"])
def api_get_correspondence(proj_id):
    db = store()
    include_bodies = request.args.get("include_bodies", "0") == "1"
    return jsonify({"ok": True, "correspondence": db.get_project_correspondence(proj_id, include_bodies),
                    "stats": db.get_correspondence_stats(proj_id)})


@bp.route("/api/projects/<proj_id>/correspondence", methods=["POST"])
def api_add_correspondence(proj_id):
    db = store()
    if not db.project_exists(proj_id):
        return error_response(404, f"Project {proj_id} not found")
    corr_id = db.save_correspondence(proj_id, body(), source="manual")
    if corr_id is None:
        return error_response(500, "Failed to save correspondence")
    return jsonify({"ok": True, "id": corr_id})


@bp.route("/api/projects/<proj_id>/correspondence", methods=["DELETE"])
def api_delete_correspondence(proj_id):
    import shutil
    store().delete_project_correspondence(proj_id)
    d = rt().paths.project_files / proj_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    return jsonify({"ok": True})


@bp.route("/api/projects/<proj_id>/correspondence/<int:corr_id>", methods=["DELETE"])
def api_unfile_duplicate_correspondence(proj_id, corr_id):
    """Remove ONE double-filed row. Refuses unless the same message also lives
    on another project ({"duplicate_of": <id>}), so this can never drop the
    only record of an email."""
    db = store()
    data = body()
    row = db.conn.execute("SELECT id, subject, received_at FROM project_correspondence WHERE id = ? "
                          "AND project_id = ?", (corr_id, proj_id)).fetchone()
    if not row:
        return error_response(404, "correspondence not on this project")
    dup_id = data.get("duplicate_of")
    if not dup_id:
        return error_response(400, "duplicate_of is required")
    twin = db.conn.execute("SELECT id, project_id FROM project_correspondence WHERE id = ? AND project_id != ? "
                           "AND subject = ? AND received_at = ?", (int(dup_id), proj_id, row[1], row[2])).fetchone()
    if not twin:
        return jsonify({"ok": False, "error": "duplicate_of is not the same message on another project"}), 409
    files = [r[0] for r in db.conn.execute("SELECT filename FROM correspondence_files WHERE correspondence_id = ? "
                                           "AND project_id = ?", (corr_id, proj_id)).fetchall()]
    db.delete_correspondence(proj_id, corr_id)
    removed = []
    base = rt().paths.project_files / proj_id
    for f in files:
        if f and "/" not in f and "\\" not in f:
            p = base / "attachments" / f
            if p.is_file():
                try:
                    p.unlink()
                    removed.append(p.name)
                except OSError:
                    pass
    db.append_note(proj_id, f"auto: removed duplicate filing of '{row[1]}' (correspondence #{corr_id}); "
                            f"the message stays on {twin[1]} as #{twin[0]}", db._note_time("auto"))
    return jsonify({"ok": True, "kept_on": twin[1], "kept_id": twin[0], "disk_removed": removed})


@bp.route("/api/correspondence/<int:corr_id>", methods=["GET"])
def api_get_one_correspondence(corr_id):
    db = store()
    row = db.get_correspondence(corr_id)
    if not row:
        return error_response(404, "correspondence not found")
    row["attachment_summary"] = db._parse_json(row.get("attachment_summary"), []) or []
    row["files"] = db.get_correspondence_files(corr_id)
    return jsonify({"ok": True, "correspondence": row})


@bp.route("/api/correspondence/<int:corr_id>/files", methods=["GET"])
def api_get_correspondence_files(corr_id):
    return jsonify({"ok": True, "files": store().get_correspondence_files(corr_id, include_data=False)})


@bp.route("/api/correspondence/files/<int:file_id>/download", methods=["GET"])
def api_download_correspondence_file(file_id):
    f = store().get_correspondence_file_data(file_id)
    if not f or not f.get("file_data"):
        return error_response(404, "File not found")
    return send_file(BytesIO(f["file_data"]), mimetype=f.get("mime_type") or "application/octet-stream",
                     as_attachment=True, download_name=f.get("filename") or "file")


@bp.route("/api/projects/<proj_id>/reassign-correspondence", methods=["POST"])
def api_reassign_correspondence(proj_id):
    """Body: {target_project_id, correspondence_ids?}. Omitted ids = move all."""
    db = store()
    data = body()
    target = (data.get("target_project_id") or "").strip()
    if not target:
        return error_response(400, "target_project_id is required")
    if not db.project_exists(target):
        return error_response(404, f"Target project {target} not found")
    ids = data.get("correspondence_ids")
    db.reassign_correspondence(proj_id, target, ids)
    from .projects import _move_project_files
    moved = _move_project_files(proj_id, target) if not ids else 0
    return jsonify({"ok": True, "files_moved": moved, "source": proj_id, "target": target})


@bp.route("/api/projects/<proj_id>/intake", methods=["GET"])
def api_get_intake(proj_id):
    return jsonify({"ok": True, "items": store().get_intake(proj_id)})


@bp.route("/api/projects/<proj_id>/intake", methods=["POST"])
def api_add_intake(proj_id):
    db = store()
    if not db.project_exists(proj_id):
        return error_response(404, f"Project {proj_id} not found")
    upload = request.files.get("file") if request.files else None
    if upload is not None and upload.filename:
        # Multipart upload from the board's dump zone: store the bytes under
        # project_files/<project>/intake/ and record it like a file_path intake.
        import re
        from werkzeug.utils import secure_filename
        safe = secure_filename(upload.filename) or "upload"
        dest_dir = rt().paths.project_files / proj_id / "intake"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe
        stem, dot, ext = safe.rpartition(".")
        n = 1
        while dest.exists():
            n += 1
            dest = dest_dir / (f"{stem}_{n}.{ext}" if dot else f"{safe}_{n}")
        upload.save(str(dest))
        data = {
            "file_path": str(dest), "file_name": upload.filename,
            "file_type": (upload.mimetype or re.sub(r"^.*\.", "", safe).lower() or ""),
            "file_size": dest.stat().st_size, "source": request.form.get("source", "upload"),
            "content": request.form.get("content", ""),
        }
    else:
        data = body()
        if not data and request.form:
            data = {k: request.form.get(k) for k in ("content", "text", "file_path", "file_name",
                                                    "file_type", "file_size", "source") if k in request.form}
    content = (data.get("content") or data.get("text") or "").strip()
    if not content and not data.get("file_path"):
        return error_response(400, "content or file_path is required")
    intake_id = db.add_intake(proj_id, intake_type="file" if data.get("file_path") else "text",
                              content=content, file_path=data.get("file_path"),
                              file_name=data.get("file_name", ""), file_type=data.get("file_type", ""),
                              file_size=data.get("file_size", 0), source=data.get("source", "manual"))
    db.update_project(proj_id, last_activity=db._now())
    return jsonify({"ok": True, "intake_id": intake_id}), 201


@bp.route("/api/projects/<proj_id>/intake/<int:intake_id>", methods=["DELETE"])
def api_delete_intake(proj_id, intake_id):
    store().delete_intake(intake_id)
    return jsonify({"ok": True, "deleted": intake_id})
