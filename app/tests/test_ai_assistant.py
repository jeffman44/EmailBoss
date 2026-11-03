"""Assistant pass end-to-end on FakeStore + FakeLLMProvider."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from tests.fakes_ai import FakeLLMProvider, FakeStore  # noqa: E402
from plannerboard.ai.assistant import (AssistantPass, AssistantScheduler, board_order,  # noqa: E402
                                       start_assistant_thread)
from plannerboard.ai.registry import LLM  # noqa: E402
from plannerboard.ai.usage import UsageMeter  # noqa: E402

NOW = datetime.now()
AGO = lambda h: (NOW - timedelta(hours=h)).isoformat(timespec="seconds")  # noqa: E731

PROFESSION = {
    "entity": {"project": "Project", "projects": "Projects", "client": "Client", "quote": "Proposal"},
    "triage": {"classifications": ["request", "client_reply", "approval", "informational", "junk"],
               "notification_senders": ["noreply@", "no-reply@"],
               "internal_admin_subjects": ["payroll"]},
    "vocab": {"keywords": [], "acronyms": ["COI"]},
    "prompt_profile": {"industry": "general business", "role_noun": "professional"},
    "id_patterns": [{"key": "reference_no", "label": "Ref #", "regex": r"\b[A-Z]{2}-\d{4,6}\b", "unique_match": True}],
    "customer_visible_fields": ["name", "scope", "location"],
    "work_types": [{"id": "task", "label": "Task", "workflow_id": "general"}],
    "workflows": {"workflows": [{"id": "general", "entry_stage": "action_required",
                                 "stages": [{"id": "action_required", "role": "todo"}, {"id": "waiting", "role": "waiting"},
                                            {"id": "complete", "role": "closed"}]}],
                  "sort": {"priority": ["deadline", "sla", "follow_up", "aging"]}},
}


def _cfg(tmp_path, assistant_on=True, drafts_to="local"):
    return {"user": {"name": "Pat Example", "email": "pat@example.test", "company": "Example Co",
                     "role": "Account Manager", "internal_domains": ["example.test"]},
            "paths": {"data_root": str(tmp_path)},
            "ai": {"provider": "fake", "daily_budget_usd": 5.0,
                   "features": {"classify": True, "summarize": True, "match_rerank": True,
                                "draft_reply": True, "assistant": assistant_on}},
            "assistant": {"schedule_minutes": 60, "max_items": 10, "drafts_to": drafts_to}}


def _projects():
    return [
        # Top of the board: a "Respond to" task with an inbound that needs no judgment -> draft
        {"id": "proj_a", "name": "Harbor Plaza Lobby Refresh", "status": "action_required", "workflow_id": "general",
         "scope": "Refresh the lobby finishes", "quote_amount": 12450.0,
         "key_details": {"reference_no": "AB-10234", "location": "Harbor Plaza", "internal_margin": "31%"},
         "contacts": [{"name": "Sam Client", "email": "sam@client.test"}],
         "subtasks": [{"id": "st_1", "text": "Respond to Sam Client", "status": "pending", "date_type": "sla",
                       "created": AGO(5), "sla_created": AGO(5)}],
         "correspondence": [{"id": "c1", "direction": "inbound", "from_email": "sam@client.test", "from_name": "Sam Client",
                             "subject": "Revised drawings", "received_at": AGO(5),
                             "body": "Attached are the revised drawings for the lobby. Can you confirm you received them?"}],
         "notes": []},
        # Needs judgment (price) -> question
        {"id": "proj_b", "name": "Northgate Suite 200 Fit-out", "status": "action_required", "workflow_id": "general",
         "key_details": {"reference_no": "CD-55555"}, "contacts": [{"name": "Lee", "email": "lee@tenant.test"}],
         "subtasks": [{"id": "st_2", "text": "Respond to Lee", "status": "pending", "date_type": "follow_up",
                       "created": AGO(30), "follow_up_date": (NOW + timedelta(days=1)).date().isoformat()}],
         "correspondence": [{"id": "c2", "direction": "inbound", "from_email": "lee@tenant.test", "from_name": "Lee",
                             "subject": "Pricing question", "received_at": AGO(30),
                             "body": "Your number came in higher than the other bid. Can you sharpen the price?"}],
         "notes": []},
        # Pure acknowledgement -> rules clear it, no model needed
        {"id": "proj_c", "name": "Riverside Annex", "status": "waiting", "workflow_id": "general",
         "key_details": {}, "contacts": [],
         "subtasks": [{"id": "st_3", "text": "Respond to Dana", "status": "pending", "date_type": "follow_up",
                       "created": AGO(50), "follow_up_date": (NOW + timedelta(days=3)).date().isoformat()}],
         "correspondence": [{"id": "c3", "direction": "inbound", "from_email": "dana@client.test", "subject": "RE: schedule",
                             "received_at": AGO(50), "body": "Thanks!"}],
         "notes": []},
        # Idle, no pending task
        {"id": "proj_d", "name": "Idle Thing", "status": "waiting", "workflow_id": "general", "key_details": {},
         "subtasks": [], "notes": []},
    ]


def _cards():
    return [
        {"triage_id": "msg_1", "card_type": "email", "direction": "inbound", "from_email": "sam@client.test",
         "subject": "AB-10234 drawings", "body_raw": "See ref AB-10234, drawings attached.", "received_at": AGO(2),
         "conversation_id": "conv_new1", "project_matches": [], "signals": {}},
        {"triage_id": "msg_2", "card_type": "email", "direction": "inbound", "from_email": "shared@client.test",
         "subject": "Quick question", "body_raw": "Hey, following up on that thing we discussed. " * 3,
         "received_at": AGO(3), "conversation_id": "conv_new2",
         "project_matches": [{"id": "proj_a"}, {"id": "proj_b"}], "signals": {}},
        {"triage_id": "msg_3", "card_type": "email", "direction": "inbound", "from_email": "someone@elsewhere.test",
         "subject": "Automatic reply: out of office", "body_raw": "I am out until next week.", "received_at": AGO(4),
         "conversation_id": "conv_new3", "project_matches": [], "signals": {}},
        {"triage_id": "msg_4", "card_type": "email", "direction": "inbound", "from_email": "stranger@nowhere.test",
         "subject": "Hello", "body_raw": "Can you help with a thing at our building sometime?", "received_at": AGO(1),
         "conversation_id": "conv_new4", "project_matches": [], "signals": {}},
    ]


def _llm(store, cfg, responses):
    prov = FakeLLMProvider(responses)
    return LLM(prov, UsageMeter(store, 5.0), cfg, PROFESSION), prov


RESPONSES = {
    "Task: triage.match_rerank": {"project_id": None, "confidence": 0.4, "ambiguous": True, "evidence": ""},
    "Task: project.health": {"health": "warm", "recommendation": "Reply to the latest message", "priority": 3},
    "Task: reply.no_reply_classifier": lambda s, u: (
        {"decision": "draft", "reason": "ack the sender is waiting on"} if "Revised drawings" in u
        else {"decision": "ask", "reason": "price challenge",
              "question_for_user": "Lee says the price is high. Hold, sharpen, or explain?",
              "directions": ["Hold the price", "Offer a revised number", "Explain the scope difference"]}),
    "Task: reply.draft": {"body": "Hi Sam,\nYes, the revised drawings for the lobby came through. "
                                  "Our margin on this is 31% and the price is $12,450. "
                                  "I will come back to you by 3/14 with any questions.\nBest regards,\nPat Example"},
}


def test_full_pass(tmp_path):
    store = FakeStore(projects=_projects(), triage=_cards(),
                      ids={"AB-10234": ["proj_a"], "CD-55555": ["proj_b"]},
                      contacts={"sam@client.test": ["proj_a"], "lee@tenant.test": ["proj_b"],
                                "shared@client.test": ["proj_a", "proj_b"]})
    cfg = _cfg(tmp_path)
    llm, prov = _llm(store, cfg, RESPONSES)
    ap = AssistantPass(store, cfg, PROFESSION, llm, mail_provider=None)
    summary = ap.run(max_items=10)

    # --- triage ---------------------------------------------------------
    assert store.triage["msg_1"]["status"] == "approved" and store.triage["msg_1"]["matched_project"] == "proj_a"
    assert store.triage["msg_2"]["status"] == "pending"          # ambiguous, rerank abstained
    assert store.triage["msg_3"]["status"] == "dismissed"        # OOO on unrouted thread
    assert store.triage["msg_4"]["status"] == "pending"          # no project, auto_create off
    left = summary["triage"]["left"]
    assert left == {"multi_project": 1, "no_project": 1}
    assert summary["triage"]["approved"] == 1 and summary["triage"]["dismissed"] == 1
    tri_items = [i for i in summary["items"] if i["item_type"] == "triage"]
    assert len(tri_items) == 4                                    # one verdict per card
    assert any(i["result"] == "left: multi_project" and "proj_a" in i["evidence"] for i in tri_items)

    # --- board ----------------------------------------------------------
    assert [p["id"] for p in board_order(store.active_projects(), PROFESSION)] == ["proj_a", "proj_b", "proj_c", "proj_d"]

    # proj_a: a draft was created, scrubbed, task still open, nothing sent
    assert len(store.drafts) == 1
    d = store.drafts[0]
    assert d["pid"] == "proj_a" and d["to"] == ["sam@client.test"] and d["subject"] == "RE: Revised drawings"
    assert d["sent"] is False
    assert "revised drawings" in d["body"].lower()
    assert "31%" not in d["body"] and "12,450" not in d["body"]      # non-whitelisted fields scrubbed
    assert "3/14" not in d["body"]                                   # date not in thread: grounded out
    assert "Best regards" not in d["body"] and "Pat Example" not in d["body"] and not d["body"].startswith("Hi")
    assert store.projects["proj_a"]["subtasks"][0]["status"] == "pending"   # stays open until the user sends
    assert any("DRAFT (unsent)" in n["text"] for n in store.notes if n["pid"] == "proj_a")

    # proj_b: judgment -> one question with directions
    qs = store.open_questions("proj_b")
    assert len(qs) == 1 and "Lee" in qs[0]["question"] and len(qs[0]["options"]) == 3
    assert qs[0]["options"][0]["label"] == "Hold the price"

    # proj_c: rules cleared the acknowledgement, logged with the classification
    assert store.projects["proj_c"]["subtasks"][0]["status"] == "complete"
    assert store.projects["proj_c"]["subtasks"][0]["completed_by"] == "assistant"
    assert any("no reply needed" in n["text"] for n in store.notes if n["pid"] == "proj_c")

    # hard rules: no status change, no money write, nothing sent
    assert not any(c[0] == "update_project" for c in store.calls)
    for pid, before in zip(("proj_a", "proj_b", "proj_c", "proj_d"), _projects()):
        assert store.projects[pid]["status"] == before["status"]
        assert store.projects[pid].get("quote_amount") == before.get("quote_amount")
    assert summary["nothing_sent"] is True

    # every item has an outcome; counts add up
    assert all(i["outcome"] in ("acted", "drafted", "question_posted", "subtask_added", "deferred") for i in summary["items"])
    assert summary["counts"]["drafted"] == 1 and summary["counts"]["question_posted"] == 1
    assert summary["counts"]["acted"] >= 3       # approve, dismiss, clear
    assert sum(summary["counts"].values()) == len(summary["items"])

    # logs: store + JSONL
    assert any(r["outcome"] == "drafted" and r["pid"] == "proj_a" for r in store.assist_log)
    files = list((tmp_path / "logs").glob("assistant_*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(l) for l in files[0].read_text().splitlines()]
    assert any(r["outcome"] == "drafted" and r.get("draft_v0") for r in rows)
    assert all(r["run_id"] == summary["run_id"] for r in rows)


def test_second_pass_dedupes_draft_and_respects_open_question(tmp_path):
    store = FakeStore(projects=_projects(), triage=[], ids={}, contacts={})
    cfg = _cfg(tmp_path)
    llm, _ = _llm(store, cfg, RESPONSES)
    AssistantPass(store, cfg, PROFESSION, llm).run(10)
    s2 = AssistantPass(store, cfg, PROFESSION, llm).run(10)
    assert len(store.drafts) == 1                       # not rebuilt
    assert len(store.open_questions("proj_b")) == 1     # one open question per project
    reasons = {i.get("reason") for i in s2["items"]}
    assert {"draft_exists", "question_open"} <= reasons


def test_rules_only_without_model(tmp_path):
    store = FakeStore(projects=_projects(), triage=_cards(), ids={"AB-10234": ["proj_a"]}, contacts={})
    cfg = _cfg(tmp_path)
    summary = AssistantPass(store, cfg, PROFESSION, llm=None).run(10)
    assert store.triage["msg_1"]["status"] == "approved"            # unique id match needs no model
    assert store.triage["msg_2"]["status"] == "pending"
    assert store.drafts == []                                       # no model, no draft
    assert store.projects["proj_c"]["subtasks"][0]["status"] == "complete"
    assert len(store.open_questions("proj_a")) == 1                 # judgment falls to the user
    assert summary["llm_used"] is False


def test_rerank_approves_only_above_bar(tmp_path):
    cards = [c for c in _cards() if c["triage_id"] == "msg_2"]
    store = FakeStore(projects=_projects(), triage=cards, contacts={"shared@client.test": ["proj_a", "proj_b"]})
    cfg = _cfg(tmp_path)
    llm, _ = _llm(store, cfg, {"Task: triage.match_rerank": {"project_id": "proj_b", "confidence": 0.93,
                                                              "ambiguous": False, "evidence": "Suite 200"}})
    AssistantPass(store, cfg, PROFESSION, llm).run(1)
    assert store.triage["msg_2"]["matched_project"] == "proj_b"
    # an id outside the candidate list is never accepted
    store2 = FakeStore(projects=_projects(), triage=cards, contacts={"shared@client.test": ["proj_a", "proj_b"]})
    llm2, _ = _llm(store2, cfg, {"Task: triage.match_rerank": {"project_id": "proj_zzz", "confidence": 0.99, "ambiguous": False}})
    AssistantPass(store2, cfg, PROFESSION, llm2).run(1)
    assert store2.triage["msg_2"]["status"] == "pending"


def test_mail_provider_draft_path(tmp_path):
    class Mail:
        def __init__(self):
            self.drafts = []

        def create_draft(self, reply_to, to, cc, subject, body_html, attachments=()):
            self.drafts.append((to, subject, body_html))
            return "draft-123"

    store = FakeStore(projects=_projects()[:1])
    cfg = _cfg(tmp_path, drafts_to="provider")
    llm, _ = _llm(store, cfg, RESPONSES)
    mail = Mail()
    s = AssistantPass(store, cfg, PROFESSION, llm, mail_provider=mail).run(5)
    assert len(mail.drafts) == 1 and store.drafts == []
    assert s["items"][0]["result"].startswith("draft provider:draft-123")


def test_visible_fields_enforcement_refuses_when_nothing_left(tmp_path):
    store = FakeStore(projects=_projects()[:1])
    ap = AssistantPass(store, _cfg(tmp_path), PROFESSION, None)
    text, removed = ap.enforce_visible_fields("Our margin is 31% on this one.", store.get_project("proj_a"))
    assert text == "" and removed == 1
    text, removed = ap.enforce_visible_fields("Harbor Plaza lobby refresh is on track.", store.get_project("proj_a"))
    assert removed == 0 and text


def test_scheduler_run_once_and_status(tmp_path):
    store = FakeStore(projects=_projects()[2:3])
    ext = {"store": store, "cfg": _cfg(tmp_path), "profession": PROFESSION, "llm": None, "mail": None}
    sched = start_assistant_thread(ext, interval_minutes=60)
    assert isinstance(ext["assistant"], AssistantScheduler) and sched.thread.is_alive()
    summary = sched.run_once(5)
    assert summary and ext["assistant_status"]["last_run"] == summary["finished"]
    assert ext["assistant_status"]["running"] is False
    sched.stop()
