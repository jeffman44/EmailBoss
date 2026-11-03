from plannerboard import profession, workflows as wfm

EXPECTED = {"general", "hvac_contractor", "construction_gc", "property_management", "legal", "real_estate",
            "insurance_agency", "consulting", "it_msp", "recruiting"}


def test_all_templates_ship_and_validate():
    ids = set(profession.template_ids())
    assert EXPECTED <= ids
    for tid in EXPECTED:
        doc = profession.load_template(tid)
        assert profession.validate_template(doc) == [], tid
        for key in profession.REQUIRED_KEYS:
            assert key in doc, (tid, key)
        wdoc = wfm.normalize(doc["workflows"])
        assert wfm.validate(wdoc, profession.date_type_ids(doc)) == [], tid
        for wf in wdoc["workflows"]:
            for st in wf["stages"]:
                assert st["role"] in wfm.ROLES, (tid, wf["id"], st["id"])
            assert wfm.entry_stage(wdoc, wf["id"]) in [s["id"] for s in wf["stages"]]
        assert doc["triage"]["classifications"]
        assert doc["business_hours"]["tz"]
        assert doc["prompt_profile"]["industry"]
        assert doc["entity"]["project"] and doc["entity"]["client"]


def test_effective_merges_overrides_and_adds_user_features():
    prof = profession.effective("hvac_contractor", {"entity": {"client": "Owner"}, "sla_hours": 2,
                                                    "vocab": {"keywords": ["custom"]}},
                                user={"name": "Pat"}, features={"money": False})
    assert prof["entity"]["client"] == "Owner" and prof["entity"]["project"] == "Job"
    assert prof["sla_hours"] == 2
    assert prof["vocab"]["keywords"] == ["custom"]                  # lists replace
    assert prof["user"] == {"name": "Pat"} and prof["features"]["money"] is False
    assert "work_order" in prof["customer_visible_fields"]
    assert profession.customer_visible_fields(prof) == prof["customer_visible_fields"]


def test_effective_falls_back_to_general():
    prof = profession.effective("does_not_exist", {})
    assert prof["id"] == "general" and prof.get("template_error")


def test_hvac_template_is_generic_and_complete():
    doc = profession.load_template("hvac_contractor")
    wf_ids = [w["id"] for w in doc["workflows"]["workflows"]]
    assert wf_ids == ["quote", "service_call", "general"]
    wt_targets = {w["workflow_id"] for w in doc["work_types"]}
    assert wt_targets == {"quote", "service_call", "general"}
    assert {d["id"] for d in doc["date_types"]} >= {"follow_up", "deadline", "sla", "quote"}
    tabs = [t["id"] for t in doc["workflows"]["tabs"]]
    assert "deadlines" in tabs and "quotes" in tabs
    assert doc["workflows"]["sort"]["priority"][-1]["match"] == {}
    import json
    blob = json.dumps(doc)
    for forbidden in ("QRF", "SharePoint", "Jeff", "amsofusa"):
        assert forbidden not in blob
