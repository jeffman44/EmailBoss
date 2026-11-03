import threading

import pytest

from plannerboard import ladder


def test_create_update_get_project(store):
    pid = store.create_project("Acme Plaza roof unit", work_type="repair", location="Acme Plaza",
                               contacts=[{"name": "Sue", "email": "Sue@Acme.com"}], key_details={"a": 1},
                               tags=["x"], source="test")
    assert pid == "proj_001"
    p = store.get_project(pid)
    assert p["workflow_id"] == "quote" and p["status"] == "quote_needed"
    assert p["location_slug"] == "acme_plaza"
    assert p["contacts"][0]["email"] == "Sue@Acme.com"
    assert p["key_details"] == {"a": 1} and p["tags"] == ["x"]
    p2 = store.update_project(pid, key_details={"b": 2}, scope="replace the unit")
    assert p2["key_details"] == {"a": 1, "b": 2}          # spread-merge
    assert p2["scope"] == "replace the unit"
    with pytest.raises(ValueError):
        store.update_project(pid, status="not_a_stage")
    store.update_project(pid, status="quote_sent")
    assert store.get_project(pid)["status"] == "quote_sent"
    assert store.get_project("proj_999") is None
    assert store.update_project("proj_999", scope="x") is None


def test_active_excludes_closed_and_indexes(store):
    a = store.create_project("Open one", work_type="repair", contacts=[{"name": "Bob", "email": "bob@x.com"}],
                             key_details={"work_order": "123456", "references": ["123456"]})
    b = store.create_project("Closed one", work_type="repair", status="complete")
    store.add_note(a, "Customer PO 654321 attached")
    active = [p["id"] for p in store.active_projects()]
    assert a in active and b not in active
    assert len(store.all_projects()) == 2
    assert store.contact_index()["bob@x.com"] == [a]
    idx = store.id_index()
    assert idx["123456"] == [a] and idx["654321"] == [a]
    assert a in store.keyword_index()["open"]
    assert store.extract_ids("see WO #777777 and PO 888888") == {"work_order": ["777777"], "po_number": ["888888"]}


def test_subtasks_notes_and_send_guard(store):
    pid = store.create_project("Job", work_type="repair")
    st = store.add_subtask(pid, "Call the vendor", date_type="follow_up", follow_up_date="2026-10-10")
    assert st and store.add_subtask(pid, "Call the vendor") is None          # dedup
    assert [s["text"] for s in store.open_subtasks(pid)] == ["Prepare and send proposal", "Call the vendor"] or \
        "Call the vendor" in [s["text"] for s in store.open_subtasks(pid)]
    sla = store.add_subtask(pid, "Respond to Bob: hello", date_type="sla", sla_hours=4)
    row = next(s for s in store.open_subtasks(pid) if s["id"] == sla)
    assert row["sla_type"] == "response" and row["sla_created"] and row["sla_hours"] == 4
    send = store.add_subtask(pid, "Send proposal to customer")
    assert store.complete_subtask(pid, send, completed_by="auto_sync") is False     # guard
    assert store.complete_subtask(pid, send, completed_by="manual") is True
    assert store.complete_subtask(pid, send, completed_by="x") is True             # idempotent
    assert store.complete_subtasks_matching(pid, "call the", completed_by="system") == 1
    assert store.append_note(pid, "Spoke with Bob about the schedule") is True
    assert store.append_note(pid, "Spoke with Bob about the schedule!") is False    # similar
    store.add_note(pid, "Totally different fact 42")
    assert any("different fact" in n["text"] for n in store.get_project(pid)["notes"])


def test_reactivation_on_new_subtask_uses_roles(store):
    pid = store.create_project("Done job", work_type="repair", status="complete")
    store.add_subtask(pid, "Customer called back")
    assert store.get_project(pid)["status"] == "quote_needed"       # entry stage, not a literal
    ign = store.create_project("Ignored", work_type="repair", status="ignored")
    store.add_subtask(ign, "noise")
    assert store.get_project(ign)["status"] == "ignored"


def test_routing_dedup_and_auto_commit(store):
    pid = store.create_project("Thread job", work_type="repair")
    assert store.routing_lookup("c1") is None
    store.routing_set("c1", pid, "test")
    assert store.routing_lookup("c1") == pid
    assert not store.message_seen("m1")
    store.message_mark("m1", "staged", triage_id="t1")
    assert store.message_seen("m1")
    email = {"conversation_id": "c1", "direction": "inbound", "from_email": "bob@x.com", "from_name": "Bob",
             "subject": "Re: thread", "received_at": "2026-10-05T15:00:00Z", "message_id": "m2"}
    assert store.should_auto_commit(email) == (pid, "thread_match")
    store.update_project(pid, status="complete")
    assert store.should_auto_commit(email) == (None, None)           # closed project: never
    store.update_project(pid, status="quote_sent")
    store.add_subtask(pid, "Follow up with bob@x.com: thread", date_type="follow_up", created="2026-10-01")
    assert store.auto_commit_email(email, pid) is True
    assert not any(s["text"].startswith("Follow up with") for s in store.open_subtasks(pid))
    out = dict(email, direction="outbound", from_email="pat@example.com", subject="Proposal: thread $12,500",
               body_preview="Our price for this work is $12,500", message_id="m3", to="bob@x.com")
    store.add_subtask(pid, "Respond to Bob: thread", date_type="sla", sla_hours=4,
                      sla_created="2026-10-05T07:00:00")
    res = store.auto_commit_email(out, pid)
    assert isinstance(res, dict) and res["quote_amount"] == 12500.0
    assert not any(s["text"].startswith("Respond to") for s in store.open_subtasks(pid))
    assert store.get_project(pid)["emails_sent"] == 1


def test_triage_cards_and_approve(store):
    card = {"card_type": "email", "direction": "inbound", "from_email": "ann@client.com", "from_name": "Ann Lee",
            "subject": "Quote request WO 445566", "body_raw": "Please quote", "received_at": "2026-10-05T14:00:00Z",
            "message_id": "mm1", "conversation_id": "cc1", "attachments": [{"name": "a.pdf"}],
            "signals": {"approval": False}, "suggestion": {"name": "Something"}, "unknown_column": "dropped"}
    tid = store.add_triage_card(card)
    got = store.get_triage_card(tid)
    assert got["attachments"] == [{"name": "a.pdf"}] and got["signals"] == {"approval": False}
    assert got["has_attachments"] == 1 and "unknown_column" not in got
    assert store.list_triage("pending")[0]["triage_id"] == tid
    res = store.approve_triage(tid, "__new__", new_name="Chiller quote", new_location="Client HQ",
                               quote_request=True, actor="test")
    proj = res["project"]
    assert res["ok"] and proj["status"] == "quote_needed" and proj["workflow_id"] == "quote"
    assert proj["name"] == "Client HQ Chiller quote"
    assert proj["key_details"]["work_order"] == "445566"
    assert proj["contacts"][0]["email"] == "ann@client.com"
    texts = [s["text"] for s in proj["subtasks"]]
    assert "Prepare and send proposal" in texts and any(t.startswith("Respond to Ann Lee") for t in texts)
    assert store.routing_lookup("cc1") == proj["id"]
    assert store.get_triage_card(tid)["status"] == "approved"
    assert store.message_seen("mm1")
    assert store.get_correspondence_stats(proj["id"])["email_count"] == 1
    again = store.approve_triage(tid, "__new__")
    assert again["already_approved"] is True
    # sibling card on the same thread auto-commits when another card is approved
    sib = store.add_triage_card(dict(card, message_id="mm2", subject="Re: Quote request"))
    main = store.add_triage_card(dict(card, message_id="mm3", subject="Re: Quote request again"))
    res2 = store.approve_triage(main, proj["id"], actor="test")
    assert res2["thread_siblings_committed"] == 1
    assert store.get_triage_card(sib)["status"] == "auto_committed"
    with pytest.raises(LookupError):
        store.approve_triage("nope", "__new__")
    d = store.add_triage_card(dict(card, message_id="mm4", conversation_id="cc1"))
    assert store.file_before_dismiss(store.get_triage_card(d), d) == proj["id"]
    store.dismiss_triage(d, reason="noise", actor="test")
    assert store.get_triage_card(d)["status"] == "dismissed"
    store.revive_triage(d)
    assert store.get_triage_card(d)["status"] == "pending"


def test_correspondence_files_and_merge(store, tmp_path):
    a = store.create_project("Target", work_type="repair", quote_amount=None)
    b = store.create_project("Source", work_type="repair", quote_amount=500, contacts=[{"name": "Z", "email": "z@z.z"}])
    f = tmp_path / "quote.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    cid = store.file_correspondence(b, {"subject": "S", "message_id": "x1", "direction": "inbound",
                                        "from_email": "z@z.z", "received_at": "2026-10-05T10:00:00"}, [str(f)])
    assert cid and cid.isdigit()
    files = store.get_correspondence_files(int(cid))
    assert files[0]["filename"] == "quote.pdf" and files[0]["file_type"] == "pdf"
    assert store.get_correspondence_file_data(files[0]["id"])["file_data"] == b"%PDF-1.4 fake"
    assert store.file_correspondence(b, {"subject": "S", "message_id": "x1"}, []) == cid    # dedup
    stats = store.merge_projects(a, b)
    assert stats["contacts"] == 1 and stats["correspondence"] == 1
    merged = store.get_project(a)
    assert merged["quote_amount"] == 500 and merged["contacts"][0]["email"] == "z@z.z"
    src = store.get_project(b)
    assert src["archived"] == 1 and src["status"] == "complete"
    assert store.get_correspondence_stats(a)["email_count"] == 1


def test_questions_drafts_usage_log_alerts_meta(store):
    pid = store.create_project("Q", work_type="task")
    qid = store.add_question(pid, "Which unit?", [{"label": "A"}], recommend="A", asked_by="assistant")
    assert store.add_question(pid, "Which unit?", []) == qid
    assert store.open_questions(pid)[0]["options"] == [{"label": "A"}]
    assert store.answer_question(qid, "answered", "A")["status"] == "answered"
    assert store.open_questions(pid) == []
    did = store.add_draft(pid, "bob@x.com", "", "Re: hello", "Body text", created_by="assistant")
    assert store.list_drafts(pid)[0]["id"] == did
    store.update_draft(did, status="sent")
    assert store.list_drafts(pid) == [] and store.list_drafts(pid, status="all")[0]["status"] == "sent"
    with pytest.raises(ValueError):
        store.update_draft(did, status="weird")
    store.record_usage("triage.classify", "anthropic", "m", 100, 20, 0.002, 300, True)
    store.record_usage("triage.classify", "anthropic", "m", 100, 20, 0.001, 300, False)
    today = store.usage_today()
    assert today["calls"] == 2 and round(today["cost_usd"], 4) == 0.003
    assert today["by_task"]["triage.classify"]["failed"] == 1
    store.log_assistant("run1", pid, "triage_card", "acted", {"why": "unique match"})
    log = store.assistant_log()
    assert log[0]["outcome"] == "acted" and log[0]["detail"] == {"why": "unique match"}
    aid = store.add_alert("mail_down", "Mail capture silent", severity="hard")
    assert store.add_alert("mail_down", "Mail capture silent") == aid            # dedup bumps
    assert store.open_alerts()[0]["seen_count"] == 2
    store.ack_alert(aid, by="test")
    assert store.list_alerts("acked")[0]["id"] == aid
    assert store.resolve_alerts(keys=["mail_down"]) == 1
    assert store.resolve_alerts(keys=["mail_down"]) == 0            # zero-row update still commits
    store.meta_set("k", "v")
    assert store.meta_get("k") == "v" and store.meta_get("none", "d") == "d"
    store.license_set("activation", {"lid": "L1"})
    assert store.license_get("activation") == {"lid": "L1"}
    ev = store.replay_events(action="project_created", limit=5)
    assert isinstance(ev, list)


def test_threads_get_their_own_connection(store):
    pid = store.create_project("Threaded", work_type="task")
    seen = {}

    def worker():
        seen["conn"] = store.conn
        seen["proj"] = store.get_project(pid)["name"]

    t = threading.Thread(target=worker, name="worker-1")
    t.start()
    t.join()
    assert seen["proj"] == "Threaded"
    assert seen["conn"] is not store.conn
    assert store.write_probe()["ok"] is True
