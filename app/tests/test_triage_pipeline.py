"""End-to-end triage cycle against the FakeStore and the drop-folder fixtures."""
import json

from plannerboard.triage.pipeline import after_approve, run_cycle
from tests.conftest import FIXTURES, drop_fixture
from tests.fakes import FakeLLM, FakeStore


def _store(profession, with_project=True):
    st = FakeStore(id_patterns=profession["id_patterns"])
    if with_project:
        pid = st.create_project("Northwind Plaza lobby renovation", "general", "waiting",
                                contacts=[{"name": "Dana Whitfield", "email": "dana@northwind.example"}],
                                key_details={"reference_no": "PR-10442", "client": "Northwind"})
        return st, pid
    return st, None


def test_auto_commit_by_thread_and_outbound_clears_respond(cfg, profession, mail_cache):
    st, pid = _store(profession)
    st.routing_set("CONV-NORTHWIND-1", pid, source="approve")
    drop_fixture(mail_cache, "inbox_approval.json", "inbox")
    s = run_cycle(st, cfg, profession, mail_cache=mail_cache)
    assert s["auto_committed"] == 1 and s["staged"] == 0 and s["errors"] == 0, s
    assert st.correspondence[0]["pid"] == pid
    assert st.correspondence[0]["attachments"] and st.correspondence[0]["attachments"][0].endswith("Signed Proposal.pdf")
    card = st.list_triage("auto_committed")[0]
    assert card["matched_project"] == pid and card["auto_reason"] == "thread_match"
    assert card["signals"]["approval"] is True and "approved" in card["signals"]["completion"]
    assert card["dollar_amount"] is None or isinstance(card["dollar_amount"], float)
    assert card["cc"] == "ops@northwind.example"
    # inbound armed the SLA task
    opens = st.open_subtasks(pid)
    assert len(opens) == 1 and opens[0]["text"].startswith("Respond to Dana Whitfield:") and opens[0]["sla_hours"] == 4
    assert opens[0]["date_type"] == "sla"
    assert st.messages["AAMkAGExample001"]["status"] == "auto_committed"
    assert not list((mail_cache / "inbox").glob("*.json"))
    assert any(p.name == "inbox_approval.json" for p in (mail_cache / "processed").rglob("*.json"))
    assert not (mail_cache / "attachments" / "AAMkAGExample001").exists()

    # the user's own reply on the thread completes the "Respond to" task
    drop_fixture(mail_cache, "sent_reply.json", "sent", with_attachments=False)
    s2 = run_cycle(st, cfg, profession, mail_cache=mail_cache)
    assert s2["auto_committed"] == 1
    assert st.open_subtasks(pid)[0]["text"].startswith("Follow up with Dana Whitfield:")
    done = [x for x in st.subtasks[pid] if x["status"] == "done"]
    assert done and done[0]["completed_by"] == "sent_reply"
    assert st.meta_get("last_triage")


def test_inbound_reply_clears_follow_up(cfg, profession, mail_cache):
    st, pid = _store(profession)
    st.routing_set("CONV-NORTHWIND-1", pid)
    st.add_subtask(pid, "Follow up with Dana: proposal", date_type="follow_up")
    drop_fixture(mail_cache, "inbox_approval.json", "inbox")
    run_cycle(st, cfg, profession, mail_cache=mail_cache)
    texts = {x["text"].split(":")[0]: x["status"] for x in st.subtasks[pid]}
    assert texts["Follow up with Dana"] == "done"
    assert any(k.startswith("Respond to") and v == "open" for k, v in texts.items())


def test_auto_commit_by_unique_id(cfg, profession, mail_cache):
    st, pid = _store(profession)
    assert st.routing_lookup("CONV-NORTHWIND-1") is None
    drop_fixture(mail_cache, "inbox_approval.json", "inbox")
    s = run_cycle(st, cfg, profession, mail_cache=mail_cache)
    assert s["auto_committed"] == 1, s
    card = st.list_triage("auto_committed")[0]
    assert card["auto_reason"] == "id:reference_no:PR-10442"
    # the thread is now registered for next time
    assert st.routing_lookup("CONV-NORTHWIND-1") == pid
    assert any("Auto-filed (id:reference_no:PR-10442)" in n[1] for n in st.notes)


def test_pending_stage_and_dedup(cfg, profession, mail_cache):
    st, pid = _store(profession)
    # make the id non-unique so nothing auto-commits: a second project with the same ref
    st.create_project("Northwind Plaza second phase", "general", "action_required",
                      key_details={"reference_no": "PR-10442"})
    drop_fixture(mail_cache, "inbox_approval.json", "inbox")
    llm = FakeLLM({"triage.summarize_bullets": {"text": "- approved to proceed\n- schedule the work"},
                   "triage.classify": {"parsed": {"classification": "approval", "needs_reply": True,
                                                  "follow_up_days": 1}}})
    s = run_cycle(st, cfg, profession, llm=llm, mail_cache=mail_cache)
    assert s["staged"] == 1 and s["auto_committed"] == 0, s
    card = st.list_triage("pending")[0]
    expected_keys = {"card_type", "status", "direction", "from_email", "from_name", "to", "cc", "subject", "body_raw",
                     "body_preview", "received_at", "conversation_id", "message_id", "internet_message_id",
                     "attachments", "attachment_data", "has_attachments", "project_matches", "matched_project",
                     "confidence", "match_reason", "classification", "needs_reply", "auto_action", "auto_follow_up",
                     "auto_date_type", "dollar_amount", "summary", "signals", "suggestion", "doc_links",
                     "is_self_note", "auto_reason", "source_file", "source_folder"}
    assert expected_keys <= set(card)
    assert len(card["project_matches"]) == 2 and {m["project_id"] for m in card["project_matches"]} == {pid, "proj_002"}
    assert card["matched_project"] in (pid, "proj_002") and card["confidence"] == "high"
    assert card["classification"] == "approval" and card["needs_reply"] is True
    assert card["auto_date_type"] == "sla" and card["auto_follow_up"]
    assert card["summary"].startswith("- approved")
    assert card["suggestion"]["name"]
    assert card["attachment_data"][0]["filename"] == "Signed Proposal.pdf"
    assert [c[0] for c in llm.calls] == ["triage.classify", "triage.summarize_bullets"]
    # No subtask spawned at staging; nothing filed.
    assert st.open_subtasks(pid) == [] and st.correspondence == []
    assert st.messages["AAMkAGExample001"]["status"] == "pending"

    # dedup: the same record dropped again is skipped, not re-staged
    drop_fixture(mail_cache, "inbox_approval.json", "inbox")
    s2 = run_cycle(st, cfg, profession, llm=llm, mail_cache=mail_cache)
    assert s2["dedup_skipped"] == 1 and s2["staged"] == 0
    assert len(st.cards) == 1
    assert not list((mail_cache / "inbox").glob("*.json"))

    # approve: registers the thread and arms the SLA task
    res = st.approve_triage(card["id"], pid)
    after_approve(st, cfg, profession, st.cards[card["id"]], res["project_id"])
    assert st.routing_lookup("CONV-NORTHWIND-1") == pid
    assert st.open_subtasks(pid)[0]["text"].startswith("Respond to Dana Whitfield")


def test_notification_dismissed_and_capture_status(cfg, profession, mail_cache):
    st, _pid = _store(profession)
    drop_fixture(mail_cache, "inbox_notification.json", "inbox", with_attachments=False)
    (mail_cache / "inbox" / "garbage.json").write_text("{{")
    s = run_cycle(st, cfg, profession, mail_cache=mail_cache)
    assert s["dismissed"] == 1 and s["staged"] == 0
    card = st.list_triage("dismissed")[0]
    assert card["classification"] == "notification" and card["dismiss_reason"].startswith("notification")
    assert st.messages["AAMkAGExample002"]["status"] == "dismissed"
    cap = json.loads((mail_cache / "capture_status.json").read_text())
    assert cap["unreadable"] == ["inbox/garbage.json (JSONDecodeError)"]
    assert (mail_cache / "inbox" / "garbage.json").exists()
    assert st.alerts and st.alerts[0]["kind"] == "mail_unreadable"


def test_internal_admin_dismissed(cfg, profession, mail_cache):
    st, _ = _store(profession)
    rec = json.loads((FIXTURES / "inbox_notification.json").read_text())
    rec.update({"id": "ADMIN-1", "internetMessageId": "<admin1@acme.example>", "from": "hr@acme.example",
                "toRecipients": "sam@acme.example;team@acme.example", "subject": "Timesheet reminder",
                "body": "Please submit your timesheet.", "internetMessageHeaders": []})
    (mail_cache / "inbox" / "admin.json").write_text(json.dumps(rec))
    s = run_cycle(st, cfg, profession, mail_cache=mail_cache)
    assert s["dismissed"] == 1
    assert st.list_triage("dismissed")[0]["classification"] == "internal_admin"


def test_closed_project_thread_falls_through_to_card(cfg, profession, mail_cache):
    st, pid = _store(profession)
    st.update_project(pid, status="complete")
    st.routing_set("CONV-NORTHWIND-1", pid)
    drop_fixture(mail_cache, "inbox_approval.json", "inbox")
    s = run_cycle(st, cfg, profession, mail_cache=mail_cache)
    assert s["auto_committed"] == 0 and s["staged"] == 1
    assert st.list_triage("pending")[0]["matched_project"] == ""


def test_per_email_error_containment(cfg, profession, mail_cache, monkeypatch):
    st, _ = _store(profession)
    drop_fixture(mail_cache, "inbox_approval.json", "inbox")
    import plannerboard.triage.pipeline as P
    monkeypatch.setattr(P, "match_email_to_projects", lambda e, c: (_ for _ in ()).throw(RuntimeError("boom")))
    s = run_cycle(st, cfg, profession, mail_cache=mail_cache)
    assert s["errors"] == 1 and s["staged"] == 0
    assert list((mail_cache / "inbox").glob("*.json"))          # left on disk for retry
    assert "AAMkAGExample001" not in st.messages                 # never marked processed
