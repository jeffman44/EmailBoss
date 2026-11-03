"""
The ladder: task arming, rung advance / revert, entry-stage tasks and the
response SLA, as pure functions over a Store and a workflow document.

Ported from the reference server.py 2504-2722 (_wf_id_for, _ladder_rows,
_ladder_rung_for_text, _ensure_ladder_task, _advance_job_ladder,
_revert_job_ladder, _ensure_entry_stage_task), 2868-2979
(_ensure_build_proposal_subtask, now the generic quote task) and 3011-3081
(_spawn_response_subtask). Every literal status check is a role or a lookup
in the workflow document; the business clock comes from store.hours.

EVERY rung advance is the user checking a box. The trigger is their own click
through the store's send-guard, never a system inference of what an email
"probably means".
"""

import re

from . import workflows as wfm

DEFAULT_SLA_HOURS = 4

_ROBOT_LOCAL_RE = re.compile(
    r'^(no.?reply|do.?not.?reply|auto.?reply|customer.?service|orders?|shipping|tracking|'
    r'notifications?|support|mailer|info|sales)$')
_TXN_SUBJECT_RE = re.compile(
    r'\b(order|shipment|shipped|delivered|delivery|delayed|backorder|back\s*order|tracking|invoice|'
    r'receipt|confirmation|packing\s*slip|ready\s*for\s*pickup)\b', re.IGNORECASE)


def sla_hours(store):
    try:
        return float(store.profession().get("sla_hours") or DEFAULT_SLA_HOURS)
    except (TypeError, ValueError):
        return DEFAULT_SLA_HOURS


def wf_id_for(store, proj, doc=None):
    """The workflow id a project walks. Never raises."""
    doc = doc or store.wf_doc()
    try:
        wf = wfm.workflow_for_project(doc, proj or {}, store.work_types())
        return (wf or {}).get("id") or wfm.default_workflow_id(doc)
    except Exception:
        return wfm.default_workflow_id(doc)


def ladder_by_status(store, proj, doc=None):
    doc = doc or store.wf_doc()
    return wfm.ladder_by_status(doc, wf_id_for(store, proj, doc))


def ladder_statuses(store, proj, doc=None):
    return list(ladder_by_status(store, proj, doc).keys())


def rung_for_text(store, text, proj=None, doc=None):
    doc = doc or store.wf_doc()
    return wfm.rung_for_text(doc, text, wf_id_for(store, proj, doc) if proj is not None else None)


def ensure_ladder_task(store, pid, rung, note=True):
    """Find-or-arm the ONE subtask that owns `rung`. Reopens a complete task of
    the same text instead of adding a second one, which is what makes the
    un-check revert land cleanly. server.py:2576-2630."""
    proj = store.get_project(pid)
    if not proj:
        return None
    row = ladder_by_status(store, proj).get(rung)
    if not row:
        return None
    _, text, _nxt, category, hours, fu_days = row
    now_iso = store.hours.now_local_naive().isoformat(timespec="seconds")
    fu_date = store.hours.add_biz_days(store.hours.today(), fu_days) if fu_days else None
    existing = next((s for s in store.get_subtasks(pid)
                     if (s.get("text") or "").strip().lower() == text.lower()), None)
    if existing:
        store.update_subtask_fields(
            pid, existing["id"], status="pending", completed_date=None, completed_by=None,
            sla_type="response" if hours else None, sla_created=now_iso if hours else None,
            sla_hours=hours, follow_up_date=fu_date, category=category,
            date_type="sla" if hours else "follow_up")
        return existing["id"]
    st_id = store.add_subtask(
        pid, text, date_type="sla" if hours else "follow_up", follow_up_date=fu_date,
        sla_hours=hours, source="system", category=category,
        sla_type="response" if hours else None, sla_created=now_iso if hours else None,
        metadata={"auto": "ladder", "rung": rung})
    if st_id and note:
        store.append_note(pid, f"Ladder: armed '{text}' at {rung}", store._note_time("auto"))
    return st_id


def advance_job_ladder(store, pid, from_rung):
    """Checking a rung's task moves the project to the next rung and arms that
    rung's task. Caller MUST have verified status == from_rung.
    server.py:2633-2665."""
    proj = store.get_project(pid)
    if not proj:
        return None
    by_status = ladder_by_status(store, proj)
    row = by_status.get(from_rung)
    if not row:
        return None
    next_status = row[2]
    store.update_project(pid, status=next_status)
    store.append_note(pid, f"'{row[1]}' checked -> status auto-set to {next_status}",
                      f"{store.hours.today()}, auto")
    if next_status in by_status:
        ensure_ladder_task(store, pid, next_status)
    else:
        doc = store.wf_doc()
        role = wfm.role_of(doc, next_status, proj.get("workflow_id"))
        if role in ("closed", "in_progress") and not proj.get("closed_date"):
            store.update_project(pid, closed_date=store.hours.today())
    return next_status


def revert_job_ladder(store, pid, to_rung):
    """Symmetric un-check: reopening a rung's task while the project sits at
    the rung AFTER it pulls the project back. server.py:2668-2684."""
    proj = store.get_project(pid)
    row = ladder_by_status(store, proj).get(to_rung)
    store.update_project(pid, status=to_rung)
    store.append_note(pid, f"'{(row[1] if row else to_rung)}' reopened -> status reverted to {to_rung}",
                      f"{store.hours.today()}, auto")


def quote_task_text(store, proj, doc=None):
    doc = doc or store.wf_doc()
    _, ot = wfm.quote_task_for(doc, wf_id_for(store, proj, doc))
    return (ot or {}).get("text") or "Prepare and send proposal"


def is_quote_task(store, row, proj=None, doc=None):
    """THE quote task for a project: rides the quote aging curve (date_type
    'quote') or carries the workflow's quote task text. server.py:2859-2865."""
    if (row.get("date_type") or "") == "quote":
        return True
    doc = doc or store.wf_doc()
    texts = set()
    wids = [wf_id_for(store, proj, doc)] if proj is not None else wfm.workflow_ids(doc)
    for wid in wids:
        _, ot = wfm.quote_task_for(doc, wid)
        if ot and ot.get("text"):
            texts.add(ot["text"].strip().lower())
    return (row.get("text") or "").strip().lower() in texts


def ensure_quote_task(store, pid, proj=None, reset_clock=False):
    """A project entering its quote stage carries exactly ONE aging quote task.
    Find-or-arm: a pending 'quote' task -> no-op (clock reset on genuine
    re-entry); a pending deadline-typed task -> left alone (a real due date
    outranks the curve); a pending same-text follow-up -> flipped to 'quote';
    otherwise added. allow_if_completed so a re-quote re-arms.
    server.py:2868-2979 generalized."""
    proj = proj or store.get_project(pid)
    if not proj:
        return None
    doc = store.wf_doc()
    wid = wf_id_for(store, proj, doc)
    qstage, ot = wfm.quote_task_for(doc, wid)
    if not qstage or not ot:
        return None
    today = store.hours.today()
    now_ts = store.hours.now_local_naive().isoformat(timespec="seconds")
    pending = store.get_subtasks(pid, status="pending")
    armed = next((s for s in pending if (s.get("date_type") or "") == "quote"), None)
    if armed:
        if reset_clock and (armed.get("follow_up_date") or "")[:10] != today:
            store.update_subtask_fields(pid, armed["id"], follow_up_date=today, sla_created=now_ts)
        return armed["id"]
    text = ot.get("text") or "Prepare and send proposal"
    existing = next((s for s in pending if (s.get("text") or "").strip().lower() == text.lower()), None)
    if existing:
        if (existing.get("date_type") or "") == "deadline":
            return existing["id"]
        store.update_subtask_fields(pid, existing["id"], date_type="quote", follow_up_date=today,
                                    sla_created=now_ts)
        return existing["id"]
    st_id = store.add_subtask(pid, text, date_type="quote", follow_up_date=today, source="system",
                              category=ot.get("category") or "", sla_created=now_ts,
                              metadata={"auto": "quote_task"}, allow_if_completed=True)
    if st_id:
        store.append_note(pid, f"Auto-added '{text}' (quote aging) on {qstage}", f"{today}, auto")
    return st_id


def ensure_entry_stage_task(store, pid, proj=None):
    """Arm the owning_task of the workflow's ENTRY stage on a new project. The
    quote task is delegated to ensure_quote_task; every other entry task goes
    through the rung machinery; an entry stage with no task is a no-op.
    Never raises. server.py:2687-2722."""
    try:
        proj = proj or store.get_project(pid)
        if not proj:
            return None
        doc = store.wf_doc()
        wf = wfm.workflow_for_project(doc, proj, store.work_types())
        if not wf:
            return None
        entry = wf.get("entry_stage")
        if not entry or proj.get("status") != entry:
            return None
        st = wfm.get_stage(wf, entry) or {}
        ot = st.get("owning_task") or {}
        if not ot or not st.get("auto_arm", True):
            return None
        if wfm.is_quote_owning_task(ot):
            return ensure_quote_task(store, pid, proj=proj)
        if entry in wfm.ladder_by_status(doc, wf.get("id")):
            return ensure_ladder_task(store, pid, entry)
        # An entry task that advances nowhere (the general template's
        # 'Respond' SLA) is still armed, once.
        hours = ot.get("sla_hours") if (ot.get("date_type") or "") == "sla" else None
        days = ot.get("days") if (ot.get("date_type") or "follow_up") in ("follow_up", "deadline") else None
        fu = store.hours.add_biz_days(store.hours.today(), days) if days else None
        return store.add_subtask(pid, ot["text"], date_type=ot.get("date_type") or "follow_up",
                                 follow_up_date=fu, sla_hours=hours, source="system",
                                 category=ot.get("category") or "", metadata={"auto": "entry_task"})
    except Exception:
        return None


def rearm_on_status_change(store, pid, proj_before, new_status):
    """An explicit status pick restores the invariant: a project sitting on a
    rung has THAT rung's task pending and no other rung's task pending.
    Off the end of the ladder (closed / in_progress roles) the stale rung tasks
    are completed, not deleted: the completed row is the audit trail.
    server.py:1338-1387."""
    doc = store.wf_doc()
    by_status = ladder_by_status(store, proj_before, doc)
    if new_status in by_status:
        want = by_status[new_status][1].strip().lower()
        for s in store.get_subtasks(pid, status="pending"):
            txt = (s.get("text") or "").strip()
            if rung_for_text(store, txt, proj_before, doc) and txt.lower() != want:
                store.delete_subtask(pid, s["id"])
        ensure_ladder_task(store, pid, new_status)
        return
    role = wfm.role_of(doc, new_status, (proj_before or {}).get("workflow_id"))
    if role in ("closed", "in_progress", "ignored"):
        for s in store.get_subtasks(pid, status="pending"):
            if rung_for_text(store, s.get("text"), proj_before, doc):
                store.complete_subtask(pid, s["id"], completed_by="manual")
    qstage, _ = wfm.quote_task_for(doc, wf_id_for(store, proj_before, doc))
    if qstage and new_status == qstage and (proj_before or {}).get("status") != qstage:
        ensure_quote_task(store, pid, reset_clock=True)


def spawn_response_subtask(store, email_card, pid, from_is_user):
    """Inbound from someone else: 'Respond to X' with the response SLA.
    The user's own outbound: 'Follow up with X' two business days out, no SLA.
    Transactional robot mail (no-reply + shipping subject) spawns nothing.
    server.py:3011-3081."""
    if not pid or pid in ("", "__new__", "undefined", "null", "None"):
        return None
    from_email = (email_card.get("from_email") or "").lower()
    from_name = email_card.get("from_name") or email_card.get("from_email") or "contact"
    subject = email_card.get("subject") or "(no subject)"
    subj_short = subject[:60] + ("..." if len(subject) > 60 else "")
    if not from_is_user:
        local = from_email.split("@")[0]
        if _ROBOT_LOCAL_RE.match(local) and _TXN_SUBJECT_RE.search(subject):
            return None
    if from_is_user:
        to_field = email_card.get("to") or ""
        if isinstance(to_field, list):
            recip = to_field[0] if to_field else "recipient"
        else:
            recip = str(to_field).split(";")[0].split(",")[0].strip() or "recipient"
        text = f"Follow up with {recip}: {subj_short}"
    else:
        text = f"Respond to {from_name}: {subj_short}"
    from .business_hours import parse_dt
    dt = parse_dt(email_card.get("received_at") or email_card.get("sent_at"))
    local_dt = store.hours.to_local_naive(dt) if dt else store.hours.now_local_naive()
    email_iso = local_dt.isoformat(timespec="seconds")
    email_day = local_dt.strftime("%Y-%m-%d")
    hours = sla_hours(store)
    return store.add_subtask(
        pid, text,
        date_type="follow_up" if from_is_user else "sla",
        follow_up_date=store.hours.add_biz_days(store.hours.today(), 2) if from_is_user else None,
        sla_hours=None if from_is_user else hours, source="email_response", created=email_day,
        sla_type=None if from_is_user else "response", sla_created=None if from_is_user else email_iso,
        allow_if_completed=True)
