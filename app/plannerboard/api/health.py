"""
/api/health, /api/restart, /api/diag/db-lock, /api/repair/conversation-routing,
/api/email-analytics.

Ported from server.py 7438-7691 (watcher rows, restart_needed), 7795-7958
(email-only analytics: two tables, no money), 7961-8250 (health), 8635-8648
(routing repair), 11879-11977 (write probe, db-lock diag), 11980-12032
(restart; now cross-platform: re-exec in place, no scheduler).

Rule kept from the original: a detector that is not running reads as DOWN.
Absent evidence is `down: true`, never green.
"""

import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, request

from .. import __version__
from ..db import CONN_REGISTRY
from ._common import body, error_response, int_arg, rt, store

bp = Blueprint("health", __name__)

_PROCESS_START = time.time()
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _age_minutes(hours, iso_ts):
    if not iso_ts:
        return None
    try:
        dt = hours.to_local(iso_ts)
        return round((hours.now() - dt).total_seconds() / 60.0, 1)
    except Exception:
        return None


def _watcher(hours, name, kind, expected_every_min, last_seen, grace=2.5, note=""):
    age = _age_minutes(hours, last_seen)
    if last_seen is None:
        state, down = "down", True
    elif expected_every_min and age is not None and age > expected_every_min * grace:
        state, down = "down", True
    else:
        state, down = "ok", False
    return {"name": name, "kind": kind, "expected_every_min": expected_every_min, "last_seen": last_seen,
            "age_min": age, "down": down, "state": state, "note": note}


def build_watchers(r):
    rows = []
    for name, w in sorted(r.watchers.items()):
        rows.append(_watcher(r.hours, name, w.get("kind", "loop"), w.get("expected_every_min"),
                             w.get("last_seen"), note=w.get("note", "")))
    return rows


def build_restart_needed():
    """Modules of THIS package imported by this process whose source changed
    after the process started."""
    out = []
    try:
        root = str(PACKAGE_ROOT)
        for name, mod in list(sys.modules.items()):
            f = getattr(mod, "__file__", None)
            if not f:
                continue
            try:
                pf = Path(f).resolve()
                if not str(pf).startswith(root):
                    continue
                m = pf.stat().st_mtime
            except Exception:
                continue
            if m > _PROCESS_START + 1:
                out.append({"module": pf.name, "source_modified": datetime.fromtimestamp(m).isoformat(timespec="seconds"),
                            "process_started": datetime.fromtimestamp(_PROCESS_START).isoformat(timespec="seconds")})
        out.sort(key=lambda x: x["module"])
    except Exception:
        pass
    return out


def _ai_credit_fields(r) -> dict:
    """balance / low_credits / credits_exhausted for the built-in provider.
    Reads only what the provider already knows (no network call here);
    None balance means "not known yet" or not the built-in provider."""
    out = {"balance": None, "low_credits": False, "credits_exhausted": False}
    try:
        llm = None
        v = getattr(r, "ext", None)
        if isinstance(v, dict):
            llm = v.get("llm")
        llm = llm or getattr(r, "llm", None)
        prov = getattr(llm, "provider", None)
        if getattr(prov, "name", "") != "emailboss":
            return out
        bal = getattr(prov, "last_balance", None)
        out["balance"] = int(bal) if bal is not None else None
        out["credits_exhausted"] = bool(getattr(llm, "credits_exhausted", False)) or out["balance"] == 0
        out["low_credits"] = out["balance"] is not None and out["balance"] < 50
    except Exception:
        pass
    return out


def health_payload(r):
    db = r.store
    probe = db.write_probe()
    watchers = build_watchers(r)
    try:
        alerts = db.list_alerts("open", 50)
    except Exception:
        alerts = []
    try:
        mail = r.mail_status() or {}
    except Exception as e:
        mail = {"provider": r.config.mail_provider, "ok": False, "last_fetch": None, "error": str(e)}
    try:
        ai = r.ai_status() or {}
        ai.setdefault("today_cost_usd", db.usage_today().get("cost_usd", 0.0))
    except Exception as e:
        ai = {"provider": r.config.ai_provider, "ok": False, "error": str(e)}
    ai.setdefault("budget_hit", False)
    ai.update(_ai_credit_fields(r))
    try:
        lic = r.license_status() or {}
    except Exception as e:
        lic = {"status": "error", "error": str(e)}
    pending = db.count_pending_triage()
    try:
        from ..updater import updater_from_app
        from flask import current_app
        upd = updater_from_app(current_app).summary()
    except Exception as e:  # noqa: BLE001
        upd = {"available": False, "latest": None, "current": __version__, "checked_at": None, "error": str(e)}
    return {
        "status": "ok" if probe.get("ok") else "degraded",
        "version": __version__,
        "data_root": str(getattr(getattr(r, "paths", None), "data_root", "") or ""),
        "uptime_s": r.uptime_s(),
        "db_write_ok": bool(probe.get("ok")),
        "db_write_probe": probe,
        "setup_required": r.config.setup_required,
        "profession": r.config.profession_template,
        "watchers": watchers,
        "watchers_down": [w["name"] for w in watchers if w.get("down")],
        "restart_needed": build_restart_needed(),
        "restart_requested": r.restart_requested,
        "alerts_open": len(alerts),
        "alerts_hard": sum(1 for a in alerts if a.get("severity") == "hard"),
        "alerts": alerts[:10],
        "last_triage": r.triage_state.get("last_run") or db.meta_get("last_triage", "") or None,
        "triage": dict(r.triage_state, pending=pending),
        "backup": dict(r.backup_state),
        "mail": mail,
        "ai": ai,
        "license": lic,
        "update": upd,
        "business_hours": r.hours.as_dict(),
        "project_count": db.conn.execute("SELECT COUNT(*) FROM projects WHERE archived = 0").fetchone()[0],
        "pending_triage": pending,
    }


@bp.route("/api/health", methods=["GET"])
def api_health():
    return jsonify(health_payload(rt()))


@bp.route("/api/diag/db-lock", methods=["GET"])
def api_diag_db_lock():
    db = store()
    out = {"db_path": str(db.db_path), "this_pid": os.getpid()}
    rows = []
    for tname, conn in list(CONN_REGISTRY.items()):
        try:
            rows.append({"thread": tname, "in_transaction": bool(conn.in_transaction)})
        except Exception as e:
            rows.append({"thread": tname, "in_transaction": f"?{type(e).__name__}"})
    out["connections"] = rows
    out["threads_alive"] = sorted(t.name for t in threading.enumerate())
    out["probe"] = db.write_probe(1.5)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db.db_path) + suffix)
        out[f"file{suffix or '_db'}"] = ({"size": f.stat().st_size,
                                          "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")}
                                         if f.exists() else None)
    return jsonify(out)


@bp.route("/api/restart", methods=["POST"])
def api_restart():
    """Restart in place: after `delay` seconds the process re-execs itself with
    the same argv (sockets and the lock file are close-on-exec). Under a
    supervisor that prefers a clean exit, set PB_RESTART_EXIT=1 and the
    process exits with code 3 instead. Poll /api/health afterwards."""
    r = rt()
    try:
        delay = max(0.2, min(10.0, float(body().get("delay", 1.0))))
    except (TypeError, ValueError):
        delay = 1.0
    r.restart_requested = True

    def _go():
        time.sleep(delay)
        try:
            sys.stdout.flush()
        except Exception:
            pass
        if os.environ.get("PB_RESTART_EXIT") == "1" or os.environ.get("PB_NO_EXEC") == "1":
            os._exit(3)
        # Always restart through the module entry point: sys.argv[0] may be a
        # console-script shim or the start.py file itself, neither of which
        # survives a bare re-exec as a plain script.
        if getattr(sys, "frozen", False):
            # Frozen build: sys.executable IS the app, "-m" would be parsed as its argument.
            cmd = [sys.executable] + list(sys.argv[1:])
        else:
            cmd = [sys.executable, "-m", "plannerboard.start"] + list(sys.argv[1:])
        # The new process waits for this port to free up before its own
        # single-instance probe (start.py reads PB_RESTART_WAIT).
        os.environ["PB_RESTART_WAIT"] = "1"
        tray = getattr(r, "tray_proc", None)
        if tray is not None:
            try:
                tray.terminate()    # the new process starts its own icon
            except Exception:
                pass
        # Werkzeug marks its listening socket inheritable (for its reloader);
        # the re-exec'd process must not inherit it or the port stays busy.
        try:
            fds = [int(x) for x in os.listdir("/proc/self/fd")]
        except Exception:
            fds = list(range(3, 1024))
        for fd in fds:
            if fd > 2:
                try:
                    os.set_inheritable(fd, False)
                except OSError:
                    pass
        try:
            if os.name == "nt":
                # No execv semantics on Windows: hand off to a child that
                # waits for this port to free up, then exit.
                import subprocess
                env = dict(os.environ, PB_RESTART_WAIT="1")
                subprocess.Popen(cmd, close_fds=True, env=env,
                                 creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                os._exit(0)
            os.execv(sys.executable, cmd)
        except Exception:
            os._exit(3)

    threading.Thread(target=_go, daemon=True, name="restart").start()
    return jsonify({"ok": True, "restarting_in_s": delay, "note": "poll /api/health; back in a few seconds"})


@bp.route("/api/repair/conversation-routing", methods=["POST"])
def api_repair_conversation_routing():
    db = store()
    data = body()
    conv, pid = data.get("conversation_id", ""), data.get("project_id", "")
    if not conv or not pid:
        return error_response(400, "conversation_id and project_id required")
    if not db.project_exists(pid):
        return error_response(404, f"Project {pid} not found")
    db.routing_set(conv, pid, "repair")
    return jsonify({"ok": True, "conversation_id": conv, "project_id": pid})


def _pct(values, q):
    if not values:
        return None
    v = sorted(values)
    return v[max(0, min(len(v) - 1, int(round(q * (len(v) - 1)))))]


@bp.route("/api/email-analytics", methods=["GET"])
def api_email_analytics():
    """Email-only analytics: project_correspondence + subtasks, no money."""
    r = rt()
    db = r.store
    hours = r.hours
    days = max(1, min(366, int_arg("days", 28)))
    since = hours.now_local_naive() - timedelta(days=days)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S")
    conn = db.conn
    rows = conn.execute(
        "SELECT project_id, conversation_id, subject, from_name, from_email, direction, received_at "
        "FROM project_correspondence WHERE COALESCE(received_at, created_at) >= ? "
        "ORDER BY COALESCE(received_at, created_at) ASC", (since_iso,)).fetchall()
    by_day, contacts, threads = {}, {}, {}
    inbound = outbound = 0
    for x in rows:
        d = (x["received_at"] or since_iso)[:10]
        slot = by_day.setdefault(d, {"date": d, "inbound": 0, "outbound": 0})
        if x["direction"] == "outbound":
            outbound += 1
            slot["outbound"] += 1
        else:
            inbound += 1
            slot["inbound"] += 1
            key = (x["from_email"] or "").lower() or "(unknown)"
            c = contacts.setdefault(key, {"contact": key, "name": x["from_name"] or "", "inbound": 0,
                                          "replied": 0, "response_hours": []})
            c["inbound"] += 1
        tk = x["conversation_id"] or f"subject:{(x['subject'] or '').lower()[:80]}"
        t = threads.setdefault(tk, {"thread": tk, "project_id": x["project_id"], "subject": x["subject"] or "",
                                    "messages": 0, "inbound": 0, "outbound": 0, "last": ""})
        t["messages"] += 1
        t["inbound" if x["direction"] != "outbound" else "outbound"] += 1
        if (x["received_at"] or "") > t["last"]:
            t["last"] = x["received_at"] or ""
    per_project = {}
    for x in rows:
        per_project.setdefault(x["project_id"], []).append(x)
    resp_hours, awaiting = [], 0
    for msgs in per_project.values():
        pending_in = None
        for m in msgs:
            if m["direction"] == "outbound":
                if pending_in is not None:
                    try:
                        h = hours.elapsed_biz_hours(pending_in["received_at"], m["received_at"])
                    except Exception:
                        h = None
                    if h is not None and h >= 0:
                        resp_hours.append(round(h, 2))
                        key = (pending_in["from_email"] or "").lower() or "(unknown)"
                        if key in contacts:
                            contacts[key]["replied"] += 1
                            contacts[key]["response_hours"].append(round(h, 2))
                    pending_in = None
            elif pending_in is None:
                pending_in = m
        if pending_in is not None:
            awaiting += 1
    sla_rows = conn.execute("SELECT status, sla_created, sla_hours, completed_date FROM subtasks "
                            "WHERE sla_type = 'response' AND sla_created >= ?", (since_iso,)).fetchall()
    hit = breached = open_ = 0
    default_sla = float(r.profession().get("sla_hours") or 4)
    for s in sla_rows:
        window = s["sla_hours"] or default_sla
        try:
            elapsed = hours.elapsed_biz_hours(s["sla_created"], s["completed_date"]) if s["status"] == "complete" \
                else hours.elapsed_biz_hours(s["sla_created"])
        except Exception:
            elapsed = None
        if s["status"] == "complete":
            if elapsed is None or elapsed <= window:
                hit += 1
            else:
                breached += 1
        elif elapsed is not None and elapsed > window:
            breached += 1
        else:
            open_ += 1
    fu_rows = conn.execute("SELECT project_id, id, text, follow_up_date, date_type FROM subtasks "
                           "WHERE status = 'pending' AND follow_up_date IS NOT NULL AND follow_up_date != ''").fetchall()
    today = hours.now().date()
    fu_open = fu_overdue = oldest = 0
    oldest_items = []
    for s in fu_rows:
        fu_open += 1
        try:
            d = datetime.strptime(s["follow_up_date"][:10], "%Y-%m-%d").date()
        except Exception:
            continue
        over = (today - d).days
        if over > 0:
            fu_overdue += 1
            oldest = max(oldest, over)
            oldest_items.append({"project_id": s["project_id"], "subtask_id": s["id"], "text": s["text"],
                                 "date_type": s["date_type"] or "follow_up", "days_overdue": over})
    oldest_items.sort(key=lambda x: -x["days_overdue"])
    top_contacts = sorted(({"contact": c["contact"], "name": c["name"], "inbound": c["inbound"],
                            "replied": c["replied"], "median_response_hours": _pct(c["response_hours"], 0.5)}
                           for c in contacts.values()), key=lambda x: -x["inbound"])
    total_sla = hit + breached + open_
    return jsonify({
        "window_days": days, "since": since_iso, "generated_at": hours.now_local_naive().isoformat(timespec="seconds"),
        "volume": {"inbound": inbound, "outbound": outbound, "total": inbound + outbound,
                   "by_day": [by_day[k] for k in sorted(by_day)]},
        "response_time": {"samples": len(resp_hours), "median_biz_hours": _pct(resp_hours, 0.5),
                          "p90_biz_hours": _pct(resp_hours, 0.9), "awaiting_reply": awaiting},
        "sla": {"total": total_sla, "hit": hit, "breached": breached, "open": open_,
                "hit_pct": round(hit / total_sla * 100) if total_sla else None},
        "follow_ups": {"open": fu_open, "overdue": fu_overdue, "oldest_days": oldest},
        "oldest_waiting": oldest_items[:15], "top_contacts": top_contacts[:20],
        "top_threads": sorted(threads.values(), key=lambda t: -t["messages"])[:20],
    })
