"""
Game layer: /api/hp-history GET/PUT and /api/boss-dps. Ported from
server.py 8322-8356 (hp history, camelCase remap) and reduced to a generic
computed object for boss-dps. Both 404 when features.game_layer is off.
"""

from flask import Blueprint, jsonify

from .. import workflows as wfm
from ._common import body, rt, store

bp = Blueprint("game", __name__)


def _gated():
    return not rt().config.feature("game_layer")


@bp.route("/api/hp-history", methods=["GET"])
def api_get_hp_history():
    if _gated():
        return jsonify({"error": "not found", "disabled_feature": "game_layer"}), 404
    history = store().get_hp_history(days=8)
    days = {}
    for k, day in history.items():
        days[k] = {"startHP": day.get("start_hp"), "startTime": day.get("start_time"),
                   "peakHP": day.get("peak_hp"), "dailyLow": day.get("daily_low"),
                   "snapshots": day.get("snapshots", []), "damageEvents": day.get("damageEvents", [])}
    return jsonify({"ok": True, "days": days})


@bp.route("/api/hp-history", methods=["PUT"])
def api_put_hp_history():
    if _gated():
        return jsonify({"error": "not found", "disabled_feature": "game_layer"}), 404
    db = store()
    data = body()
    date_str = data.get("date") or db.hours.today()
    state = data.get("state", data)
    db.save_hp_snapshot(date_str, start_hp=state.get("startHP") or state.get("start_hp"),
                        start_time=state.get("startTime") or state.get("start_time"),
                        peak_hp=state.get("peakHP") or state.get("peak_hp"),
                        daily_low=state.get("dailyLow") or state.get("daily_low"),
                        snapshots=state.get("snapshots"),
                        damage_events=state.get("damageEvents") or state.get("damage_events"))
    return jsonify({"ok": True, "days_stored": len(db.get_hp_history())})


@bp.route("/api/boss-dps", methods=["GET"])
def api_boss_dps():
    """Generic computed object: open work, SLA pressure, threads closed today.
    No money, no install-specific tables."""
    if _gated():
        return jsonify({"error": "not found", "disabled_feature": "game_layer"}), 404
    r = rt()
    db = r.store
    doc = r.workflows.load()
    today = r.hours.today()
    projects = db.all_projects()
    open_projects = [p for p in projects if not wfm.is_closed(doc, p.get("status"), p.get("workflow_id"))]
    pending = sum(1 for p in open_projects for s in p.get("subtasks") or [] if s.get("status") == "pending")
    sla_open = sla_breached = 0
    default_sla = float(r.profession().get("sla_hours") or 4)
    for p in open_projects:
        for s in p.get("subtasks") or []:
            if s.get("status") != "pending" or s.get("sla_type") != "response" or not s.get("sla_created"):
                continue
            sla_open += 1
            try:
                if r.hours.elapsed_biz_hours(s["sla_created"]) > float(s.get("sla_hours") or default_sla):
                    sla_breached += 1
            except Exception:
                pass
    closed_today = db.conn.execute(
        "SELECT COUNT(*) FROM projects WHERE closed_date = ?", (today,)).fetchone()[0]
    tasks_done_today = db.conn.execute(
        "SELECT COUNT(*) FROM subtasks WHERE status = 'complete' AND completed_date = ?", (today,)).fetchone()[0]
    hist = db.get_hp_history(days=1).get(today) or {}
    return jsonify({"ok": True, "date": today, "open_projects": len(open_projects), "pending_subtasks": pending,
                    "sla_open": sla_open, "sla_breached": sla_breached, "threads_closed_today": closed_today,
                    "tasks_done_today": tasks_done_today, "damageEvents": hist.get("damageEvents", []),
                    "hud": (doc.get("hud") or {})})
