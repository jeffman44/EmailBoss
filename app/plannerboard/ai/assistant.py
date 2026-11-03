"""The in-app assistant pass (DESIGN.md section 6, "Assistant pass").

A deterministic loop; the model is consulted for judgment only. Behaviour is
a generic port of the reference install's assistant-mode skill rules:

  * Drafts only. Nothing here can send mail. A draft goes to the mail
    provider's Drafts folder when it supports that, else to the board's own
    drafts table. The originating task stays OPEN until the user sends.
  * Never changes a project's status, money fields or user-owned dates. The
    only writes are: triage approve/dismiss, complete a task that is
    demonstrably done, add a note, add a draft, add a question, add a task.
  * Every item touched ends in exactly one logged outcome:
    acted | drafted | question_posted | subtask_added | deferred.
    A pending triage card that cannot be resolved is `deferred` with a
    reason code (multi_project | no_project | needs_human), per card.
  * Several questions may be open on a project; an answered question is read
    on the next pass for that project and becomes the `direction_note` of a draft.
  * Customer-facing text may only carry fields in the profession's
    `customer_visible_fields` list; every other project value is scrubbed.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..triage.license_gate import is_read_only
from .guards import (clean_body, ground_text, residual_content,
                     strip_noise_lines, strip_signature_from_draft)

OUTCOME_ACTED = "acted"
OUTCOME_DRAFTED = "drafted"
OUTCOME_QUESTION = "question_posted"
OUTCOME_SUBTASK = "subtask_added"
OUTCOME_DEFERRED = "deferred"
OUTCOMES = (OUTCOME_ACTED, OUTCOME_DRAFTED, OUTCOME_QUESTION, OUTCOME_SUBTASK, OUTCOME_DEFERRED)

# Triage reason codes (left pending)
REASON_MULTI = "multi_project"
REASON_NONE = "no_project"
REASON_HUMAN = "needs_human"

RERANK_MIN_CONFIDENCE = 0.85
MAX_OPEN_QUESTIONS = 5        # per project; one per task, never a wall of them
DEFAULT_VISIBLE_FIELDS = ["name", "scope", "location", "site", "status_label"]
MONEY_FIELDS = ("quote_amount", "approved_amount", "amount", "cost", "margin", "price")

_RESPOND_RE = re.compile(r'^\s*(?:respond|reply)\b', re.I)
_FOLLOWUP_RE = re.compile(r'^\s*follow[\s-]*up\b', re.I)

# Rules half of the no-reply classifier (skill: "NO-REPLY, clear it").
_NO_REPLY_SUBJECT_RE = re.compile(
    r'(?i)\b(automatic reply|auto-?reply|out of (?:the )?office|read receipt|read:|accepted:|'
    r'declined:|tentative:|delivery (?:confirmation|notification)|your order|order confirmation|'
    r'shipping confirmation|has shipped|tracking (?:number|info)|shipment)\b')
_NO_REPLY_BODY_RE = re.compile(
    r'(?i)^\s*(?:thanks?(?: you)?[!.,]*|got it[!.,]*|received[!.,]*|sounds good[!.,]*|perfect[!.,]*|'
    r'great[!.,]*|ok(?:ay)?[!.,]*|will do[!.,]*|noted[!.,]*)\s*$')
_QUESTION_RE = re.compile(r'\?')
_JUDGMENT_RE = re.compile(
    r'(?i)\b(price|pricing|cost|quote|credit|refund|discount|warranty|dispute|scope change|'
    r'change order|invoice|deadline|by (?:monday|tuesday|wednesday|thursday|friday)|'
    r'fill (?:out|in)|complete and return|sign and return|w-?9|form attached)\b')
_OOO_RE = re.compile(r'(?i)\b(automatic reply|auto-?reply|out of (?:the )?office)\b')
_TEST_RE = re.compile(r'(?i)^\s*(?:test|testing)\b')


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_dt(val: Any) -> datetime | None:
    if not val:
        return None
    s = str(val).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19] if "T" in s or " " in s else s[:10], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _renewal_date(kd: dict, today: date) -> date | None:
    """key_details.renewal_date, else the next yearly anniversary of
    key_details.effective_date. None when neither is a usable date."""
    d = _parse_dt(kd.get("renewal_date"))
    if d:
        return d.date()
    eff = _parse_dt(kd.get("effective_date"))
    if not eff:
        return None
    eff = eff.date()
    if eff > today:
        try:
            return eff.replace(year=eff.year + 1)
        except ValueError:
            return eff + timedelta(days=365)
    for yr in (today.year, today.year + 1):
        try:
            cand = eff.replace(year=yr)
        except ValueError:
            cand = date(yr, 3, 1)
        if cand >= today - timedelta(days=14):
            return cand
    return None


def _age_text(ts: Any) -> str:
    dt = _parse_dt(ts)
    if not dt:
        return "unknown age"
    delta = datetime.now() - dt
    if delta.days >= 1:
        return f"{delta.days}d"
    return f"{max(1, delta.seconds // 3600)}h"


def _sort_priority(profession: dict) -> list[str]:
    wf = (profession or {}).get("workflows") or {}
    raw = ((wf.get("sort") or {}).get("priority")) or ["deadline", "sla", "follow_up", "aging"]
    # DESIGN section 3 writes the list as ids; the shipped templates and the
    # normalized workflows.json carry {id, label, match} rows. Accept both.
    out = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("id") or (item.get("match") or {}).get("date_type") or ""
        if item:
            out.append(str(item))
    return out or ["deadline", "sla", "follow_up", "aging"]


def _task_key(t: dict, priority: list[str]):
    rank_of = {name: i for i, name in enumerate(priority)}
    dt = t.get("date_type") or "follow_up"
    when = t.get("follow_up_date") or t.get("sla_created") or t.get("created") or "9999"
    return (rank_of.get(dt, len(priority) - 1), str(when))


def hottest_task(p: dict, profession: dict) -> dict | None:
    """The pending task that ranks this project: earliest in `sort.priority`
    order, then the oldest date."""
    pending = [t for t in (p.get("subtasks") or []) if (t.get("status") or "pending") == "pending"]
    if not pending:
        return None
    priority = _sort_priority(profession)
    return min(pending, key=lambda t: _task_key(t, priority))


def board_order(projects: list[dict], profession: dict) -> list[dict]:
    """Rank projects by their hottest pending task using the profession's
    `sort.priority` list (e.g. deadline, sla, follow_up, aging). Projects with
    no pending task sink below everything that has one."""
    priority = _sort_priority(profession)
    floor = len(priority)

    def proj_key(p: dict):
        t = hottest_task(p, profession)
        if t is not None:
            k = _task_key(t, priority)
            return (0, k[0], k[1])
        return (1, floor, str(p.get("last_activity") or p.get("last_communication") or p.get("created") or ""))

    return sorted(projects, key=proj_key)


class AssistantPass:
    def __init__(self, store: Any, cfg: dict, profession: dict, llm: Any = None,
                 mail_provider: Any = None, data_root: str | None = None):
        self.store = store
        self.cfg = cfg or {}
        self.profession = profession or {}
        self.llm = llm
        self.mail = mail_provider
        self.run_id = "run_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        acfg = self.cfg.get("assistant") or {}
        self.auto_create = bool(acfg.get("auto_create", False))
        self.drafts_to = acfg.get("drafts_to") or "provider"
        self.default_max_items = int(acfg.get("max_items") or 10)
        self.data_root = data_root or (self.cfg.get("paths") or {}).get("data_root") \
            or os.environ.get("PB_DATA_ROOT") or str(Path.home() / "EmailBoss")
        self.visible_fields = list(self.profession.get("customer_visible_fields") or DEFAULT_VISIBLE_FIELDS)
        self.profile = {
            "user_name": (self.cfg.get("user") or {}).get("name") or "",
            "company": (self.cfg.get("user") or {}).get("company") or "",
            "role": (self.cfg.get("user") or {}).get("role") or "",
        }
        self.internal_domains = [d.lower().lstrip("@") for d in ((self.cfg.get("user") or {}).get("internal_domains") or [])]
        tri = self.profession.get("triage") or {}
        self.notification_senders = [s.lower() for s in (tri.get("notification_senders") or [])]
        self.admin_subjects = [s.lower() for s in (tri.get("internal_admin_subjects") or [])]
        self.id_patterns = []
        for pat in self.profession.get("id_patterns") or []:
            try:
                self.id_patterns.append((pat.get("key", ""), re.compile(pat["regex"]), bool(pat.get("unique_match"))))
            except (KeyError, re.error):
                continue
        self.items: list[dict] = []
        self.counts = {o: 0 for o in OUTCOMES}
        self.triage_counts = {"approved": 0, "dismissed": 0, "created": 0, "left": {}}
        self.escalations: list[str] = []
        self.last_draft_ref = ""

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------
    def run(self, max_items: int | None = None) -> dict:
        max_items = int(max_items or self.default_max_items)
        started = _now_iso()
        t0 = time.monotonic()
        try:
            self._drain_triage(max_items)
        except Exception as e:  # never let one half kill the other
            self.escalations.append(f"triage drain failed: {e}")
        try:
            self._walk_board(max_items)
        except Exception as e:
            self.escalations.append(f"board walk failed: {e}")
        try:
            self._renewal_sweep()
        except Exception as e:
            self.escalations.append(f"renewal sweep failed: {e}")
        summary = {
            "run_id": self.run_id,
            "started": started,
            "finished": _now_iso(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "max_items": max_items,
            "llm_used": bool(self.llm is not None and getattr(self.llm, "available", False)),
            "counts": dict(self.counts),
            "triage": {"approved": self.triage_counts["approved"],
                       "dismissed": self.triage_counts["dismissed"],
                       "created": self.triage_counts["created"],
                       "left": dict(self.triage_counts["left"])},
            "escalations": list(self.escalations),
            "items": list(self.items),
            "nothing_sent": True,
        }
        self._log("", "run", "summary", {"counts": summary["counts"], "triage": summary["triage"],
                                         "escalations": summary["escalations"]})
        return summary

    # ------------------------------------------------------------------
    # step 1: triage
    # ------------------------------------------------------------------
    def _drain_triage(self, cap: int) -> None:
        cards = list(self.store.list_triage("pending") or [])
        cards.sort(key=lambda c: str(c.get("received_at") or ""))
        for card in cards[:max(cap, 1)]:
            try:
                self._triage_card(card)
            except Exception as e:
                self._record(card.get("matched_project") or "", f"triage {card.get('triage_id') or card.get('id')}",
                             "triage", OUTCOME_DEFERRED, result=f"left: error {e}", reason="error")

    def _triage_card(self, card: dict) -> None:
        tid = card.get("triage_id") or card.get("id")
        item = f"triage {tid}"
        ctype = (card.get("card_type") or "email").lower()
        subject = card.get("subject") or ""
        body = card.get("body_raw") or card.get("body_preview") or ""
        from_email = (card.get("from_email") or "").lower()
        conv = card.get("conversation_id") or ""

        if ctype not in ("email", "message", ""):
            self._leave(tid, item, REASON_HUMAN, f"card_type {ctype} needs a person")
            return

        # Junk: dismiss. Only the narrow classes, each logged with its class.
        junk_class = self._junk_class(card)
        if junk_class is None and self.llm is not None and not card.get("classification"):
            cls = self._llm_classify(card)
            if cls and cls.get("classification") == "junk":
                junk_class = "junk (model)"
        if junk_class:
            self.store.dismiss_triage(tid, reason=junk_class, actor="assistant")
            self.triage_counts["dismissed"] += 1
            self._record("", item, "triage", OUTCOME_ACTED, result=f"dismissed ({junk_class})",
                         evidence=subject[:120])
            return

        # Money / scope / dispute signals stay with a person.
        sig = card.get("signals") or {}
        if any(sig.get(k) for k in ("money", "dispute", "scope_change", "price_challenge")):
            self._leave(tid, item, REASON_HUMAN, "money/scope/dispute signal")
            return

        # Resolve the project.
        active_ids = {p.get("id") for p in (self.store.active_projects() or [])}
        routed = self.store.routing_lookup(conv) if conv else None
        if routed and routed in active_ids:
            self._approve(card, routed, f"thread already routed ({conv})")
            return

        id_hits, id_values = self._id_candidates(subject + "\n" + body, active_ids)
        if len(id_hits) == 1:
            pid = next(iter(id_hits))
            self._approve(card, pid, f"reference {id_values.get(pid, '?')} resolves to one project")
            return

        match_hits = set()
        for m in card.get("project_matches") or []:
            mid = m.get("id") if isinstance(m, dict) else m
            if mid in active_ids:
                match_hits.add(mid)
        if card.get("matched_project") in active_ids:
            match_hits.add(card["matched_project"])

        contact_hits = set()
        try:
            cidx = self.store.contact_index() or {}
        except Exception:
            cidx = {}
        # The SENDER only: To/Cc addresses are attacker-controlled (anyone can
        # copy a known contact onto a message), so they never route a card.
        for pid in cidx.get(from_email, []):
            if pid in active_ids:
                contact_hits.add(pid)

        strong = id_hits | match_hits
        candidates = strong | contact_hits
        if len(strong) == 1 and len(candidates) == 1:
            pid = next(iter(strong))
            self._approve(card, pid, "card's own match confirmed, no competing project")
            return
        if candidates:
            # A contact-only match is never approved, by rule or by the model:
            # a thread or id/match hit is required.
            if strong:
                # Ambiguous: the model may tie-break, with a high bar, but only
                # onto a project that has a thread/id/match hit.
                choice = self._rerank(card, sorted(candidates), allowed=strong)
                if choice:
                    self._approve(card, choice["project_id"], f"rerank {choice['confidence']:.2f}: {choice.get('evidence', '')}"[:200])
                    return
                self._leave(tid, item, REASON_MULTI if len(candidates) > 1 else REASON_HUMAN,
                            "candidates: " + ", ".join(sorted(candidates)))
                return
            self._leave(tid, item, REASON_HUMAN,
                        "sender is a known contact only; needs a thread or reference match: " + ", ".join(sorted(contact_hits)))
            return

        # No candidate: mint only when allowed and the email carries enough.
        if self.auto_create:
            sugg = self._suggestion(card)
            if sugg:
                wf = self._entry_workflow()
                res = self.store.approve_triage(tid, "__new__", workflow_id=wf["id"], status=wf["entry"],
                                                work_type=sugg.get("work_type") or "", new_name=sugg["name"],
                                                actor="assistant")
                pid = (res or {}).get("project_id") or (res or {}).get("id") or ""
                self.triage_counts["created"] += 1
                self.triage_counts["approved"] += 1
                self._record(pid, item, "triage", OUTCOME_ACTED, result=f"approved -> new {pid} '{sugg['name']}'",
                             evidence=sugg.get("scope", "")[:160])
                return
        self._leave(tid, item, REASON_NONE, "no project resolves; would mint on thin info")

    def _approve(self, card: dict, pid: str, evidence: str) -> None:
        tid = card.get("triage_id") or card.get("id")
        res = self.store.approve_triage(tid, pid, actor="assistant")
        self.triage_counts["approved"] += 1
        note = "approved -> " + pid
        if isinstance(res, dict) and res.get("already_approved"):
            note += " (already_approved)"
        self._record(pid, f"triage {tid}", "triage", OUTCOME_ACTED, result=note, evidence=evidence)

    def _leave(self, tid: Any, item: str, reason: str, detail: str) -> None:
        self.triage_counts["left"][reason] = self.triage_counts["left"].get(reason, 0) + 1
        # Show the reason on the card (the board reads `leave_reason`).
        upd = getattr(self.store, "update_triage_card", None)
        if callable(upd) and tid:
            try:
                upd(tid, leave_reason=reason)
            except Exception:
                pass
        self._record("", item, "triage", OUTCOME_DEFERRED, result=f"left: {reason}", evidence=detail,
                     reason=reason)

    def _junk_class(self, card: dict) -> str | None:
        subject = card.get("subject") or ""
        from_email = (card.get("from_email") or "").lower()
        conv = card.get("conversation_id") or ""
        routed = bool(conv and self.store.routing_lookup(conv))
        if (card.get("classification") or "").lower() == "junk":
            return "junk"
        if _OOO_RE.search(subject) and not routed:
            return "auto-reply on unrouted thread"
        user_email = ((self.cfg.get("user") or {}).get("email") or "").lower()
        if user_email and from_email == user_email and _TEST_RE.search(subject) and len(residual_content(card.get("body_raw") or "")) < 40:
            return "self-sent test"
        subj_l = subject.lower()
        if any(s in subj_l for s in self.admin_subjects) and self._is_internal(from_email):
            return "internal admin notice"
        if any(from_email.startswith(n) for n in self.notification_senders) and not routed \
                and not self._id_values(subject + "\n" + (card.get("body_raw") or "")):
            return "notification sender, unrouted"
        return None

    def _is_internal(self, email: str) -> bool:
        dom = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        return bool(dom) and dom in self.internal_domains

    def _id_values(self, text: str) -> list[str]:
        """Every profession id_pattern value present in the text."""
        vals: list[str] = []
        for _key, rx, _unique in self.id_patterns:
            for val in rx.findall(text or ""):
                v = val if isinstance(val, str) else val[0]
                if v and v not in vals:
                    vals.append(v)
        return vals

    def _id_candidates(self, text: str, active_ids: set) -> tuple[set, dict]:
        """Profession id_patterns -> ({pid}, {pid: value}) via store.id_index()."""
        hits: set = set()
        values: dict = {}
        vals = self._id_values(text)
        if not vals:
            return hits, values
        try:
            idx = self.store.id_index() or {}
        except Exception:
            idx = {}
        for v in vals:
            for pid in idx.get(v, []) + idx.get(v.upper(), []) + idx.get(v.lower(), []):
                if not active_ids or pid in active_ids:
                    hits.add(pid)
                    values[pid] = v
        return hits, values

    def _llm_classify(self, card: dict) -> dict | None:
        if self.llm is None:
            return None
        body = strip_noise_lines(clean_body(card.get("body_raw") or card.get("body_preview") or "", 600, self.profile), self.profile)
        out = self.llm.run("triage.classify", {
            "direction": card.get("direction") or "inbound",
            "from_name": card.get("from_name") or "", "from_email": card.get("from_email") or "",
            "to": ", ".join(card.get("to") or []) if isinstance(card.get("to"), list) else str(card.get("to") or ""),
            "subject": card.get("subject") or "", "body": body,
            "project_name": "", "project_status": "",
        })
        return out[0] if out else None

    def _rerank(self, card: dict, candidates: list[str], allowed: set | None = None) -> dict | None:
        if self.llm is None:
            return None
        rows = []
        for pid in candidates:
            p = self.store.get_project(pid) or {}
            kd = p.get("key_details") or {}
            refs = ", ".join(f"{k}={v}" for k, v in kd.items() if isinstance(v, (str, int)) and len(str(v)) < 40)[:200]
            contacts = ", ".join(c.get("email", "") for c in (p.get("contacts") or []) if isinstance(c, dict))[:160]
            rows.append(f"{pid} | {p.get('name', '')} | {p.get('status', '')} | {kd.get('location') or kd.get('site') or p.get('site') or ''} | {contacts} | {refs}")
        body = strip_noise_lines(clean_body(card.get("body_raw") or card.get("body_preview") or "", 900, self.profile), self.profile)
        out = self.llm.run("triage.match_rerank", {
            "from_name": card.get("from_name") or "", "from_email": card.get("from_email") or "",
            "subject": card.get("subject") or "", "body": body, "candidates": "\n".join(rows),
        })
        if not out:
            return None
        res = out[0] or {}
        pid = res.get("project_id")
        try:
            conf = float(res.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if pid in candidates and (allowed is None or pid in allowed) \
                and conf >= RERANK_MIN_CONFIDENCE and not res.get("ambiguous"):
            return {"project_id": pid, "confidence": conf, "evidence": res.get("evidence", "")}
        return None

    def _suggestion(self, card: dict) -> dict | None:
        sugg = card.get("suggestion") or {}
        if not sugg.get("name") and self.llm is not None:
            body = strip_noise_lines(clean_body(card.get("body_raw") or card.get("body_preview") or "", 900, self.profile), self.profile)
            out = self.llm.run("triage.suggest_project", {
                "from_name": card.get("from_name") or "", "from_email": card.get("from_email") or "",
                "subject": card.get("subject") or "", "body": body,
                "attachment_names": ", ".join(a.get("filename", "") if isinstance(a, dict) else str(a) for a in (card.get("attachments") or [])),
                "source_text": (card.get("subject") or "") + "\n" + body,
            })
            sugg = out[0] if out else {}
        if not sugg.get("name"):
            return None
        if sugg.get("is_request") is False:
            return None
        if not (sugg.get("client_name") or sugg.get("location") or card.get("from_email")):
            return None
        return sugg

    def _entry_workflow(self) -> dict:
        wfs = ((self.profession.get("workflows") or {}).get("workflows")) or []
        wf = wfs[0] if wfs else {"id": "general", "entry_stage": "action_required"}
        return {"id": wf.get("id", "general"), "entry": wf.get("entry_stage") or "action_required"}

    # ------------------------------------------------------------------
    # step 2: the board walk
    # ------------------------------------------------------------------
    def _walk_board(self, cap: int) -> None:
        projects = list(self.store.active_projects() or [])
        for p in board_order(projects, self.profession)[:max(cap, 0)]:
            try:
                self._work_project(p)
            except Exception as e:
                self._record(p.get("id", ""), p.get("name", ""), "project", OUTCOME_DEFERRED,
                             result=f"error: {e}", reason="error")

    def _work_project(self, p: dict) -> None:
        pid = p.get("id", "")
        pending = [t for t in (p.get("subtasks") or []) if (t.get("status") or "pending") == "pending"]
        health = self._health(p)
        for t in pending:
            if (t.get("date_type") == "deadline") and t.get("follow_up_date") \
                    and str(t["follow_up_date"])[:10] < date.today().isoformat():
                self.escalations.append(f"{pid}: deadline passed on '{t.get('text', '')}' ({t['follow_up_date']})")
        answers = self._unread_answers(pid)
        if answers and pending:
            if self._work_answers(p, pending, answers, health):
                return
        if not pending:
            self._record(pid, p.get("name", ""), "project", OUTCOME_DEFERRED,
                         result="no pending task", reason="idle", health=health)
            return
        item = hottest_task(p, self.profession) or pending[0]
        text = (item.get("text") or "").strip()
        if _RESPOND_RE.search(text):
            self._work_respond(p, item, health)
        elif _FOLLOWUP_RE.search(text):
            self._work_followup(p, item, health)
        else:
            self._work_other(p, item, health)

    def _health(self, p: dict) -> dict | None:
        if self.llm is None:
            return None
        pending = [t for t in (p.get("subtasks") or []) if (t.get("status") or "pending") == "pending"]
        deadline = next((t.get("follow_up_date") for t in pending if t.get("date_type") == "deadline"), "")
        notes = p.get("notes") or []
        out = self.llm.run("project.health", {
            "project_name": p.get("name", ""), "project_status": p.get("status", ""),
            "created": p.get("created", ""), "last_communication": p.get("last_communication") or p.get("last_activity") or "",
            "follow_up_date": p.get("follow_up_date") or "", "deadline": deadline or "",
            "emails_sent": p.get("emails_sent", 0), "pending_count": len(pending),
            "pending_tasks": "\n".join(f"- {t.get('text', '')} ({t.get('date_type') or 'follow_up'} {t.get('follow_up_date') or ''})" for t in pending[:8]),
            "recent_notes": "\n".join(f"- {(n.get('text') if isinstance(n, dict) else str(n))[:120]}" for n in notes[-3:]),
            "today": date.today().isoformat(),
        })
        return out[0] if out else None

    # -- answered questions ---------------------------------------------------
    def _unread_answers(self, pid: str) -> list[dict]:
        fn = getattr(self.store, "unconsumed_answers", None)
        if not callable(fn):
            return []
        try:
            return list(fn(pid) or [])
        except Exception:
            return []

    def _sender_known(self, p: dict, inbound: dict) -> bool:
        addr = (inbound.get("from_email") or "").strip().lower()
        if not addr:
            return False
        for c in p.get("contacts") or []:
            e = c.get("email") if isinstance(c, dict) else c
            if e and str(e).strip().lower() == addr:
                return True
        try:
            return p.get("id") in (self.store.contact_index() or {}).get(addr, [])
        except Exception:
            return False

    def _work_answers(self, p: dict, pending: list[dict], answers: list[dict], health: dict | None) -> bool:
        """Act on answers the user gave to this project's questions: the answer
        is the `direction_note` of a draft written now. Never changes status."""
        pid = p.get("id", "")
        note = "; ".join(f"{(a.get('question') or '')[:120]} -> {(a.get('answer') or '')[:300]}" for a in answers)
        first = answers[0]
        task = next((t for t in pending if (t.get("text") or "") == (first.get("unblocks") or "")), None)
        if task is None:
            task = next((t for t in pending if _RESPOND_RE.search(t.get("text") or "")), None) \
                or hottest_task(p, self.profession) or {"text": first.get("unblocks") or "Respond"}
        text = (task.get("text") or "").strip()
        ref = ""
        if _FOLLOWUP_RE.search(text):
            ref = self._draft_followup(p, task, health)
        elif _RESPOND_RE.search(text) or not pending:
            ref = self._work_respond(p, task, health, direction_note=note)
        else:
            self.store.add_note(pid, f"Direction from you on '{text[:50]}': {note[:300]}", source="assistant")
            self._record(pid, text, "task", OUTCOME_ACTED, result="answer noted on the project",
                         evidence=note[:160], subtask_id=task.get("id"), health=health)
            ref = "noted"
        self.last_draft_ref = ref if ref and ref not in ("noted", "exists") else self.last_draft_ref
        if ref or self.llm is not None:
            # Consumed once acted on. With no model the answer waits for one.
            for a in answers:
                try:
                    self.store.mark_question_consumed(a["id"])
                except Exception:
                    pass
        return True

    def respond_for_project(self, pid: str) -> dict:
        """Synchronous, one project: act on its unread answers now. Used by
        POST /api/questions/<id>/answer. Bounded: one project, one draft."""
        self.last_draft_ref = ""
        p = self.store.get_project(pid)
        if not p:
            return {"drafted": False, "draft_id": None, "error": "project not found"}
        answers = self._unread_answers(pid)
        if not answers:
            return {"drafted": False, "draft_id": None}
        pending = [t for t in (p.get("subtasks") or self.store.open_subtasks(pid) or [])
                   if (t.get("status") or "pending") == "pending"]
        try:
            self._work_answers(p, pending, answers, None)
        except Exception as e:
            return {"drafted": False, "draft_id": None, "error": f"{type(e).__name__}: {e}"[:160]}
        ref = self.last_draft_ref or ""
        return {"drafted": bool(ref), "draft_id": ref or None}

    # -- renewals ------------------------------------------------------------
    def _renewal_sweep(self) -> None:
        """A project with a renewal date (or an effective date that recurs
        yearly) gets its Renewal task armed 30 days before. Arms the task only;
        the project's status is never moved."""
        wfs = {}
        for wf in ((self.profession.get("workflows") or {}).get("workflows")) or []:
            for st in wf.get("stages") or []:
                if st.get("id") == "renewal":
                    wfs[wf.get("id")] = (st.get("owning_task") or {}).get("text") or "Prepare renewal"
        if not wfs:
            return
        today = date.today()
        for p in self.store.active_projects() or []:
            text = wfs.get(p.get("workflow_id"))
            if not text:
                continue
            kd = p.get("key_details") or {}
            due = _renewal_date(kd, today)
            if not due or not (-14 <= (due - today).days <= 30):
                continue
            pid = p.get("id", "")
            if any((t.get("text") or "") == text and (t.get("status") or "pending") == "pending"
                   for t in (p.get("subtasks") or [])):
                continue
            key = f"renewal_armed:{pid}:{due.isoformat()}"
            if self.store.meta_get(key):
                continue
            sid = self.store.add_subtask(pid, text, date_type="follow_up", follow_up_date=today.isoformat(),
                                         source="assistant", category="renewal")
            self.store.meta_set(key, today.isoformat())
            self._record(pid, text, "renewal", OUTCOME_SUBTASK,
                         result=f"armed renewal task {sid}; renews {due.isoformat()}",
                         evidence=f"{(due - today).days} days out")

    # -- respond-type tasks ------------------------------------------------
    def _work_respond(self, p: dict, task: dict, health: dict | None, direction_note: str = "") -> str:
        """Returns the draft ref when a draft was written, else ''."""
        pid = p.get("id", "")
        inbound = self._find_inbound(p, task)
        if not inbound:
            self._record(pid, task.get("text", ""), "respond", OUTCOME_DEFERRED,
                         result="no inbound text on the project to classify", reason="no_inbound_text",
                         subtask_id=task.get("id"), health=health)
            return ""
        if direction_note:
            # The user answered a question about this: draft now, no re-asking.
            decision = {"decision": "draft", "reason": "user direction", "direction_note": direction_note}
        else:
            decision = self._no_reply_decision(p, task, inbound)
        if decision["decision"] == "clear":
            n = self.store.complete_subtasks_matching(pid, task.get("text", "")[:40], completed_by="assistant")
            self.store.add_note(pid, f"Assistant: no reply needed on '{inbound.get('subject', '')[:60]}' ({decision['reason']})", source="assistant")
            self._maybe_keep_date(p, inbound, decision)
            self._record(pid, task.get("text", ""), "respond", OUTCOME_ACTED,
                         result=f"cleared {n} task(s): {decision['reason']}", evidence=inbound.get("subject", ""),
                         subtask_id=task.get("id"), health=health)
            return ""
        if decision["decision"] == "draft" and not direction_note and not self._sender_known(p, inbound):
            # Anyone can email a project's thread text; only a known contact's
            # message gets an automatic draft. Anything else is the user's call.
            decision = {"decision": "ask", "reason": "sender is not one of this project's contacts",
                        "question_for_user": f"{inbound.get('from_email') or 'An unknown sender'} is not a contact on this "
                                             f"project. How should '{(inbound.get('subject') or '')[:60]}' be handled?",
                        "directions": ["Reply to them", "Add them as a contact, then reply", "Ignore this message"]}
        if decision["decision"] == "draft":
            if not direction_note and self._draft_exists(pid, inbound):
                self._record(pid, task.get("text", ""), "respond", OUTCOME_DEFERRED,
                             result="unsent draft already exists for this thread", reason="draft_exists",
                             subtask_id=task.get("id"), health=health)
                return ""
            body = self._draft_reply(p, inbound, decision)
            if body:
                ref = self._create_draft(p, inbound, body)
                self.store.add_note(pid, f"DRAFT (unsent): RE: {inbound.get('subject', '')[:60]} to {inbound.get('from_email', '')}", source="assistant")
                self._record(pid, task.get("text", ""), "respond", OUTCOME_DRAFTED,
                             result=f"draft {ref} to {inbound.get('from_email', '')}; task stays open",
                             evidence=decision["reason"], draft_v0=body, subtask_id=task.get("id"), health=health,
                             recipient=inbound.get("from_email", ""), subject="RE: " + (inbound.get("subject") or ""))
                return ref
            if direction_note:
                self._record(pid, task.get("text", ""), "respond", OUTCOME_DEFERRED,
                             result="no draft written for the answer (no model or nothing shareable)",
                             reason="no_draft", subtask_id=task.get("id"), health=health)
                return ""
            decision = {"decision": "ask", "reason": "draft could not be written from shareable facts",
                        "question_for_user": decision.get("question_for_user") or "", "directions": decision.get("directions") or []}
        # ask
        self._post_question(p, task, inbound, decision, health)
        return ""

    def _no_reply_decision(self, p: dict, task: dict, inbound: dict) -> dict:
        subject = inbound.get("subject") or ""
        body = strip_noise_lines(clean_body(inbound.get("body") or "", 900, self.profile), self.profile)
        # Rules first: the clear-cut no-reply classes need no model.
        if _NO_REPLY_SUBJECT_RE.search(subject) or _NO_REPLY_BODY_RE.match(body or "") \
                or (len(residual_content(body)) < 25 and not _QUESTION_RE.search(body)):
            return {"decision": "clear", "reason": "rules: notification / acknowledgement / no ask"}
        if self.llm is not None:
            out = self.llm.run("reply.no_reply_classifier", {
                "from_name": inbound.get("from_name") or "", "from_email": inbound.get("from_email") or "",
                "received_at": inbound.get("received_at") or "", "subject": subject, "body": body,
                "project_name": p.get("name", ""), "project_status": p.get("status", ""),
                "visible_facts": self._visible_facts(p),
                "open_tasks": "\n".join(f"- {t.get('text', '')}" for t in (p.get("subtasks") or []) if (t.get("status") or "pending") == "pending"),
            })
            if out and isinstance(out[0], dict):
                d = out[0]
                d.setdefault("reason", "model")
                if d["decision"] == "draft" and _JUDGMENT_RE.search(subject + " " + body):
                    d["decision"] = "ask"
                    d["reason"] = "judgment keyword present; " + d["reason"]
                return d
        # No model: a question or judgment keyword means ask; otherwise the
        # safe default is also ask, because "draft" needs a model.
        return {"decision": "ask", "reason": "rules: needs a person (no model available)",
                "question_for_user": f"How should {self.profile['user_name'] or 'you'} respond to {inbound.get('from_name') or inbound.get('from_email', 'the sender')} on '{subject[:60]}'?",
                "directions": ["Acknowledge and say more will follow", "Answer from the project facts", "No reply needed"]}

    def _maybe_keep_date(self, p: dict, inbound: dict, decision: dict) -> None:
        """Clearing a no-reply never discards a stated hard date, but the date
        must literally appear in the inbound text (never inferred)."""
        d = (decision.get("date_mentioned") or "").strip()[:10]
        if not d:
            return
        try:
            when = date.fromisoformat(d)
        except ValueError:
            return
        body = inbound.get("body") or ""
        if when <= date.today() or (d not in body and when.strftime("%-m/%-d") not in body and when.strftime("%m/%d") not in body):
            return
        self.store.add_subtask(p.get("id", ""), f"Date noted in '{(inbound.get('subject') or '')[:40]}'",
                               date_type="deadline", follow_up_date=d, source="assistant")
        self.counts[OUTCOME_SUBTASK] += 1

    def _draft_reply(self, p: dict, inbound: dict, decision: dict) -> str:
        if self.llm is None:
            return ""
        body_in = strip_noise_lines(clean_body(inbound.get("body") or "", 900, self.profile), self.profile)
        facts = self._visible_facts(p)
        source = f"{inbound.get('subject', '')}\n{body_in}\n{facts}"
        out = self.llm.run("reply.draft", {
            "from_name": inbound.get("from_name") or "", "from_email": inbound.get("from_email") or "",
            "subject": inbound.get("subject") or "", "body": body_in, "project_name": p.get("name", ""),
            "visible_facts": facts, "direction_note": decision.get("direction_note") or "",
            "source_text": source,
        })
        if not out:
            return ""
        val = out[0]
        text = val.get("body", "") if isinstance(val, dict) else str(val)
        text = strip_signature_from_draft(text, self.profile)
        text = ground_text(text, source, (self.llm.profile_vars.get("_acronyms") if hasattr(self.llm, "profile_vars") else None))
        text, _stripped = self.enforce_visible_fields(text, p)
        return text.strip()

    def _create_draft(self, p: dict, inbound: dict, body: str, subject_override: str = "") -> str:
        to = [inbound.get("from_email")] if inbound.get("from_email") else []
        subject = subject_override or "RE: " + (inbound.get("subject") or "")
        if self.drafts_to == "provider" and self.mail is not None and hasattr(self.mail, "create_draft"):
            try:
                # Model output is untrusted text: escape before the HTML conversion.
                ref = self.mail.create_draft(inbound.get("message") or None, to, [], subject,
                                             _html.escape(body).replace("\n", "<br>"))
                if ref:
                    return f"provider:{ref}"
            except Exception:
                pass  # fall through to the board's drafts table
        return "local:" + str(self.store.add_draft(p.get("id", ""), to, [], subject, body,
                                                   reply_to_corr_id=inbound.get("corr_id"), created_by="assistant"))

    def _draft_exists(self, pid: str, inbound: dict) -> bool:
        subj = ("RE: " + (inbound.get("subject") or "")).lower()
        try:
            for row in self.store.assistant_log(limit=300) or []:
                if (row.get("pid") or row.get("project_id")) == pid and row.get("outcome") == OUTCOME_DRAFTED:
                    detail = row.get("detail") or {}
                    if isinstance(detail, str):
                        try:
                            detail = json.loads(detail)
                        except ValueError:
                            detail = {}
                    if (detail.get("subject") or "").lower() == subj:
                        return True
        except Exception:
            return False
        return False

    def _post_question(self, p: dict, task: dict, inbound: dict | None, decision: dict, health: dict | None) -> None:
        pid = p.get("id", "")
        unblocks = task.get("text", "")
        open_q = self.store.open_questions(pid) or []
        if any((q.get("unblocks") or "") == unblocks for q in open_q) or len(open_q) >= MAX_OPEN_QUESTIONS:
            self._record(pid, task.get("text", ""), "respond", OUTCOME_DEFERRED,
                         result="a question is already open on this task", reason="question_open",
                         subtask_id=task.get("id"), health=health)
            return
        q = decision.get("question_for_user") or decision.get("question") or \
            f"How should this be answered: '{(inbound or {}).get('subject', task.get('text', ''))[:70]}'?"
        dirs = [d for d in (decision.get("directions") or decision.get("options") or []) if d][:4]
        if not dirs:
            dirs = ["Acknowledge and say more will follow", "Answer from the project facts", "No reply needed"]
        options = [{"id": f"opt{i + 1}", "label": d} for i, d in enumerate(dirs)]
        evidence = f"{task.get('text', '')} open {_age_text(task.get('sla_created') or task.get('created'))}"
        if inbound:
            evidence += f"; inbound from {inbound.get('from_email', '')} {_age_text(inbound.get('received_at'))} ago"
        qid = self.store.add_question(pid, q, options, recommend=decision.get("recommend", ""),
                                      evidence=evidence, unblocks=task.get("text", ""), asked_by="assistant")
        self._record(pid, task.get("text", ""), "respond", OUTCOME_QUESTION, result=f"question {qid}: {q[:80]}",
                     evidence=decision.get("reason", ""), subtask_id=task.get("id"), health=health)

    # -- follow-up tasks -----------------------------------------------------
    def _work_followup(self, p: dict, task: dict, health: dict | None) -> None:
        pid = p.get("id", "")
        created = _parse_dt(task.get("created") or task.get("sla_created"))
        inbound = self._find_inbound(p, task, after=created)
        if inbound:
            n = self.store.complete_subtasks_matching(pid, task.get("text", "")[:40], completed_by="assistant")
            self._record(pid, task.get("text", ""), "follow_up", OUTCOME_ACTED,
                         result=f"reply received {inbound.get('received_at', '')}; cleared {n}", subtask_id=task.get("id"),
                         health=health)
            return
        due = self._followup_due(task)
        if not due:
            self._record(pid, task.get("text", ""), "follow_up", OUTCOME_DEFERRED,
                         result="no reply yet; not due for a nudge", reason="awaiting_reply",
                         subtask_id=task.get("id"), health=health)
            return
        ref = self._draft_followup(p, task, health)
        if not ref:
            self._record(pid, task.get("text", ""), "follow_up", OUTCOME_DEFERRED,
                         result="follow-up is due; no draft could be written (nudge is the user's call)",
                         reason="awaiting_reply", subtask_id=task.get("id"), health=health)

    def _followup_due(self, task: dict) -> bool:
        """Overdue = the task's date has arrived; with no date, N days
        (assistant.followup_after_days, default 3) after the task was made."""
        today = date.today()
        when = _parse_dt(task.get("follow_up_date"))
        if when:
            return when.date() <= today
        created = _parse_dt(task.get("created") or task.get("sla_created"))
        days = int((self.cfg.get("assistant") or {}).get("followup_after_days") or 3)
        return bool(created and (today - created.date()).days >= days)

    def _find_outbound(self, p: dict) -> dict | None:
        corr = p.get("correspondence")
        if corr is None and hasattr(self.store, "get_project_correspondence"):
            try:
                corr = self.store.get_project_correspondence(p.get("id", ""))
            except Exception:
                corr = []
        outs = [c for c in (corr or []) if isinstance(c, dict) and (c.get("direction") or "") == "outbound"]
        if not outs:
            return None
        outs.sort(key=lambda c: str(c.get("received_at") or c.get("date") or ""))
        return outs[-1]

    def _contact_email(self, p: dict) -> tuple[str, str]:
        for c in p.get("contacts") or []:
            if isinstance(c, dict) and c.get("email"):
                return c["email"], c.get("name") or ""
        return "", ""

    def _draft_followup(self, p: dict, task: dict, health: dict | None) -> str:
        """Write a follow-up nudge DRAFT with the followup.draft prompt. Never
        sends; the task stays open. Returns the draft ref or ''."""
        pid = p.get("id", "")
        if self.llm is None:
            return ""
        to_addr, to_name = self._contact_email(p)
        last_out = self._find_outbound(p)
        if not to_addr and last_out:
            raw_to = last_out.get("to_list") or last_out.get("to") or ""
            if isinstance(raw_to, list):
                raw_to = ",".join(str(x) for x in raw_to)
            m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", str(raw_to))
            to_addr = m.group(0) if m else ""
        if not to_addr:
            return ""
        subject = ("RE: " + (last_out.get("subject") or "").removeprefix("RE: ").removeprefix("Re: ")) \
            if last_out and last_out.get("subject") else f"Following up: {p.get('name', '')[:60]}"
        if self._followup_draft_exists(pid, task, subject):
            self._record(pid, task.get("text", ""), "follow_up", OUTCOME_DEFERRED,
                         result="unsent follow-up draft already exists", reason="draft_exists",
                         subtask_id=task.get("id"), health=health)
            return "exists"
        sent = _parse_dt((last_out or {}).get("received_at") or task.get("created"))
        days = (date.today() - sent.date()).days if sent else "?"
        attempt = 1 + sum(1 for r in (self.store.assistant_log(limit=300) or [])
                          if (r.get("pid") or r.get("project_id")) == pid and r.get("outcome") == OUTCOME_DRAFTED
                          and (r.get("item_type") == "follow_up"))
        facts = self._visible_facts(p)
        vars_ = {
            "project_name": p.get("name", ""), "customer_name": to_name or to_addr,
            "work_description": (p.get("scope") or p.get("name") or "")[:200],
            "days_since_sent": days, "last_communication": p.get("last_communication") or p.get("last_activity") or "unknown",
            "attempt_number": attempt, "visible_facts": facts,
        }
        vars_["source_text"] = "\n".join(str(v) for v in vars_.values())
        out = self.llm.run("followup.draft", vars_)
        if not out:
            return ""
        val = out[0]
        text = val.get("body", "") if isinstance(val, dict) else str(val)
        text = strip_signature_from_draft(text, self.profile)
        text = ground_text(text, vars_["source_text"], (self.llm.profile_vars.get("_acronyms") if hasattr(self.llm, "profile_vars") else None))
        text, _n = self.enforce_visible_fields(text, p)
        text = text.strip()
        if not text:
            return ""
        ref = self._create_draft(p, {"from_email": to_addr, "subject": subject.removeprefix("RE: "),
                                     "message": None}, text, subject_override=subject)
        self.store.add_note(pid, f"DRAFT (unsent): follow-up to {to_addr}", source="assistant")
        self._record(pid, task.get("text", ""), "follow_up", OUTCOME_DRAFTED,
                     result=f"follow-up draft {ref} to {to_addr}; task stays open", evidence=f"{days} days since last sent",
                     draft_v0=text, subtask_id=task.get("id"), health=health, recipient=to_addr, subject=subject)
        return ref

    def _followup_draft_exists(self, pid: str, task: dict, subject: str) -> bool:
        try:
            for row in self.store.assistant_log(limit=300) or []:
                if (row.get("pid") or row.get("project_id")) != pid or row.get("outcome") != OUTCOME_DRAFTED:
                    continue
                detail = row.get("detail") or {}
                if isinstance(detail, str):
                    try:
                        detail = json.loads(detail)
                    except ValueError:
                        detail = {}
                if (detail.get("subject") or "").lower() == subject.lower() \
                        and str(detail.get("subtask_id") or "") == str(task.get("id") or ""):
                    return True
        except Exception:
            return False
        return False

    # -- everything else ----------------------------------------------------
    def _work_other(self, p: dict, task: dict, health: dict | None) -> None:
        pid = p.get("id", "")
        allowed = [OUTCOME_QUESTION, OUTCOME_SUBTASK, OUTCOME_DEFERRED]
        res = None
        if self.llm is not None:
            inbound = self._find_inbound(p, task)
            open_q = self.store.open_questions(pid)
            out = self.llm.run("assistant.board_pass", {
                "project_name": p.get("name", ""), "project_status": p.get("status", ""),
                "item_text": task.get("text", ""), "item_type": task.get("date_type") or "follow_up",
                "item_age": _age_text(task.get("sla_created") or task.get("created")),
                "allowed_outcomes": ", ".join(allowed),
                "inbound_summary": (f"{inbound.get('from_email', '')}: {inbound.get('subject', '')}\n" +
                                    strip_noise_lines(clean_body(inbound.get('body') or '', 500, self.profile), self.profile)) if inbound else "(none)",
                "open_tasks": "\n".join(f"- {t.get('text', '')}" for t in (p.get("subtasks") or []) if (t.get("status") or "pending") == "pending"),
                "recent_notes": "\n".join(f"- {(n.get('text') if isinstance(n, dict) else str(n))[:120]}" for n in (p.get("notes") or [])[-3:]),
                "open_question": (open_q[0].get("question", "") if open_q else "none"),
                "today": date.today().isoformat(),
            })
            res = out[0] if out and isinstance(out[0], dict) else None
        if res and res.get("outcome") in allowed:
            if res["outcome"] == OUTCOME_QUESTION:
                self._post_question(p, task, None, {"question": res.get("question"), "directions": res.get("options"),
                                                    "reason": res.get("reason", "")}, health)
                return
            if res["outcome"] == OUTCOME_SUBTASK and res.get("subtask_text"):
                sid = self.store.add_subtask(pid, res["subtask_text"][:120], date_type="follow_up", source="assistant")
                self._record(pid, task.get("text", ""), "task", OUTCOME_SUBTASK, result=f"added {sid}: {res['subtask_text'][:80]}",
                             evidence=res.get("reason", ""), subtask_id=task.get("id"), health=health)
                return
            days = int(res.get("defer_days") or 3)
            self._record(pid, task.get("text", ""), "task", OUTCOME_DEFERRED, result=res.get("reason", "deferred"),
                         reason="model_deferred", defer_until=(date.today() + timedelta(days=days)).isoformat(),
                         subtask_id=task.get("id"), health=health)
            return
        self._record(pid, task.get("text", ""), "task", OUTCOME_DEFERRED,
                     result="no rule covers this task" + ("" if self.llm is not None else " and no model is available"),
                     reason="no_rule", subtask_id=task.get("id"), health=health)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _find_inbound(self, p: dict, task: dict, after: datetime | None = None) -> dict | None:
        """Latest inbound message on the project, from whatever the project
        dict carries: task metadata, a correspondence list, or key_details."""
        cands: list[dict] = []
        meta = task.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        if isinstance(meta.get("email"), dict):
            cands.append(meta["email"])
        corr = p.get("correspondence")
        if corr is None and hasattr(self.store, "get_project_correspondence"):
            # The real Store keeps correspondence out of the project dict
            # (section 9 lists only file_correspondence); use the accessor
            # when it exists, with bodies.
            try:
                corr = self.store.get_project_correspondence(p.get("id", ""), include_bodies=True)
            except TypeError:
                corr = self.store.get_project_correspondence(p.get("id", ""))
            except Exception:
                corr = []
        for c in (corr or []):
            if isinstance(c, dict) and (c.get("direction") or "inbound") == "inbound":
                cands.append(c)
        kd = p.get("key_details") or {}
        if isinstance(kd.get("last_inbound"), dict):
            cands.append(kd["last_inbound"])
        norm = []
        for c in cands:
            body = c.get("body") or c.get("body_raw") or c.get("body_preview") or ""
            if not body and c.get("body_html"):
                body = re.sub(r"<[^>]+>", " ", str(c["body_html"]))
            when = c.get("received_at") or c.get("date") or c.get("ts") or ""
            if after and (_parse_dt(when) or datetime.min) <= after:
                continue
            norm.append({"subject": c.get("subject") or "", "body": body, "from_email": c.get("from_email") or c.get("from") or "",
                         "from_name": c.get("from_name") or "", "received_at": when, "corr_id": c.get("id") or c.get("corr_id"),
                         "message": c.get("message")})
        if not norm:
            return None
        norm.sort(key=lambda c: str(c["received_at"]))
        return norm[-1]

    def _visible_facts(self, p: dict) -> str:
        kd = p.get("key_details") or {}
        lines = []
        for key in self.visible_fields:
            val = p.get(key, kd.get(key))
            if val in (None, "", [], {}):
                continue
            if any(m in key for m in MONEY_FIELDS) and not self._money_visible():
                continue
            lines.append(f"{key}: {val}")
        return "\n".join(lines) or "(none)"

    def _money_visible(self) -> bool:
        return any(any(m in f for m in MONEY_FIELDS) for f in self.visible_fields)

    def forbidden_values(self, p: dict) -> list[str]:
        """Every project value NOT in the whitelist, in the spellings a draft
        might carry (12450 / 12,450 / 12,450.00 / $12,450)."""
        kd = p.get("key_details") or {}
        out: list[str] = []
        pool = dict(kd)
        for k, v in p.items():
            if k not in ("subtasks", "notes", "contacts", "correspondence", "key_details", "id", "status", "workflow_id"):
                pool.setdefault(k, v)
        for key, val in pool.items():
            if key in self.visible_fields or val in (None, "", [], {}):
                continue
            if isinstance(val, bool):
                continue
            if isinstance(val, (int, float)):
                n = float(val)
                out += [f"{n:,.2f}", f"{n:,.0f}", f"{n:.0f}", f"{n:.2f}"]
            elif isinstance(val, str) and len(val.strip()) >= 3 and key not in ("name", "created", "last_activity"):
                out.append(val.strip())
        return [v for v in dict.fromkeys(out) if v]

    def enforce_visible_fields(self, text: str, p: dict) -> tuple[str, int]:
        """Strip any sentence that carries a non-whitelisted project value.
        Returns (clean_text, sentences_removed). Empty text means refuse."""
        if not text:
            return "", 0
        forbidden = self.forbidden_values(p)
        if not forbidden:
            return text, 0
        sentences = re.split(r'(?<=[.!?])\s+|\n+', text.strip())
        kept, removed = [], 0
        for s in sentences:
            if not s.strip():
                continue
            low = s.lower()
            if any(f.lower() in low for f in forbidden):
                removed += 1
                continue
            kept.append(s.strip())
        return " ".join(kept).strip(), removed

    # -- logging -------------------------------------------------------------
    def _record(self, pid: str, item: str, item_type: str, outcome: str, **detail) -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome}")
        self.counts[outcome] += 1
        entry = {"ts": _now_iso(), "run_id": self.run_id, "proj": pid or "", "item": item, "item_type": item_type,
                 "outcome": outcome}
        body_v0 = detail.pop("draft_v0", None)
        entry.update({k: v for k, v in detail.items() if v not in (None, "")})
        if body_v0:
            # Never write a whole draft into the logs: length and a short head only.
            entry["draft_len"] = len(body_v0)
            entry["draft_head"] = body_v0[:80]
        self.items.append(entry)
        self._log(pid, item_type, outcome, {k: v for k, v in entry.items()
                                            if k not in ("ts", "run_id", "proj", "item_type", "outcome")})

    def _log(self, pid: str, item_type: str, outcome: str, detail: dict) -> None:
        try:
            self.store.log_assistant(self.run_id, pid or "", item_type, outcome, detail)
        except Exception:
            pass
        try:
            logs = Path(self.data_root) / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            row = {"ts": _now_iso(), "run_id": self.run_id, "proj": pid or "", "item_type": item_type,
                   "outcome": outcome, **detail}
            with open(logs / f"assistant_{date.today().isoformat()}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except Exception:
            pass


# ----------------------------------------------------------------------
# weekly digest (a draft to self; nothing is sent)
# ----------------------------------------------------------------------

DIGEST_SUBJECT = "Your week in Email Boss"


def _client_of(p: dict) -> str:
    kd = p.get("key_details") or {}
    c = p.get("client") or kd.get("client") or kd.get("client_name") or kd.get("customer") or ""
    if not c:
        for ct in p.get("contacts") or []:
            if isinstance(ct, dict) and ct.get("company"):
                c = ct["company"]
                break
    return str(c).strip() or "No client named"


def digest_stats(store: Any, profession: dict, today: date | None = None) -> dict:
    """Open items, overdue and waiting-on-me counts, grouped by client."""
    today = today or date.today()
    clients: dict[str, dict] = {}
    for p in store.active_projects() or []:
        pend = [t for t in (p.get("subtasks") or []) if (t.get("status") or "pending") == "pending"]
        if not pend:
            continue
        row = clients.setdefault(_client_of(p), {"open": 0, "overdue": 0, "waiting_on_me": 0, "projects": []})
        row["open"] += len(pend)
        late = 0
        for t in pend:
            fu = str(t.get("follow_up_date") or "")[:10]
            if fu and fu < today.isoformat():
                late += 1
            if _RESPOND_RE.search(t.get("text") or ""):
                row["waiting_on_me"] += 1
        row["overdue"] += late
        row["projects"].append(p.get("name", ""))
    totals = {k: sum(c[k] for c in clients.values()) for k in ("open", "overdue", "waiting_on_me")}
    return {"clients": clients, "totals": totals, "week_of": today.isoformat()}


def digest_stats_block(stats: dict) -> str:
    t = stats["totals"]
    lines = [f"Open items: {t['open']}", f"Overdue: {t['overdue']}", f"Waiting on you to reply: {t['waiting_on_me']}"]
    for name in sorted(stats["clients"], key=lambda n: (-stats["clients"][n]["overdue"], n.lower())):
        c = stats["clients"][name]
        lines.append(f"{name}: {c['open']} open, {c['overdue']} overdue, {c['waiting_on_me']} waiting on you")
    return "\n".join(lines)


def digest_plain_text(stats: dict) -> str:
    """The digest with no model: the same grouped counts, in plain sentences."""
    t = stats["totals"]
    if not stats["clients"]:
        return "Nothing is open on your board this week."
    out = [f"You have {t['open']} open items. {t['overdue']} are overdue and {t['waiting_on_me']} are waiting on your reply.", ""]
    for name in sorted(stats["clients"], key=lambda n: (-stats["clients"][n]["overdue"], n.lower())):
        c = stats["clients"][name]
        out.append(f"{name}: {c['open']} open, {c['overdue']} overdue, {c['waiting_on_me']} waiting on you.")
    return "\n".join(out)


def _weekday_from(value: Any) -> int:
    names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if isinstance(value, str) and value.strip().lower()[:3] in [n[:3] for n in names]:
        return [n[:3] for n in names].index(value.strip().lower()[:3])
    try:
        return int(value) % 7
    except (TypeError, ValueError):
        return 0


def run_digest(store: Any, cfg: dict, profession: dict, llm: Any = None, mail: Any = None) -> dict:
    """Build the weekly digest and leave it as a DRAFT to the user themself
    (provider Drafts folder, else the board's drafts). Never sends."""
    stats = digest_stats(store, profession)
    block = digest_stats_block(stats)
    text = ""
    if llm is not None and getattr(llm, "available", False):
        try:
            out = llm.run("digest.weekly", {"stats_block": block, "today": stats["week_of"]})
            if out:
                val = out[0]
                text = (val.get("digest") or val.get("text") or val.get("body") or "") if isinstance(val, dict) else str(val)
        except Exception:
            text = ""
    text = (text or "").strip() or digest_plain_text(stats)
    to_addr = ((cfg.get("user") or {}).get("email") or "").strip()
    if not to_addr:
        return {"ok": False, "error": "no email address to leave the digest for"}
    ref = ""
    if (cfg.get("assistant") or {}).get("drafts_to", "provider") == "provider" and mail is not None \
            and hasattr(mail, "create_draft"):
        try:
            r = mail.create_draft(None, [to_addr], [], DIGEST_SUBJECT,
                                  _html.escape(text + "\n\n" + block).replace("\n", "<br>"))
            if r:
                ref = f"provider:{r}"
        except Exception:
            ref = ""
    if not ref:
        ref = "local:" + str(store.add_draft("", [to_addr], [], DIGEST_SUBJECT, text + "\n\n" + block,
                                             reply_to_corr_id=None, created_by="assistant"))
    return {"ok": True, "draft": ref, "subject": DIGEST_SUBJECT, "stats": stats["totals"]}


# ----------------------------------------------------------------------
# scheduler
# ----------------------------------------------------------------------

class AssistantScheduler:
    """Hourly (configurable) unattended pass. Shares `app_ext['assistant_lock']`
    with the API so a manual run and the timer never overlap."""

    def __init__(self, app_ext: dict, interval_minutes: int = 60):
        self.ext = app_ext
        self.interval = max(1, int(interval_minutes or 60))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.ext.setdefault("assistant_lock", threading.Lock())
        self.ext.setdefault("assistant_status", {"running": False, "last_run": None, "last_summary": None,
                                                 "next_run_at": None, "error": ""})

    def _enabled(self) -> bool:
        cfg = self.ext.get("cfg") or {}
        return bool(((cfg.get("ai") or {}).get("features") or {}).get("assistant", False))

    def run_once(self, max_items: int | None = None) -> dict | None:
        lock: threading.Lock = self.ext["assistant_lock"]
        if not lock.acquire(blocking=False):
            return None
        status = self.ext["assistant_status"]
        try:
            if is_read_only(self.ext):
                # Read-only license: background work writes nothing.
                now = _now_iso()
                summary = {"run_id": "", "started": now, "finished": now, "duration_ms": 0, "max_items": 0,
                           "llm_used": False, "counts": {o: 0 for o in OUTCOMES},
                           "triage": {"approved": 0, "dismissed": 0, "created": 0, "left": {}},
                           "escalations": [], "items": [], "nothing_sent": True, "skipped": "license_read_only"}
                status["last_summary"] = summary
                status["error"] = ""
                return summary
            status["running"] = True
            cfg = self.ext.get("cfg") or {}
            ap = AssistantPass(self.ext["store"], cfg, self.ext.get("profession") or {},
                               self.ext.get("llm"), self.ext.get("mail"))
            summary = ap.run(max_items or (cfg.get("assistant") or {}).get("max_items") or 10)
            status["last_summary"] = summary
            status["last_run"] = summary["finished"]
            status["error"] = ""
            return summary
        except Exception as e:
            status["error"] = str(e)
            return None
        finally:
            status["running"] = False
            lock.release()

    # -- weekly digest ----------------------------------------------------
    def _local_now(self) -> datetime:
        hours = getattr(self.ext.get("store"), "hours", None) or self.ext.get("hours")
        try:
            return hours.now_local_naive() if hours is not None else datetime.now()
        except Exception:
            return datetime.now()

    def digest_due(self, now: datetime | None = None) -> bool:
        acfg = (self.ext.get("cfg") or {}).get("assistant") or {}
        wd = acfg.get("digest_weekday", 0)
        if wd is None or str(wd).lower() in ("off", "none", "false", "-1"):
            return False
        now = now or self._local_now()
        try:
            hour = int(acfg.get("digest_hour", 7))
        except (TypeError, ValueError):
            hour = 7
        if now.weekday() != _weekday_from(wd) or now.hour < hour:
            return False
        st = self.ext.get("store")
        return not (st is not None and st.meta_get("digest_last_sent") == now.date().isoformat())

    def maybe_run_digest(self, now: datetime | None = None) -> dict | None:
        """Runs the digest once on the configured weekday (default Monday,
        07:00 local) and catches up later that day. Draft only."""
        if not self.digest_due(now):
            return None
        if is_read_only(self.ext):
            return None
        st = self.ext["store"]
        now = now or self._local_now()
        res = run_digest(st, self.ext.get("cfg") or {}, self.ext.get("profession") or {},
                         self.ext.get("llm"), self.ext.get("mail"))
        if res.get("ok"):
            st.meta_set("digest_last_sent", now.date().isoformat())
        return res

    def _loop(self) -> None:
        status = self.ext["assistant_status"]
        next_pass = time.monotonic() + self.interval * 60
        status["next_run_at"] = (datetime.now() + timedelta(minutes=self.interval)).isoformat(timespec="seconds")
        while not self.stop_event.is_set():
            if self.stop_event.wait(30):
                break
            try:
                self.maybe_run_digest()
            except Exception as e:
                status["error"] = f"digest: {e}"
            if time.monotonic() >= next_pass:
                if self._enabled():
                    self.run_once()
                next_pass = time.monotonic() + self.interval * 60
                status["next_run_at"] = (datetime.now() + timedelta(minutes=self.interval)).isoformat(timespec="seconds")

    def start(self) -> "AssistantScheduler":
        if self.thread and self.thread.is_alive():
            return self
        self.thread = threading.Thread(target=self._loop, name="assistant-pass", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()


def start_assistant_thread(app_ext: dict, interval_minutes: int = 60) -> AssistantScheduler:
    """Create, register under `app_ext['assistant']`, and start the scheduler."""
    sched = AssistantScheduler(app_ext, interval_minutes)
    app_ext["assistant"] = sched
    return sched.start()
