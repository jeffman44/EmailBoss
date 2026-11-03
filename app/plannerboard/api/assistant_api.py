"""
/api/assistant/run | status | log: the in-app assistant pass (DESIGN.md
section 6). The pass is `ai.assistant.AssistantPass`, driven through the
`AssistantScheduler` so a manual run and the hourly timer share one lock.
Drafts only: the pass never sends anything, and the status says so.
"""

from __future__ import annotations

import json

from flask import Blueprint, jsonify, request

from ..ai.assistant import AssistantScheduler
from ._common import body, error_response, view

bp = Blueprint("assistant_api", __name__)


def _scheduler(v):
    sched = v.get("assistant")
    if sched is None:
        minutes = int(((v.get("cfg") or {}).get("assistant") or {}).get("schedule_minutes") or 60)
        sched = AssistantScheduler(v, minutes)
        v["assistant"] = sched
    return sched


def _public_summary(summary):
    if not isinstance(summary, dict):
        return None
    out = {k: val for k, val in summary.items() if k != "items"}
    out["item_count"] = len(summary.get("items") or [])
    return out


@bp.route("/api/assistant/status", methods=["GET"])
def api_assistant_status():
    v = view()
    cfg = v.get("cfg") or {}
    status = v.get("assistant_status") or {}
    sched = v.get("assistant")
    acfg = cfg.get("assistant") or {}
    return jsonify({
        "ok": True,
        "installed": True,
        "enabled": bool(((cfg.get("ai") or {}).get("features") or {}).get("assistant", False)),
        "running": bool(status.get("running")),
        "drafts_only": True,
        "drafts_to": acfg.get("drafts_to") or "local",
        "auto_create": bool(acfg.get("auto_create", False)),
        "schedule_minutes": int(acfg.get("schedule_minutes") or 60),
        "max_items": int(acfg.get("max_items") or 10),
        "last_run": status.get("last_run"),
        "last_summary": _public_summary(status.get("last_summary")),
        "next_run_at": status.get("next_run_at"),
        "error": status.get("error") or "",
        "thread_alive": bool(sched and sched.thread and sched.thread.is_alive()),
        "llm": (v.get("llm").status() if v.get("llm") is not None else {"provider": "none", "ok": False}),
    })


@bp.route("/api/assistant/log", methods=["GET"])
def api_assistant_log():
    v = view()
    st = v.get("store")
    try:
        limit = max(1, min(2000, int(request.args.get("limit", 200))))
    except (TypeError, ValueError):
        limit = 200
    run_id = request.args.get("run_id") or None
    outcome = request.args.get("outcome") or None
    pid = request.args.get("project_id") or None
    try:
        rows = st.assistant_log(limit=limit) or []
    except Exception as e:
        return error_response(500, f"{type(e).__name__}: {e}")
    out = []
    for r in rows:
        r = dict(r)
        if isinstance(r.get("detail"), str):
            try:
                r["detail"] = json.loads(r["detail"])
            except ValueError:
                pass
        r.setdefault("project_id", r.get("pid", ""))
        r.setdefault("pid", r.get("project_id", ""))
        if run_id and r.get("run_id") != run_id:
            continue
        if outcome and r.get("outcome") != outcome:
            continue
        if pid and r.get("project_id") != pid:
            continue
        out.append(r)
    return jsonify({"ok": True, "entries": out, "log": out, "count": len(out)})


@bp.route("/api/assistant/run", methods=["POST"])
def api_assistant_run():
    """Run one pass now. Body: {max_items?}. 409 while a pass is running.
    Works without a model (rules only); nothing is ever sent."""
    v = view()
    data = body()
    max_items = data.get("max_items")
    if max_items not in (None, ""):
        try:
            max_items = int(max_items)
            if max_items <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return error_response(400, "max_items must be a positive integer")
    else:
        max_items = None
    status = v.get("assistant_status") or {}
    if status.get("running"):
        return error_response(409, "assistant pass already running")
    sched = _scheduler(v)
    summary = sched.run_once(max_items)
    if summary is None:
        err = (v.get("assistant_status") or {}).get("error") or "assistant pass already running"
        code = 409 if "running" in err else 500
        return error_response(code, err)
    return jsonify({"ok": True, "summary": summary})
