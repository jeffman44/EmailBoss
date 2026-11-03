"""
/api/update GET (cached; ?refresh=1 re-checks now), POST /api/update/download,
POST /api/update/install (Windows only). See plannerboard/updater.py.

Nothing installs by itself: the board asks, the user clicks, then install.
These routes stay open in read-only license mode so an expired license can
still be fixed by updating.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..updater import UpdateError, updater_from_app

bp = Blueprint("update_api", __name__)


def _u():
    return updater_from_app(current_app)


def _err(e: UpdateError):
    return jsonify({"ok": False, "error": str(e), "code": e.code}), e.http_status


@bp.route("/api/update", methods=["GET"])
def api_update():
    u = _u()
    if request.args.get("refresh") in ("1", "true", "yes"):
        u.check()
    return jsonify(u.state())


@bp.route("/api/update/download", methods=["POST"])
def api_update_download():
    u = _u()
    try:
        path = u.download()
    except UpdateError as e:
        return _err(e)
    return jsonify({"ok": True, "path": str(path)})


@bp.route("/api/update/install", methods=["POST"])
def api_update_install():
    u = _u()
    try:
        return jsonify(u.install())
    except UpdateError as e:
        return _err(e)
