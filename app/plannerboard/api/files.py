"""
Sandboxed file reads. /api/read-file serves any file UNDER DATA_ROOT as
base64 (never the database files); project_files/<id>/{attachments,emails}
are listed and served per project. Ported from server.py 5857-5913 and
6796-6823 with the workspace root replaced by DATA_ROOT.
"""

import base64
from datetime import datetime
from pathlib import Path

from flask import Blueprint, abort, jsonify, request, send_file

from ._common import error_response, rt

bp = Blueprint("files", __name__)

MAX_READ = 50 * 1024 * 1024


def _safe_data_path(rel):
    r = rt()
    if not rel:
        abort(400, "path required")
    root = r.paths.data_root.resolve()
    p = (root / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        abort(403, "path outside the data root")
    if p.suffix.lower() in (".db", ".db-wal", ".db-shm") or p.name.lower().startswith(r.paths.db_file.name.lower()):
        abort(403, "database files not served")
    if p.name == r.config.source_path.name if r.config.source_path else False:
        abort(403, "config file not served")
    if p.name.lower() in ("desktop.json", "plannerboard.json") or p.name.lower().endswith((".pem", ".key")):
        abort(403, "private file not served")
    if not p.exists() or not p.is_file():
        abort(404, f"not found: {rel}")
    return p


@bp.route("/api/read-file", methods=["GET"])
def api_read_file():
    """?path=<relative to DATA_ROOT> -> {path, size, b64}."""
    p = _safe_data_path(request.args.get("path", ""))
    if p.stat().st_size > MAX_READ:
        abort(413, "file > 50MB")
    data = p.read_bytes()
    try:
        shown = str(p.relative_to(rt().paths.data_root.resolve()))
    except ValueError:
        shown = str(p)
    return jsonify({"path": shown, "size": len(data), "b64": base64.b64encode(data).decode()})


@bp.route("/api/projects/<proj_id>/files", methods=["GET"])
def api_list_project_files(proj_id):
    root = rt().paths.project_files / proj_id
    result = {"attachments": [], "emails": [], "stats": {"attachment_count": 0, "email_count": 0, "total_size": 0}}
    if not root.exists():
        return jsonify({"ok": True, **result})
    total = 0
    for sub in ("attachments", "emails"):
        d = root / sub
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file():
                size = f.stat().st_size
                total += size
                result[sub].append({"filename": f.name, "size": size,
                                    "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
                                    "url": f"/api/projects/{proj_id}/files/{sub}/{f.name}"})
    result["stats"] = {"attachment_count": len(result["attachments"]), "email_count": len(result["emails"]),
                       "total_size": total}
    return jsonify({"ok": True, **result})


@bp.route("/api/projects/<proj_id>/files/<subdir>/<path:filename>", methods=["GET"])
def api_serve_project_file(proj_id, subdir, filename):
    if subdir not in ("attachments", "emails"):
        return error_response(400, "Invalid subdirectory")
    root = rt().paths.project_files.resolve()
    p = (root / proj_id / subdir / filename).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return error_response(403, "Access denied")
    if not p.is_file():
        return error_response(404, "File not found")
    return send_file(str(p), as_attachment=True, download_name=p.name)
