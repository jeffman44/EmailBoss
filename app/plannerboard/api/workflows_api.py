"""
/api/workflows GET/PUT and /api/board-tabs. Ported from server.py 6208-6462;
response shapes unchanged. The document is backed by the profession's
workflows block (seeded into <DATA_ROOT>/workflows.json on first read).
"""

from datetime import date

from flask import Blueprint, jsonify

from .. import ladder
from .. import workflows as wfm
from ._common import body, error_response, rt, store

bp = Blueprint("workflows_api", __name__)

_DEADLINE_BANDS = ((2, 900), (7, 600), (14, 300))


def deadline_score(days_out):
    """>14d -> 100, 7-14d -> 300, 2-7d -> 600, <=2d -> 900, past due -> 999."""
    if days_out is None:
        return 100
    if days_out < 0:
        return 999
    for limit, score in _DEADLINE_BANDS:
        if days_out <= limit:
            return score
    return 100


@bp.route("/api/workflows", methods=["GET"])
def api_get_workflows():
    r = rt()
    doc = r.workflows.load()
    counts = {}
    try:
        for p in r.store.all_projects():
            st = p.get("status") or ""
            counts[st] = counts.get(st, 0) + 1
    except Exception:
        pass
    ladders = {w.get("id"): [list(x) for x in wfm.ladder_rows(doc, w.get("id"))] for w in doc.get("workflows") or []}
    return jsonify({
        "ok": True, "workflows": doc, "path": str(r.workflows.path), "load_error": r.workflows.last_error,
        "derived": {"ladders": ladders, "stage_counts": counts, "all_stages": wfm.all_stage_ids(doc),
                    "closed_stages": sorted(wfm.closed_stage_ids(doc)),
                    "roles": {sid: wfm.role_of(doc, sid) for sid in wfm.all_stage_ids(doc)}},
    })


@bp.route("/api/workflows", methods=["PUT"])
def api_put_workflows():
    """Replace the document. 409 when a stage that still has projects is
    being deleted (with the ids), so the board can offer a move."""
    r = rt()
    data = body()
    if not isinstance(data, dict):
        return error_response(400, "body must be the workflow document object")
    doc = data.get("workflows") if isinstance(data.get("workflows"), dict) else data
    doc = wfm.normalize(dict(doc))
    errs = wfm.validate(doc, r.workflows.date_types)
    if errs:
        return jsonify({"ok": False, "error": "invalid", "errors": errs}), 400
    old = r.workflows.load()
    removed = set(wfm.all_stage_ids(old)) - set(wfm.all_stage_ids(doc))
    if removed:
        occupied = {}
        for row in r.store.conn.execute("SELECT id, status FROM projects WHERE archived = 0").fetchall():
            if row["status"] in removed:
                occupied.setdefault(row["status"], []).append(row["id"])
        if occupied:
            return jsonify({"ok": False, "error": "stage_in_use",
                            "detail": "A stage that still has projects cannot be deleted.",
                            "stages": {k: {"count": len(v), "project_ids": v[:50]} for k, v in occupied.items()},
                            "remaining_stages": sorted(wfm.all_stage_ids(doc))}), 409
    ok, save_errs, bpath = r.workflows.save(doc)
    if not ok:
        return jsonify({"ok": False, "error": "invalid", "errors": save_errs}), 400
    r.store.append_event("workflows_saved", {"backup": bpath,
                                             "workflows": [w.get("id") for w in doc.get("workflows") or []]})
    return jsonify({"ok": True, "backup": bpath, "workflows": r.workflows.load(),
                    "stages_allowed": wfm.all_stage_ids(doc)})


@bp.route("/api/board-tabs", methods=["GET"])
def api_board_tabs():
    """One row per tab: {id, label, kind, count, member_ids, urgency_score,
    urgency_kind, color_hint, workflow_id, stage_id, pulse}. Only the deadlines
    tab is scored server-side (a calendar date); the board aggregates the rest
    from member_ids with its own urgency curve."""
    r = rt()
    db = r.store
    doc = r.workflows.load()
    projects = db.all_projects()
    closed_ids = wfm.closed_stage_ids(doc)
    wf_color = {w.get("id"): w.get("color") for w in doc.get("workflows") or []}
    today = date.fromisoformat(r.hours.today())

    def members(filt):
        filt = filt or {}
        out = []
        for p in projects:
            st = p.get("status") or ""
            if "stage_in" in filt and st not in (filt.get("stage_in") or []):
                continue
            if "closed" in filt and bool(st in closed_ids) != bool(filt["closed"]):
                continue
            if "role_in" in filt and wfm.role_of(doc, st, p.get("workflow_id")) not in (filt.get("role_in") or []):
                continue
            if "workflow_in" in filt and ladder.wf_id_for(db, p, doc) not in (filt.get("workflow_in") or []):
                continue
            out.append(p["id"])
        return out

    deadline_items, nearest = [], None
    for p in projects:
        for s in p.get("subtasks") or []:
            if s.get("status") != "pending" or (s.get("date_type") or "") != "deadline":
                continue
            due = (s.get("follow_up_date") or "")[:10]
            days = None
            if due:
                try:
                    days = (date.fromisoformat(due) - today).days
                except ValueError:
                    days = None
            deadline_items.append({"project_id": p["id"], "project_name": p.get("name", ""),
                                   "subtask_id": s.get("id"), "text": s.get("text", ""), "due": due or None,
                                   "days_out": days, "score": deadline_score(days)})
            if days is not None and (nearest is None or days < nearest):
                nearest = days
    deadline_items.sort(key=lambda d: (d["days_out"] is None, d["days_out"]))

    tabs = []
    for t in doc.get("tabs") or []:
        tid, kind = t.get("id"), t.get("urgency", "max")
        if tid == "deadlines" or kind == "deadline":
            tabs.append({"id": tid, "label": t.get("label", "Deadlines"), "kind": "deadline",
                         "count": len(deadline_items),
                         "member_ids": sorted({d["project_id"] for d in deadline_items}),
                         "items": deadline_items[:100], "urgency_kind": "deadline",
                         "urgency_score": deadline_score(nearest) if deadline_items else 0,
                         "nearest_days": nearest, "pulse": bool(nearest is not None and nearest <= 2),
                         "color_hint": None, "workflow_id": None, "stage_id": None})
            continue
        ids = members(t.get("filter"))
        tabs.append({"id": tid, "label": t.get("label", tid), "kind": "filter", "count": len(ids),
                     "member_ids": ids, "urgency_kind": kind, "urgency_score": None, "color_hint": None,
                     "workflow_id": None, "stage_id": None, "pulse": False})
    covered = set()
    for t in doc.get("tabs") or []:
        covered.update(((t.get("filter") or {}).get("stage_in") or []))
    if (doc.get("tab_rules") or {}).get("auto_stage_tabs", True):
        seen = set()
        for w in doc.get("workflows") or []:
            for st in w.get("stages") or []:
                sid = st.get("id")
                if not sid or not st.get("column") or sid in covered or sid in seen:
                    continue
                seen.add(sid)
                ids = [p["id"] for p in projects if (p.get("status") or "") == sid]
                tabs.append({"id": f"stage_{sid}", "label": st.get("label", sid), "kind": "stage",
                             "count": len(ids), "member_ids": ids, "urgency_kind": st.get("urgency", "max"),
                             "urgency_score": None, "color_hint": wf_color.get(w.get("id")),
                             "workflow_id": w.get("id"), "stage_id": sid, "role": st.get("role"), "pulse": False})
    return jsonify({"ok": True, "tabs": tabs,
                    "urgency_note": "project urgency_score is null by design; the board aggregates member_ids "
                                    "with its own curve; only the deadlines tab is scored server-side",
                    "generated_at": r.hours.now_local_naive().isoformat(timespec="seconds")})
