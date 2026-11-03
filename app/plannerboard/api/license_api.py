"""
/api/license GET, POST /api/license/activate | refresh | deactivate.

Backed by plannerboard.license.client.LicenseClient (built once per app by
`client_from_app`). These routes stay open while the board is read-only, so
a user can always get back to a licensed state.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from ..license.client import LicenseClient, LicenseError, client_from_app
from ._common import body, error_response

bp = Blueprint("license_api", __name__)


def _client() -> LicenseClient:
    return client_from_app(current_app)


def _status(client: LicenseClient, use_cache: bool = False) -> dict:
    st = client.status(use_cache=use_cache)
    st["dev_mode"] = LicenseClient.dev_mode()
    st["key_present"] = bool(client.stored_key())
    return st


@bp.route("/api/license", methods=["GET"])
def api_license():
    try:
        client = _client()
    except Exception as e:
        return error_response(500, f"license client unavailable: {e}")
    return jsonify({"ok": True, **_status(client, use_cache=True)})


@bp.route("/api/license/activate", methods=["POST"])
def api_license_activate():
    """Body: {key}. Verifies the key locally, activates this machine on the
    server, stores the signed activation in the database meta."""
    data = body()
    key = (data.get("key") or data.get("license_key") or "").strip()
    if not key:
        return error_response(400, "key required")
    client = _client()
    try:
        result = client.activate(key)
    except LicenseError as e:
        http = e.http_status if 400 <= (e.http_status or 0) < 600 else 400
        return jsonify({"ok": False, "error": str(e), "code": e.code, "status": _status(client)}), http
    except Exception as e:
        return error_response(502, f"{type(e).__name__}: {e}")
    client._invalidate()
    return jsonify({"ok": True, "license": result.get("license"), "activation": result.get("activation"),
                    "status": _status(client)})


@bp.route("/api/license/trial", methods=["POST"])
def api_license_trial():
    """Body: {email}. Starts the free trial on the license server (no portal
    visit): the key is emailed, stored here and this machine is activated."""
    data = body()
    email = (data.get("email") or "").strip()
    if not email:
        return error_response(400, "email required")
    client = _client()
    try:
        result = client.start_trial(email)
    except LicenseError as e:
        http = e.http_status if 400 <= (e.http_status or 0) < 600 else 400
        return jsonify({"ok": False, "error": str(e), "code": e.code, "status": _status(client)}), http
    except Exception as e:
        return error_response(502, f"{type(e).__name__}: {e}")
    client._invalidate()
    return jsonify({"ok": True, "license": result.get("license"), "activation": result.get("activation"),
                    "status": _status(client)})


@bp.route("/api/license/refresh", methods=["POST"])
def api_license_refresh():
    """Forced heartbeat: re-signs the activation when the server agrees."""
    client = _client()
    try:
        result = client.heartbeat(force=True)
    except Exception as e:
        return error_response(502, f"{type(e).__name__}: {e}")
    client._invalidate()
    return jsonify({"ok": bool(result.get("ok")), "result": result, "status": _status(client)})


@bp.route("/api/license/deactivate", methods=["POST"])
def api_license_deactivate():
    client = _client()
    try:
        result = client.deactivate()
    except Exception as e:
        return error_response(502, f"{type(e).__name__}: {e}")
    client._invalidate()
    return jsonify({"ok": bool(result.get("ok")), "result": result, "status": _status(client)})
