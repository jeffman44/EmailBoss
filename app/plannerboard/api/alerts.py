"""/api/alerts: board banner channel. Ported from server.py 7694-7760."""

from flask import Blueprint, jsonify, request

from ._common import body, error_response, int_arg, store

bp = Blueprint("alerts", __name__)


@bp.route("/api/alerts", methods=["GET"])
def api_list_alerts():
    rows = store().list_alerts(status=request.args.get("status", "open"), limit=int_arg("limit", 100))
    return jsonify({"alerts": rows, "count": len(rows),
                    "hard": sum(1 for r in rows if r.get("severity") == "hard")})


@bp.route("/api/alerts", methods=["POST"])
def api_post_alerts():
    """Body: {source, alerts: [{key, title, detail, severity}], clear_missing}.
    A single alert may be posted flat."""
    db = store()
    data = body()
    source = data.get("source") or "external"
    items = data.get("alerts")
    if isinstance(items, dict):
        items = [items]
    elif isinstance(items, str) or items is None:
        items = [data] if data.get("key") else []
    elif not isinstance(items, list):
        items = []
    raised = []
    for a in items:
        if not isinstance(a, dict) or not a.get("key"):
            continue
        aid = db.raise_alert(key=str(a.get("key"))[:120], title=str(a.get("title") or a.get("key"))[:200],
                             detail=str(a.get("detail") or "")[:2000],
                             severity=a.get("severity") if a.get("severity") in ("hard", "warn", "info") else "warn",
                             source=source)
        if aid:
            raised.append({"id": aid, "key": a.get("key")})
    cleared = 0
    if data.get("clear_missing"):
        cleared = db.resolve_alerts(source=source, except_keys=[a.get("key") for a in items if isinstance(a, dict)])
    return jsonify({"ok": True, "raised": raised, "resolved": cleared})


@bp.route("/api/alerts/<int:alert_id>/ack", methods=["POST"])
def api_ack_alert(alert_id):
    row = store().ack_alert(alert_id, by=body().get("by") or "user")
    if not row:
        return error_response(404, f"Alert {alert_id} not found")
    return jsonify({"ok": True, "alert": row})


@bp.route("/api/alerts/resolve", methods=["POST"])
def api_resolve_alerts():
    keys = body().get("keys") or []
    return jsonify({"ok": True, "resolved": store().resolve_alerts(keys=keys)})
