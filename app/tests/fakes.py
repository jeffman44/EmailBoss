"""In-memory ``Store`` implementing the DESIGN.md section 9 interface, for tests."""
from __future__ import annotations

import itertools
import re
from datetime import datetime


class FakeStore:
    def __init__(self, id_patterns=None):
        self._ids = itertools.count(1)
        self.projects: dict[str, dict] = {}
        self.routing: dict[str, str] = {}
        self.messages: dict[str, dict] = {}
        self.cards: dict[str, dict] = {}
        self.correspondence: list[dict] = []
        self.subtasks: dict[str, list[dict]] = {}
        self.notes: list[tuple] = []
        self.questions: list[dict] = []
        self.drafts: list[dict] = []
        self.usage: list[tuple] = []
        self.assistant: list[dict] = []
        self.meta: dict = {}
        self.alerts: list[dict] = []
        self.id_patterns = id_patterns or []

    # projects
    def active_projects(self):
        return [p for p in self.projects.values() if not p.get("archived") and p.get("status") not in ("complete", "ignored")]

    def all_projects(self, include_archived=False):
        return [p for p in self.projects.values() if include_archived or not p.get("archived")]

    def get_project(self, pid):
        return self.projects.get(pid)

    def create_project(self, name, workflow_id, status, work_type="", contacts=None, key_details=None,
                       scope="", tags=None, source=""):
        pid = f"proj_{next(self._ids):03d}"
        self.projects[pid] = {"id": pid, "name": name, "workflow_id": workflow_id, "status": status,
                              "work_type": work_type, "contacts": contacts or [], "key_details": key_details or {},
                              "scope": scope, "tags": tags or [], "source": source, "subtasks": [],
                              "last_activity": datetime.now().strftime("%Y-%m-%d"), "archived": False}
        return pid

    def update_project(self, pid, **fields):
        p = self.projects[pid]
        kd = fields.pop("key_details", None)
        if kd:
            p.setdefault("key_details", {}).update(kd)
        p.update(fields)
        return p

    # indexes
    def contact_index(self):
        idx = {}
        for p in self.active_projects():
            for c in p.get("contacts") or []:
                em = (c.get("email") if isinstance(c, dict) else c) or ""
                if em:
                    idx.setdefault(em.lower(), []).append(p["id"])
        return idx

    def id_index(self):
        idx = {}
        for p in self.active_projects():
            kd = p.get("key_details") or {}
            vals = []
            for pat in self.id_patterns:
                v = kd.get(pat["key"])
                if v:
                    vals.append(str(v))
                for m in re.finditer(pat["regex"], p.get("name", "") + " " + " ".join(str(x) for x in (p.get("notes") or [])), re.I):
                    vals.append(m.group(1) if m.groups() else m.group(0))
            for r in kd.get("refs") or []:
                vals.append(str(r))
            for v in vals:
                key = v if v.startswith(("ORD:", "TRK:")) else re.sub(r"\s+", "", v).upper()
                idx.setdefault(key, [])
                if p["id"] not in idx[key]:
                    idx[key].append(p["id"])
        return idx

    def keyword_index(self):
        idx = {}
        for p in self.active_projects():
            for tok in (p.get("name", "") + " " + p.get("scope", "")).lower().split():
                tok = tok.strip(".,;:!?()[]{}\"'")
                if len(tok) >= 3:
                    idx.setdefault(tok, [])
                    if p["id"] not in idx[tok]:
                        idx[tok].append(p["id"])
        return idx

    # threads + dedup
    def routing_lookup(self, thread_key):
        return self.routing.get(thread_key)

    def routing_set(self, thread_key, pid, source=""):
        self.routing[thread_key] = pid

    def message_seen(self, message_id):
        return message_id in self.messages

    def message_mark(self, message_id, status, pid=None, triage_id=None):
        self.messages[message_id] = {"status": status, "pid": pid, "triage_id": triage_id}

    # triage
    def add_triage_card(self, card):
        tid = f"tri_{next(self._ids):03d}"
        card = dict(card)
        card["id"] = tid
        card.setdefault("status", "pending")
        self.cards[tid] = card
        return tid

    def list_triage(self, status="pending"):
        return [c for c in self.cards.values() if c.get("status") == status]

    def approve_triage(self, triage_id, pid_or_new, *, workflow_id=None, status=None, work_type=None,
                       new_name=None, actor="system"):
        card = self.cards[triage_id]
        if pid_or_new == "__new__":
            pid = self.create_project(new_name or card.get("subject", "New"), workflow_id or "general",
                                      status or "action_required", work_type=work_type or "")
        else:
            pid = pid_or_new
        card["status"] = "approved"
        card["matched_project"] = pid
        self.file_correspondence(pid, card, [])
        return {"project_id": pid, "triage_id": triage_id}

    def dismiss_triage(self, triage_id, reason="", actor="system"):
        self.cards[triage_id]["status"] = "dismissed"
        self.cards[triage_id]["dismiss_reason"] = reason

    # filing
    def file_correspondence(self, pid, email_obj, attachment_paths):
        cid = f"corr_{next(self._ids):03d}"
        self.correspondence.append({"id": cid, "pid": pid, "subject": email_obj.get("subject"),
                                    "attachments": list(attachment_paths)})
        return cid

    # subtasks / notes / questions / drafts
    def add_subtask(self, pid, text, date_type="follow_up", follow_up_date=None, sla_hours=None,
                    priority=None, source="system", category=None):
        sid = f"st_{next(self._ids):03d}"
        self.subtasks.setdefault(pid, []).append({"id": sid, "text": text, "date_type": date_type,
                                                  "follow_up_date": follow_up_date, "sla_hours": sla_hours,
                                                  "status": "open", "source": source})
        return sid

    def open_subtasks(self, pid):
        return [s for s in self.subtasks.get(pid, []) if s["status"] == "open"]

    def complete_subtasks_matching(self, pid, text_prefix, completed_by="system"):
        n = 0
        for s in self.subtasks.get(pid, []):
            if s["status"] == "open" and s["text"].startswith(text_prefix):
                s["status"] = "done"
                s["completed_by"] = completed_by
                n += 1
        return n

    def add_note(self, pid, text, source="system"):
        self.notes.append((pid, text, source))

    def add_question(self, pid, question, options, recommend="", evidence="", unblocks="", asked_by="assistant"):
        qid = f"q_{next(self._ids):03d}"
        self.questions.append({"id": qid, "pid": pid, "question": question})
        return qid

    def open_questions(self, pid=None):
        return [q for q in self.questions if pid is None or q["pid"] == pid]

    def add_draft(self, pid, to, cc, subject, body, reply_to_corr_id=None, created_by="assistant"):
        did = f"draft_{next(self._ids):03d}"
        self.drafts.append({"id": did, "pid": pid, "to": to, "subject": subject})
        return did

    # ai bookkeeping
    def record_usage(self, *a):
        self.usage.append(a)

    def usage_today(self):
        return {"cost_usd": 0.0, "calls": len(self.usage), "by_task": {}}

    def log_assistant(self, run_id, pid, item_type, outcome, detail):
        self.assistant.append({"run_id": run_id, "pid": pid, "outcome": outcome})

    def assistant_log(self, limit=200):
        return self.assistant[-limit:]

    # misc
    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = value

    def add_alert(self, kind, text, severity="warn"):
        self.alerts.append({"kind": kind, "text": text, "severity": severity})

    def open_alerts(self):
        return list(self.alerts)


class FakeLLM:
    """Returns canned results per prompt id; records calls."""

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def run(self, prompt_id, vars):
        self.calls.append((prompt_id, vars))
        r = self.results.get(prompt_id)
        if callable(r):
            return r(vars)
        return r
