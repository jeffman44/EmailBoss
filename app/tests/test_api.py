import json
from datetime import datetime, timedelta, timezone

from plannerboard import create_app
from plannerboard.config import Config
from tests.conftest import make_config


def test_setup_required_serves_wizard_and_setup_post(data_root):
    app = create_app(config=Config.load())
    c = app.test_client()
    assert b"setup" in c.get("/").data.lower()
    h = c.get("/api/health").json
    assert h["setup_required"] is True and h["status"] == "ok"
    assert {p["id"] for p in c.get("/api/professions").json["professions"]} >= {"general", "legal"}
    r = c.post("/api/setup", json={"name": "Pat", "email": "bad"})
    assert r.status_code == 400
    r = c.post("/api/setup", json={"name": "Pat Example", "email": "pat@example.com", "profession": "legal",
                                   "ai": {"provider": "ollama", "model": "qwen"}})
    assert r.status_code == 200, r.json
    assert r.json["status"] == "ok" and r.json["profession"] == "legal"
    assert (data_root / "plannerboard.json").exists()
    assert "api_token" not in (data_root / "plannerboard.json").read_text() or \
        json.loads((data_root / "plannerboard.json").read_text())["server"]["api_token"] == ""
    r = c.post("/api/setup", json={"name": "Pat Example", "email": "pat@example.com"})
    assert r.status_code == 409                       # never silently overwrite
    assert c.get("/api/config").json["setup_required"] is False
    prof = c.get("/api/profession").json
    assert prof["id"] == "legal" and prof["entity"]["project"] == "Matter"
    assert prof["user"]["name"] == "Pat Example"
    assert b"Email Boss" in c.get("/").data
    wf = c.get("/api/workflows").json
    assert "matter" in [w["id"] for w in wf["workflows"]["workflows"]]


def test_project_subtask_triage_health_flow(client):
    c = client
    r = c.post("/api/projects", json={"name": "Acme Plaza RTU-3", "work_type": "repair", "location": "Acme Plaza",
                                      "contacts": [{"name": "Sue", "email": "sue@acme.com"}]})
    assert r.status_code == 201
    proj = r.json["project"]
    pid = proj["id"]
    assert proj["status"] == "quote_needed" and proj["workflow_id"] == "quote"
    assert [s["text"] for s in proj["subtasks"]] == ["Prepare and send proposal"]
    board = c.get("/api/projects").json
    assert set(board) == {"meta", "projects", "archived"} and board["projects"][0]["id"] == pid
    assert c.get(f"/api/projects/{pid}").json["name"] == "Acme Plaza RTU-3"
    assert c.get("/api/projects/proj_999").status_code == 404

    # add + complete a subtask through the dedicated routes
    r = c.post(f"/api/projects/{pid}/subtasks", json={"text": "Measure the curb", "date_type": "deadline",
                                                      "follow_up_date": "2026-10-20"})
    sid = r.json["subtask_id"]
    assert sid
    open_ = c.get(f"/api/projects/{pid}/subtasks").json["subtasks"]
    assert any(s["id"] == sid and s["date_type"] == "deadline" for s in open_)
    tabs = c.get("/api/board-tabs").json["tabs"]
    dl = next(t for t in tabs if t["id"] == "deadlines")
    assert dl["count"] == 1 and dl["items"][0]["subtask_id"] == sid
    r = c.post(f"/api/subtasks/{sid}/complete", json={"project_id": pid, "completed_by": "manual"})
    assert r.json["status"] == "completed"
    assert not any(s["id"] == sid for s in c.get(f"/api/projects/{pid}/subtasks").json["subtasks"])

    # PUT: full-array subtasks, quote cascade, key_details merge, notes
    full = c.get(f"/api/projects/{pid}").json
    subs = full["subtasks"]
    for s in subs:
        if s["text"] == "Prepare and send proposal":
            s["status"] = "complete"
            s["completed_by"] = "manual"
    r = c.put(f"/api/projects/{pid}", json={"subtasks": subs, "key_details": {"po_number": "778899"},
                                            "notes": [{"text": "Sent it", "time": "now"}]})
    p = r.json["project"]
    assert p["status"] == "quote_sent" and p["quote_sent_date"]
    assert p["key_details"]["po_number"] == "778899"
    assert any(n["text"] == "Sent it" for n in p["notes"])
    # won cascade and ladder
    p = c.put(f"/api/projects/{pid}", json={"won_date": "2026-10-05"}).json["project"]
    assert p["status"] == "job_setup"
    assert [s["text"] for s in p["subtasks"] if s["status"] == "pending"] == ["Set up job"]
    for s in p["subtasks"]:
        if s["text"] == "Set up job":
            s["status"] = "complete"
            s["completed_by"] = "manual"
    p = c.put(f"/api/projects/{pid}", json={"subtasks": p["subtasks"]}).json["project"]
    assert p["status"] == "parts_needed"
    # un-won revert drops the ladder tasks
    p = c.put(f"/api/projects/{pid}", json={"won_date": None}).json["project"]
    assert p["status"] == "quote_sent" and not any(s["text"] == "Order parts" and s["status"] == "pending"
                                                   for s in p["subtasks"])
    assert c.put(f"/api/projects/{pid}", json={"status": "bogus"}).status_code == 400

    # triage: stage, list, approve as new, thread reply auto-commits
    card = {"card_type": "email", "direction": "inbound", "from_email": "bob@client.com", "from_name": "Bob Jones",
            "subject": "Please quote rooftop unit WO 123456", "body_preview": "Need a price",
            "received_at": (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), "message_id": "m1", "conversation_id": "conv1",
            "to": "pat@example.com"}
    r = c.post("/api/triage", json=card)
    assert r.json["staged"] == 1 and r.json["total"] == 1
    tid = r.json["ids"][0]
    assert c.post("/api/triage", json=card).json["deduped"] == 1
    items = c.get("/api/triage").json["items"]
    assert items[0]["triage_id"] == tid and items[0]["project_matches"] == []
    r = c.post(f"/api/triage/{tid}/approve", json={"matched_project": "__new__", "workflow_id": "quote",
                                                   "new_project_name": "Rooftop unit quote",
                                                   "new_project_location": "Client HQ", "quote_request": True})
    assert r.status_code == 200
    newp = r.json["project"]
    assert newp["status"] == "quote_needed" and newp["key_details"]["work_order"] == "123456"
    assert r.json["remaining"] == 0
    r = c.post("/api/triage", json=dict(card, message_id="m2", subject="Re: Please quote"))
    assert r.json["auto_committed"] == 1
    corr = c.get(f"/api/projects/{newp['id']}/correspondence").json
    assert corr["stats"]["email_count"] == 2
    # dismiss + revive
    tid2 = c.post("/api/triage", json=dict(card, message_id="m3", conversation_id="other")).json["ids"][0]
    assert c.delete(f"/api/triage/{tid2}").json["dismissed"] == tid2
    assert c.get("/api/triage/dismissed").json["items"][0]["triage_id"] == tid2
    assert c.post(f"/api/triage/{tid2}/revive").json["revived"] == tid2
    assert c.get("/api/triage/status").json["pending"] == 1
    r = c.post("/api/triage/run")                                 # the triage layer is wired in
    assert r.status_code == 200 and r.json["summary"]["scanned"] == 0

    # questions, drafts, alerts
    r = c.post("/api/questions", json={"project_id": pid, "question": "Which unit?", "options": [{"label": "A"}]})
    assert r.status_code == 201
    qid = r.json["question_id"]
    assert c.post("/api/questions", json={"project_id": pid, "question": "Which unit?"}).json["duplicate"]
    assert c.get(f"/api/questions?project_id={pid}").json["count"] == 1
    assert c.post(f"/api/questions/{qid}", json={"status": "answered", "answer": "A"}).json["status"] == "answered"
    r = c.post("/api/drafts", json={"project_id": pid, "to": "sue@acme.com", "subject": "Re: RTU-3", "body": "Hi"})
    assert r.status_code == 201
    did = r.json["id"]
    assert c.get("/api/drafts").json["count"] == 1
    assert c.put(f"/api/drafts/{did}", json={"status": "sent"}).json["draft"]["status"] == "sent"
    assert c.get("/api/drafts").json["count"] == 0
    r = c.post("/api/alerts", json={"source": "test", "alerts": [{"key": "k1", "title": "T", "severity": "hard"}]})
    assert r.json["raised"][0]["key"] == "k1"
    al = c.get("/api/alerts").json
    assert al["hard"] == 1
    assert c.post(f"/api/alerts/{al['alerts'][0]['id']}/ack", json={"by": "me"}).json["alert"]["status"] == "acked"

    # health + config + misc
    h = c.get("/api/health").json
    for key in ("status", "db_write_ok", "watchers", "restart_needed", "alerts", "last_triage", "mail", "ai",
                "license", "version", "watchers_down"):
        assert key in h, key
    assert h["db_write_ok"] is True and h["version"] == "1.2.0"
    assert h["mail"]["provider"] == "none" and "today_cost_usd" in h["ai"]
    cfg = c.get("/api/config").json
    assert cfg["profession"]["template"] == "hvac_contractor" and cfg["features"]["money"] is True
    assert c.get("/api/email-analytics?days=7").json["volume"]["inbound"] == 2
    assert c.get("/api/boss-dps").json["ok"] is True
    r = c.put("/api/hp-history", json={"date": "2026-10-05", "state": {"startHP": 100, "damageEvents": [{"time": "t", "src": "x", "amt": 5}]}})
    assert r.json["days_stored"] == 1
    assert c.get("/api/hp-history").json["days"]["2026-10-05"]["damageEvents"][0]["amt"] == 5
    r = c.post("/api/repair/conversation-routing", json={"conversation_id": "zz", "project_id": pid})
    assert r.json["ok"] is True
    # merge
    other = c.post("/api/projects", json={"name": "Dup", "work_type": "repair"}).json["project"]["id"]
    r = c.post(f"/api/projects/{pid}/merge", json={"source_id": other})
    assert r.json["ok"] and r.json["stats"]["subtasks"] >= 0
    assert c.delete(f"/api/projects/{other}").json["deleted"] == other
    # the other layers are wired in: no model -> 503, no key -> 400, no provider -> 400
    assert c.post("/api/ai/draft-reply", json={}).status_code == 503
    assert c.post("/api/license/activate", json={}).status_code == 400
    assert c.get("/api/license").json["status"] == "dev"
    assert c.post("/api/mail/test", json={}).status_code == 400
    assert c.get("/api/mail/providers").json["current"] == "none"
    assert c.get("/api/ai/usage").json["today"]["calls"] == 0
    st = c.get("/api/assistant/status").json
    assert st["installed"] is True and st["running"] is False and st["drafts_only"] is True
    r = c.post("/api/assistant/run", json={"max_items": 2})           # rules only, nothing sent
    assert r.status_code == 200 and r.json["summary"]["nothing_sent"] is True
    assert c.get("/api/assistant/log").json["count"] >= 1


def test_money_cascade_and_feature_off(data_root):
    cfg = make_config(data_root)
    c = create_app(config=cfg).test_client()
    pid = c.post("/api/projects", json={"name": "Money", "work_type": "repair"}).json["project"]["id"]
    p = c.put(f"/api/projects/{pid}", json={"quote_amount": 4200}).json["project"]
    assert p["status"] == "quote_sent" and p["quote_amount"] == 4200
    # a project with no out stage in its workflow does not move
    svc = c.post("/api/projects", json={"name": "Svc", "work_type": "service_call"}).json["project"]["id"]
    assert c.put(f"/api/projects/{svc}", json={"quote_amount": 10}).json["project"]["status"] == "call_in"
    cfg2 = make_config(data_root / "two", features={"money": False, "game_layer": False, "analytics": True})
    c2 = create_app(config=cfg2).test_client()
    pid2 = c2.post("/api/projects", json={"name": "No money", "work_type": "repair", "quote_amount": 99}).json["project"]["id"]
    p2 = c2.put(f"/api/projects/{pid2}", json={"quote_amount": 4200}).json["project"]
    assert p2["status"] == "quote_needed" and p2["quote_amount"] is None
    assert c2.get("/api/boss-dps").status_code == 404
    assert c2.get("/api/hp-history").status_code == 404


def test_workflows_put_guards_occupied_stage(client):
    c = client
    c.post("/api/projects", json={"name": "Occupied", "work_type": "service_call"})
    doc = c.get("/api/workflows").json["workflows"]
    doc["workflows"] = [w for w in doc["workflows"] if w["id"] != "service_call"]
    r = c.put("/api/workflows", json=doc)
    assert r.status_code == 409 and "call_in" in r.json["stages"]
    doc = c.get("/api/workflows").json["workflows"]
    doc["workflows"][0]["stages"].insert(1, {"id": "review", "label": "Review", "role": "in_progress", "column": True})
    r = c.put("/api/workflows", json=doc)
    assert r.status_code == 200 and "review" in r.json["stages_allowed"]
    assert any(t["id"] == "stage_review" for t in c.get("/api/board-tabs").json["tabs"])
    r = c.put("/api/workflows", json={"workflows": [{"id": "x", "label": "X", "stages": [], "entry_stage": "a"}]})
    assert r.status_code == 400
    r = c.put("/api/profession", json={"overrides": {"entity": {"client": "Owner"}}})
    assert r.json["profession"]["entity"]["client"] == "Owner"
    assert c.get("/api/profession").json["entity"]["client"] == "Owner"


def test_bearer_token_required_off_loopback(data_root):
    cfg = make_config(data_root, server={"port": 8080, "bind": "0.0.0.0", "api_token": "tok123"})
    app = create_app(config=cfg)
    app.config["PB_ENFORCE_AUTH"] = True
    c = app.test_client()
    assert c.get("/api/health").status_code == 401
    assert c.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/health", headers={"Authorization": "Bearer tok123"}).status_code == 200
    # the token is never written into the page (it used to be)
    assert b'content="tok123"' not in c.get("/").data
    # the configured token also works as a bootstrap link off loopback
    assert c.get("/auth/tok123").status_code == 302
    assert c.get("/api/health").status_code == 200


def test_read_file_sandbox(client, rt):
    (rt.paths.data_root / "notes.txt").write_text("hello", encoding="utf-8")
    r = client.get("/api/read-file?path=notes.txt")
    assert r.status_code == 200 and r.json["size"] == 5
    assert client.get("/api/read-file?path=../../etc/passwd").status_code == 403
    assert client.get(f"/api/read-file?path={rt.paths.db_file.name}").status_code == 403
    assert client.get("/api/read-file?path=missing.txt").status_code == 404
    files = client.get("/api/projects/proj_001/files").json
    assert files["ok"] and files["stats"]["attachment_count"] == 0


def test_intake_accepts_json_and_multipart_upload(client, rt):
    from io import BytesIO
    pid = client.post("/api/projects", json={"name": "Intake target", "work_type": "repair"}).json["project"]["id"]
    # JSON path (unchanged contract)
    r = client.post(f"/api/projects/{pid}/intake", json={"content": "paste from a phone call"})
    assert r.status_code == 201 and r.json["intake_id"]
    # multipart path: the board's dump zone posts FormData with a `file` field
    r = client.post(f"/api/projects/{pid}/intake",
                    data={"file": (BytesIO(b"PK\x03\x04 fake-xlsx"), "Quote Sheet.xlsx")},
                    content_type="multipart/form-data")
    assert r.status_code == 201, r.json
    items = client.get(f"/api/projects/{pid}/intake").json["items"]
    f = next(i for i in items if i["intake_type"] == "file")
    assert f["file_name"] == "Quote Sheet.xlsx" and f["file_size"] == len(b"PK\x03\x04 fake-xlsx")
    stored = rt.paths.project_files / pid / "intake" / "Quote_Sheet.xlsx"
    assert stored.is_file() and f["file_path"] == str(stored)
    # a second upload of the same name does not overwrite the first
    r = client.post(f"/api/projects/{pid}/intake",
                    data={"file": (BytesIO(b"v2"), "Quote Sheet.xlsx")}, content_type="multipart/form-data")
    assert r.status_code == 201
    assert (rt.paths.project_files / pid / "intake" / "Quote_Sheet_2.xlsx").read_bytes() == b"v2"
    assert stored.read_bytes().startswith(b"PK")
    # empty multipart is still a 400
    r = client.post(f"/api/projects/{pid}/intake", data={}, content_type="multipart/form-data")
    assert r.status_code == 400


def test_manifest_and_health_budget_flag(client, rt):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.mimetype == "application/manifest+json"
    m = r.json
    assert m["display"] == "standalone" and m["theme_color"] == "#0f1115" and m["background_color"] == "#0f1115"
    assert m["icons"][0]["src"].startswith("data:image/svg+xml,")
    assert m["name"].endswith("Email Boss")
    assert b'<link rel="manifest" href="/manifest.webmanifest">' in client.get("/").data
    h = client.get("/api/health").json
    assert "budget_hit" in h["ai"] and h["ai"]["budget_hit"] in (True, False)
