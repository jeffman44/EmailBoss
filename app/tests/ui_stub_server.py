#!/usr/bin/env python3
"""
Stub API server for the board UI smoke test.

Serves plannerboard/static/board.html at `/` (and setup.html at `/setup`) with
canned JSON for every endpoint the page calls. No database, no profession
loader: this exists so Playwright can load the page, exercise the drawer,
triage and settings tabs, and prove the UI has zero console errors against
the response shapes DESIGN.md section 3/4 describe.

Run directly:  python tests/ui_stub_server.py [port]
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_file

STATIC = Path(__file__).resolve().parents[1] / "plannerboard" / "static"
TODAY = date.today()
NOW = datetime.now().replace(microsecond=0)


def d(days):
    return (TODAY + timedelta(days=days)).isoformat()


def ts(hours):
    return (NOW + timedelta(hours=hours)).isoformat(timespec="seconds")


PROFESSION = {
    "id": "general", "label": "General", "description": "A plain to-do / waiting / done board.", "icon": "\U0001f4cb",
    "entity": {"project": "Matter", "projects": "Matters", "client": "Client", "location": "Account",
               "contact": "Contact", "quote": "Proposal"},
    "work_types": [
        {"id": "task", "label": "Task", "short": "TASK", "color": "#8899aa", "workflow_id": "general"},
        {"id": "deal", "label": "Deal", "short": "DEAL", "color": "#c0a0f0", "workflow_id": "sales"},
    ],
    "fields": [
        {"key": "reference_no", "label": "Ref #", "type": "id", "show_on": ["card", "drawer", "list"],
         "searchable": True, "group": "Details", "source": "key_details"},
        {"key": "portal_url", "label": "Portal", "type": "url", "show_on": ["drawer", "card"], "group": "Details",
         "source": "key_details"},
        {"key": "priority", "label": "Priority", "type": "enum", "show_on": ["card", "drawer"], "group": "Details",
         "source": "key_details", "options": [{"id": "low", "label": "Low"}, {"id": "high", "label": "High"}]},
        {"key": "due_on", "label": "Due", "type": "date", "show_on": ["drawer", "list"], "group": "Details",
         "source": "key_details"},
        {"key": "quote_amount", "label": "Quoted", "type": "money", "show_on": ["card", "drawer", "list"],
         "searchable": False, "group": "Money", "source": "column"},
    ],
    "id_patterns": [{"key": "reference_no", "label": "Ref #", "regex": "\\b[A-Z]{2}-\\d{4,6}\\b", "unique_match": True}],
    "date_types": [{"id": "follow_up", "label": "Follow-up"}, {"id": "deadline", "label": "Deadline"},
                   {"id": "sla", "label": "SLA"}],
    "teams": [{"id": "north", "label": "North", "color": "#2ecc71", "members": "Ana / Ben"},
              {"id": "south", "label": "South", "color": "#e67e22", "members": "Cy / Dee"}],
    "business_hours": {"tz": "America/New_York", "start": 8, "end": 17, "days": [0, 1, 2, 3, 4]},
    "sla_hours": 4,
    "money": {"currency": "USD", "annual_goal": None},
    "triage": {"classifications": ["request", "client_reply", "approval", "vendor", "scheduling", "internal",
                                   "informational", "needs_reply", "no_reply", "junk"]},
    "vocab": {"keywords": [], "acronyms": [], "scope_phrases": []},
    "prompt_profile": {"industry": "general business", "role_noun": "professional"},
    "customer_visible_fields": ["name", "location", "scope", "contacts"],
    "features": {"money": True, "game_layer": True, "analytics": True},
    "user": {"name": "Sam Rivera", "email": "sam@example.com", "aliases": ["sam", "srivera"],
             "company": "Example Co", "role": "Account Manager", "internal_domains": ["example.com"]},
}

WORKFLOW_DOC = {
    "version": 1,
    "workflows": [
        {"id": "general", "label": "General Task", "icon": "\U0001f4cb", "color": "#a0a0a0",
         "stages": [
             {"id": "action_required", "label": "To Do", "role": "todo", "column": True,
              "owning_task": {"text": "Respond", "date_type": "sla", "sla_hours": 4}},
             {"id": "waiting", "label": "Waiting", "role": "waiting", "column": True},
             {"id": "complete", "label": "Done", "role": "closed", "column": False, "closed": True},
             {"id": "ignored", "label": "Ignored", "role": "ignored", "column": False, "closed": True},
         ], "entry_stage": "action_required", "won_transition": None, "extra_tasks": []},
        {"id": "sales", "label": "Sales", "icon": "\U0001f4b0", "color": "#c0a0f0",
         "stages": [
             {"id": "lead", "label": "Lead", "role": "todo", "column": True,
              "owning_task": {"text": "Qualify and send proposal", "date_type": "follow_up", "days": 2}},
             {"id": "proposal_sent", "label": "Proposal Sent", "role": "out", "column": True,
              "owning_task": {"text": "Follow up on proposal", "date_type": "follow_up", "days": 5}},
             {"id": "won", "label": "Won", "role": "won", "column": True},
             {"id": "cold", "label": "Cold", "role": "cold", "column": False},
             {"id": "closed", "label": "Closed", "role": "closed", "column": False, "closed": True},
         ], "entry_stage": "lead", "won_transition": {"from": "proposal_sent", "to": "won"}, "extra_tasks": []},
    ],
    "tabs": [
        {"id": "active", "label": "Active", "filter": {"closed": False}, "urgency": "sum"},
        {"id": "todo", "label": "To Do", "filter": {"todo": True}, "urgency": "max"},
        {"id": "deadlines", "label": "Deadlines", "filter": {"subtask_date_type": "deadline", "pending": True},
         "urgency": "deadline"},
        {"id": "closed", "label": "Closed", "filter": {"closed": True}, "urgency": "none"},
    ],
    "tab_rules": {"auto_stage_tabs": True},
    "sort": {"mode": "subtask", "priority": [
        {"id": "deadline", "label": "Deadline", "match": {"date_type": "deadline"}},
        {"id": "sla", "label": "SLA", "match": {"date_type": "sla"}},
        {"id": "follow_up", "label": "Follow-up", "match": {"date_type": "follow_up"}},
        {"id": "other", "label": "Everything else", "match": {}}]},
    "hud": {"hp": "won_vs_goal", "mp": "quoted_vs_goal", "xp": "threads_closed"},
    "theme": {"skin": "default"},
}


def _roles():
    out = {}
    for w in WORKFLOW_DOC["workflows"]:
        for s in w["stages"]:
            out[s["id"]] = s.get("role", "")
    return out


def _proj(pid, name, location, wid, status, wt, **kw):
    base = {
        "id": pid, "name": name, "location": location, "location_slug": location.lower().replace(" ", "-"),
        "workflow_id": wid, "status": status, "work_type": wt, "scope": kw.get("scope", ""), "open_question": "",
        "key_details": kw.get("key_details", {}), "quote_amount": kw.get("quote_amount"),
        "approved_amount": kw.get("approved_amount"), "quote_sent_date": kw.get("quote_sent_date"),
        "created": d(-20), "follow_up_date": kw.get("follow_up_date"), "snoozed_until": None,
        "closed_date": kw.get("closed_date"), "won_date": kw.get("won_date"),
        "last_activity": d(-1) + " — Called back about the scope", "last_communication": d(-1),
        "emails_sent": 2, "tags": kw.get("tags", []), "source": "manual", "archived": 0,
        "contacts": kw.get("contacts", []), "subtasks": kw.get("subtasks", []),
        "notes": [{"text": "Created from triage: Re: kickoff", "time": "9/12 10:02 AM"}],
        "team": kw.get("team", ""),
    }
    return base


PROJECTS = [
    _proj("proj_001", "Acme Corp Website refresh", "Acme Corp", "sales", "proposal_sent", "deal",
          scope="Redesign the marketing site and migrate the blog.",
          key_details={"reference_no": "AC-10422", "portal_url": "https://portal.example.com/ac-10422",
                       "priority": "high", "due_on": d(12)},
          quote_amount=18500, quote_sent_date=d(-6), follow_up_date=d(-1), team="north",
          contacts=[{"name": "Priya Shah", "role": "Marketing Director", "email": "priya@acme.example", "phone": "",
                     "direct": "", "mobile": "", "cell": "", "company": "Acme Corp"}],
          subtasks=[
              {"id": "st_a1", "text": "Follow up on proposal", "status": "pending", "source": "system",
               "created": d(-6), "follow_up_date": d(-1), "date_type": "follow_up", "completed_date": None,
               "completed_by": None, "blocked_by": None, "sla_type": None, "sla_created": None, "sla_hours": None,
               "category": None, "priority": 0, "metadata": {}},
              {"id": "st_a2", "text": "Send the revised timeline", "status": "complete", "source": "manual",
               "created": d(-5), "follow_up_date": d(-4), "date_type": "follow_up", "completed_date": d(-4),
               "completed_by": "manual", "blocked_by": None, "sla_type": None, "sla_created": None,
               "sla_hours": None, "category": None, "priority": 0, "metadata": {}},
          ]),
    _proj("proj_002", "Birch Dental Insurance renewal", "Birch Dental", "general", "action_required", "task",
          scope="Renewal paperwork due end of month.",
          key_details={"reference_no": "BD-2231", "priority": "low", "due_on": d(3)}, follow_up_date=d(2),
          contacts=[{"name": "Tom Nguyen", "role": "Office Manager", "email": "tom@birchdental.example",
                     "phone": "555-0101", "direct": "", "mobile": "", "cell": "", "company": ""}],
          subtasks=[
              {"id": "st_b1", "text": "Respond to Tom Nguyen", "status": "pending", "source": "triage",
               "created": ts(-3), "follow_up_date": d(0), "date_type": "sla", "completed_date": None,
               "completed_by": None, "blocked_by": None, "sla_type": "response", "sla_created": ts(-3),
               "sla_hours": 4, "category": None, "priority": 0, "metadata": {}},
              {"id": "st_b2", "text": "File the renewal", "status": "pending", "source": "manual",
               "created": d(-2), "follow_up_date": d(3), "date_type": "deadline", "completed_date": None,
               "completed_by": None, "blocked_by": None, "sla_type": None, "sla_created": None, "sla_hours": None,
               "category": None, "priority": 0, "metadata": {}},
          ]),
    _proj("proj_003", "Cedar Labs Onboarding", "Cedar Labs", "sales", "won", "deal",
          scope="Kickoff scheduled; collect access credentials.",
          key_details={"reference_no": "CL-7788", "priority": "high"},
          quote_amount=9200, approved_amount=9200, quote_sent_date=d(-15), won_date=d(-3), team="south",
          contacts=[{"name": "Lee Park", "role": "CTO", "email": "lee@cedarlabs.example", "phone": "", "direct": "",
                     "mobile": "", "cell": "", "company": "Cedar Labs"}],
          subtasks=[
              {"id": "st_c1", "text": "Collect portal credentials", "status": "pending", "source": "manual",
               "created": d(-2), "follow_up_date": d(1), "date_type": "follow_up", "completed_date": None,
               "completed_by": None, "blocked_by": None, "sla_type": None, "sla_created": None, "sla_hours": None,
               "category": None, "priority": 0, "metadata": {}},
          ]),
]

TRIAGE = [
    {"triage_id": "tri_001", "card_type": "email", "status": "pending", "direction": "inbound",
     "from_email": "priya@acme.example", "from_name": "Priya Shah", "to": "sam@example.com", "cc": "",
     "subject": "Re: Website refresh proposal", "body_preview": "Thanks Sam, the board approved the budget.",
     "body_raw": "<p>Hi Sam,</p><p>Thanks, the board approved the budget. Can we start in two weeks?</p>"
                 "<p>Priya Shah<br>Marketing Director | Acme Corp</p>",
     "received_at": ts(-2), "conversation_id": "conv_a", "message_id": "<m1@acme.example>", "attachments": [],
     "has_attachments": 0, "project_matches": ["proj_001"], "matched_project": "proj_001", "confidence": "high",
     "classification": "approval", "match_reason": "contact priya@acme.example", "summary": "",
     "ai_summary": "Budget approved; asks to start in two weeks.", "signals": {},
     "suggestion": {"location": "Acme Corp", "name": "Website refresh", "ids": [{"key": "reference_no", "value": "AC-10422"}]},
     "triage_actions": ["approval_signal"], "completion_signals": ["follow up on proposal"],
     "auto_action": "Confirm kickoff date", "auto_follow_up": d(2), "auto_date_type": "follow_up",
     "dollar_amount": None, "is_quote_request": 0, "doc_links": [{"url": "https://docs.google.com/x", "label": "Doc"}],
     "staged_at": ts(-2)},
    {"triage_id": "tri_002", "card_type": "email", "status": "pending", "direction": "inbound",
     "from_email": "new@maple.example", "from_name": "Jordan Maple", "to": "sam@example.com", "cc": "",
     "subject": "Quote for annual service", "body_preview": "Could you send us a quote for the annual plan?",
     "body_raw": "Could you send us a quote for the annual plan? Ref MP-5521.", "received_at": ts(-1),
     "conversation_id": "conv_b", "message_id": "<m2@maple.example>", "attachments": [], "has_attachments": 0,
     "project_matches": [], "matched_project": "", "confidence": "low", "classification": "request",
     "match_reason": "", "summary": "", "signals": {}, "suggestion": {"location": "Maple", "name": "Annual service",
                                                                      "is_new_location": True},
     "triage_actions": [], "completion_signals": [], "auto_action": "Build and send the proposal",
     "auto_follow_up": d(1), "auto_date_type": "follow_up", "dollar_amount": None, "is_quote_request": 1,
     "doc_links": [], "staged_at": ts(-1)},
    {"triage_id": "tri_003", "card_type": "acknowledgment", "status": "pending", "direction": "inbound",
     "from_email": "tom@birchdental.example", "from_name": "Tom Nguyen", "subject": "Renewal question",
     "body_preview": "Any update on the renewal?", "body_raw": "Any update on the renewal?", "received_at": ts(-5),
     "matched_project": "proj_002", "ack_urgency": 62, "ack_elapsed_hours": 3.1, "ack_sla_hours": 4,
     "staged_at": ts(-5)},
    {"triage_id": "tri_004", "card_type": "email", "status": "auto_committed", "direction": "outbound",
     "from_email": "sam@example.com", "to": "lee@cedarlabs.example", "subject": "Kickoff agenda",
     "matched_project": "proj_003", "match_reason": "Auto-filed: thread routing", "received_at": ts(-8),
     "staged_at": ts(-8)},
]

TRIAGE_SEED = [dict(t) for t in TRIAGE]   # pristine copy; tests may reset TRIAGE[:] from it

QUESTIONS = [
    {"id": "q_001", "project_id": "proj_001", "question": "Priya asked for a two-week start. Commit to it?",
     "options": [{"label": "Yes", "detail": "Reply confirming the date"}, {"label": "Ask for three weeks"}],
     "recommend": "Yes", "evidence": "Board approved budget (email 2h ago).", "unblocks": "Kickoff scheduling",
     "asked_by": "assistant", "status": "open", "created": ts(-1)},
]

ALERTS = [
    {"id": 1, "kind": "mail", "key": "mail_stale", "title": "No mail fetched in 6h", "detail": "IMAP watermark unchanged",
     "severity": "warn", "seen_count": 2, "status": "open"},
]

HEALTH = {
    "status": "ok", "version": "0.1.0", "db_write_ok": True, "setup_required": False, "profession": "general",
    "watchers": [], "watchers_down": [], "restart_needed": [], "alerts_open": 1, "alerts_hard": 0,
    "alerts": ALERTS, "last_triage": ts(-1), "triage": {"pending": 3},
    "mail": {"provider": "imap", "ok": True, "last_fetch": ts(-1), "error": None, "pending": 3},
    "ai": {"provider": "anthropic", "ok": True, "today_cost_usd": 0.42, "budget_usd": 2.0, "budget_hit": False},
    "license": {"status": "ok", "plan": "solo", "expires": d(300), "grace_days_left": None},
    "business_hours": PROFESSION["business_hours"], "project_count": 3, "pending_triage": 3,
}

CONFIG = {
    "schema_version": 1, "user": PROFESSION["user"], "profession": {"template": "general", "overrides": {}},
    "paths": {"data_root": "/tmp/pb", "mail_cache": "/tmp/pb/mail_cache", "backups": "/tmp/pb/backups"},
    "server": {"port": 8080, "bind": "127.0.0.1"},
    "mail": {"provider": "imap", "poll_seconds": 300, "client_enabled": True, "send_enabled": False,
             "imap": {"host": "imap.example.com", "port": 993, "user": "sam@example.com"},
             "smtp": {"host": "", "port": 587, "starttls": True, "ssl": False}},
    "ai": {"provider": "anthropic", "model": "claude-sonnet-4-5", "base_url": "", "daily_budget_usd": 2.0,
           "features": {"classify": True, "summarize": True, "match_rerank": True, "draft_reply": True, "assistant": False}},
    "assistant": {"schedule_minutes": 60, "max_items": 10, "drafts_to": "provider"},
    "license": {"server_url": "https://license.example.com"},
    "features": {"money": True, "game_layer": True, "analytics": True},
    "setup_required": False, "version": "0.1.0", "business_hours": PROFESSION["business_hours"],
}

CORRESPONDENCE = {
    "proj_001": [
        {"id": 11, "direction": "inbound", "from_email": "priya@acme.example", "from_name": "Priya Shah",
         "to_list": "sam@example.com", "subject": "Website refresh proposal", "received_at": ts(-50),
         "body_html": "<p>Hi Sam, can you send a proposal for the refresh?</p><p>Priya</p>", "body_preview": "", "file_count": 1},
        {"id": 12, "direction": "outbound", "from_email": "sam@example.com", "from_name": "Sam Rivera",
         "to_list": "priya@acme.example", "subject": "Re: Website refresh proposal", "received_at": ts(-46),
         "body_html": "<p>Hi Priya, proposal attached. Sam Rivera | Account Manager</p>", "body_preview": "", "file_count": 0},
    ],
}

ASSISTANT_LOG = [
    {"ts": ts(-1), "run_id": "run_1", "project_id": "proj_001", "item_type": "triage", "outcome": "deferred",
     "detail": {"reason": "needs_human"}},
    {"ts": ts(-1), "run_id": "run_1", "project_id": "proj_002", "item_type": "project", "outcome": "drafted",
     "detail": {"summary": "Drafted reply to Tom Nguyen"}},
]

app = Flask(__name__, static_folder=None)
app.calls = []       # every API path hit, for the test to inspect
app.unknown = []     # API paths that fell through to the catch-all


@app.before_request
def _log():
    if request.path.startswith("/api/"):
        app.calls.append((request.method, request.path))


@app.route("/")
def index():
    return send_file(STATIC / "board.html")


@app.route("/setup")
def setup_page():
    return send_file(STATIC / "setup.html")


@app.route("/static/<path:name>")
def static_asset(name):
    return send_file(STATIC / name)


# ── mail client stub ────────────────────────────────────────────────────────
MAIL_FOLDERS = [
    {"id": "INBOX", "name": "INBOX", "kind": "inbox", "unread": 1, "total": 2, "path": "INBOX"},
    {"id": "Drafts", "name": "Drafts", "kind": "drafts", "unread": 0, "total": 0, "path": "Drafts"},
    {"id": "Sent", "name": "Sent", "kind": "sent", "unread": None, "total": None, "path": "Sent"},
    {"id": "Trash", "name": "Trash", "kind": "trash", "unread": None, "total": None, "path": "Trash"},
]
MAIL_MSGS = {
    "7:42:INBOX": {"ref": "7:42:INBOX", "id": "m42", "subject": "Lobby project photos", "from": "Priya Shah <priya@acme.example>",
                   "from_email": "priya@acme.example", "to": "sam@example.com", "date": ts(-3), "preview": "",
                   "is_read": False, "flagged": False, "has_attachments": True, "importance": "normal",
                   "conversation_id": "C1", "internet_message_id": "<m42@acme.example>", "folder": "inbox",
                   "body": "<p>Hi Sam, photos attached: <img src='cid:pic1'></p>", "is_html": True, "cc": "", "reply_to": "",
                   "to_list": [{"email": "sam@example.com", "name": ""}], "cc_list": [],
                   "attachments": [{"index": 0, "name": "roof.jpg", "content_type": "image/jpeg", "size": 51200, "is_inline": False, "content_id": None},
                                   {"index": 1, "name": "pic1.png", "content_type": "image/png", "size": 900, "is_inline": True, "content_id": "pic1"}]},
    "7:41:INBOX": {"ref": "7:41:INBOX", "id": "m41", "subject": "Invoice 1042", "from": "ops@birch.example",
                   "from_email": "ops@birch.example", "to": "sam@example.com", "date": ts(-30), "preview": "",
                   "is_read": True, "flagged": True, "has_attachments": False, "importance": "normal",
                   "conversation_id": "C2", "internet_message_id": "<m41@birch.example>", "folder": "inbox",
                   "body": "plain text body", "is_html": False, "cc": "", "reply_to": "", "to_list": [], "cc_list": [], "attachments": []},
}
MAIL_STATE = {"send_enabled": False, "sent": [], "drafts": [], "flags": [], "moved": [], "filed": [], "staged": []}


@app.route("/api/mail/client/capabilities")
def api_mail_caps():
    return jsonify({"ok": True, "provider": "imap", "client_enabled": True, "send_enabled": MAIL_STATE["send_enabled"],
                    "me": "sam@example.com", "signed_in": True,
                    "capabilities": {k: True for k in ("folders", "list", "read", "flags", "move", "delete", "search", "draft", "send", "attachments")}})


@app.route("/api/mail/client/folders")
def api_mail_folders():
    return jsonify({"ok": True, "provider": "imap", "folders": MAIL_FOLDERS})


@app.route("/api/mail/client/messages")
def api_mail_messages():
    q = (request.args.get("q") or "").lower()
    rows = [{k: v for k, v in m.items() if k not in ("body", "attachments")} for m in MAIL_MSGS.values()
            if request.args.get("folder") == "INBOX" and (not q or q in m["subject"].lower())]
    return jsonify({"ok": True, "folder": request.args.get("folder"), "messages": rows, "next_cursor": None, "total": len(rows)})


@app.route("/api/mail/client/message")
def api_mail_message():
    m = MAIL_MSGS.get(request.args.get("ref") or "")
    if not m:
        return jsonify({"ok": False, "error": "message not found"}), 404
    d = dict(m, ok=True, provider="imap")
    d["attachments"] = [dict(a, url=f"/api/mail/client/attachment?ref={m['ref']}&index={a['index']}") for a in m["attachments"]]
    return jsonify(d)


@app.route("/api/mail/client/attachment")
def api_mail_attachment():
    return b"\x89PNG", 200, {"Content-Type": "image/png"}


@app.route("/api/mail/client/compose", methods=["POST"])
def api_mail_compose():
    b = request.get_json(force=True)
    m = MAIL_MSGS[b["ref"]]
    if b.get("mode") == "forward":
        return jsonify({"ok": True, "mode": "forward", "to": "", "cc": "", "subject": "Fw: " + m["subject"],
                        "body_html": "<div>---------- Forwarded message ----------</div>", "reply_ref": None, "attachments": []})
    return jsonify({"ok": True, "mode": b.get("mode"), "to": m["from"], "cc": "ops@acme.example" if b.get("mode") == "reply_all" else "",
                    "subject": "Re: " + m["subject"], "body_html": "<div class='pb-quote'>quoted</div>", "reply_ref": b["ref"], "attachments": []})


@app.route("/api/mail/client/flags", methods=["POST"])
def api_mail_flags():
    MAIL_STATE["flags"].append(request.get_json(force=True))
    return jsonify({"ok": True})


@app.route("/api/mail/client/move", methods=["POST"])
@app.route("/api/mail/client/delete", methods=["POST"])
def api_mail_move():
    MAIL_STATE["moved"].append((request.path, request.get_json(force=True)))
    return jsonify({"ok": True})


@app.route("/api/mail/client/draft", methods=["POST"])
def api_mail_draft():
    MAIL_STATE["drafts"].append(request.get_json(force=True))
    return jsonify({"ok": True, "where": "provider", "id": "9:5:Drafts"})


@app.route("/api/mail/client/send", methods=["POST"])
def api_mail_send():
    if not MAIL_STATE["send_enabled"]:
        return jsonify({"ok": False, "error": "Sending is turned off.", "send_enabled": False}), 403
    MAIL_STATE["sent"].append(request.get_json(force=True))
    return jsonify({"ok": True, "accepted": ["priya@acme.example"], "refused": {}})


@app.route("/api/mail/client/file", methods=["POST"])
def api_mail_file():
    MAIL_STATE["filed"].append(request.get_json(force=True))
    return jsonify({"ok": True, "project_id": request.get_json(force=True).get("project_id"), "correspondence_id": "77"})


@app.route("/api/mail/client/stage", methods=["POST"])
def api_mail_stage():
    MAIL_STATE["staged"].append(request.get_json(force=True))
    return jsonify({"ok": True, "already": False, "staged": 1, "auto_committed": 0})


@app.route("/api/_mail_state")
def api_mail_state():
    return jsonify(MAIL_STATE)


@app.route("/api/profession")
def api_profession():
    return jsonify(PROFESSION)


@app.route("/api/professions")
def api_professions():
    return jsonify({"ok": True, "professions": [
        {"id": "general", "label": "General", "description": "A plain board.", "icon": "\U0001f4cb"},
        {"id": "legal", "label": "Legal", "description": "Matters and deadlines.", "icon": "⚖️"},
        {"id": "real_estate", "label": "Real estate", "description": "Listings and closings.", "icon": "\U0001f3e0"},
    ]})


@app.route("/api/config", methods=["GET", "PUT"])
def api_config():
    if request.method == "PUT":
        b = request.get_json(force=True) or {}
        if isinstance(b.get("mail"), dict):
            CONFIG["mail"].update(b["mail"])
            if "send_enabled" in b["mail"]:
                MAIL_STATE["send_enabled"] = bool(b["mail"]["send_enabled"])
        return jsonify({"ok": True, "config": CONFIG})
    return jsonify(CONFIG)


@app.route("/api/workflows", methods=["GET", "PUT"])
def api_workflows():
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        doc = body.get("workflows") if isinstance(body.get("workflows"), dict) else body
        WORKFLOW_DOC.update(doc)
        return jsonify({"ok": True, "backup": "/tmp/pb/workflows.bak", "workflows": WORKFLOW_DOC,
                        "stages_allowed": list(_roles()), "derived": {"roles": _roles()}})
    return jsonify({"ok": True, "workflows": WORKFLOW_DOC, "path": "/tmp/pb/workflows.json", "load_error": None,
                    "derived": {"ladders": {}, "stage_counts": {}, "all_stages": list(_roles()),
                                "closed_stages": ["complete", "ignored", "closed"], "roles": _roles()}})


@app.route("/api/board-tabs")
def api_board_tabs():
    tabs = []
    for t in WORKFLOW_DOC["tabs"]:
        tabs.append({"id": t["id"], "label": t["label"], "kind": "filter", "count": 0, "member_ids": [],
                     "urgency_kind": t.get("urgency", "max"), "urgency_score": None, "color_hint": None,
                     "workflow_id": None, "stage_id": None, "pulse": False})
    for w in WORKFLOW_DOC["workflows"]:
        for s in w["stages"]:
            if s.get("column"):
                tabs.append({"id": "stage_" + s["id"], "label": s["label"], "kind": "stage", "count": 0,
                             "member_ids": [], "urgency_kind": "max", "urgency_score": None,
                             "color_hint": w.get("color"), "workflow_id": w["id"], "stage_id": s["id"],
                             "role": s.get("role"), "pulse": False})
    return jsonify({"ok": True, "tabs": tabs, "generated_at": NOW.isoformat()})


@app.route("/api/projects", methods=["GET", "POST"])
def api_projects():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        pid = f"proj_{len(PROJECTS) + 1:03d}"
        p = _proj(pid, body.get("name", "New"), body.get("location", body.get("site", "")),
                  body.get("workflow_id", "general"), body.get("status", "action_required"),
                  body.get("work_type", "task"), follow_up_date=body.get("follow_up_date"))
        PROJECTS.append(p)
        return jsonify({"ok": True, "project": p}), 201
    return jsonify({"meta": {"next_id": "4", "last_triage": ts(-1), "last_updated": ts(0)},
                    "projects": PROJECTS, "archived": []})


@app.route("/api/projects/<pid>", methods=["GET", "PUT", "DELETE"])
def api_project(pid):
    p = next((x for x in PROJECTS if x["id"] == pid), None)
    if not p:
        return jsonify({"ok": False, "error": "not_found"}), 404
    if request.method == "DELETE":
        PROJECTS.remove(p)
        return jsonify({"ok": True, "deleted": pid, "name": p["name"]})
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        kd = body.pop("key_details", None)
        notes = body.pop("notes", None)
        if isinstance(kd, dict):
            p["key_details"] = {**p.get("key_details", {}), **kd}
        if notes:
            p["notes"] = p.get("notes", []) + notes
        if "site" in body and "location" not in body:
            body["location"] = body.pop("site")
        p.update(body)
        return jsonify({"ok": True, "project": p})
    return jsonify(p)


@app.route("/api/projects/<pid>/subtasks", methods=["GET", "POST"])
def api_subtasks(pid):
    p = next((x for x in PROJECTS if x["id"] == pid), None)
    subs = [s for s in (p or {}).get("subtasks", []) if s["status"] != "complete"]
    return jsonify({"ok": True, "subtasks": subs, "project_id": pid})


@app.route("/api/projects/<pid>/merge", methods=["POST"])
def api_merge(pid):
    return jsonify({"ok": True, "project": PROJECTS[0], "stats": {"subtasks": 1, "contacts": 1, "notes": 1}})


@app.route("/api/projects/<pid>/correspondence", methods=["GET"])
def api_corr(pid):
    items = CORRESPONDENCE.get(pid, [])
    return jsonify({"ok": True, "correspondence": items,
                    "stats": {"email_count": len(items), "file_count": sum(i.get("file_count", 0) for i in items),
                              "total_file_size": 120000}})


@app.route("/api/correspondence/<int:cid>/files")
def api_corr_files(cid):
    return jsonify({"ok": True, "files": [{"id": 1, "filename": "proposal.pdf", "file_type": "pdf", "file_size": 120000}]})


@app.route("/api/projects/<pid>/intake", methods=["GET", "POST"])
def api_intake(pid):
    if request.method == "POST":
        return jsonify({"ok": True, "intake_id": 7}), 201
    return jsonify({"ok": True, "items": [
        {"id": 7, "intake_type": "text", "content": "Pasted call notes about the refresh scope.", "created_at": ts(-3),
         "processed": 1, "extraction": {"contacts": [{"name": "x"}], "notes": ["n"]}}]})


@app.route("/api/projects/<pid>/intake/<int:iid>", methods=["DELETE"])
def api_intake_del(pid, iid):
    return jsonify({"ok": True, "deleted": iid})


@app.route("/api/projects/<pid>/intake/<int:iid>/process", methods=["POST"])
def api_intake_proc(pid, iid):
    return jsonify({"ok": True, "extraction": {"notes": ["n"]}, "needs_review": False})


@app.route("/api/triage", methods=["GET", "POST"])
def api_triage():
    if request.method == "POST":
        return jsonify({"ok": True, "staged": 1, "auto_committed": 0, "deduped": 0})
    return jsonify({"ok": True, "items": TRIAGE})


@app.route("/api/triage/dismissed")
def api_triage_dismissed():
    return jsonify({"ok": True, "items": [
        {"triage_id": "tri_900", "card_type": "email", "subject": "Newsletter", "from_name": "News", "staged_at": ts(-30)}]})


@app.route("/api/triage/status")
def api_triage_status():
    return jsonify({"pipeline_available": True, "last_run": ts(-1)})


@app.route("/api/triage/<tid>", methods=["PUT", "DELETE"])
def api_triage_item(tid):
    item = next((t for t in TRIAGE if t["triage_id"] == tid), None)
    if request.method == "DELETE":
        if item:
            TRIAGE.remove(item)
        return jsonify({"ok": True, "dismissed": tid, "filed_to": None, "remaining": len(TRIAGE)})
    if item:
        item.update(request.get_json(silent=True) or {})
    return jsonify({"ok": True, "item": item or {}})


@app.route("/api/triage/<tid>/approve", methods=["POST"])
def api_triage_approve(tid):
    item = next((t for t in TRIAGE if t["triage_id"] == tid), None)
    if item:
        TRIAGE.remove(item)
    return jsonify({"ok": True, "project": PROJECTS[0], "thread_siblings_committed": 0})


@app.route("/api/triage/<tid>/revive", methods=["POST"])
def api_triage_revive(tid):
    return jsonify({"ok": True, "revived": tid, "remaining": len(TRIAGE)})


@app.route("/api/questions", methods=["GET"])
def api_questions():
    pid = request.args.get("project_id")
    rows = [q for q in QUESTIONS if not pid or q["project_id"] == pid]
    return jsonify({"questions": rows, "count": len(rows)})


@app.route("/api/questions/<qid>", methods=["POST", "DELETE"])
def api_question(qid):
    return jsonify({"id": qid, "status": "answered"})


@app.route("/api/alerts")
def api_alerts():
    return jsonify({"alerts": ALERTS, "count": len(ALERTS)})


@app.route("/api/alerts/<int:aid>/ack", methods=["POST"])
def api_alert_ack(aid):
    return jsonify({"ok": True, "id": aid})


@app.route("/api/health")
def api_health():
    return jsonify(HEALTH)


@app.route("/api/hp-history", methods=["GET", "PUT"])
def api_hp():
    if request.method == "PUT":
        return jsonify({"ok": True, "days_stored": 1})
    return jsonify({"ok": True, "days": {}})


@app.route("/api/boss-dps")
def api_boss_dps():
    return jsonify({"ok": True, "date": TODAY.isoformat(), "series": [
        {"date": d(-i), "damage": 400 - i * 20, "dpm": 3.1 - i * 0.1, "n": 12} for i in range(7, 0, -1)],
        "stats": {"total": 2000, "best_dpm": 3.0, "avg_dpm": 2.5}})


@app.route("/api/email-analytics")
def api_email_analytics():
    days = int(request.args.get("days", 28))
    return jsonify({"ok": True, "window_days": days,
                    "volume": {"total": 42, "inbound": 25, "outbound": 17,
                               "by_day": [{"date": d(-i), "inbound": i % 4, "outbound": i % 3} for i in range(days, 0, -1)]},
                    "response_time": {"median_biz_hours": 1.4, "p90_biz_hours": 6.2, "samples": 14, "awaiting_reply": 2},
                    "sla": {"hit": 11, "breached": 2, "open": 1, "hit_pct": 85},
                    "follow_ups": {"open": 5, "overdue": 1, "oldest_days": 3},
                    "oldest_waiting": [{"project_id": "proj_001", "days_overdue": 3, "text": "Follow up on proposal"}],
                    "top_contacts": [{"contact": "priya@acme.example", "name": "Priya Shah", "inbound": 6, "replied": 5,
                                      "median_response_hours": 1.1}],
                    "top_threads": [{"subject": "Website refresh proposal", "messages": 7, "inbound": 4, "outbound": 3}]})


@app.route("/api/mail/status")
def api_mail_status():
    return jsonify({"ok": True, **HEALTH["mail"]})


@app.route("/api/mail/test", methods=["POST"])
def api_mail_test():
    return jsonify({"ok": True, "detail": "IMAP login ok, 3 new messages"})


@app.route("/api/mail/providers")
def api_mail_providers():
    return jsonify({"ok": True, "providers": [{"id": p, "label": p} for p in ("pa_folder", "imap", "gmail", "graph", "none")],
                    "current": "imap"})


@app.route("/api/ai/providers")
def api_ai_providers():
    return jsonify({"ok": True, "providers": [{"id": p, "label": p} for p in ("none", "anthropic", "openai", "ollama")],
                    "current": "anthropic", "key_present": True})


@app.route("/api/ai/usage")
def api_ai_usage():
    return jsonify({"ok": True, "cost_usd": 0.42, "calls": 9, "by_task": {"triage.classify": 6, "reply.draft": 3},
                    "budget_usd": 2.0, "budget_hit": False})


@app.route("/api/ai/test", methods=["POST"])
def api_ai_test():
    return jsonify({"ok": True, "model": "claude-sonnet-4-5", "latency_ms": 640})


@app.route("/api/assistant/log")
def api_assistant_log():
    return jsonify({"ok": True, "log": ASSISTANT_LOG, "count": len(ASSISTANT_LOG)})


@app.route("/api/assistant/status")
def api_assistant_status():
    return jsonify({"ok": True, "installed": True, "enabled": False, "schedule_minutes": 60, "last_run": ts(-1),
                    "last_run_id": "run_1"})


@app.route("/api/assistant/run", methods=["POST"])
def api_assistant_run():
    return jsonify({"ok": True, "summary": {"acted": 1, "drafted": 1, "deferred": 1}})


@app.route("/api/license")
def api_license():
    return jsonify({"ok": True, **HEALTH["license"], "key_present": True, "server_url": "https://license.example.com",
                    "email": "sam@example.com"})


@app.route("/api/license/activate", methods=["POST"])
def api_license_activate():
    return jsonify({"ok": True, "status": "ok", "plan": "solo"})


@app.route("/api/license/deactivate", methods=["POST"])
def api_license_deactivate():
    return jsonify({"ok": True})


@app.route("/api/setup", methods=["POST"])
def api_setup():
    body = request.get_json(silent=True) or {}
    app.last_setup_body = body
    return jsonify({"status": "ok", "config_path": "/tmp/pb/plannerboard.json", "profession": body.get("profession", "general"),
                    "problems": [], "secrets_stored": [], "restart_required": False, "api_token": None})


@app.route("/api/setup/probe")
def api_setup_probe():
    return jsonify({"ollama": False, "path": None, "suggest": {"data_root": "/home/me/EmailBoss", "mail_cache": "/home/me/EmailBoss/mail_cache"},
                    "keyring_available": True, "mail": {"providers": ["pa_folder", "imap", "gmail", "graph", "none"], "current": "none"},
                    "ai": {"providers": ["none", "anthropic", "openai", "ollama"], "current": "none", "key_present": False},
                    "professions": [], "configured": False})


@app.route("/api/mail/oauth/start", methods=["POST"])
def api_oauth_start():
    return jsonify({"ok": True, "auth_url": "https://accounts.example.com/o/oauth2/auth?x=1", "state": "abc"})


@app.route("/api/<path:rest>", methods=["GET", "POST", "PUT", "DELETE"])
def api_catchall(rest):
    app.unknown.append((request.method, "/api/" + rest))
    return jsonify({"ok": True, "stub": True})


@app.route("/__stub/calls")
def stub_calls():
    return jsonify({"calls": app.calls, "unknown": app.unknown})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    app.run(host="127.0.0.1", port=port, debug=False)
