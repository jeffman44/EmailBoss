# Smoke log

Command: `PB_DATA_ROOT=/tmp/pbtest PB_LICENSE_DEV=1 python -m plannerboard.start --port 8099 --no-tray`

Run at 2026-10-05 10:59:42 against a fresh data root.

### GET / before setup serves the wizard

```
curl -s -X GET http://127.0.0.1:8099/
```

HTTP 200

```
2973 bytes; title: Email Boss Setup; pb-token meta present: True
```

### POST /api/setup (full body)

```
curl -s -X POST http://127.0.0.1:8099/api/setup -H Content-Type:application/json -d '{"user": {"name": "Pat Example", "email": "pat@example.com", "company": "Example Co", "role": "Owner"}, "profession": "general", "paths": {"data_root": "/tmp/pbtest"}, "mail": {"provider": "none"}, "ai": {"provider": "none"}}'
```

HTTP 200

```
{
 "ai_provider": "none",
 "api_token": null,
 "config_path": "/tmp/pbtest/plannerboard.json",
 "features": {
  "analytics": true,
  "game_layer": true,
  "money": true
 },
 "folders_created": [
  "/tmp/pbtest",
  "/tmp/pbtest/mail_cache",
  "/tmp/pbtest/mail_cache/inbox",
  "/tmp/pbtest/mail_cache/sent",
  "/tmp/pbtest/mail_cache/attachments",
  "/tmp/pbtest/backups",
  "/tmp/pbtest/backups/workflows",
  "/tmp/pbtest/logs",
  "/tmp/pbtest/project_files"
 ],
 "mail_provider": "none",
 "preset": "general",
 "problems": [],
 "profession": "general",
 "restart_required": false,
 "secrets_stored": [],
 "status": "ok",
 "workflows_seeded": "workflows.json"
}
```

### GET /api/config shows configured

```
curl -s -X GET http://127.0.0.1:8099/api/config
```

HTTP 200

```
{
 "configured": true,
 "setup_required": false,
 "profession": {
  "overrides": {},
  "template": "general"
 },
 "mail": {
  "gmail": {
   "client_id": ""
  },
  "graph": {
   "client_id": "",
   "tenant": "common"
  },
  "imap": {
   "host": "",
   "inbox": "INBOX",
   "port": 993,
   "sent": "Sent",
   "user": ""
  },
  "poll_seconds": 300,
  "provider": "none"
 },
 "ai": {
  "base_url": "",
  "daily_budget_usd": 2.0,
  "features": {
   "assistant": false,
   "classify": true,
   "draft_reply": true,
   "match_rerank": true,
   "summarize": true
  },
  "key_present": false,
  "model": "",
  "provider": "none"
 },
 "license": {
  "dev_mode": true,
  "key_present": false,
  "server_url": "https://license.example.com"
 },
 "features": {
  "analytics": true,
  "game_layer": true,
  "money": true
 },
 "data_root": "/tmp/pbtest"
}
```

### GET / after setup serves the board

```
curl -s -X GET http://127.0.0.1:8099/
```

HTTP 200

```
title: Email Boss
```

### GET /api/profession

```
curl -s -X GET http://127.0.0.1:8099/api/profession
```

HTTP 200

```
keys: business_hours, customer_visible_fields, date_types, description, entity, features, fields, icon, id, id_patterns, label, money, prompt_profile, sla_hours, teams, triage, user, vocab, work_types, workflows
{
 "id": "general",
 "label": "General",
 "entity": {
  "client": "Client",
  "contact": "Contact",
  "location": "Location",
  "project": "Project",
  "projects": "Projects",
  "quote": "Proposal"
 },
 "work_types": [
  {
   "color": "#8899aa",
   "id": "task",
   "label": "Task",
   "short": "TASK",
   "workflow_id": "general"
  }
 ],
 "id_patterns": [
  {
   "key": "reference_no",
   "label": "Ref #",
   "regex": "\\b[A-Z]{2}-\\d{4,6}\\b",
   "unique_match": true
  }
 ],
 "date_types": [
  {
   "id": "follow_up",
   "label": "Follow-up"
  },
  {
   "id": "deadline",
   "label": "Deadline"
  },
  {
   "id": "sla",
   "label": "SLA"
  }
 ],
 "user": {
  "aliases": [
   "example,pat",
   "patexample"
  ],
  "company": "Example Co",
  "email": "pat@example.com",
  "internal_domains": [],
  "name": "Pat Example",
  "role": "Owner"
 },
 "features": {
  "analytics": true,
  "game_layer": true,
  "money": true
 }
}
```

### GET /api/professions

```
curl -s -X GET http://127.0.0.1:8099/api/professions
```

HTTP 200

```
{
 "ok": true,
 "professions": [
  {
   "description": "Bids through award and preconstruction, change orders, RFIs and submittals.",
   "icon": "\ud83c\udfd7\ufe0f",
   "id": "construction_gc",
   "label": "General Contractor"
  },
  {
   "description": "Engagements from scoping through proposal, kickoff and delivery.",
   "icon": "\ud83d\udcbc",
   "id": "consulting",
   "label": "Consulting"
  },
  {
   "description": "A plain to-do / waiting / done board for any kind of work.",
   "icon": "\ud83d\udccb",
   "id": "general",
   "label": "General"
  },
  {
   "description": "Quote requests through a sold-job ladder (setup, parts, schedule), service calls, and maintenance agreements.",
   "
...
```

### GET /api/workflows

```
curl -s -X GET http://127.0.0.1:8099/api/workflows
```

HTTP 200

```
keys: derived, load_error, ok, path, workflows
workflow ids: ['general']
stages: ['action_required', 'waiting', 'complete', 'ignored']
```

### GET /api/board-tabs

```
curl -s -X GET http://127.0.0.1:8099/api/board-tabs
```

HTTP 200

```
tabs: [('active', 'filter', 0), ('todo', 'filter', 0), ('deadlines', 'deadline', 0), ('closed', 'filter', 0), ('ignored', 'filter', 0), ('stage_waiting', 'stage', 0)]
```

### GET /api/health

```
curl -s -X GET http://127.0.0.1:8099/api/health
```

HTTP 200

```
{
 "status": "ok",
 "version": "1.0.0",
 "db_write_ok": true,
 "last_triage": null,
 "mail": {
  "error": null,
  "last_fetch": null,
  "ok": true,
  "provider": "none"
 },
 "ai": {
  "budget_hit": false,
  "budget_usd": 2.0,
  "calls_today": 0,
  "last_error": "",
  "model": "",
  "ok": false,
  "provider": "none",
  "today_cost_usd": 0.0
 },
 "license": {
  "email": "",
  "expires": "",
  "features": [
   "*"
  ],
  "fingerprint": "4e4f8a94a1ab810471445d787f630d731d1daa4e6f76609fd0aa30211ef232db",
  "grace_days_left": 0,
  "message": "Developer mode: license checks skipped (PB_LICENSE_DEV=1).",
  "plan": "dev",
  "read_only": false,
  "seats": 1,
  "server_url": "https://license.example.com",
  "status": "dev",
  "valid_until": ""
 },
 "watchers": [
  {
   "age_min": 0.0,
   "down": false,
   "expected_every_min": 1440,
   "kind": "loop",
   "last_seen": "2026-10-05T12:59:43-04:00",
   "name": "db_backup",
   "note": "database -> /tmp/pbtest/backups",
   "state": "ok"
  },
  {
   "age_min": null,
   "down": true,
   "expected_every_min": 5,
   "kind": "loop",
   "last_seen": null,
   "name": "triage_loop",
   "note": "mail cache -> triage cards",
   "state": "down"
  }
 ],
 "restart_needed": [],
 "setup_required": false
}
```

### POST /api/projects

```
curl -s -X POST http://127.0.0.1:8099/api/projects -H Content-Type:application/json -d '{"name": "Harbor Plaza Lobby Refresh", "work_type": "task", "location": "Harbor Plaza", "scope": "Refresh lobby finishes", "contacts": [{"name": "Dana Whitfield", "email": "dana@northwind.example"}], "key_details": {"reference_no": "PR-10442", "client": "Northwind"}}'
```

HTTP 201

```
id=proj_001 status=action_required workflow=general subtasks=['Respond']
```

### PUT /api/projects/<id> with key_details merge

```
curl -s -X PUT http://127.0.0.1:8099/api/projects/proj_001 -H Content-Type:application/json -d '{"key_details": {"po_number": "778899"}, "notes": [{"text": "Kickoff call done", "time": "now"}]}'
```

HTTP 200

```
key_details={'client': 'Northwind', 'po_number': '778899', 'reference_no': 'PR-10442'} notes=['Kickoff call done']
```

### POST subtask

```
curl -s -X POST http://127.0.0.1:8099/api/projects/proj_001/subtasks -H Content-Type:application/json -d '{"text": "Measure the lobby", "date_type": "deadline", "follow_up_date": "2026-10-20"}'
```

HTTP 200

```
{
 "deduped": false,
 "project_id": "proj_001",
 "subtask_id": "st_bfc84279"
}
```

### POST complete subtask

```
curl -s -X POST http://127.0.0.1:8099/api/subtasks/st_bfc84279/complete -H Content-Type:application/json -d '{"project_id": "proj_001", "completed_by": "manual"}'
```

HTTP 200

```
{
 "project_id": "proj_001",
 "status": "completed",
 "subtask_id": "st_bfc84279"
}
```

### GET /api/projects (one item)

```
curl -s -X GET http://127.0.0.1:8099/api/projects
```

HTTP 200

```
{
 "approved_amount": null,
 "archived": 0,
 "client": "Northwind",
 "closed_date": null,
 "contacts": [
  {
   "cell": "",
   "company": "",
   "direct": "",
   "email": "dana@northwind.example",
   "mobile": "",
   "name": "Dana Whitfield",
   "phone": "",
   "role": ""
  }
 ],
 "created": "2026-10-05",
 "emails_sent": 0,
 "follow_up_date": null,
 "id": "proj_001",
 "key_details": {
  "client": "Northwind",
  "po_number": "778899",
  "reference_no": "PR-10442"
 },
 "last_activity": null,
 "last_communication": null,
 "location": "Harbor Plaza",
 "location_slug": "harbor_plaza",
 "name": "Harbor Plaza Lobby Refresh",
 "notes": [
  {
   "text": "Kickoff call done",
   "time": "now"
  }
 ],
 "open_question": "",
 "quote_amount": null,
 "quote_sent_date": null,
 "scope": "Refresh lobby finishes",
 "site": "Harbor Plaza",
 "snoozed_until": null,
 "source": "manual",
 "status": "action_required",
 "subtasks": [
  {
   "blocked_by": null,
   "category": "",
   "completed_by": null,
   "completed_date": null,
   "created": "2026-10-05",
   "date_type": "sla",
   "follow_up_date": null,
   "id": "st_b9a702bf",
   "metadata": {
    "auto": "entry_task"
   },
   "priority": 0,
   "sla_created": "2026-10-05T12:59:43",
   "sla_hours": 4.0,
   "sla_type": "response",
   "source": "system",
   "status": "pending",
   "text": "Respond"
  },
  {
   "blocked_by": null,
   "category": "",
   "completed_by": "manual",
   "completed_date": "2026-10-05",
   "created": "2026-10-05",
   "date_type": "deadline",
   "follow_up_date": "2026-10-20",
   "id": "st_bfc84279",
   "metadata": {},
   "priority": 0,
   "sla_created": null,
   "sla_hours": null,
   "sla_type": null,
   "source": "manual",
   "status": "complete",
   "text": "Measure the lobby"
  }
 ],
 "tags": [],
 "updated_at": "2026-10-05T12:59:43-04:00",
 "won_date": null,
 "work_type": "task",
 "workflow_id": "general"
}
```

### Drop a PA-format email into the inbox

```
cp tests/fixtures/inbox_approval.json /tmp/pbtest/mail_cache/inbox/inbox_request.json  (subject/body edited to a quote request)
```

### POST /api/triage/run

```
curl -s -X POST http://127.0.0.1:8099/api/triage/run
```

HTTP 200

```
{
 "at": "2026-10-05T10:59:43",
 "auto_committed": 0,
 "dedup_skipped": 0,
 "dismissed": 0,
 "error_detail": [],
 "errors": 0,
 "scanned": 1,
 "seconds": 0.05,
 "staged": 1
}
```

### GET /api/triage (one card)

```
curl -s -X GET http://127.0.0.1:8099/api/triage
```

HTTP 200

```
{
 "attachment_data": [
  {
   "error": "PDF extraction failed: No /Root object! - Is this really a PDF?",
   "file_type": "pdf_proposal",
   "filename": "Signed Proposal.pdf",
   "full_text_length": 0,
   "page_count": 0,
   "size_bytes": 23,
   "tables": [],
   "text_summary": ""
  }
 ],
 "attachments": [
  {
   "contentType": "application/pdf",
   "isInline": false,
   "name": "Signed Proposal.pdf",
   "size": 24
  }
 ],
 "auto_action": "Respond with the requested information or a proposal",
 "auto_date_type": "sla",
 "auto_follow_up": "2026-10-07",
 "auto_match_reason": "",
 "auto_matched_project": "",
 "auto_reason": "",
 "body_preview": "Can you send a quote for the lobby refresh?",
 "body_raw": "<html><body><p>Hi,</p><p>Can you send a quote for the lobby ...",
 "card_type": "email",
 "cc": "ops@northwind.example",
 "classification": "request",
 "completion_signals": [],
 "confidence": "high",
 "conversation_id": "CONV-NEW-1",
 "direction": "inbound",
 "dismiss_reason": null,
 "doc_links": [],
 "dollar_amount": null,
 "edited": 0,
 "edited_at": null,
 "from_email": "dana@northwind.example",
 "from_name": "Dana Whitfield",
 "has_attachments": 1,
 "id": "msg_5d0a9a3aa587",
 "inline_image_refs": [],
 "internet_message_id": "<abc-001@mail.northwind.example>",
 "is_quote_request": 0,
 "is_self_note": 0,
 "match_reason": "from_email",
 "matched_project": "proj_001",
 "message_id": "AAMkAGExample001",
 "missing_inline_images": 0,
 "needs_reply": true,
 "new_project_location": null,
 "new_project_name": null,
 "project_id": "",
 "project_matches": [
  {
   "confidence": "high",
   "match_type": "from_email",
   "project_id": "proj_001"
  }
 ],
 "reason": null,
 "received_at": "2026-10-01T15:12:03+00:00",
 "resolved_at": null,
 "resolved_by": null,
 "signals": {
  "approval": false,
  "completion": [],
  "phrases": [],
  "refs": [],
  "thread_key_alt": null
 },
 "source_file": "inbox_request.json",
 "source_folder": "inbox",
 "staged_at": "2026-10-05T12:59:43-04:00",
 "status": "pending",
 "subject": "Northwind Plaza lobby project - new request",
 "subtask_completions": [],
 "suggestion": {
  "client": "Northwind Plaza",
  "client_source": "subject_heuristic",
  "confidence": "low",
  "ids": [],
  "is_new_client": false,
  "name": "Northwind Plaza",
  "refs": [],
  "topic": ""
 },
 "summary": "Can you send a quote for the lobby refresh?",
 "thread_key_alt": null,
 "to": "sam@acme.example",
 "triage_actions": [],
 "triage_id": "msg_5d0a9a3aa587",
 "work_type": "",
 "workflow_id": ""
}
```

### POST /api/triage/<id>/approve as __new__

```
curl -s -X POST http://127.0.0.1:8099/api/triage/msg_5d0a9a3aa587/approve -H Content-Type:application/json -d '{"matched_project": "__new__", "new_project_name": "Lobby Refresh Quote", "new_project_location": "Northwind Plaza"}'
```

HTTP 200

```
ok=True project=proj_002 name='Northwind Plaza Lobby Refresh Quote' status=action_required subtasks=['Respond', 'Respond to Dana Whitfield: Northwind Plaza lobby project - new request'] effects={'completed': 0, 'subtask': None} remaining=0
```

### GET /api/projects shows the new project

```
curl -s -X GET http://127.0.0.1:8099/api/projects
```

HTTP 200

```
[('proj_001', 'Harbor Plaza Lobby Refresh', 'action_required'), ('proj_002', 'Northwind Plaza Lobby Refresh Quote', 'action_required')]
```

### GET correspondence on the new project

```
curl -s -X GET http://127.0.0.1:8099/api/projects/proj_002/correspondence
```

HTTP 200

```
{
 "stats": {
  "email_count": 1,
  "file_count": 0,
  "total_file_size": 0
 },
 "first": {
  "id": 1,
  "subject": "Northwind Plaza lobby project - new request",
  "from_email": "dana@northwind.example",
  "direction": "inbound",
  "file_count": 0
 }
}
```

### POST /api/assistant/run (no LLM: rules only)

```
curl -s -X POST http://127.0.0.1:8099/api/assistant/run -H Content-Type:application/json -d '{"max_items": 5}'
```

HTTP 200

```
{
 "run_id": "run_20261005_105943_af84da",
 "max_items": 5,
 "llm_used": false,
 "counts": {
  "acted": 0,
  "deferred": 1,
  "drafted": 0,
  "question_posted": 1,
  "subtask_added": 0
 },
 "triage": {
  "approved": 0,
  "created": 0,
  "dismissed": 0,
  "left": {}
 },
 "nothing_sent": true,
 "escalations": []
}
items: [(None, 'respond', 'question_posted'), (None, 'respond', 'deferred')]
```

### GET /api/assistant/status

```
curl -s -X GET http://127.0.0.1:8099/api/assistant/status
```

HTTP 200

```
{
 "installed": true,
 "enabled": false,
 "running": false,
 "drafts_only": true,
 "last_run": "2026-10-05T10:59:43",
 "last_summary": {
  "counts": {
   "acted": 0,
   "deferred": 1,
   "drafted": 0,
   "question_posted": 1,
   "subtask_added": 0
  },
  "duration_ms": 5,
  "escalations": [],
  "finished": "2026-10-05T10:59:43",
  "item_count": 2,
  "llm_used": false,
  "max_items": 5,
  "nothing_sent": true,
  "run_id": "run_20261005_105943_af84da",
  "started": "2026-10-05T10:59:43",
  "triage": {
   "approved": 0,
   "created": 0,
   "dismissed": 0,
   "left": {}
  }
 }
}
```

### GET /api/questions (posted by the assistant)

```
curl -s -X GET http://127.0.0.1:8099/api/questions
```

HTTP 200

```
count=1 ["How should Pat Example respond to Dana Whitfield on 'Northwind Plaza lobby project - new request'?"]
```

### GET /api/license (dev)

```
curl -s -X GET http://127.0.0.1:8099/api/license
```

HTTP 200

```
{
 "dev_mode": true,
 "email": "",
 "expires": "",
 "features": [
  "*"
 ],
 "fingerprint": "4e4f8a94a1ab810471445d787f630d731d1daa4e6f76609fd0aa30211ef232db",
 "grace_days_left": 0,
 "key_present": false,
 "message": "Developer mode: license checks skipped (PB_LICENSE_DEV=1).",
 "ok": true,
 "plan": "dev",
 "read_only": false,
 "seats": 1,
 "server_url": "https://license.example.com",
 "status": "dev",
 "valid_until": ""
}
```

### GET /api/mail/providers

```
curl -s -X GET http://127.0.0.1:8099/api/mail/providers
```

HTTP 200

```
['pa_folder', 'imap', 'gmail', 'graph', 'none'] current=none
```

### GET /api/ai/providers

```
curl -s -X GET http://127.0.0.1:8099/api/ai/providers
```

HTTP 200

```
['none', 'anthropic', 'openai', 'ollama'] current={'budget_hit': False, 'budget_usd': 2.0, 'calls_today': 0, 'last_error': '', 'model': '', 'ok': False, 'provider': 'none', 'today_cost_usd': 0.0}
```

### POST /api/ai/draft-reply with no model

```
curl -s -X POST http://127.0.0.1:8099/api/ai/draft-reply -H Content-Type:application/json -d '{"subject": "x"}'
```

HTTP 503

```
{
 "error": "AI provider is offline",
 "ok": false
}
```

### POST /api/restart (re-exec in place)

```
curl -s -X POST http://127.0.0.1:8099/api/restart -H Content-Type:application/json -d '{"delay": 0.3}'
```

HTTP 200

```
{
 "note": "poll /api/health; back in a few seconds",
 "ok": true,
 "restarting_in_s": 0.3
}
```

### GET /api/health after restart

```
curl -s -X GET http://127.0.0.1:8099/api/health
```

HTTP 200

```
status=ok uptime_s=4 project_count=2
```


All steps passed.
