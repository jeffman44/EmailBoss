"""
/api/ai/*: providers, test, usage, and the model-backed endpoints the board
calls (draft-reply, draft-follow-up, suggest-job-name, suggest-rename,
project-health, project-health/batch, search, weekly-digest).

Response shapes follow the reference server.py 9379-9670 so board.html keeps
working: an offline provider answers 503 {"ok": false, "error": "AI provider
is offline"}; search degrades to a plain text filter instead of failing.
Every call goes through the prompt registry (`ai.registry.LLM.run`) so the
budget cap, grounding guards and usage ledger apply. Drafts are returned to
the caller; nothing here sends mail.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request

from .. import workflows as wfm
from ..ai.factory import PROVIDER_CATALOG, SECRET_KEY, make_provider
from ..ai.base import NullProvider
from ..ai.guards import clean_body, strip_noise_lines, strip_signature_from_draft
from ..ai.registry import prompt_ids
from ._common import body, error_response, feature_on, view

bp = Blueprint("ai_api", __name__)

OFFLINE = {"ok": False, "error": "AI provider is offline"}
DEFAULT_VISIBLE_FIELDS = ["name", "scope", "location"]
MONEY_FIELDS = ("amount", "price", "cost", "margin", "quote", "total")


# ── helpers ──────────────────────────────────────────────────────────────────

def _llm():
    return view().get("llm")


def _offline(llm) -> bool:
    return llm is None or not getattr(llm, "available", False)


def _profile(cfg: dict) -> dict:
    u = cfg.get("user") or {}
    return {"user_name": u.get("name") or "", "company": u.get("company") or "", "role": u.get("role") or ""}


def _money_on(v) -> bool:
    feats = (v.get("cfg") or {}).get("features") or {}
    return bool(feats.get("money", True))


def _visible_fields(v) -> list:
    return list((v.get("profession") or {}).get("customer_visible_fields") or DEFAULT_VISIBLE_FIELDS)


def _visible_facts(v, p: dict) -> str:
    """Facts a draft may state: the profession's customer_visible_fields plus
    the quoted amount when the money feature is on."""
    kd = p.get("key_details") or {}
    lines = []
    for key in _visible_fields(v):
        val = p.get(key, kd.get(key))
        if val in (None, "", [], {}):
            continue
        lines.append(f"{key}: {val}")
    if _money_on(v):
        amt = p.get("quote_amount")
        if isinstance(amt, (int, float)) and amt:
            lines.append(f"quoted: {amt:,.0f}")
    return "\n".join(lines) or "(none)"


def _wf_doc(v) -> dict:
    return (v.get("profession") or {}).get("workflows") or {}


def _project_summary(p: dict) -> dict:
    return {k: val for k, val in p.items() if k not in ("subtasks", "notes", "correspondence")}


def _get_project(v, pid):
    st = v.get("store")
    if st is None or not pid:
        return None
    try:
        return st.get_project(pid)
    except Exception:
        return None


def _active_projects(v) -> list:
    st = v.get("store")
    try:
        return list(st.all_projects(include_archived=False) or [])
    except TypeError:
        return list(st.all_projects() or [])
    except Exception:
        return []


def _suggest_name(v, llm, subject: str, body_text: str, from_name="", from_email=""):
    out = llm.run("triage.suggest_project", {
        "from_name": from_name, "from_email": from_email, "subject": subject,
        "body": body_text, "attachment_names": "",
        "source_text": subject + "\n" + body_text,
    })
    if not out:
        return None
    val = out[0]
    return (val.get("name") if isinstance(val, dict) else str(val)) or None


# ── providers / usage / test ─────────────────────────────────────────────────

@bp.route("/api/ai/providers", methods=["GET"])
def api_ai_providers():
    v = view()
    llm = v.get("llm")
    getter = v.get("secret_getter")
    key_present = False
    try:
        key_present = bool(getter(SECRET_KEY)) if getter else False
    except Exception:
        key_present = False
    current = llm.status() if llm is not None else {"provider": (v.get("cfg").get("ai") or {}).get("provider", "none")}
    return jsonify({"ok": True, "providers": PROVIDER_CATALOG, "current": current,
                    "prompts": prompt_ids(), "key_present": key_present})


@bp.route("/api/ai/usage", methods=["GET"])
def api_ai_usage():
    v = view()
    llm = v.get("llm")
    st = v.get("store")
    today = {"cost_usd": 0.0, "calls": 0, "by_task": {}}
    try:
        today = (llm.meter.today() if llm is not None and llm.meter else st.usage_today()) or today
    except Exception:
        pass
    budget = float(getattr(getattr(llm, "meter", None), "daily_budget_usd",
                           ((v.get("cfg").get("ai") or {}).get("daily_budget_usd") or 0.0)))
    prov = _builtin_provider(llm)
    month = _usage_month(st)
    if prov is not None:
        month["credits"] = int(round(float(month.get("cost_usd") or 0) * 100))
    return jsonify({"ok": True, "today": today, "month": month, "budget_usd": budget,
                    "budget_hit": bool(llm.budget_hit) if llm is not None else False,
                    "provider": llm.provider_name if llm is not None else "none",
                    "model": llm.model if llm is not None else "",
                    "builtin": prov is not None,
                    "balance": getattr(prov, "last_balance", None) if prov is not None else None,
                    "credits_today": int(round(float(today.get("cost_usd") or 0) * 100)) if prov is not None else None,
                    "credits_exhausted": bool(getattr(llm, "credits_exhausted", False))})


def _usage_month(st) -> dict:
    """Calls and cost this calendar month from the local usage ledger."""
    out = {"calls": 0, "cost_usd": 0.0}
    if st is None:
        return out
    try:
        conn = getattr(st, "conn", None) or getattr(st, "_conn", None)
        row = conn.execute("SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM ai_usage WHERE substr(ts, 1, 7) = ?",
                           (date.today().strftime("%Y-%m"),)).fetchone()
        out = {"calls": int(row[0] or 0), "cost_usd": round(float(row[1] or 0), 4)}
    except Exception:
        pass
    return out


_balance_cache: dict = {"ts": 0.0, "data": {}}


def _builtin_provider(llm):
    prov = getattr(llm, "provider", None)
    return prov if getattr(prov, "name", "") == "emailboss" else None


@bp.route("/api/ai/balance", methods=["GET"])
def api_ai_balance():
    """Credit balance + packs from the relay (cached 60 s). Shape is stable
    even when the built-in provider is not in use."""
    import time as _t
    v = view()
    llm = v.get("llm")
    prov = _builtin_provider(llm)
    if prov is None:
        return jsonify({"ok": True, "builtin": False, "balance": None, "packs": [], "low": False})
    force = (request.args.get("refresh") or "") == "1"
    if force or _t.time() - _balance_cache["ts"] > 60 or not _balance_cache["data"]:
        data = prov.balance() or {}
        if data:
            _balance_cache.update(ts=_t.time(), data=data)
    data = _balance_cache["data"] or {}
    bal = data.get("balance")
    bal = int(bal) if bal is not None else prov.last_balance
    return jsonify({"ok": True, "builtin": True, "balance": bal,
                    "packs": data.get("packs") or [], "trial_granted": bool(data.get("trial_granted")),
                    "low": (bal is not None and bal < 50), "reachable": bool(data),
                    "credits_exhausted": bool(getattr(llm, "credits_exhausted", False))})


@bp.route("/api/ai/checkout", methods=["POST"])
def api_ai_checkout():
    """{pack} -> {url}. The UI opens the URL in the system browser."""
    v = view()
    prov = _builtin_provider(v.get("llm"))
    if prov is None:
        return jsonify({"ok": False, "error": "Email Boss AI is not the active provider"}), 400
    pack = str(body().get("pack") or "")
    if not pack:
        return jsonify({"ok": False, "error": "pack required"}), 400
    url, err = prov.checkout_url(pack)
    if not url:
        return jsonify({"ok": False, "error": err or "could not start checkout"}), 502
    _balance_cache["ts"] = 0.0
    return jsonify({"ok": True, "url": url})


@bp.route("/api/ai/test", methods=["POST"])
def api_ai_test():
    """Try a provider with the posted {provider, api_key, model, base_url}
    (nothing is saved). A missing key answers ok:false with provider none."""
    v = view()
    data = body()
    overrides = {k: data.get(k) for k in ("provider", "api_key", "model", "base_url") if data.get(k) not in (None, "")}
    try:
        from ..ai.wiring import license_creds_getter
        prov = make_provider(v.get("cfg") or {}, v.get("secret_getter"), overrides=overrides,
                             license_creds=license_creds_getter(v.backing if hasattr(v, "backing") else v))
    except Exception as e:
        return jsonify({"ok": False, "provider": "none", "error": f"{type(e).__name__}: {e}"})
    if isinstance(prov, NullProvider):
        return jsonify({"ok": False, "provider": "none", "error": prov.reason})
    try:
        if not prov.available():
            return jsonify({"ok": False, "provider": prov.name, "error": "provider not reachable"})
        res = prov.complete("Reply with the single word OK.", "OK", max_tokens=5, temperature=0.0)
    except Exception as e:
        return jsonify({"ok": False, "provider": prov.name, "error": f"{type(e).__name__}: {e}"})
    return jsonify({"ok": bool(res.ok), "provider": prov.name, "model": res.model,
                    "latency_ms": res.latency_ms, "error": res.error or None,
                    "sample": (res.text or "")[:80]})


# ── drafts ───────────────────────────────────────────────────────────────────

@bp.route("/api/ai/draft-reply", methods=["POST"])
def api_ai_draft_reply():
    """Body: {subject, body_preview|body, from_name, from_email, project_id?,
    project_context?, direction_note?}. Returns {"ok": true, "draft": text}."""
    v = view()
    llm = v.get("llm")
    if _offline(llm):
        return jsonify(OFFLINE), 503
    data = body()
    profile = _profile(v.get("cfg") or {})
    body_in = strip_noise_lines(clean_body(data.get("body") or data.get("body_preview") or "", 900, profile), profile)
    proj = _get_project(v, data.get("project_id")) or {}
    facts = _visible_facts(v, proj) if proj else (data.get("project_context") or "(none)")
    subject = data.get("subject") or ""
    out = llm.run("reply.draft", {
        "from_name": data.get("from_name") or "", "from_email": data.get("from_email") or "",
        "subject": subject, "body": body_in, "project_name": proj.get("name") or data.get("project_name") or "",
        "visible_facts": facts, "direction_note": data.get("direction_note") or "",
        "source_text": f"{subject}\n{body_in}\n{facts}",
    })
    if not out:
        return error_response(502, llm.last_error or "no draft produced")
    val = out[0]
    text = val.get("body", "") if isinstance(val, dict) else str(val)
    return jsonify({"ok": True, "draft": strip_signature_from_draft(text, profile).strip()})


@bp.route("/api/ai/draft-follow-up", methods=["POST"])
def api_ai_draft_follow_up():
    """Body: {project_id} or the explicit fields (project_name, customer_name,
    work_description, days_since_sent, last_communication, attempt_number)."""
    v = view()
    llm = v.get("llm")
    if _offline(llm):
        return jsonify(OFFLINE), 503
    data = dict(body())
    proj = _get_project(v, data.get("project_id")) or {}
    if proj:
        data.setdefault("project_name", proj.get("name", ""))
        contacts = proj.get("contacts") or []
        data.setdefault("customer_name", (contacts[0].get("name") if contacts else "") or proj.get("client") or "")
        data.setdefault("work_description", proj.get("scope") or proj.get("name", ""))
        sent = proj.get("quote_sent_date")
        if sent and "days_since_sent" not in data:
            try:
                data["days_since_sent"] = (date.today() - date.fromisoformat(str(sent)[:10])).days
            except ValueError:
                pass
        data.setdefault("last_communication", proj.get("last_communication") or proj.get("last_activity") or "")
    facts = _visible_facts(v, proj) if proj else "(none)"
    vars_ = {
        "project_name": data.get("project_name") or "", "customer_name": data.get("customer_name") or "",
        "work_description": data.get("work_description") or "",
        "days_since_sent": data.get("days_since_sent") if data.get("days_since_sent") is not None else "?",
        "last_communication": data.get("last_communication") or "unknown",
        "attempt_number": int(data.get("attempt_number") or 1), "visible_facts": facts,
    }
    vars_["source_text"] = "\n".join(str(x) for x in vars_.values())
    out = llm.run("followup.draft", vars_)
    if not out:
        return error_response(502, llm.last_error or "no draft produced")
    val = out[0]
    text = val.get("body", "") if isinstance(val, dict) else str(val)
    return jsonify({"ok": True, "draft": strip_signature_from_draft(text, _profile(v.get("cfg") or {})).strip()})


# ── names ────────────────────────────────────────────────────────────────────

@bp.route("/api/ai/suggest-job-name", methods=["POST"])
def api_ai_suggest_job_name():
    """Reference shape: {equipment_type, repair, building, model} -> job_name.
    Also accepts {subject, body, from_name, from_email}."""
    v = view()
    llm = v.get("llm")
    if _offline(llm):
        return jsonify(OFFLINE), 503
    data = body()
    subject = data.get("subject") or " ".join(
        str(data.get(k) or "") for k in ("building", "equipment_type", "model", "repair")).strip()
    name = _suggest_name(v, llm, subject, data.get("body") or "", data.get("from_name") or "",
                         data.get("from_email") or "")
    if not name:
        return error_response(502, llm.last_error or "no suggestion")
    return jsonify({"ok": True, "job_name": name})


@bp.route("/api/ai/suggest-rename", methods=["POST"])
def api_ai_suggest_rename():
    v = view()
    llm = v.get("llm")
    if _offline(llm):
        return jsonify(OFFLINE), 503
    data = body()
    proj = _get_project(v, data.get("project_id")) or {}
    current = data.get("current_name") or proj.get("name") or ""
    parts = [data.get("site") or proj.get("location") or proj.get("site") or "",
             data.get("scope") or proj.get("scope") or "", data.get("work_type") or proj.get("work_type") or ""]
    name = _suggest_name(v, llm, current, "\n".join(p for p in parts if p))
    if not name:
        return error_response(502, llm.last_error or "no suggestion")
    return jsonify({"ok": True, "suggestion": name})


# ── health ───────────────────────────────────────────────────────────────────

def _assess(llm, p: dict):
    pending = [t for t in (p.get("subtasks") or []) if (t.get("status") or "pending") == "pending"]
    deadline = next((t.get("follow_up_date") for t in pending if t.get("date_type") == "deadline"), "")
    notes = p.get("notes") or []
    out = llm.run("project.health", {
        "project_name": p.get("name", ""), "project_status": p.get("status", ""),
        "created": p.get("created", ""),
        "last_communication": p.get("last_communication") or p.get("last_activity") or "",
        "follow_up_date": p.get("follow_up_date") or "", "deadline": deadline or "",
        "emails_sent": p.get("emails_sent", 0), "pending_count": len(pending),
        "pending_tasks": "\n".join(f"- {t.get('text', '')} ({t.get('date_type') or 'follow_up'} "
                                   f"{t.get('follow_up_date') or ''})" for t in pending[:8]),
        "recent_notes": "\n".join(f"- {(n.get('text') if isinstance(n, dict) else str(n))[:120]}" for n in notes[-3:]),
        "today": date.today().isoformat(),
    })
    if not out or not isinstance(out[0], dict):
        return None
    val = out[0]
    try:
        prio = int(val.get("priority") or 3)
    except (TypeError, ValueError):
        prio = 3
    return {"health": val.get("health", ""), "recommendation": val.get("recommendation", ""),
            "reason": val.get("reason", ""), "priority": max(1, min(5, prio))}


@bp.route("/api/ai/project-health", methods=["POST"])
def api_ai_project_health():
    v = view()
    llm = v.get("llm")
    if _offline(llm):
        return jsonify(OFFLINE), 503
    data = body()
    pid = data.get("project_id")
    if pid:
        proj = _get_project(v, pid)
        if not proj:
            return error_response(404, f"Project {pid} not found")
    else:
        proj = data
    assessment = _assess(llm, proj)
    if assessment is None:
        return error_response(502, llm.last_error or "no assessment")
    return jsonify({"ok": True, "assessment": assessment})


@bp.route("/api/ai/project-health/batch", methods=["POST"])
def api_ai_project_health_batch():
    v = view()
    llm = v.get("llm")
    if _offline(llm):
        return jsonify(OFFLINE), 503
    data = body()
    ids = data.get("project_ids")
    if ids:
        projects = [p for p in (_get_project(v, pid) for pid in ids) if p]
    else:
        projects = _active_projects(v)
    results = {}
    for p in projects[:20]:
        a = _assess(llm, p)
        if a is not None:
            results[p.get("id")] = a
    return jsonify({"ok": True, "assessments": results, "count": len(results)})


# ── search ───────────────────────────────────────────────────────────────────

def _match_filters(v, p: dict, f: dict, today: date) -> bool:
    kd = p.get("key_details") or {}
    if f.get("status") and p.get("status") != f["status"]:
        return False
    if f.get("work_type") and p.get("work_type") != f["work_type"]:
        return False
    amt = p.get("quote_amount") or 0
    if f.get("min_amount") and amt < float(f["min_amount"]):
        return False
    if f.get("max_amount") and amt > float(f["max_amount"]):
        return False
    hay = f"{p.get('name', '')} {p.get('location') or p.get('site') or ''}".lower()
    if f.get("name_contains") and str(f["name_contains"]).lower() not in hay:
        return False
    if f.get("site_contains") and str(f["site_contains"]).lower() not in str(p.get("location") or p.get("site") or "").lower():
        return False
    if f.get("client_contains"):
        needle = str(f["client_contains"]).lower()
        pool = [p.get("client") or "", kd.get("client") or ""] + [c.get("name", "") for c in (p.get("contacts") or [])]
        if not any(needle in str(x).lower() for x in pool):
            return False
    if f.get("overdue_only"):
        fu = p.get("follow_up_date")
        if not fu or str(fu)[:10] >= today.isoformat():
            return False
    if f.get("has_quote") is True and not amt:
        return False
    if f.get("has_quote") is False and amt:
        return False
    if f.get("days_since_activity"):
        last = p.get("last_communication") or p.get("last_activity") or ""
        cutoff = (today - timedelta(days=int(f["days_since_activity"]))).isoformat()
        if last and str(last)[:10] >= cutoff:
            return False
    return True


@bp.route("/api/ai/search", methods=["POST"])
def api_ai_search():
    """Natural-language search over the board. Without a model the query is a
    plain name filter, so the box always answers."""
    v = view()
    llm = v.get("llm")
    data = body()
    query = (data.get("query") or "").strip()
    if not query:
        return error_response(400, "No query provided")
    filters = None
    if not _offline(llm):
        out = llm.run("search.parse", {"query": query})
        if out and isinstance(out[0], dict):
            filters = {k: val for k, val in out[0].items() if val not in (None, "")}
    if filters is None:
        filters = {"name_contains": query}
    today = date.today()
    doc = _wf_doc(v)
    results = []
    for p in _active_projects(v):
        if p.get("archived"):
            continue
        if doc and wfm.is_closed(doc, p.get("status"), p.get("workflow_id")) and not filters.get("status"):
            continue
        if _match_filters(v, p, filters, today):
            results.append(_project_summary(p))
    sort = filters.get("sort_by") or "urgency"
    if sort == "amount":
        results.sort(key=lambda p: -(p.get("quote_amount") or 0))
    elif sort == "name":
        results.sort(key=lambda p: (p.get("name") or "").lower())
    elif sort == "date":
        results.sort(key=lambda p: p.get("created") or "", reverse=True)
    else:
        results.sort(key=lambda p: (p.get("follow_up_date") or "9999-12-31"))
    results = results[:25]
    return jsonify({"ok": True, "query": query, "filters": filters, "results": results, "count": len(results),
                    "text": _search_sentence(len(results), filters, ai_used=not _offline(llm)),
                    "items": [_item(p, v, filters, today) for p in results]})


def _why(p: dict, f: dict | None, today: date | None = None) -> str:
    """One short reason this project matched, in plain words."""
    f = f or {}
    today = today or date.today()
    loc = str(p.get("location") or p.get("site") or "")
    if f.get("overdue_only"):
        return f"follow-up was due {str(p.get('follow_up_date') or '')[:10]}"
    if f.get("days_since_activity"):
        return f"no activity in {int(f['days_since_activity'])} days"
    if f.get("client_contains"):
        return f"client or contact matches \"{f['client_contains']}\""
    if f.get("site_contains"):
        return f"at {loc}" if loc else "place matches"
    if f.get("status"):
        return f"stage is {p.get('status')}"
    if f.get("name_contains"):
        needle = str(f["name_contains"]).lower()
        if needle in str(p.get("name") or "").lower():
            return f"name contains \"{f['name_contains']}\""
        return f"place contains \"{f['name_contains']}\""
    fu = p.get("follow_up_date")
    return f"follow up {str(fu)[:10]}" if fu else "matches your question"


def _item(p: dict, v=None, filters: dict | None = None, today: date | None = None) -> dict:
    """A row the board can render without knowing the project schema.
    id / name / why is the shape the Ask list uses; project_id / label /
    detail stay for older boards."""
    detail = [str(p.get("location") or p.get("site") or "")]
    fu = p.get("follow_up_date")
    if fu:
        detail.append(f"follow up {str(fu)[:10]}")
    return {"id": p.get("id", ""), "name": p.get("name", ""), "why": _why(p, filters, today),
            "project_id": p.get("id", ""), "label": p.get("name", ""),
            "detail": " · ".join(d for d in detail if d)}


def _search_sentence(n: int, filters: dict, ai_used: bool) -> str:
    if not n:
        return "Nothing on the board matches that."
    what = "1 match" if n == 1 else f"{n} matches"
    if not ai_used:
        return f"Found {what} by name. Turn on Email Boss AI to ask in plain words."
    return f"Found {what}."


# ── weekly digest ────────────────────────────────────────────────────────────

def _digest_stats(v) -> dict:
    st = v.get("store")
    doc = _wf_doc(v)
    today = date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    try:
        everything = list(st.all_projects(include_archived=True) or [])
    except TypeError:
        everything = list(st.all_projects() or [])
    active = [p for p in everything if not p.get("archived")
              and not (doc and wfm.is_closed(doc, p.get("status"), p.get("workflow_id")))]
    out_stages = set(wfm.stages_with_role(doc, "out")) if doc else set()

    def _won_day(p):
        return p.get("won_date") or p.get("closed_date") or ""

    pipeline = sum(float(p.get("quote_amount") or 0) for p in active)
    won_ytd = sum(float(p.get("approved_amount") or 0) for p in everything
                  if p.get("approved_amount") and str(_won_day(p)).startswith(str(today.year)))
    overdue = sum(1 for p in active if p.get("follow_up_date") and str(p["follow_up_date"])[:10] < today.isoformat())
    stale = sum(1 for p in active if (p.get("last_communication") or p.get("last_activity"))
                and str(p.get("last_communication") or p.get("last_activity"))[:10] < week_ago)
    top = max((p for p in active if p.get("quote_amount")), key=lambda p: p.get("quote_amount") or 0, default=None)
    top_name = f"{top['name']} ({top['quote_amount']:,.0f})" if top else "none"
    win_count = sum(1 for p in everything if p.get("approved_amount"))
    quotable = sum(1 for p in everything if p.get("quote_amount") or p.get("approved_amount"))
    return {
        "quotes_sent": sum(1 for p in active if p.get("status") in out_stages),
        "approvals": sum(1 for p in everything if p.get("approved_amount") and str(_won_day(p)) >= week_ago),
        "new_projects": sum(1 for p in active if str(p.get("created") or "") >= week_ago),
        "overdue_count": overdue,
        "total_pipeline": pipeline,
        "won_ytd": won_ytd,
        "win_rate": (win_count / max(1, quotable)) * 100,
        "top_opportunity": top_name,
        "stale_count": stale,
    }


@bp.route("/api/ai/weekly-digest", methods=["POST"])
def api_ai_weekly_digest():
    v = view()
    llm = v.get("llm")
    stats = _digest_stats(v)
    money = _money_on(v)
    if _offline(llm):
        return _digest_without_ai(v, stats, money)
    lines = []
    for k, val in stats.items():
        if not money and any(m in k for m in ("pipeline", "won", "win_rate", "opportunity")):
            continue
        lines.append(f"{k}: {val:,.0f}" if isinstance(val, float) else f"{k}: {val}")
    out = llm.run("digest.weekly", {"stats_block": "\n".join(lines), "today": date.today().isoformat()})
    if not out:
        return _digest_without_ai(v, stats, money)
    val = out[0]
    digest = (val.get("digest") or val.get("text")) if isinstance(val, dict) else str(val)
    digest = (digest or "").strip()
    return jsonify({"ok": True, "digest": digest, "text": digest, "items": _digest_items(stats, money),
                    "stats": stats, "generated_at": datetime.now().isoformat(timespec="seconds")})


def _digest_without_ai(v, stats: dict, money: bool):
    """AI offline: the engine's plain-sentence digest, never a 503."""
    from ..ai import assistant as _assistant
    try:
        text = _assistant.digest_plain_text(_assistant.digest_stats(v.get("store"), v.get("profession") or {}))
    except Exception:
        text = ""
    if not text:
        text = "Here are this week's numbers."
    return jsonify({"ok": True, "digest": text, "text": text, "items": _digest_items(stats, money), "stats": stats,
                    "ai_used": False, "generated_at": datetime.now().isoformat(timespec="seconds")})


_DIGEST_LABELS = (
    ("new_projects", "New this week"), ("quotes_sent", "Waiting on an answer"), ("approvals", "Won this week"),
    ("overdue_count", "Overdue"), ("stale_count", "Quiet for over a week"), ("total_pipeline", "Open quotes"),
    ("won_ytd", "Won this year"), ("top_opportunity", "Biggest open quote"),
)


def _digest_items(stats: dict, money: bool) -> list:
    """The numbers behind the digest as plain labelled rows."""
    out = []
    for key, label in _DIGEST_LABELS:
        if key not in stats:
            continue
        if not money and key in ("total_pipeline", "won_ytd", "top_opportunity"):
            continue
        val = stats[key]
        out.append({"label": f"{label}: {val:,.0f}" if isinstance(val, float) else f"{label}: {val}", "detail": ""})
    return out
