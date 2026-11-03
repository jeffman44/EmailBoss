"""Test doubles for the AI layer: an in-memory Store (DESIGN.md section 9
subset the AI code calls) and a scripted LLMProvider."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any, Callable

from plannerboard.ai.base import LLMResult


def import_api_module(name: str):
    """Import plannerboard.api.<name> without running the api package's
    __init__ (which imports every blueprint, some owned by other builders
    and possibly absent while this layer is tested on its own)."""
    import importlib
    import sys
    import types
    from pathlib import Path
    pkg_name = "plannerboard.api"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "plannerboard" / "api")]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg
    return importlib.import_module(f"{pkg_name}.{name}")


class FakeStore:
    """Only the Store methods the ai/ package calls. Every write is recorded
    in `.calls` so tests can assert what did and did not happen."""

    def __init__(self, projects: list[dict] | None = None, triage: list[dict] | None = None,
                 routing: dict | None = None, contacts: dict | None = None, ids: dict | None = None):
        self.projects = {p["id"]: p for p in (projects or [])}
        self.triage = {c["triage_id"]: dict(c, status=c.get("status", "pending")) for c in (triage or [])}
        self.routing = dict(routing or {})
        self.contacts = dict(contacts or {})
        self.ids = dict(ids or {})
        self.usage: list[dict] = []
        self.assist_log: list[dict] = []
        self.questions: list[dict] = []
        self.drafts: list[dict] = []
        self.notes: list[dict] = []
        self.alerts: list[dict] = []
        self.meta: dict = {}
        self.calls: list[tuple] = []
        self._usage_today_override: dict | None = None

    # projects
    def active_projects(self):
        return [dict(p) for p in self.projects.values() if not p.get("archived")]

    def all_projects(self, include_archived=False):
        return [dict(p) for p in self.projects.values() if include_archived or not p.get("archived")]

    def get_project(self, pid):
        p = self.projects.get(pid)
        return dict(p) if p else None

    def create_project(self, name, workflow_id, status, work_type="", contacts=None, key_details=None,
                       scope="", tags=None, source=""):
        pid = "proj_" + uuid.uuid4().hex[:6]
        self.projects[pid] = {"id": pid, "name": name, "workflow_id": workflow_id, "status": status,
                              "work_type": work_type, "contacts": contacts or [], "key_details": key_details or {},
                              "scope": scope, "subtasks": [], "notes": []}
        self.calls.append(("create_project", pid))
        return pid

    def update_project(self, pid, **fields):
        self.calls.append(("update_project", pid, fields))
        self.projects[pid].update(fields)
        return self.projects[pid]

    # indexes
    def contact_index(self):
        return dict(self.contacts)

    def id_index(self):
        return dict(self.ids)

    def keyword_index(self):
        return {}

    # threads
    def routing_lookup(self, thread_key):
        return self.routing.get(thread_key)

    def routing_set(self, thread_key, pid, source=""):
        self.routing[thread_key] = pid

    def message_seen(self, message_id):
        return False

    def message_mark(self, message_id, status, pid=None, triage_id=None):
        pass

    # triage
    def add_triage_card(self, card):
        tid = card.get("triage_id") or "msg_" + uuid.uuid4().hex[:6]
        self.triage[tid] = dict(card, triage_id=tid, status="pending")
        return tid

    def list_triage(self, status="pending"):
        return [dict(c) for c in self.triage.values() if c.get("status") == status]

    def approve_triage(self, triage_id, pid_or_new, *, workflow_id=None, status=None, work_type=None,
                       new_name=None, actor="system"):
        card = self.triage[triage_id]
        if card.get("status") != "pending":
            return {"ok": True, "already_approved": True, "project_id": card.get("matched_project")}
        pid = pid_or_new
        if pid_or_new == "__new__":
            pid = self.create_project(new_name or card.get("subject", "New"), workflow_id or "general",
                                      status or "action_required", work_type=work_type or "")
        card["status"] = "approved"
        card["matched_project"] = pid
        card["approved_by"] = actor
        if card.get("conversation_id"):
            self.routing[card["conversation_id"]] = pid
        self.calls.append(("approve_triage", triage_id, pid, actor))
        return {"ok": True, "project_id": pid, "triage_id": triage_id}

    def dismiss_triage(self, triage_id, reason="", actor="system"):
        self.triage[triage_id]["status"] = "dismissed"
        self.triage[triage_id]["dismiss_reason"] = reason
        self.calls.append(("dismiss_triage", triage_id, reason))

    # filing
    def file_correspondence(self, pid, email_obj, attachment_paths):
        return "corr_" + uuid.uuid4().hex[:6]

    # subtasks / notes / questions / drafts
    def add_subtask(self, pid, text, date_type="follow_up", follow_up_date=None, sla_hours=None,
                    priority=None, source="system", category=None):
        sid = "st_" + uuid.uuid4().hex[:6]
        self.projects[pid].setdefault("subtasks", []).append({
            "id": sid, "text": text, "status": "pending", "date_type": date_type,
            "follow_up_date": follow_up_date, "source": source, "created": datetime.now().isoformat()})
        self.calls.append(("add_subtask", pid, text, date_type, follow_up_date))
        return sid

    def open_subtasks(self, pid):
        return [t for t in self.projects[pid].get("subtasks", []) if t.get("status") == "pending"]

    def complete_subtasks_matching(self, pid, text_prefix, completed_by="system"):
        n = 0
        for t in self.projects[pid].get("subtasks", []):
            if t.get("status") == "pending" and t.get("text", "").startswith(text_prefix):
                t["status"] = "complete"
                t["completed_by"] = completed_by
                n += 1
        self.calls.append(("complete_subtasks_matching", pid, text_prefix, completed_by, n))
        return n

    def add_note(self, pid, text, source="system"):
        self.notes.append({"pid": pid, "text": text, "source": source})
        self.projects[pid].setdefault("notes", []).append({"text": text, "source": source})
        self.calls.append(("add_note", pid, text))

    def add_question(self, pid, question, options, recommend="", evidence="", unblocks="", asked_by="assistant"):
        qid = "q_" + uuid.uuid4().hex[:6]
        self.questions.append({"id": qid, "pid": pid, "question": question, "options": options,
                               "recommend": recommend, "evidence": evidence, "unblocks": unblocks,
                               "asked_by": asked_by, "status": "open"})
        self.calls.append(("add_question", pid, question))
        return qid

    def open_questions(self, pid=None):
        return [q for q in self.questions if q["status"] == "open" and (pid is None or q["pid"] == pid)]

    def add_draft(self, pid, to, cc, subject, body, reply_to_corr_id=None, created_by="assistant"):
        did = "draft_" + uuid.uuid4().hex[:6]
        self.drafts.append({"id": did, "pid": pid, "to": to, "cc": cc, "subject": subject, "body": body,
                            "reply_to_corr_id": reply_to_corr_id, "created_by": created_by, "sent": False})
        self.calls.append(("add_draft", pid, subject))
        return did

    # ai bookkeeping
    def record_usage(self, task, provider, model, in_tok, out_tok, cost_usd, latency_ms, ok):
        self.usage.append({"task": task, "provider": provider, "model": model, "in_tok": in_tok,
                           "out_tok": out_tok, "cost_usd": cost_usd, "latency_ms": latency_ms, "ok": ok})

    def usage_today(self):
        if self._usage_today_override is not None:
            return self._usage_today_override
        by_task: dict[str, int] = {}
        for u in self.usage:
            by_task[u["task"]] = by_task.get(u["task"], 0) + 1
        return {"cost_usd": sum(u["cost_usd"] for u in self.usage), "calls": len(self.usage), "by_task": by_task}

    def log_assistant(self, run_id, pid, item_type, outcome, detail):
        self.assist_log.append({"run_id": run_id, "pid": pid, "item_type": item_type, "outcome": outcome,
                                "detail": json.loads(json.dumps(detail, default=str)),
                                "ts": datetime.now().isoformat()})

    def assistant_log(self, limit=200):
        return list(reversed(self.assist_log))[:limit]

    # misc
    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = value

    def add_alert(self, kind, text, severity="warn"):
        self.alerts.append({"kind": kind, "text": text, "severity": severity})

    def open_alerts(self):
        return list(self.alerts)


class FakeLLMProvider:
    """Scripted provider. `responses` maps a substring of the rendered prompt
    (every shipped prompt starts its system text with "Task: <id>.") to the
    value to return: a dict/list (structured) or a str (text). A callable
    value receives (system, user) and returns the value."""

    name = "fake"
    model = "fake-model"

    def __init__(self, responses: dict[str, Any] | None = None, available: bool = True,
                 in_tokens: int = 100, out_tokens: int = 50, default: Any = None):
        self.responses = dict(responses or {})
        self._available = available
        self.in_tokens = in_tokens
        self.out_tokens = out_tokens
        self.default = default
        self.calls: list[dict] = []

    def available(self) -> bool:
        return self._available

    def complete(self, system, user, *, max_tokens=800, temperature=0.2, json_schema=None, model=None):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens,
                           "temperature": temperature, "json_schema": json_schema, "model": model})
        hay = system + "\n" + user
        value = self.default
        for key, val in self.responses.items():
            if key in hay:
                value = val
                break
        if callable(value):
            value = value(system, user)
        if value is None:
            return LLMResult.failure("fake: no scripted response", model=model or self.model)
        if isinstance(value, (dict, list)):
            return LLMResult(text=json.dumps(value), parsed=json.loads(json.dumps(value)),
                             input_tokens=self.in_tokens, output_tokens=self.out_tokens,
                             cost_usd=0.0, latency_ms=5, model=model or self.model, ok=True)
        return LLMResult(text=str(value), parsed=None, input_tokens=self.in_tokens,
                         output_tokens=self.out_tokens, cost_usd=0.0, latency_ms=5,
                         model=model or self.model, ok=True)


def task_id_of(system: str) -> str:
    m = re.search(r"Task:\s*([a-z_.]+)", system or "")
    return m.group(1).rstrip(".") if m else ""
