"""Prompt registry: load, render, validate, budget/feature fallbacks."""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from tests.fakes_ai import FakeLLMProvider, FakeStore  # noqa: E402
from plannerboard.ai.base import NullProvider  # noqa: E402
from plannerboard.ai.registry import (LLM, build_profile_vars, load_prompts, render,  # noqa: E402
                                      template_placeholders)
from plannerboard.ai.usage import UsageMeter  # noqa: E402

EXPECTED_IDS = {
    "triage.classify", "triage.summarize_bullets", "triage.suggest_project", "triage.match_rerank",
    "reply.no_reply_classifier", "reply.draft", "followup.draft", "project.health", "search.parse",
    "digest.weekly", "assistant.board_pass",
}

CFG = {"user": {"name": "Pat Example", "company": "Example Co", "role": "Account Manager",
                "internal_domains": ["example.test"]},
       "ai": {"provider": "fake", "daily_budget_usd": 1.0, "features": {"classify": True, "summarize": True,
                                                                        "match_rerank": True, "draft_reply": True,
                                                                        "assistant": True}}}
PROFESSION = {
    "entity": {"project": "Matter", "projects": "Matters", "client": "Client", "quote": "Engagement letter"},
    "triage": {"classifications": ["request", "client_reply", "junk", "informational"]},
    "vocab": {"keywords": ["retainer", "filing"], "acronyms": ["NDA", "LOI"]},
    "prompt_profile": {"industry": "legal services", "role_noun": "attorney"},
    "work_types": [{"id": "matter", "label": "Matter"}],
    "workflows": {"workflows": [{"id": "general", "stages": [{"id": "action_required"}, {"id": "waiting"}]}]},
}


def _llm(provider, budget=1.0, cfg=CFG):
    store = FakeStore()
    return LLM(provider, UsageMeter(store, budget), cfg, PROFESSION), store


def test_all_shipped_prompts_load_and_only_use_known_vars():
    reg = load_prompts()
    assert set(reg) == EXPECTED_IDS
    known = set(build_profile_vars(CFG, PROFESSION)) | {
        # per-call vars the code passes
        "direction", "from_name", "from_email", "to", "subject", "project_name", "project_status", "body",
        "attachment_names", "candidates", "received_at", "visible_facts", "open_tasks", "direction_note",
        "customer_name", "work_description", "days_since_sent", "last_communication", "attempt_number",
        "created", "follow_up_date", "deadline", "emails_sent", "pending_count", "pending_tasks",
        "recent_notes", "today", "query", "stats_block", "item_text", "item_type", "item_age",
        "allowed_outcomes", "inbound_summary", "open_question", "source_text",
    }
    for spec in reg.values():
        assert spec.system.startswith(f"Task: {spec.id}."), spec.id
        unknown = template_placeholders(spec) - known
        assert not unknown, f"{spec.id} uses unknown vars {unknown}"
        for v in spec.post:
            assert v.split(":")[0] in ("json", "labels", "grounding", "max_words")


def test_profile_vars_from_cfg_and_profession():
    v = build_profile_vars(CFG, PROFESSION)
    assert v["user_name"] == "Pat Example" and v["company"] == "Example Co"
    assert v["role"] == "Account Manager" and v["industry"] == "legal services"
    assert v["entity_project"] == "Matter" and v["entity_client"] == "Client"
    assert v["classifications"] == "request, client_reply, junk, informational"
    assert v["acronyms"] == "NDA, LOI" and v["internal_domains"] == "example.test"
    assert render("Hello {user_name} at {company}, {missing}!", v) == "Hello Pat Example at Example Co, !"


def test_run_renders_validates_labels_and_records_usage():
    prov = FakeLLMProvider({"Task: triage.classify": {"classification": "Request", "needs_reply": True,
                                                      "follow_up_days": 2}})
    llm, store = _llm(prov)
    out = llm.run("triage.classify", {"direction": "inbound", "from_name": "Sam", "from_email": "sam@c.test",
                                      "to": "pat@example.test", "subject": "Retainer question", "body": "Can we talk?"})
    assert out is not None
    value, res = out
    assert value["classification"] == "request"       # case repaired against the allowed list
    sent = prov.calls[0]
    assert "Matter" in sent["user"] and "example.test" in sent["system"]
    assert "Retainer question" in sent["user"] and sent["json_schema"]["type"] == "object"
    assert sent["max_tokens"] == 200 and sent["temperature"] == 0.1
    assert store.usage[0]["task"] == "triage.classify" and store.usage[0]["provider"] == "fake"
    assert res.ok


def test_invalid_label_rejected_and_grounding_validator_runs():
    prov = FakeLLMProvider({
        "Task: triage.classify": {"classification": "spam", "needs_reply": False, "follow_up_days": 0},
        "Task: triage.summarize_bullets": {"bullets": ["Quote requested for the lobby unit",
                                                       "Amount approved: $9,999"]},
    })
    llm, _ = _llm(prov)
    assert llm.run("triage.classify", {"body": "x"}) is None
    assert "rejected" in llm.last_error
    out = llm.run("triage.summarize_bullets", {"subject": "Lobby unit", "body": "Quote requested for the lobby unit",
                                               "attachment_names": "", "source_text": "Lobby unit\nQuote requested for the lobby unit"})
    assert out[0]["bullets"] == ["Quote requested for the lobby unit"]


def test_text_prompt_max_words_and_json_text_fallback():
    long = " ".join(["word"] * 400)
    prov = FakeLLMProvider({"Task: digest.weekly": long,
                            "Task: search.parse": 'Here you go: {"name_contains": "lobby", "overdue_only": false, "sort_by": "urgency"}'})
    llm, _ = _llm(prov)
    value, _ = llm.run("digest.weekly", {"stats_block": "- a: 1"})
    assert len(value.split()) == 160
    value, _ = llm.run("search.parse", {"query": "lobby"})
    assert value["name_contains"] == "lobby"


def test_fallbacks_return_none():
    # provider off
    llm, store = _llm(NullProvider())
    assert llm.run("triage.classify", {}) is None and not llm.available and store.usage == []
    # feature disabled
    cfg = {**CFG, "ai": {**CFG["ai"], "features": {"classify": False}}}
    llm, _ = _llm(FakeLLMProvider({"Task: triage.classify": {"classification": "request", "needs_reply": False, "follow_up_days": 0},
                                   "Task: project.health": {"health": "warm", "recommendation": "x", "priority": 3}}), cfg=cfg)
    assert llm.run("triage.classify", {}) is None
    assert llm.run("project.health", {}) is not None   # not gated by that feature
    # budget hit
    prov = FakeLLMProvider({"Task:": {"health": "warm", "recommendation": "x", "priority": 3}}, in_tokens=0, out_tokens=0)
    llm, store = _llm(prov, budget=0.001)
    store._usage_today_override = {"cost_usd": 5.0, "calls": 3, "by_task": {}}
    assert llm.run("project.health", {}) is None and llm.budget_hit and not llm.available
    assert llm.status()["budget_hit"] is True
    # model error
    llm, _ = _llm(FakeLLMProvider({}))
    assert llm.run("project.health", {}) is None
    # unknown prompt
    assert llm.run("nope.nothing", {}) is None
