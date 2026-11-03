"""The triage cycle: dedup -> normalize -> match -> auto-commit or stage.

Ports Phase 0.5 / 1 / 2 of engine/decision_tree.md and the staging logic of
v2/server.py ``_run_triage_cycle`` (lines 10585-11334: thread match,
allowlisted alternate thread key, unique exact-reference auto-commit, the
dismissed-card path for notifications and internal admin mail, the pending
card, per-email error containment, mark-processed only after durable
handling) plus the approve-time bookkeeping of ``_approve_email``
(4000-4486: register the conversation, outbound clears "Respond to",
inbound clears "Follow up with" and arms the SLA task) and
``_spawn_response_subtask`` (3011-3081, including the no-SLA rule for robot
mailboxes with transactional subjects).

Everything runs against the ``Store`` interface of DESIGN.md section 9.
Status never changes here; the store's own cascades are the only writers.

Card dict produced (``add_triage_card``)::

    card_type, status, direction, from_email, from_name, to, cc, subject,
    body_raw, body_preview, received_at, conversation_id, message_id,
    internet_message_id, attachments[], attachment_data[], has_attachments,
    project_matches[{project_id, confidence, match_type}], matched_project,
    confidence, match_reason, classification, needs_reply, auto_action,
    auto_follow_up, auto_date_type, dollar_amount, summary,
    signals{completion[], approval, phrases[], refs[], thread_key_alt},
    suggestion{client, client_source, topic, name, confidence, ids, refs,
    is_new_client}, doc_links[], is_self_note, auto_reason, source_file,
    source_folder
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from ..mail.message import parse_iso
from .attachments import attachment_files, extract_attachment_data, labeled_total
from .classify import classify, is_robot_sender
from .match import MatchContext, compile_id_patterns, is_internal, match_email_to_projects, unique_exact_match
from .normalize import (ScanWarnings, move_to_processed, purge_processed, scan_email_files,
                        write_capture_status)
from .signals import enrich_signals
from .suggest import suggest_project

_TXN_SUBJ_RE = re.compile(
    r"\b(order|shipment|shipped|delivered|delivery|delayed|backorder|back\s*order|tracking|"
    r"invoice|receipt|confirmation|packing\s*slip|ready\s*for\s*pickup|statement)\b", re.IGNORECASE)


# ── small helpers ───────────────────────────────────────────────────────────

def mail_cache_dir(cfg: dict) -> Path:
    paths = (cfg or {}).get("paths") or {}
    if paths.get("mail_cache"):
        return Path(paths["mail_cache"])
    root = paths.get("data_root") or str(Path.home() / "EmailBoss")
    return Path(root) / "mail_cache"


def business_days(profession: dict | None) -> set[int]:
    days = ((profession or {}).get("business_hours") or {}).get("days")
    try:
        return {int(d) for d in days} if days else {0, 1, 2, 3, 4}
    except (TypeError, ValueError):
        return {0, 1, 2, 3, 4}


def add_biz_days(from_date: date, n: int, profession: dict | None = None) -> str:
    """ISO date ``n`` business days after ``from_date`` (profession calendar)."""
    workdays = business_days(profession)
    d = from_date
    left = max(0, int(n))
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() in workdays:
            left -= 1
    return d.isoformat()


def sla_hours(profession: dict | None) -> int:
    try:
        return int((profession or {}).get("sla_hours") or 4)
    except (TypeError, ValueError):
        return 4


def user_identity(cfg: dict | None) -> tuple[str, set[str], list[str]]:
    user = (cfg or {}).get("user") or {}
    email = (user.get("email") or "").lower().strip()
    aliases = {str(a).lower().strip() for a in (user.get("aliases") or []) if a}
    if email:
        aliases.add(email)
    return email, aliases, list(user.get("internal_domains") or [])


def is_user(email_obj: dict, cfg: dict | None) -> bool:
    email, aliases, _ = user_identity(cfg)
    fe = (email_obj.get("from_email") or "").lower().strip()
    fn = (email_obj.get("from_name") or "").lower().strip()
    return bool(fe and fe in aliases) or bool(fn and fn in aliases)


def is_notification(email_obj: dict, profession: dict | None) -> bool:
    senders = ((profession or {}).get("triage") or {}).get("notification_senders") or []
    fe = (email_obj.get("from_email") or "").lower().strip()
    if not fe:
        return False
    for s in senders:
        s = str(s).lower().strip()
        if not s:
            continue
        if s.endswith("@") and fe.startswith(s):
            return True
        if s.startswith("@") and fe.endswith(s):
            return True
        if s == fe:
            return True
    return False


def is_internal_admin(email_obj: dict, profession: dict | None, cfg: dict | None) -> str | None:
    """Sender internal AND every recipient internal AND the subject matches a
    profession ``internal_admin_subjects`` phrase. Returns the phrase or None."""
    _e, _a, internal = user_identity(cfg)
    if not internal:
        return None
    fe = (email_obj.get("from_email") or "").lower().strip()
    if not is_internal(fe, internal):
        return None
    recips = [r.strip() for r in ((email_obj.get("to") or "") + ";" + (email_obj.get("cc") or "")).lower()
              .replace(",", ";").split(";") if r.strip()]
    if not recips or not all(is_internal(r, internal) for r in recips):
        return None
    subject = (email_obj.get("subject") or "").lower()
    for phrase in ((profession or {}).get("triage") or {}).get("internal_admin_subjects") or []:
        if str(phrase).lower() in subject:
            return str(phrase)
    return None


def message_key(email_obj: dict) -> str:
    mid = email_obj.get("message_id") or email_obj.get("internet_message_id")
    if mid:
        return str(mid)
    return "file:" + hashlib.sha1(str(email_obj.get("source_file") or "").encode()).hexdigest()


def _active_ids(projects: list[dict]) -> set[str]:
    return {p.get("id") for p in projects if p.get("id")}


def _subj_short(subject: str) -> str:
    subject = subject or "(no subject)"
    return subject[:60] + ("..." if len(subject) > 60 else "")


# ── subtask bookkeeping (shared by auto-commit and approve) ─────────────────

def apply_thread_effects(store, email_obj: dict, pid: str, cfg: dict | None, profession: dict | None,
                         actor: str = "triage") -> dict:
    """Outbound: complete "Respond to" tasks, arm a follow-up. Inbound:
    complete "Follow up with" tasks, arm the SLA "Respond to" task (unless the
    sender is a robot mailbox with a transactional subject)."""
    out = {"completed": 0, "subtask": None}
    direction = email_obj.get("direction", "inbound")
    from_user = is_user(email_obj, cfg)
    self_note = bool(email_obj.get("is_self_note"))
    if self_note:
        return out
    today = date.today()
    if direction == "outbound" or from_user:
        out["completed"] = store.complete_subtasks_matching(pid, "Respond to", completed_by="sent_reply") or 0
        to_field = email_obj.get("to") or ""
        recip = ""
        for r in email_obj.get("recipients") or []:
            if r.get("kind") == "to":
                recip = r.get("name") or r.get("email")
                break
        recip = recip or to_field.replace(",", ";").split(";")[0].strip() or "recipient"
        out["subtask"] = store.add_subtask(
            pid, f"Follow up with {recip}: {_subj_short(email_obj.get('subject'))}",
            date_type="follow_up", follow_up_date=add_biz_days(today, 2, profession),
            source="email_response")
        return out
    out["completed"] = store.complete_subtasks_matching(pid, "Follow up with", completed_by="reply_received") or 0
    fe = email_obj.get("from_email") or ""
    if is_robot_sender(fe) and _TXN_SUBJ_RE.search(email_obj.get("subject") or ""):
        return out
    name = email_obj.get("from_name") or fe or "sender"
    out["subtask"] = store.add_subtask(
        pid, f"Respond to {name}: {_subj_short(email_obj.get('subject'))}",
        date_type="sla", sla_hours=sla_hours(profession), source="email_response")
    return out


def register_conversation(store, email_or_card: dict, pid: str, source: str = "approve") -> None:
    conv = email_or_card.get("conversation_id")
    if conv:
        store.routing_set(conv, pid, source=source)
    alt = email_or_card.get("thread_key_alt") or ((email_or_card.get("signals") or {}).get("thread_key_alt"))
    if alt:
        store.routing_set("alt:" + alt, pid, source=source)


def after_approve(store, cfg: dict | None, profession: dict | None, card: dict, pid: str) -> dict:
    """Call after ``store.approve_triage`` resolved a card onto ``pid``:
    registers the thread for future auto-commit and applies the subtask
    effects. The store's approve_triage does the filing itself."""
    register_conversation(store, card, pid, source="approve")
    email_like = dict(card)
    email_like.setdefault("recipients", [])
    return apply_thread_effects(store, email_like, pid, cfg, profession, actor="approve")


# ── card builders ───────────────────────────────────────────────────────────

def _base_card(email_obj: dict, msg_key: str) -> dict:
    sig = email_obj.get("triage_signals") or {}
    return {
        "card_type": "email",
        "status": "pending",
        "direction": email_obj.get("direction", "inbound"),
        "from_email": email_obj.get("from_email", ""),
        "from_name": email_obj.get("from_name", ""),
        "to": email_obj.get("to", ""),
        "cc": email_obj.get("cc", ""),
        "subject": email_obj.get("subject", ""),
        "body_raw": email_obj.get("body_raw", ""),
        "body_preview": email_obj.get("body_preview", ""),
        "received_at": email_obj.get("received_at", ""),
        "conversation_id": email_obj.get("conversation_id", ""),
        "message_id": msg_key,
        "internet_message_id": email_obj.get("internet_message_id", ""),
        "attachments": email_obj.get("attachments", []),
        "attachment_data": email_obj.get("attachment_data", []),
        "has_attachments": 1 if email_obj.get("has_attachments") else 0,
        "project_matches": email_obj.get("project_matches", []),
        "matched_project": "",
        "confidence": "low",
        "match_reason": "",
        "classification": "",
        "needs_reply": False,
        "auto_action": "",
        "auto_follow_up": "",
        "auto_date_type": "follow_up",
        "dollar_amount": labeled_total(email_obj.get("attachment_data") or []),
        "summary": "",
        "signals": {
            "completion": email_obj.get("completion_signals", []),
            "approval": bool(sig.get("approval")),
            "phrases": sig.get("phrases", []),
            "refs": sig.get("refs", []),
            "thread_key_alt": email_obj.get("thread_key_alt"),
        },
        "suggestion": email_obj.get("suggestion") or {},
        "doc_links": email_obj.get("doc_links", []),
        "is_self_note": 1 if email_obj.get("is_self_note") else 0,
        "auto_reason": "",
        "source_file": email_obj.get("source_file", ""),
        "source_folder": email_obj.get("source_folder", ""),
    }


def _summary(email_obj: dict, cfg: dict | None, llm) -> str:
    features = ((cfg or {}).get("ai") or {}).get("features") or {}
    if llm is not None and features.get("summarize", True):
        try:
            res = llm.run("triage.summarize_bullets", {
                "subject": email_obj.get("subject", ""),
                "body": (email_obj.get("body") or email_obj.get("body_preview") or "")[:1500],
                "attachments": "; ".join((a.get("text_summary") or "")[:200]
                                         for a in email_obj.get("attachment_data") or []),
            })
            text = res.get("text") if isinstance(res, dict) else getattr(res, "text", None)
            if text and str(text).strip():
                return str(text).strip()[:600]
        except Exception:
            pass
    return (email_obj.get("body_preview") or email_obj.get("body") or "")[:250].strip()


# ── the cycle ───────────────────────────────────────────────────────────────

def run_cycle(store, cfg: dict, profession: dict, llm=None, mail_cache=None) -> dict:
    """One triage pass over ``<mail_cache>/inbox`` and ``/sent``. Never raises
    for a single email; returns a summary dict."""
    cfg = cfg or {}
    profession = profession or {}
    mail_cache = Path(mail_cache or mail_cache_dir(cfg))
    started = datetime.now()
    warnings = ScanWarnings()
    summary = {"scanned": 0, "dedup_skipped": 0, "auto_committed": 0, "staged": 0,
               "dismissed": 0, "errors": 0, "error_detail": [], "cards": [],
               "at": started.isoformat(timespec="seconds")}

    emails = (scan_email_files(mail_cache / "inbox", None, cfg, warnings)
              + scan_email_files(mail_cache / "sent", None, cfg, warnings))
    emails.sort(key=lambda e: (parse_iso(e.get("received_at")) or started.astimezone()).isoformat())
    summary["scanned"] = len(emails)
    if not emails:
        write_capture_status(mail_cache, summary, warnings)
        _safe(store.meta_set, "last_triage", started.isoformat(timespec="seconds"))
        return summary

    projects = _safe(store.active_projects) or []
    active = _active_ids(projects)
    ctx = MatchContext(projects, profession, cfg,
                       contact_index=_safe(store.contact_index) or None,
                       id_index=_safe(store.id_index) or {},
                       keyword_index=_safe(store.keyword_index) or None)
    id_patterns = compile_id_patterns(profession)
    known_clients = [p.get("client") or (p.get("key_details") or {}).get("client") for p in projects]
    known_clients = [k for k in known_clients if k]

    for email in emails:
        msg_key = message_key(email)
        try:
            if store.message_seen(msg_key):
                summary["dedup_skipped"] += 1
                move_to_processed(mail_cache, email, status="duplicate")
                continue

            extract_attachment_data(mail_cache, email, id_patterns=id_patterns)
            enrich_signals(email, cfg, profession, id_patterns)
            if is_user(email, cfg) and email.get("direction") == "inbound":
                # Our own message captured through the inbox (a bcc to self, a
                # shared mailbox): treat as outbound for the thread effects.
                email["from_user_inbox"] = True

            # ── dismissed: platform notifications, internal admin ──
            admin_phrase = is_internal_admin(email, profession, cfg)
            if is_notification(email, profession) or admin_phrase:
                label = "internal_admin" if admin_phrase else "notification"
                why = f"{label}: {admin_phrase or email.get('from_email', '')}"
                card = _base_card(email, msg_key)
                card.update({"status": "dismissed", "classification": label, "auto_reason": why,
                             "auto_action": "Filed automatically; revive if this was real work."})
                tid = store.add_triage_card(card)
                store.dismiss_triage(tid, reason=why, actor="triage")
                store.message_mark(msg_key, "dismissed", triage_id=tid)
                move_to_processed(mail_cache, email, status="dismissed")
                summary["dismissed"] += 1
                summary["cards"].append({"triage_id": tid, "status": "dismissed", "reason": why,
                                         "subject": email.get("subject", "")[:80]})
                continue

            # ── Phase 0.5a: thread routing ──
            pid, reason = None, ""
            conv = email.get("conversation_id") or ""
            if conv:
                pid = store.routing_lookup(conv)
                reason = "thread_match"
            if not pid and email.get("thread_key_alt"):
                pid = store.routing_lookup("alt:" + email["thread_key_alt"])
                reason = "alt_thread_match"
            if pid and pid not in active:
                pid, reason = None, ""     # closed/archived project: stage instead

            # ── Phase 1 step 1: matching ──
            matches = match_email_to_projects(email, ctx)
            email["project_matches"] = matches

            # ── Phase 0.5b: unique exact reference ──
            if not pid:
                uid = unique_exact_match(matches, ctx)
                if uid and uid in active:
                    pid, reason = uid, matches[0]["match_type"]

            if pid:
                tid = _auto_commit(store, cfg, profession, mail_cache, email, msg_key, pid, reason)
                summary["auto_committed"] += 1
                summary["cards"].append({"triage_id": tid, "status": "auto_committed", "project_id": pid,
                                         "reason": reason, "subject": email.get("subject", "")[:80]})
                continue

            # ── Phase 1 step 2-4: stage a pending card ──
            tid = _stage_pending(store, cfg, profession, llm, mail_cache, email, msg_key, matches, known_clients)
            summary["staged"] += 1
            summary["cards"].append({"triage_id": tid, "status": "pending",
                                     "matched_project": email.get("_prefill") or "",
                                     "subject": email.get("subject", "")[:80]})
        except Exception as e:
            # Per-email containment: the file stays in place and is retried
            # next cycle; nothing is marked processed.
            summary["errors"] += 1
            summary["error_detail"].append(
                f"{email.get('source_file', '')}: {type(e).__name__}: {str(e)[:160]}")

    try:
        purge_processed(mail_cache)
    except Exception:
        pass
    summary["seconds"] = round((datetime.now() - started).total_seconds(), 2)
    write_capture_status(mail_cache, {k: v for k, v in summary.items() if k != "cards"}, warnings)
    _safe(store.meta_set, "last_triage", started.isoformat(timespec="seconds"))
    if warnings.unreadable:
        _safe(store.add_alert, "mail_unreadable",
              f"{len(warnings.unreadable)} unreadable mail record(s) left on disk for retry", "warn")
    return summary


def _auto_commit(store, cfg, profession, mail_cache, email, msg_key, pid, reason) -> str:
    att_paths = [str(p) for p in attachment_files(mail_cache, email)]
    store.file_correspondence(pid, email, att_paths)
    register_conversation(store, email, pid, source=reason)
    direction = email.get("direction", "inbound")
    store.add_note(pid, f"Auto-filed ({reason}): {direction} from "
                        f"{email.get('from_name') or email.get('from_email', '')}: "
                        f"{(email.get('subject') or '')[:100]}", source="triage")
    effects = apply_thread_effects(store, email, pid, cfg, profession)
    card = _base_card(email, msg_key)
    card.update({
        "status": "auto_committed", "matched_project": pid, "confidence": "high",
        "match_reason": reason, "auto_reason": reason,
        "classification": classify(email, profession, cfg, None).get("classification", ""),
        "auto_action": (f"Auto-filed to project ({reason})."
                        + (" Respond-to task armed." if effects.get("subtask") and direction == "inbound" else "")),
    })
    tid = store.add_triage_card(card)
    store.message_mark(msg_key, "auto_committed", pid=pid, triage_id=tid)
    move_to_processed(mail_cache, email, status="auto_committed")
    return tid


def _stage_pending(store, cfg, profession, llm, mail_cache, email, msg_key, matches, known_clients) -> str:
    prefill, prefill_conf, prefill_reason = "", "low", ""
    for m in matches:
        if m.get("confidence") == "high" and m.get("project_id"):
            prefill, prefill_conf, prefill_reason = m["project_id"], "high", m.get("match_type", "")
            break
    project = _safe(store.get_project, prefill) if prefill else None
    cls = classify(email, profession, cfg, llm=llm, project=project)
    email["suggestion"] = suggest_project(email, profession, cfg, known_clients=known_clients)
    card = _base_card(email, msg_key)
    fu_days = int(cls.get("follow_up_days") or 0)
    card.update({
        "matched_project": prefill,
        "confidence": prefill_conf,
        "match_reason": prefill_reason or (f"{len(matches)} candidate(s)" if matches else "no match"),
        "classification": cls.get("classification", ""),
        "needs_reply": bool(cls.get("needs_reply")),
        "auto_action": cls.get("auto_action", ""),
        "auto_follow_up": add_biz_days(date.today(), fu_days, profession) if fu_days > 0 else "",
        "auto_date_type": "sla" if (cls.get("needs_reply") and email.get("direction") == "inbound") else "follow_up",
        "summary": _summary(email, cfg, llm),
        "suggestion": email["suggestion"],
    })
    email["_prefill"] = prefill
    tid = store.add_triage_card(card)
    store.message_mark(msg_key, "pending", pid=prefill or None, triage_id=tid)
    move_to_processed(mail_cache, email, status="pending")
    return tid


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:
        return None
