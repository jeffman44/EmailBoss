import json
from datetime import date

from plannerboard.triage import text as T
from plannerboard.triage.attachments import classify_attachment, extract_csv_data, extract_with_timeout
from plannerboard.triage.classify import classify, rules_classify
from plannerboard.triage.match import (MatchContext, extract_vendor_refs, match_email_to_projects,
                                       recency_factor, unique_exact_match)
from plannerboard.triage.normalize import move_to_processed, normalize_record, scan_email_files, ScanWarnings
from plannerboard.triage.signals import (compute_thread_key_alt, extract_completion_signals,
                                         extract_triage_signals)
from plannerboard.triage.suggest import suggest_project
from tests.conftest import drop_fixture
from tests.fakes import FakeLLM


# ── text ────────────────────────────────────────────────────────────────────

def test_strip_html_and_forward_headers():
    html = "<html><style>p{}</style><body><p>New reply here, thanks.</p><br>From: Bob<br>Sent: Mon<br>To: Me<br>Subject: x<br>old stuff</body></html>"
    plain = T.strip_html(html)
    assert "p{}" not in plain and "New reply here" in plain
    newest = T.strip_forward_headers(plain)
    assert newest.startswith("New reply here") and "old stuff" not in newest
    bare_fwd = "FYI\nFrom: Bob\nSent: Mon\nTo: Me\nSubject: x\nThe forwarded body is long enough to keep."
    assert T.strip_forward_headers(bare_fwd).startswith("The forwarded body")
    gm = "Sounds good, see you then.\n\nOn Tue, Oct 1, 2026 at 9:00 AM Dana wrote:\n> old"
    assert T.strip_forward_headers(gm) == "Sounds good, see you then."


def test_signature_markers_from_cfg(cfg):
    body = "Please send the revised quote.\nSam User\nAcme Services\n123 Main St"
    cut = T.strip_signature_block(body, T.signature_markers_from_cfg(cfg))
    assert cut == "Please send the revised quote."
    assert T.strip_signature_block("Thanks for the update\nThanks,\nDana") == "Thanks for the update"


def test_extract_links_doc_and_internal(cfg):
    html = ('<a href="https://www.dropbox.com/s/x/plan.pdf">plan</a> see https://portal.acme.example/job/1 '
            'and https://example.org/nothing')
    links = T.extract_links(html, T.doc_domains_from_cfg(cfg), cfg["user"]["internal_domains"])
    labels = {l["label"] for l in links}
    assert labels == {"Dropbox", "Internal"}
    assert len(links) == 2


# ── normalize ───────────────────────────────────────────────────────────────

def test_normalize_fixture_record(cfg, mail_cache):
    p = drop_fixture(mail_cache, "inbox_approval.json", "inbox")
    e = normalize_record(json.loads(p.read_text()), p, cfg)
    expected = {"message_id", "conversation_id", "internet_message_id", "from_name", "from_email", "to", "cc",
                "recipients", "subject", "body_preview", "body", "body_raw", "received_at", "has_attachments",
                "attachments", "importance", "is_read", "source_file", "source_folder", "source_path",
                "doc_links", "direction"}
    assert expected <= set(e)
    assert e["direction"] == "inbound" and e["source_folder"] == "inbox"
    assert e["from_name"] == "Dana Whitfield"
    assert e["cc"] == "ops@northwind.example"
    assert e["recipients"][0] == {"name": "Sam User", "email": "sam@acme.example", "kind": "to"}
    assert e["recipients"][1]["kind"] == "cc" and e["recipients"][1]["name"] == "Ops Desk"
    assert "old quoted text" not in e["body"] and "Approved to proceed" in e["body"]
    assert "old quoted text" in e["body_raw"]
    assert e["doc_links"][0]["label"] == "Dropbox"
    assert e["attachments"][0]["name"] == "Signed Proposal.pdf" and "contentBytes" not in e["attachments"][0]


def test_scan_handles_unreadable_and_sent(cfg, mail_cache):
    drop_fixture(mail_cache, "inbox_approval.json", "inbox")
    drop_fixture(mail_cache, "inbox_notification.json", "inbox", with_attachments=False)
    drop_fixture(mail_cache, "sent_reply.json", "sent", with_attachments=False)
    (mail_cache / "inbox" / "broken.json").write_text("{not json")
    w = ScanWarnings()
    inbox = scan_email_files(mail_cache / "inbox", None, cfg, w)
    sent = scan_email_files(mail_cache / "sent", None, cfg, w)
    assert len(inbox) == 2 and len(sent) == 1
    assert sent[0]["direction"] == "outbound"
    assert w.unreadable == ["inbox/broken.json (JSONDecodeError)"]
    assert (mail_cache / "inbox" / "broken.json").exists()
    moved = move_to_processed(mail_cache, inbox[0])
    assert moved and not (mail_cache / "inbox" / inbox[0]["source_file"]).exists()
    assert (mail_cache / "processed").is_dir()


# ── match ───────────────────────────────────────────────────────────────────

def _projects():
    return [
        {"id": "p1", "name": "Northwind Plaza lobby renovation", "status": "waiting", "location": "Northwind Plaza",
         "contacts": [{"name": "Dana Whitfield", "email": "dana@northwind.example"}],
         "key_details": {"reference_no": "PR-10442"}, "last_activity": date.today().isoformat()},
        {"id": "p2", "name": "Harbor Tower roof inspection", "status": "action_required", "location": "Harbor Tower",
         "contacts": [{"name": "Lee Park", "email": "lee@harbor.example"}],
         "key_details": {}, "last_activity": "2020-01-01"},
        {"id": "p3", "name": "Harbor Annex roof repair", "status": "action_required", "location": "Harbor Annex",
         "contacts": [], "key_details": {}, "last_activity": date.today().isoformat()},
    ]


def _ctx(cfg, profession, id_index=None):
    return MatchContext(_projects(), profession, cfg, id_index=id_index or {"PR-10442": ["p1"]})


def test_exact_id_short_circuit(cfg, profession):
    ctx = _ctx(cfg, profession)
    e = {"from_email": "nobody@else.example", "subject": "question on PR-10442", "body": "", "direction": "inbound"}
    m = match_email_to_projects(e, ctx)
    assert m == [{"project_id": "p1", "confidence": "high", "match_type": "id:reference_no:PR-10442"}]
    assert unique_exact_match(m, ctx) == "p1"
    # a non-unique pattern never auto-commits
    ctx2 = _ctx(cfg, profession, id_index={"5551": ["p2"]})
    m2 = match_email_to_projects({"from_email": "x@y.example", "subject": "TKT-5551", "body": "", "direction": "inbound"}, ctx2)
    assert m2[0]["project_id"] == "p2" and unique_exact_match(m2, ctx2) is None


def test_vendor_ref_keys_and_exact_match(cfg, profession):
    refs = extract_vendor_refs("Order #: 28219861 shipped, tracking 1Z999AA10123456784", "supply.example")
    assert refs == ["ORD:supply.example:28219861", "TRK:1Z999AA10123456784"]
    ctx = _ctx(cfg, profession, id_index={"ORD:supply.example:28219861": ["p3"]})
    m = match_email_to_projects({"from_email": "orders@supply.example", "subject": "Order #: 28219861 shipped",
                                 "body": "", "direction": "inbound"}, ctx)
    assert m[0]["project_id"] == "p3" and unique_exact_match(m, ctx) == "p3"


def test_contact_match_inbound_outbound_and_internal(cfg, profession):
    ctx = _ctx(cfg, profession)
    inbound = {"from_email": "dana@northwind.example", "from_name": "Dana", "subject": "hi", "body": "", "direction": "inbound"}
    assert match_email_to_projects(inbound, ctx)[0] == {"project_id": "p1", "confidence": "high", "match_type": "from_email"}
    outbound = {"from_email": "sam@acme.example", "to": "dana@northwind.example", "subject": "hi", "body": "",
                "direction": "outbound"}
    assert match_email_to_projects(outbound, ctx)[0]["match_type"] == "to_email"
    internal = {"from_email": "coworker@acme.example", "subject": "lunch", "body": "", "direction": "inbound"}
    assert match_email_to_projects(internal, ctx) == []
    # stale contact demotes to medium
    stale = {"from_email": "lee@harbor.example", "subject": "hi", "body": "", "direction": "inbound"}
    m = match_email_to_projects(stale, ctx)[0]
    assert m["confidence"] == "medium" and m["match_type"].startswith("from_email,stale")


def test_keyword_rarity_floor_margin_and_recency(cfg, profession):
    ctx = _ctx(cfg, profession)
    # "northwind" + "plaza" + "lobby" are unique to p1 -> strong
    e = {"from_email": "x@y.example", "subject": "Northwind Plaza lobby question", "body": "", "direction": "inbound"}
    m = match_email_to_projects(e, ctx)
    assert m and m[0]["project_id"] == "p1" and m[0]["confidence"] == "high"
    # "harbor roof" shared by two FRESH projects -> ambiguous, never a prefill
    projs = _projects() + [{"id": "p4", "name": "Harbor Point roof inspection", "status": "action_required",
                            "location": "Harbor Point", "contacts": [], "key_details": {},
                            "last_activity": date.today().isoformat()}]
    ctx_amb = MatchContext(projs, profession, cfg, id_index={})
    e2 = {"from_email": "x@y.example", "subject": "harbor roof", "body": "", "direction": "inbound"}
    m2 = match_email_to_projects(e2, ctx_amb)
    assert m2 and all(x["confidence"] == "low" and x["match_type"].startswith("ambiguous") for x in m2)
    # the stale p2 loses to the fresh p3 on the same words: recency decay at work
    m2b = match_email_to_projects(e2, ctx)
    assert m2b[0]["project_id"] == "p3"
    # generic single token: nothing clears the floor
    e3 = {"from_email": "x@y.example", "subject": "roof", "body": "", "direction": "inbound"}
    assert match_email_to_projects(e3, ctx) == []
    assert recency_factor(10) == 1.0 and recency_factor(None) == 0.30 and 0.3 <= recency_factor(200) < 0.5


def test_self_note_flag(cfg, profession):
    ctx = _ctx(cfg, profession)
    e = {"from_email": "sam@acme.example", "to": "sam@acme.example", "subject": "note to self", "body": "",
         "direction": "inbound"}
    match_email_to_projects(e, ctx)
    assert e.get("is_self_note") is True


# ── signals ─────────────────────────────────────────────────────────────────

def test_completion_signals_and_approval(cfg):
    e = {"from_email": "dana@northwind.example", "direction": "inbound", "subject": "RE: proposal",
         "body": "We have approved the proposal, please proceed. The job is scheduled for Tuesday. "
                 "PO 884411 has been issued and the signed contract is attached."}
    sigs = extract_completion_signals(e, cfg)
    assert "scheduled for Tuesday" in sigs and "approved" in sigs and "signed" in sigs and "PO issued" in sigs
    ts = extract_triage_signals(e, cfg)
    assert ts["approval"] is True and ts["phrases"]
    # question about approval is not an approval; permit approval is excluded
    q = dict(e, body="Has the proposal been approved yet?")
    assert extract_triage_signals(q, cfg)["approval"] is False
    permit = dict(e, body="The permit was approved by the city this morning.")
    assert extract_triage_signals(permit, cfg)["approval"] is False
    # internal sender never produces an approval tag
    internal = dict(e, from_email="boss@acme.example")
    assert extract_triage_signals(internal, cfg)["approval"] is False
    # bare words in prose do not fire
    assert extract_completion_signals({"subject": "x", "body": "The colour approval looks fine. Nothing is signed yet."}, cfg) == []


def test_alt_thread_key_only_for_allowlisted(cfg):
    e = {"from_email": "rep@flakymail.example", "to": "sam@acme.example", "subject": "RE: RE: Quote 44 [ext]",
         "direction": "inbound"}
    k1 = compute_thread_key_alt(e, ["flakymail.example"], ["acme.example"])
    e2 = dict(e, subject="Quote 44", from_email="sam@acme.example", to="rep@flakymail.example")
    k2 = compute_thread_key_alt(e2, ["flakymail.example"], ["acme.example"])
    assert k1 and k1 == k2
    assert compute_thread_key_alt(dict(e, from_email="x@other.example"), ["flakymail.example"], []) is None
    assert compute_thread_key_alt(e, [], []) is None


# ── attachments ─────────────────────────────────────────────────────────────

def test_attachment_classify_csv_and_timeout(tmp_path):
    assert classify_attachment("Signed Proposal.pdf") == "pdf_proposal"
    assert classify_attachment("lease agreement signed.pdf") == "pdf_contract"
    assert classify_attachment("site plan A1.pdf") == "pdf_plans"
    assert classify_attachment("photo.JPG") == "image"
    p = tmp_path / "costs.csv"
    p.write_text('item,amount\nwidget,"$1,250.00"\nbolt,$3.00\n')
    d = extract_csv_data(p)
    assert d["headers"] == ["item", "amount"] and d["dollar_amounts"] == [1250.0]

    def slow(fp):
        import time
        time.sleep(3)
        return {"ok": True}
    r = extract_with_timeout(slow, p, timeout_sec=1)
    assert "timed out" in r["error"]


# ── classify ────────────────────────────────────────────────────────────────

def test_rules_classifier_labels(cfg, profession):
    base = {"direction": "inbound", "from_email": "dana@northwind.example", "body_preview": ""}
    assert rules_classify(dict(base, subject="Can you send a quote for the lobby?", body="Can you send a quote?"),
                          profession, cfg)["classification"] == "request"
    r = rules_classify(dict(base, subject="RE: proposal", body="Approved, please proceed."), profession, cfg)
    assert r["classification"] == "approval" and r["needs_reply"] and r["follow_up_days"] == 1
    assert rules_classify(dict(base, from_email="noreply@someapp.example", subject="digest", body=""),
                          profession, cfg)["classification"] == "informational"
    assert rules_classify(dict(base, from_email="coworker@acme.example", subject="x", body="y"),
                          profession, cfg)["classification"] == "internal"
    out = rules_classify({"direction": "outbound", "from_email": "sam@acme.example", "subject": "Proposal attached",
                          "body": "Please see the proposal attached", "has_attachments": True}, profession, cfg)
    assert out["classification"] == "no_reply" and out["follow_up_days"] == 5 and not out["needs_reply"]


def test_llm_override_only_with_valid_label(cfg, profession):
    e = {"direction": "inbound", "from_email": "dana@northwind.example", "subject": "hello", "body": "see below"}
    good = FakeLLM({"triage.classify": {"parsed": {"classification": "scheduling", "needs_reply": True,
                                                   "follow_up_days": 1, "auto_action": "Book it"}}})
    r = classify(e, profession, cfg, llm=good)
    assert r["classification"] == "scheduling" and r["auto_action"] == "Book it" and r["confidence_note"] == "llm"
    bad = FakeLLM({"triage.classify": {"parsed": {"classification": "made_up_label"}}})
    assert classify(e, profession, cfg, llm=bad)["confidence_note"] == "rules"
    boom = FakeLLM({"triage.classify": lambda v: (_ for _ in ()).throw(RuntimeError("down"))})
    assert "llm error" in classify(e, profession, cfg, llm=boom)["confidence_note"]


def test_suggest_project(cfg, profession):
    e = {"from_email": "dana@northwind.example", "from_name": "Dana Whitfield",
         "subject": "FW: Northwind Plaza lobby renovation question PR-10442", "body": "", "direction": "inbound"}
    s = suggest_project(e, profession, cfg, known_clients=["Harbor Tower"])
    assert s["client"] == "Northwind Plaza" and s["topic"] == "Lobby Renovation"
    assert s["name"] == "Northwind Plaza Lobby Renovation" and s["ids"] == ["PR-10442"]
    assert s["is_new_client"] is True
    s2 = suggest_project({"from_email": "lee@harbor.example", "subject": "quick question", "body": ""}, profession, cfg)
    assert s2["client"] == "Harbor" and s2["client_source"] == "domain"
