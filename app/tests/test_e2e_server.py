"""End-to-end flow through the real app factory (all layers wired): setup,
profession, projects, subtasks, a PA-format email dropped in the inbox,
triage run, approve as a new project, correspondence, the rules-only
assistant pass, license (dev) and health. Mirrors tests/SMOKE_LOG.md."""
import base64
import json

from plannerboard import create_app
from plannerboard.config import Config
from tests.conftest import FIXTURES


def _drop_request_email(mail_cache):
    rec = json.loads((FIXTURES / "inbox_approval.json").read_text(encoding="utf-8"))
    rec["subject"] = "Northwind Plaza lobby project - new request"
    rec["body"] = "<html><body><p>Hi,</p><p>Can you send a quote for the lobby refresh? Thanks, Dana</p></body></html>"
    rec["bodyPreview"] = "Can you send a quote for the lobby refresh?"
    rec["conversationId"] = "CONV-NEW-1"
    (mail_cache / "inbox").mkdir(parents=True, exist_ok=True)
    (mail_cache / "inbox" / "inbox_request.json").write_text(json.dumps(rec), encoding="utf-8")
    adir = mail_cache / "attachments" / rec["id"]
    adir.mkdir(parents=True, exist_ok=True)
    for att in rec.get("attachments") or []:
        (adir / att["name"]).write_bytes(base64.b64decode(att.get("contentBytes") or ""))
    return rec


def test_full_flow_from_setup_to_assistant(data_root):
    app = create_app(config=Config.load(), testing=True)
    c = app.test_client()
    rt = app.extensions["pb"]

    # 1. first run: wizard, then setup with the full body
    page = c.get("/").data
    assert b"Setup" in page
    setup_page = c.get("/setup").data if c.get("/setup").status_code == 200 else page
    for blob in (page, setup_page):
        assert b"developer mode" not in blob.lower()
    assert b"Sign in with Google" in setup_page or b"Sign in with Google" in page
    assert b"dev_skip" not in setup_page
    r = c.post("/api/setup", json={
        "user": {"name": "Pat Example", "email": "pat@example.com", "company": "Example Co", "role": "Owner"},
        "profession": "general", "paths": {"data_root": str(data_root)},
        "mail": {"provider": "none"}, "ai": {"provider": "none"}})
    assert r.status_code == 200 and r.json["status"] == "ok" and r.json["workflows_seeded"]
    cfg = c.get("/api/config").json
    assert cfg["configured"] is True and cfg["setup_required"] is False
    assert cfg["mail"]["provider"] == "none" and cfg["ai"]["provider"] == "none"
    assert b"Email Boss" in c.get("/").data

    # 2. profession / professions / workflows / board-tabs
    prof = c.get("/api/profession").json
    assert prof["id"] == "general" and prof["user"]["name"] == "Pat Example" and "features" in prof
    assert {p["id"] for p in c.get("/api/professions").json["professions"]} >= {"general", "legal", "hvac_contractor"}
    wf = c.get("/api/workflows").json
    assert wf["ok"] and [w["id"] for w in wf["workflows"]["workflows"]] == ["general"]
    assert wf["derived"]["roles"]["action_required"] == "todo"
    tabs = c.get("/api/board-tabs").json["tabs"]
    assert {t["id"] for t in tabs} >= {"active", "todo", "deadlines", "closed"}

    # 3. health carries every block the UI reads
    h = c.get("/api/health").json
    for key in ("mail", "ai", "license", "version", "watchers", "restart_needed", "db_write_ok", "last_triage"):
        assert key in h, key
    assert h["license"]["status"] == "dev" and h["mail"]["provider"] == "none" and h["ai"]["provider"] == "none"

    # 4. projects, key_details merge, subtasks
    r = c.post("/api/projects", json={
        "name": "Harbor Plaza Lobby Refresh", "work_type": "task", "location": "Harbor Plaza",
        "scope": "Refresh lobby finishes",
        "contacts": [{"name": "Dana Whitfield", "email": "dana@northwind.example"}],
        "key_details": {"reference_no": "PR-10442", "client": "Northwind"}})
    assert r.status_code == 201
    pid = r.json["project"]["id"]
    assert r.json["project"]["status"] == "action_required"
    p = c.put(f"/api/projects/{pid}", json={"key_details": {"po_number": "778899"}}).json["project"]
    assert p["key_details"] == {"reference_no": "PR-10442", "client": "Northwind", "po_number": "778899"}
    sid = c.post(f"/api/projects/{pid}/subtasks", json={"text": "Measure the lobby", "date_type": "deadline",
                                                        "follow_up_date": "2026-10-20"}).json["subtask_id"]
    assert c.post(f"/api/subtasks/{sid}/complete", json={"project_id": pid}).json["status"] == "completed"
    board = c.get("/api/projects").json
    item = board["projects"][0]
    assert item["site"] == "Harbor Plaza" and item["client"] == "Northwind" and item["contacts"][0]["email"]

    # 5. drop a PA-format email, run triage, inspect the card
    _drop_request_email(rt.paths.mail_cache)
    r = c.post("/api/triage/run")
    assert r.status_code == 200 and r.json["summary"]["staged"] == 1, r.json
    cards = c.get("/api/triage").json["items"]
    assert len(cards) == 1
    card = cards[0]
    assert card["status"] == "pending" and card["id"] == card["triage_id"]
    assert card["matched_project"] == pid and card["project_matches"][0]["match_type"] == "from_email"
    assert card["classification"] == "request" and card["needs_reply"] is True
    assert card["cc"] == "ops@northwind.example" and card["internet_message_id"].startswith("<abc-001")
    assert isinstance(card["signals"], dict) and isinstance(card["suggestion"], dict) and card["doc_links"] == []
    assert card["auto_date_type"] == "sla" and card["summary"]
    assert c.get("/api/triage/status").json["pending"] == 1

    # 6. approve as a new project: correspondence filed, thread registered, SLA task armed
    r = c.post(f"/api/triage/{card['triage_id']}/approve",
               json={"matched_project": "__new__", "new_project_name": "Lobby Refresh Quote",
                     "new_project_location": "Northwind Plaza"})
    assert r.status_code == 200, r.json
    newp = r.json["project"]
    assert newp["id"] != pid and newp["name"] == "Northwind Plaza Lobby Refresh Quote"
    assert any(s["text"].startswith("Respond to Dana Whitfield") for s in newp["subtasks"])
    assert rt.store.routing_lookup("CONV-NEW-1") == newp["id"]
    ids = [p["id"] for p in c.get("/api/projects").json["projects"]]
    assert set(ids) == {pid, newp["id"]}
    corr = c.get(f"/api/projects/{newp['id']}/correspondence").json
    assert corr["stats"]["email_count"] == 1 and corr["correspondence"][0]["from_email"] == "dana@northwind.example"
    assert rt.store.get_project_correspondence(newp["id"], include_bodies=True)[0]["body"]
    assert c.get("/api/triage").json["items"] == []

    # 7. assistant pass with no model: rules only, nothing sent, a question is posted
    r = c.post("/api/assistant/run", json={"max_items": 5})
    assert r.status_code == 200, r.json
    s = r.json["summary"]
    assert s["nothing_sent"] is True and s["llm_used"] is False and s["escalations"] == []
    assert s["counts"]["question_posted"] >= 1
    assert c.get("/api/questions").json["count"] >= 1
    st = c.get("/api/assistant/status").json
    assert st["last_run"] == s["finished"] and st["drafts_only"] is True and st["running"] is False
    lg = c.get("/api/assistant/log").json
    assert lg["count"] >= 1 and all("project_id" in e for e in lg["entries"])
    # the real store's assistant_log rows use project_id
    assert all("project_id" in row for row in rt.store.assistant_log(limit=10))

    # 8. license (dev) and the other layers' catalog endpoints
    lic = c.get("/api/license").json
    assert lic["status"] == "dev" and lic["read_only"] is False and lic["dev_mode"] is True
    assert c.post("/api/license/activate", json={}).status_code == 400
    assert [p["id"] for p in c.get("/api/mail/providers").json["providers"]] == ["pa_folder", "imap", "gmail", "graph", "none"]
    assert {p["id"] for p in c.get("/api/ai/providers").json["providers"]} == {"emailboss", "none", "anthropic", "openai", "ollama"}
    assert c.post("/api/ai/draft-reply", json={"subject": "x"}).status_code == 503
    assert c.get("/api/mail/status").json["provider"] == "none"
    h = c.get("/api/health").json
    assert h["last_triage"] and h["project_count"] == 2 and h["pending_triage"] == 0


def test_store_extras_for_triage_layer(data_root):
    """The Store extras the triage builder listed: status persisted and
    filterable, alt: routing keys, id_index keys, vendor refs verbatim."""
    app = create_app(config=Config.load(), testing=True)
    c = app.test_client()
    c.post("/api/setup", json={"user": {"name": "Pat Example", "email": "pat@example.com"},
                               "profession": "general", "paths": {"data_root": str(data_root)}})
    st = app.extensions["pb"].store
    pid = st.create_project("Northwind lobby", "general", "action_required",
                            key_details={"reference_no": "pr 10442", "refs": ["ORD:supply.example:28219861", "TRK:1Z999"]})
    idx = st.id_index()
    assert idx["PR10442"] == [pid] and idx["ORD:supply.example:28219861"] == [pid] and idx["TRK:1Z999"] == [pid]
    st.routing_set("alt:" + "a" * 40, pid, source="approve")
    assert st.routing_lookup("alt:" + "a" * 40) == pid
    active = st.active_projects()[0]
    for key in ("last_activity", "location", "site", "contacts", "client", "key_details", "subtasks"):
        assert key in active, key
    for status in ("pending", "auto_committed", "dismissed"):
        st.add_triage_card({"card_type": "email", "status": status, "subject": status, "direction": "inbound",
                            "from_email": "x@y.example", "cc": "c@y.example", "needs_reply": True, "summary": "s",
                            "internet_message_id": "<m@y>", "signals": {"approval": False}, "suggestion": {"name": "N"},
                            "auto_reason": "thread_match", "auto_date_type": "sla", "dollar_amount": 12.5,
                            "doc_links": [{"url": "https://www.dropbox.com/x", "label": "Dropbox"}],
                            "message_id": "m-" + status})
    for status in ("pending", "auto_committed", "dismissed"):
        rows = st.list_triage(status)
        assert len(rows) == 1 and rows[0]["subject"] == status, status
        row = rows[0]
        assert row["cc"] == "c@y.example" and row["needs_reply"] is True and row["summary"] == "s"
        assert row["internet_message_id"] == "<m@y>" and row["signals"] == {"approval": False}
        assert row["suggestion"] == {"name": "N"} and row["auto_reason"] == "thread_match"
        assert row["auto_date_type"] == "sla" and row["dollar_amount"] == 12.5
        assert row["doc_links"][0]["label"] == "Dropbox"


def test_approve_all_creates_new_from_unmatched_cards(client):
    """ADD ALL with create_new: matched cards are added, unmatched email cards
    become new items named from the suggestion or the sender's display name,
    with the work type the board picked (or the card's suggested one)."""
    from datetime import datetime, timedelta, timezone
    c = client
    when = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = {"card_type": "email", "direction": "inbound", "received_at": when, "to": "pat@example.com"}
    t1 = c.post("/api/triage", json=dict(base, from_email="ann@one.example", from_name="Ann Lee",
                                         subject="hello there", message_id="mm1", conversation_id="cv1")).json["ids"][0]
    t2 = c.post("/api/triage", json=dict(base, from_email="bo@two.example", from_name="Bo Park",
                                         subject="question", message_id="mm2", conversation_id="cv2",
                                         suggestion={"name": "Rooftop service", "location": "Two Co"})).json["ids"][0]
    # old behavior: without create_new nothing unmatched is guessed
    r = c.post("/api/triage/approve-all", json={}).json
    assert r["approved"] == 0 and r["created"] == 0 and r["skipped_unmatched"] == 2
    wts = c.get("/api/profession").json["work_types"]
    pick = wts[-1]
    r = c.post("/api/triage/approve-all", json={"create_new": True,
                                                "new": {t2: {"work_type": pick["id"], "workflow_id": pick.get("workflow_id")}}}).json
    assert r["ok"] and r["created"] == 2 and r["approved"] == 0 and r["remaining"] == 0
    projects = c.get("/api/projects").json["projects"]
    names = {p["name"] for p in projects}
    assert any("Ann Lee" in n for n in names) and any("Rooftop service" in n for n in names)
    p2 = next(p for p in projects if "Rooftop service" in p["name"])
    assert p2["work_type"] == pick["id"]


def test_weekly_digest_and_search_without_ai(client):
    c = client
    c.post("/api/projects", json={"name": "Harbor roof job", "location": "Harbor"})
    r = c.post("/api/ai/weekly-digest", json={})
    assert r.status_code == 200 and r.json["ok"] and r.json["ai_used"] is False and r.json["text"]
    s = c.post("/api/ai/search", json={"query": "harbor"}).json
    assert s["items"] and {"id", "name", "why"} <= set(s["items"][0]) and "harbor" in s["items"][0]["why"].lower()
