from copy import deepcopy

from plannerboard import profession, workflows as wfm
from plannerboard.business_hours import BusinessHours


def hvac_doc():
    return wfm.normalize(deepcopy(profession.load_template("hvac_contractor")["workflows"]))


# The ladder the reference install carried as a constant. The generic template
# must derive exactly this table.
REFERENCE_LADDER = [
    ("job_setup", "Set up job", "parts_needed", "setup", 4, None),
    ("parts_needed", "Order parts", "awaiting_parts", "ordering", 4, None),
    ("awaiting_parts", "Confirm parts received", "ready_to_schedule", "ordering", None, 7),
    ("ready_to_schedule", "Schedule job", "scheduled", "scheduling", None, 3),
]


def test_hvac_ladder_matches_reference():
    doc = hvac_doc()
    assert wfm.validate(doc, {"quote"}) == []
    assert wfm.ladder_rows(doc, "quote") == REFERENCE_LADDER
    assert wfm.entry_stage(doc, "quote") == "quote_needed"
    assert wfm.entry_stage(doc, "service_call") == "call_in"
    assert wfm.entry_stage(doc, "general") == "action_required"
    assert wfm.won_target(doc, "quote") == "job_setup"
    assert wfm.won_revert_target(doc, "quote") == "quote_sent"
    armed = wfm.auto_arm_stage_ids(doc, wfm.get_workflow(doc, "quote"))
    assert armed == {"quote_needed", "job_setup", "parts_needed", "awaiting_parts", "ready_to_schedule"}
    assert "quote_sent" not in armed


def test_roles_and_helpers():
    doc = hvac_doc()
    assert wfm.role_of(doc, "quote_needed", "quote") == "todo"
    assert wfm.role_of(doc, "quote_sent", "quote") == "out"
    assert wfm.role_of(doc, "job_setup", "quote") == "won"
    assert wfm.role_of(doc, "scheduled", "quote") == "in_progress"
    assert wfm.is_closed(doc, "complete") and wfm.is_closed(doc, "ignored")
    assert not wfm.is_closed(doc, "cold")
    assert wfm.stages_with_role(doc, "out", "quote") == ["quote_sent"]
    assert "call_in" in wfm.stages_with_role(doc, "todo")
    assert wfm.quote_task_for(doc, "quote")[0] == "quote_needed"
    assert wfm.quote_task_for(doc, "service_call") == (None, None)
    assert wfm.rung_for_text(doc, "order parts", "quote") == "parts_needed"
    assert wfm.rung_for_text(doc, "Prepare and send proposal", "quote") is None
    assert wfm.closed_stage_ids(doc) == {"complete", "ignored"}


def test_normalize_infers_roles_and_renames_sold_transition():
    doc = {"workflows": [{"id": "w", "label": "W", "stages": [
        {"id": "a", "label": "A", "column": True},
        {"id": "quote_sent", "label": "Q", "column": True},
        {"id": "complete", "label": "C", "closed": True},
        {"id": "ignored", "label": "I", "closed": True}],
        "entry_stage": "a", "sold_transition": {"from": "quote_sent", "to": "a"}}]}
    wfm.normalize(doc)
    st = {s["id"]: s for s in doc["workflows"][0]["stages"]}
    assert st["a"]["role"] == "todo" and st["quote_sent"]["role"] == "out"
    assert st["complete"]["role"] == "closed" and st["ignored"]["role"] == "ignored"
    assert doc["workflows"][0]["won_transition"] == {"from": "quote_sent", "to": "a"}
    assert "sold_transition" not in doc["workflows"][0]
    assert wfm.validate(doc) == []


def test_validate_catches_breakage():
    doc = hvac_doc()
    bad = deepcopy(doc)
    bad["workflows"][0]["stages"][2]["advance_on_task_complete"] = "nowhere"
    assert any("nowhere" in e for e in wfm.validate(bad, {"quote"}))
    bad = deepcopy(doc)
    bad["workflows"][0]["stages"][0]["role"] = "sideways"
    assert any("role" in e for e in wfm.validate(bad, {"quote"}))
    bad = deepcopy(doc)
    bad["workflows"][0]["entry_stage"] = "missing"
    assert any("entry_stage" in e for e in wfm.validate(bad, {"quote"}))
    bad = deepcopy(doc)
    bad["workflows"][0]["stages"][2]["owning_task"] = {"text": "x", "date_type": "sla"}
    assert any("sla_hours" in e for e in wfm.validate(bad, {"quote"}))
    assert wfm.validate({"workflows": []})
    # Unknown date type is rejected unless the profession declares it.
    bad = deepcopy(doc)
    bad["workflows"][0]["stages"][2]["owning_task"] = {"text": "x", "date_type": "court_date"}
    assert wfm.validate(bad, {"quote"})
    assert wfm.validate(bad, {"quote", "court_date"}) == []


def test_workflow_store_seed_save_backup(tmp_path):
    path = tmp_path / "workflows.json"
    ws = wfm.WorkflowStore(path, tmp_path / "bk", seed=profession.load_template("general")["workflows"])
    doc = ws.load()
    assert path.exists()
    assert [w["id"] for w in doc["workflows"]] == ["general"]
    assert doc["workflows"][0]["stages"][0]["role"] == "todo"
    new = deepcopy(doc)
    new["workflows"][0]["stages"].insert(1, {"id": "review", "label": "Review", "role": "in_progress", "column": True})
    ok, errs, bpath = ws.save(new)
    assert ok and not errs and bpath and (tmp_path / "bk").exists()
    assert "review" in wfm.all_stage_ids(ws.load(force=True))
    ok, errs, _ = ws.save({"workflows": [{"id": "BAD ID", "label": "", "stages": []}]})
    assert not ok and errs
    path.write_text("{garbage", encoding="utf-8")
    doc = ws.load(force=True)
    assert ws.last_error and doc["workflows"]


def test_business_hours_math():
    bh = BusinessHours("America/Denver", 8, 17, [0, 1, 2, 3, 4])
    # Friday 16:00 + 4 business hours -> Monday 11:00 (1h Fri, 3h Mon)
    assert bh.add_biz_hours("2026-10-02T16:00:00", 4) == "2026-10-05T11:00:00-06:00"
    # Saturday clamps to Monday open
    assert bh.add_biz_hours("2026-10-03T10:00:00", 1) == "2026-10-05T09:00:00-06:00"
    assert bh.elapsed_biz_hours("2026-10-02T16:00:00", "2026-10-05T11:00:00") == 4.0
    assert bh.elapsed_biz_hours("2026-10-05T07:00:00", "2026-10-05T08:30:00") == 0.5
    assert bh.add_biz_days("2026-10-02", 1) == "2026-10-05"
    assert bh.add_biz_days("2026-10-05", 3) == "2026-10-08"
    assert bh.urgency_score("2026-09-21T16:00:00", 4) >= 50      # long overdue (two weeks back)
    # UTC instants convert into the business zone before clamping.
    assert bh.elapsed_biz_hours("2026-10-05T14:00:00Z", "2026-10-05T16:00:00Z") == 2.0
    custom = BusinessHours("Europe/London", 9, 18, [0, 1, 2, 3, 4, 5])
    assert custom.is_business_day(__import__("datetime").date(2026, 10, 3))   # Saturday open
    assert BusinessHours.from_profession({"business_hours": {"tz": "Asia/Tokyo", "start": 10, "end": 19}}).tz_name == "Asia/Tokyo"
