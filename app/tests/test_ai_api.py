"""/api/ai/* and /api/assistant/* blueprints on a bare Flask app + FakeStore."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from flask import Flask

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from tests.fakes_ai import FakeLLMProvider, FakeStore, import_api_module  # noqa: E402
from plannerboard.ai.registry import LLM  # noqa: E402
from plannerboard.ai.usage import UsageMeter  # noqa: E402

ai_api = import_api_module("ai_api")
assistant_api = import_api_module("assistant_api")
from tests.test_ai_assistant import PROFESSION, _cfg, _projects  # noqa: E402

RESPONSES = {
    "Task: reply.draft": {"body": "Yes, the revised drawings came through. I will review them."},
    "Task: followup.draft": {"body": "Checking in on the lobby refresh proposal. Any questions on the scope?"},
    "Task: triage.suggest_project": {"name": "Harbor Plaza Lobby Refresh", "work_type": "task",
                                     "scope": "x", "is_request": True},
    "Task: project.health": {"health": "stale", "recommendation": "Call Sam", "reason": "quiet", "priority": "2"},
    "Task: search.parse": {"name_contains": "Harbor", "overdue_only": False, "sort_by": "amount"},
    "Task: digest.weekly": "A steady week. Two items moved and nothing is overdue.\n\nFocus next on the open replies.",
}


def _app(tmp_path, llm_on=True, assistant_on=False):
    store = FakeStore(projects=_projects())
    cfg = _cfg(tmp_path, assistant_on=assistant_on)
    cfg["features"] = {"money": True}
    prov = FakeLLMProvider(RESPONSES, available=llm_on)
    llm = LLM(prov, UsageMeter(store, 5.0), cfg, PROFESSION)
    app = Flask("t")
    app.extensions["pb"] = {"store": store, "cfg": cfg, "profession": PROFESSION, "llm": llm, "mail": None,
                            "secret_getter": lambda k: ""}
    app.register_blueprint(ai_api.bp)
    app.register_blueprint(assistant_api.bp)
    return app, store, prov


def test_providers_and_usage(tmp_path):
    app, store, _ = _app(tmp_path)
    c = app.test_client()
    r = c.get("/api/ai/providers").get_json()
    assert r["ok"] and {p["id"] for p in r["providers"]} == {"emailboss", "none", "anthropic", "openai", "ollama"}
    anth = next(p for p in r["providers"] if p["id"] == "anthropic")
    assert anth["required"] == ["api_key"] and "claude-sonnet-4-5" in anth["models"]
    assert r["current"]["provider"] == "fake" and "reply.draft" in r["prompts"]
    u = c.get("/api/ai/usage").get_json()
    assert u["ok"] and u["today"] == {"cost_usd": 0.0, "calls": 0, "by_task": {}}
    assert u["budget_usd"] == 5.0 and u["budget_hit"] is False and u["provider"] == "fake"


def test_ai_test_endpoint_without_key(tmp_path):
    app, _, _ = _app(tmp_path)
    r = app.test_client().post("/api/ai/test", json={"provider": "anthropic"}).get_json()
    assert r["ok"] is False and r["provider"] == "none" and "key" in r["error"].lower()
    r = app.test_client().post("/api/ai/test", json={"provider": "none"}).get_json()
    assert r["ok"] is False


def test_offline_returns_503_with_v2_shape(tmp_path):
    app, _, _ = _app(tmp_path, llm_on=False)
    c = app.test_client()
    for path in ("/api/ai/draft-reply", "/api/ai/draft-follow-up", "/api/ai/suggest-job-name",
                 "/api/ai/project-health", "/api/ai/project-health/batch"):
        resp = c.post(path, json={})
        assert resp.status_code == 503 and resp.get_json() == {"ok": False, "error": "AI provider is offline"}
    # the weekly digest falls back to the engine's plain-text digest
    resp = c.post("/api/ai/weekly-digest", json={})
    j = resp.get_json()
    assert resp.status_code == 200 and j["ok"] and j["ai_used"] is False and j["text"] == j["digest"] and j["text"]
    # search degrades to plain text search instead of failing
    r = c.post("/api/ai/search", json={"query": "harbor"}).get_json()
    assert r["ok"] and r["filters"] == {"name_contains": "harbor"} and r["count"] == 1


def test_draft_reply_and_follow_up_shapes(tmp_path):
    app, store, prov = _app(tmp_path)
    c = app.test_client()
    r = c.post("/api/ai/draft-reply", json={"subject": "Revised drawings", "from_name": "Sam",
                                           "body_preview": "Attached are the revised drawings."}).get_json()
    assert r["ok"] and r["draft"].startswith("Yes, the revised drawings")
    r = c.post("/api/ai/draft-follow-up", json={"project_id": "proj_a", "attempt_number": 1}).get_json()
    assert r["ok"] and "lobby refresh" in r["draft"].lower()
    sent = prov.calls[-1]["user"]
    assert "Harbor Plaza Lobby Refresh" in sent and "Sam Client" in sent and "12,450" in sent
    r = c.post("/api/ai/suggest-job-name", json={"equipment_type": "lobby", "repair": "refresh"}).get_json()
    assert r == {"ok": True, "job_name": "Harbor Plaza Lobby Refresh"}
    r = c.post("/api/ai/suggest-rename", json={"project_id": "proj_a"}).get_json()
    assert r == {"ok": True, "suggestion": "Harbor Plaza Lobby Refresh"}


def test_project_health_shapes(tmp_path):
    app, _, _ = _app(tmp_path)
    c = app.test_client()
    r = c.post("/api/ai/project-health", json={"project_id": "proj_a"}).get_json()
    assert r == {"ok": True, "assessment": {"health": "stale", "recommendation": "Call Sam", "reason": "quiet", "priority": 2}}
    assert c.post("/api/ai/project-health", json={"project_id": "nope"}).status_code == 404
    r = c.post("/api/ai/project-health/batch", json={"project_ids": ["proj_a", "proj_b"]}).get_json()
    assert r["ok"] and r["count"] == 2 and set(r["assessments"]) == {"proj_a", "proj_b"}
    r = c.post("/api/ai/project-health/batch", json={}).get_json()
    assert r["count"] == 4


def test_search_and_digest(tmp_path):
    app, _, _ = _app(tmp_path)
    c = app.test_client()
    assert c.post("/api/ai/search", json={}).status_code == 400
    r = c.post("/api/ai/search", json={"query": "harbor jobs by amount"}).get_json()
    assert r["ok"] and r["query"] == "harbor jobs by amount" and r["filters"]["sort_by"] == "amount"
    assert r["count"] == 1 and r["results"][0]["id"] == "proj_a" and "subtasks" not in r["results"][0]
    r = c.post("/api/ai/weekly-digest", json={}).get_json()
    assert r["ok"] and r["digest"].startswith("A steady week")
    assert set(r["stats"]) == {"quotes_sent", "approvals", "new_projects", "overdue_count", "total_pipeline",
                               "won_ytd", "win_rate", "top_opportunity", "stale_count"}
    assert r["stats"]["total_pipeline"] == 12450.0


def test_assistant_endpoints(tmp_path):
    app, store, _ = _app(tmp_path, assistant_on=True)
    c = app.test_client()
    st = c.get("/api/assistant/status").get_json()
    assert st["ok"] and st["enabled"] is True and st["running"] is False and st["drafts_only"] is True
    assert st["last_run"] is None and st["schedule_minutes"] == 60
    assert c.post("/api/assistant/run", json={"max_items": "x"}).status_code == 400
    r = c.post("/api/assistant/run", json={"max_items": 3}).get_json()
    assert r["ok"] and r["summary"]["max_items"] == 3 and r["summary"]["nothing_sent"] is True
    assert sum(r["summary"]["counts"].values()) == len(r["summary"]["items"]) >= 3
    st = c.get("/api/assistant/status").get_json()
    assert st["last_run"] == r["summary"]["finished"] and "items" not in st["last_summary"]
    assert st["last_summary"]["item_count"] == len(r["summary"]["items"])
    lg = c.get("/api/assistant/log?limit=50").get_json()
    assert lg["ok"] and lg["count"] >= 3 and all(e["run_id"] == r["summary"]["run_id"] for e in lg["entries"])
    lg = c.get("/api/assistant/log?outcome=question_posted").get_json()
    assert all(e["outcome"] == "question_posted" for e in lg["entries"])
    # a concurrent run is refused
    app.extensions["pb"]["assistant_status"]["running"] = True
    assert c.post("/api/assistant/run", json={}).status_code == 409


def test_ui_shapes_for_search_digest_and_usage(tmp_path):
    """The board renders {text, items[]} plainly; usage carries this month."""
    app, _, _ = _app(tmp_path)
    c = app.test_client()
    r = c.post("/api/ai/search", json={"query": "harbor jobs by amount"}).get_json()
    assert r["text"] == "Found 1 match." and r["items"][0]["project_id"] == "proj_a"
    assert set(r["items"][0]) == {"id", "name", "why", "project_id", "label", "detail"}
    assert r["items"][0]["id"] == "proj_a" and r["items"][0]["name"] and r["items"][0]["why"]
    r = c.post("/api/ai/weekly-digest", json={}).get_json()
    assert r["text"] == r["digest"] and r["text"].startswith("A steady week")
    labels = [i["label"] for i in r["items"]]
    assert any(l.startswith("Overdue: ") for l in labels) and all(isinstance(l, str) for l in labels)
    u = c.get("/api/ai/usage").get_json()
    assert u["month"]["calls"] >= 0 and "cost_usd" in u["month"]


def test_search_without_model_says_so(tmp_path):
    app, _, _ = _app(tmp_path, llm_on=False)
    r = app.test_client().post("/api/ai/search", json={"query": "zzz-nothing"}).get_json()
    assert r["ok"] and r["items"] == [] and r["text"] == "Nothing on the board matches that."


def test_assistant_run_reports_a_sentence(tmp_path):
    app, _, _ = _app(tmp_path, assistant_on=True)
    r = app.test_client().post("/api/assistant/run", json={"max_items": 3}).get_json()
    assert r["message"].endswith("Nothing was sent.") and "[object" not in r["message"]
    assert r["summary"]["message"] == r["message"]
    assert assistant_api.summary_sentence({"counts": {"drafted": 2, "question_posted": 1},
                                           "triage": {"approved": 1, "created": 0, "dismissed": 0, "left": {}}}) == \
        "Added 1 email to the board, wrote 2 drafts and asked you 1 question. Nothing was sent."
