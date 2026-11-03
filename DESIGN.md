# Email Boss, Product Edition: Design Contract

Source of truth for every builder working on this fork. The reference install
(the original single-tenant board) is READ-ONLY reference. Nothing in it is modified.

## 0. Goals

1. Generic: any profession runs it. Domain vocabulary, stages, fields, ID
   patterns and prompts come from a **profession template** + user config.
2. Bring-your-own email: Power Automate drop folder (unchanged contract),
   IMAP, Gmail API, Microsoft Graph.
3. Bring-your-own AI: user pastes an API key (Anthropic / OpenAI-compatible /
   local Ollama). Built-in prompts handle triage classification, summaries,
   project matching tie-breaks, draft replies, and an "assistant pass" over
   the board. **Automation writes drafts only; it never sends.** The mail
   client (section 5a) lets a PERSON send, gated by `mail.send_enabled`
   (default false).
4. Licensed: desktop app validates a signed license against a hosted
   license/login portal, with offline grace.
5. Single-user local app, same look and feel as the reference board.

## 1. Repository layout (`/home/claude/product/`)

```
product/
  DESIGN.md
  app/                              # the desktop/server app
    pyproject.toml                  # package name: plannerboard
    plannerboard/
      __init__.py                   # create_app(), __version__
      config.py                     # load/merge/write config; secrets via keyring
      paths.py                      # RESOURCE_DIR vs DATA_ROOT (port of the frozen-build logic)
      db.py                         # SQLite store (ported + generalized from v2/db.py)
      schema.sql
      workflows.py                  # stage roster with ROLES (ported from v2/workflows.py)
      profession.py                 # loads templates, merges user overrides, serves /api/profession
      professions/*.json            # shipped templates (see §3)
      api/
        __init__.py                 # register_blueprints(app)
        projects.py subtasks.py triage.py correspondence.py drafts.py
        questions.py alerts.py health.py workflows_api.py setup.py
        files.py mail_api.py ai_api.py assistant_api.py license_api.py
      mail/
        message.py                  # Message / Attachment dataclasses + PA record (de)serialization
        base.py                     # MailProvider protocol, ProviderState, ConnResult
        pa_folder.py imap.py gmail.py graph.py
        runner.py                   # capture thread(s): provider → PA-shaped files in DATA_ROOT/mail_cache
        secrets.py                  # keyring wrapper (service "EmailBoss")
      triage/
        normalize.py                # scan_email_files port: PA record → email_obj
        text.py                     # strip_html, strip_forward_headers, strip_signature_block, extract_links
        match.py                    # match_email_to_projects port (domain + id patterns injected)
        signals.py                  # completion signals, approval/dispatch-free, order/tracking refs
        attachments.py              # pdf/xlsx/csv extraction with timeouts
        classify.py                 # rules fallback + optional LLM
        pipeline.py                 # run_cycle(db, cfg, profession, llm): dedup → normalize → match → stage/auto-commit
      ai/
        base.py                     # LLMProvider protocol, LLMResult
        anthropic_provider.py openai_provider.py ollama_provider.py
        registry.py                 # prompt registry: load prompts/*.yaml, render with profile vars, run post-validators
        prompts/*.yaml
        usage.py                    # usage ledger table + daily budget cap
        guards.py                   # _parse_json, _ground_bullets, meta/contact filters (ported)
        assistant.py                # the in-app assistant pass (see §6)
      license/
        client.py                   # verify signed license + activation, heartbeat, offline grace
        fingerprint.py
      static/
        board.html                  # ported generic UI
        setup.html                  # first-run wizard incl. profession, mail provider, AI key, license
      start.py                      # entry: lock, migrate, threads, app.run
      tray.py
    scripts/
      install_windows.ps1 install_macos.sh install_linux.sh
      build_pyinstaller.spec
    tests/
      test_*.py                     # pytest; must pass
    README.md
  license_server/                   # hosted portal (FastAPI + SQLite)
    pyproject.toml
    licserver/
      main.py models.py auth.py licenses.py admin.py billing.py keys.py
      templates/*.html static/
    scripts/gen_keys.py
    README.md
  docs/
    ARCHITECTURE.md EMAIL_PROVIDERS.md AI_SETUP.md LICENSING.md POWER_AUTOMATE.md EULA.md PRIVACY.md
```

## 2. Config

File: `<DATA_ROOT>/plannerboard.json`. Resolution: `$PB_CONFIG` → `<$PB_DATA_ROOT>/plannerboard.json` → `~/EmailBoss/plannerboard.json`. Missing file ⇒ `setup_required`.

```json
{
  "schema_version": 1,
  "user": {"name": "", "email": "", "aliases": [], "company": "", "role": "", "internal_domains": []},
  "profession": {"template": "general", "overrides": {}},
  "paths": {"data_root": "", "mail_cache": "", "backups": ""},
  "server": {"port": 8080, "bind": "127.0.0.1", "api_token": ""},
  "mail": {"provider": "pa_folder|imap|gmail|graph|none", "poll_seconds": 300,
           "imap": {"host":"","port":993,"user":"","inbox":"INBOX","sent":"Sent"},
           "gmail": {"client_id":"","client_secret_ref":"keyring"},
           "graph": {"client_id":"","tenant":"common"}},
  "ai": {"provider": "none|anthropic|openai|ollama", "model": "", "base_url": "",
         "daily_budget_usd": 2.0, "features": {"classify": true, "summarize": true, "match_rerank": true,
         "draft_reply": true, "assistant": false}},
  "assistant": {"schedule_minutes": 60, "max_items": 10, "drafts_to": "provider|local"},
  "license": {"key": "", "server_url": "https://license.example.com", "activation": {}},
  "features": {"money": true, "game_layer": true, "analytics": true}
}
```

Secrets (IMAP password, Gmail/Graph tokens, AI API key) live ONLY in the OS
keyring under service `EmailBoss`, keys `imap_password`, `gmail_token`,
`graph_token`, `ai_api_key`. Env fallback: `PB_IMAP_PASSWORD`, `PB_AI_API_KEY`.

Env: `PB_DATA_ROOT`, `PB_CONFIG`, `PB_PORT`, `PB_DB_PATH`, `PB_FORCE_SETUP=1`.

Bind `127.0.0.1` by default. If `server.bind` is not loopback, every `/api/*`
request must carry `Authorization: Bearer <api_token>` (board.html gets the token
injected at `GET /` via a `<meta name="pb-token">`).

## 3. Profession template (`professions/<id>.json`) and `/api/profession`

```json
{
  "id": "general", "label": "General", "description": "…", "icon": "📋",
  "entity": {"project": "Project", "projects": "Projects", "client": "Client",
             "location": "Location", "contact": "Contact", "quote": "Proposal"},
  "work_types": [{"id": "task", "label": "Task", "short": "TASK", "color": "#8899aa", "workflow_id": "general"}],
  "fields": [
    {"key": "reference_no", "label": "Ref #", "type": "text", "show_on": ["card","drawer","list"],
     "searchable": true, "group": "Details", "source": "key_details"},
    {"key": "quote_amount", "label": "Quoted", "type": "money", "show_on": ["card","drawer","list"], "source": "column"}
  ],
  "id_patterns": [{"key": "reference_no", "label": "Ref #", "regex": "\\b[A-Z]{2}-\\d{4,6}\\b", "unique_match": true}],
  "date_types": [{"id": "follow_up", "label": "Follow-up"}, {"id": "deadline", "label": "Deadline"}, {"id": "sla", "label": "SLA"}],
  "teams": [],
  "business_hours": {"tz": "America/Denver", "start": 8, "end": 17, "days": [0,1,2,3,4]},
  "sla_hours": 4,
  "money": {"currency": "USD", "annual_goal": null},
  "triage": {
    "classifications": ["request", "client_reply", "approval", "vendor", "scheduling", "internal", "informational", "needs_reply", "no_reply", "junk"],
    "notification_senders": ["noreply@", "no-reply@", "notifications@"],
    "internal_admin_subjects": ["timesheet", "payroll", "benefits enrollment"]
  },
  "vocab": {"keywords": [], "acronyms": [], "scope_phrases": []},
  "prompt_profile": {"industry": "general business", "role_noun": "professional"},
  "workflows": { "workflows": [ { "id": "general", "label": "General", "icon": "📋", "color": "#8899aa",
      "stages": [
        {"id": "action_required", "label": "Action Required", "role": "todo", "column": true,
         "owning_task": {"text": "Respond", "date_type": "sla", "sla_hours": 4}},
        {"id": "waiting", "label": "Waiting", "role": "waiting", "column": true},
        {"id": "complete", "label": "Done", "role": "closed", "column": false, "closed": true},
        {"id": "ignored", "label": "Ignored", "role": "ignored", "column": false, "closed": true}
      ], "entry_stage": "action_required", "won_transition": null, "extra_tasks": [] } ],
    "tabs": [], "tab_rules": {}, "sort": {"priority": ["deadline", "sla", "follow_up", "aging"]},
    "hud": {"hp": "threads_closed_vs_goal", "mp": "tasks_done_vs_goal", "xp": "threads_closed"}, "theme": "default" }
}
```

**Stage roles** (replace every literal status check): `todo | waiting | out | won | in_progress | closed | cold | ignored`.
`out` = proposal/quote/offer sent, waiting on the client. `won_transition {from,to}` generalizes `sold_transition`.

`/api/profession` returns the EFFECTIVE profession (template deep-merged with
`profession.overrides`) plus `"user"` and `"features"`. `/api/professions` lists
shipped templates `{id,label,description,icon}`. `PUT /api/profession` writes overrides.
`/api/workflows` and `/api/board-tabs` keep their existing response shapes but are
backed by the profession's `workflows` block (stored in `<DATA_ROOT>/workflows.json`
after first run, so the Settings editor keeps working).

Shipped templates: `general`, `hvac_contractor` (the reference roster, generic names),
`construction_gc`, `property_management`, `legal`, `real_estate`, `insurance_agency`,
`consulting`, `it_msp`, `recruiting`. Each defines work_types, fields, id_patterns,
vocab, triage classifications, and a workflows block with roles.

## 4. API: keep the shapes board.html already consumes

Port these from `v2/server.py`, preserving request/response JSON shapes exactly
(read the handler + the board.html caller before porting): `GET/POST /api/projects`,
`GET/PUT/DELETE /api/projects/<id>`, `/merge`, `/subtasks` (GET open, POST),
`POST /api/subtasks/<id>/complete`, `/api/triage` (list/add/approve/delete/dismissed/revive/status/run),
`/api/projects/<id>/correspondence|files|intake|reassign-correspondence`,
`/api/correspondence/<id>`, `/api/drafts`, `/api/questions`, `/api/alerts`(+ack),
`/api/workflows` GET/PUT, `/api/board-tabs`, `/api/health`, `/api/config`,
`/api/setup/*`, `/api/read-file` (sandboxed to DATA_ROOT), `/api/hp-history`,
`/api/email-analytics`, `/api/restart`, `/api/repair/conversation-routing`,
`/api/ai/draft-reply|draft-follow-up|suggest-job-name|project-health|search|weekly-digest`.

Dropped: everything quote-request-form, commission, tech analytics, crews, dispatch, RFQ, build/finish-build,
xlsx/docx, onedrive, pastjobs, price-reconcile, comps, burden-rate, customers/buildings, boss-dps.
(`/api/boss-dps` → return a generic computed object only if `features.game_layer`.)

New: `/api/profession(s)`, `/api/mail/providers|test|status|oauth/start|oauth/callback`,
`/api/ai/providers|test|usage`, `/api/assistant/run|status|log`, `/api/license` GET /
`POST /api/license/activate` / `POST /api/license/deactivate`.

Health (`GET /api/health`) keeps: `status, db_write_ok, watchers[], restart_needed[],
alerts, last_triage, mail{provider, ok, last_fetch, error}, ai{provider, ok, today_cost_usd, budget_usd},
license{status, plan, expires, grace_days_left}, version`.

## 5. Mail layer

Interchange format = the Power Automate record (keys per `v2/imap_capture.PA_REQUIRED_KEYS`),
written to `<DATA_ROOT>/mail_cache/{inbox,sent}/<id>.json` + `attachments/<id>/<name>`.
`mail/message.py` defines `Message`/`Attachment` with `to_pa_record()` / `from_pa_record()`.
Fix at this boundary: map `cc`, keep display names.

```python
class MailProvider(Protocol):
    id: str
    def test_connection(self) -> ConnResult
    def fetch_new(self, state: ProviderState, limit: int = 200) -> Iterator[Message]
    def health(self) -> dict
    def create_draft(self, reply_to: Message | None, to, cc, subject, body_html, attachments=()) -> str | None  # optional
```
- `pa_folder`: no-op fetch (files already there); health = folder reachable + recency.
- `imap`: port `v2/imap_capture.py` (presets, UID watermarks, BODY.PEEK, atomic writes), add XOAUTH2 hook.
- `gmail`: OAuth installed-app (loopback redirect on the board port), `users.history.list` watermark, labels INBOX/SENT, `drafts.create`. Raw HTTPS via `urllib`/`httpx`, no google SDK requirement.
- `graph`: device-code or auth-code PKCE, tenant `common`, delta queries on inbox + sentitems, `createReply` drafts. Tokens in keyring.
`runner.py` runs one thread per configured provider, writes PA files, then calls `triage.pipeline.run_cycle`.

## 5a. Mail client (added 1.1.0)

Optional methods on `MailProvider`, reported through `capabilities() -> dict`
(`folders list read flags move delete search draft send attachments`):

```python
def list_folders(self) -> list[dict]                     # folder_dict(id, name, kind, unread, total, path)
def list_messages(self, folder_id, limit=50, cursor=None, query=None) -> {"messages": [summary_dict], "next_cursor", "total"}
def get_message(self, ref) -> Message | None             # full body + attachment bytes; .flagged set ad hoc
def set_flags(self, ref, read=None, flagged=None) -> bool
def move_message(self, ref, folder_id) -> str | None     # folder_id = provider id or a kind (inbox|archive|trash|junk)
def delete_message(self, ref) -> bool                    # to Trash, never permanent
def send_message(self, to, cc, bcc, subject, body_html, attachments=(), reply_to=None) -> dict
```

`ref` is the provider's native handle (IMAP `uidvalidity:uid:folder`, Gmail
`id|threadId`, Graph message id) and always rides a query parameter or a JSON
body, never a URL path. Folder kinds: `inbox sent drafts trash junk archive
other` (`base.guess_folder_kind`, RFC 6154 attributes first). `compose.py` is
the one builder for outbound MIME (threading headers, text alternative,
inline parts); `smtp.py` is the one SMTP path.

Gates, enforced in `api/mail_client_api.py` on every call: `mail.client_enabled`
(default true) and `mail.send_enabled` (default FALSE; 403 while off). The
assistant and the capture threads never import the send path.

UI: `static/mail.js` + `static/mail.css`, the MAIL view in board.html.
`PBMail.show()` is idempotent across the 30 s render cycle. Bodies render in
a sandboxed iframe with `cid:` rewritten to `/api/mail/client/attachment`.

## 6. AI layer

```python
class LLMProvider(Protocol):
    name: str
    def complete(self, system: str, user: str, *, max_tokens=800, temperature=0.2,
                 json_schema: dict | None = None, model: str | None = None) -> LLMResult
    def available(self) -> bool
@dataclass
class LLMResult: text: str; parsed: Any|None; input_tokens: int; output_tokens: int; cost_usd: float; latency_ms: int; model: str; ok: bool; error: str = ""
```
Providers: `anthropic` (Messages API, raw HTTPS, default `claude-sonnet-4-5`, JSON via tool-use schema),
`openai` (Chat Completions, `response_format=json_schema`, `base_url` configurable so Groq/Together/LM Studio work),
`ollama` (port of `_call`). Price table in `usage.py` for cost estimates; unknown model ⇒ cost 0 + flag.

Prompt registry: `prompts/<id>.yaml` with `id, version, system, user_template, output_schema, max_tokens, temperature, post: [grounding|json|labels:<list>]`.
Template vars from profile: `{user_name} {company} {role} {industry} {internal_domains} {entity_project} {entity_client} {classifications} {vocab_keywords} {acronyms}`.
Shipped prompts: `triage.classify`, `triage.summarize_bullets` (verbatim mode <250 chars, grounding guard),
`triage.suggest_project`, `triage.match_rerank` (tie-break among existing candidates only; never creates a commit),
`reply.no_reply_classifier`, `reply.draft`, `followup.draft`, `project.health`, `search.parse`,
`digest.weekly`, `assistant.board_pass`.

Usage ledger table `ai_usage(ts, task, provider, model, in_tok, out_tok, cost_usd, latency_ms, ok)`;
`daily_budget_usd` cap: when hit, every call short-circuits to the rules fallback and `/api/health.ai.budget_hit=true`.

**Assistant pass** (`ai/assistant.py`, `POST /api/assistant/run`): deterministic loop, LLM for judgment only.
1. Drain triage: for each pending email card, resolve project (unique match → approve; ambiguous → `match_rerank` → approve only if confidence ≥ 0.85 and one candidate; else leave with reason code `multi_project|no_project|needs_human`). Junk classes → dismiss. Never auto-create a project unless `assistant.auto_create=true`.
2. Walk the top N open projects by board order (`sort.priority`): for each, `project.health`; if an inbound message is awaiting reply → `reply.no_reply_classifier` → `clear | draft | ask`. Draft ⇒ `reply.draft` → `MailProvider.create_draft` if supported else `POST /api/drafts`. Ask ⇒ `/api/questions`.
3. Every item ends in one of `acted | drafted | question_posted | subtask_added | deferred`, logged to `assistant_log` table and `<DATA_ROOT>/logs/assistant_YYYY-MM-DD.jsonl`.
Hard rules: drafts only; never changes status except the sanctioned cascades; never writes money fields; customer-facing text may only reference fields in the profession's `customer_visible_fields` list.

## 7. Licensing

License key = base64url(`payload_json` + `.` + Ed25519 signature) issued by the server; payload
`{lid, email, plan, seats, features[], issued, expires}`. App ships the server's PUBLIC key.
Activation: `POST {server}/v1/activate {key, fingerprint, app_version}` → signed activation
`{lid, fingerprint, issued, valid_until(+30d)}`. Heartbeat `POST /v1/heartbeat` daily refreshes it.
Offline grace: 14 days past `valid_until`, then read-only mode (board loads, writes 402).
Dev mode: `PB_LICENSE_DEV=1` skips validation with a visible banner. `cryptography` lib for Ed25519.

License server (FastAPI, SQLite, `passlib[bcrypt]`-free: use `hashlib.scrypt`):
accounts (signup/login/sessions, password reset token), licenses (issue/revoke/renew, plans trial/solo/team),
activations (list/deactivate seat), `/v1/activate`, `/v1/heartbeat`, `/v1/deactivate`, admin pages,
customer portal pages (my licenses, devices, download link), Stripe webhook stub (`checkout.session.completed` → issue).
Templates: server-rendered Jinja, same dark theme as the board.

## 8. Rules for every builder

- Python 3.11+, stdlib first; allowed deps: flask, cryptography, keyring, pyyaml, pdfplumber(optional), openpyxl(optional), httpx(optional). License server: fastapi, uvicorn, jinja2, cryptography.
- Port by lifting from `/home/claude/src/Planner` where the code is generic; cite the source line range in a comment. Remove every reference-install literal: the company name and its domain, people's names and logins, the ERP's names and form acronyms, dispatcher names, tonnage, city names, OneDrive, SharePoint and local drive paths. `make check` carries the exact pattern.
- Timezone + business hours from the profession config, never hardcoded.
- No em dashes in UI strings or docs.
- Each module gets tests in `app/tests/`. `python -m pytest -q` must pass. `python -m compileall` must pass.
- Never bind 0.0.0.0 by default. Never store secrets in JSON.

## 9. `db.Store` interface (the seam between core, triage, and AI builders)

`plannerboard/db.py` exposes `class Store` (one per process, thread-safe via per-thread
SQLite connections, WAL). Builders of `triage/` and `ai/` code ONLY against these methods;
the core builder implements them. Dict shapes mirror the existing `/api/projects` JSON.

```python
# projects
active_projects() -> list[dict]            # not archived, not closed-role; includes contacts[], subtasks[], key_details{}
all_projects(include_archived=False) -> list[dict]
get_project(pid) -> dict | None
create_project(name, workflow_id, status, work_type="", contacts=None, key_details=None, scope="", tags=None, source="") -> str  # returns id
update_project(pid, **fields) -> dict      # spread-merges key_details; validates status against workflows
# indexes for matching
contact_index() -> dict[str, list[str]]    # email_lower -> [pid]
id_index() -> dict[str, list[str]]         # id_pattern value -> [pid]  (scans key_details + notes with profession id_patterns)
keyword_index() -> dict[str, list[str]]    # token -> [pid] over name/scope
# threads + dedup
routing_lookup(thread_key) -> str | None
routing_set(thread_key, pid, source="") -> None
message_seen(message_id) -> bool
message_mark(message_id, status, pid=None, triage_id=None) -> None
# triage
add_triage_card(card: dict) -> str         # card keys: card_type, direction, from_email, from_name, to, cc, subject, body_raw, body_preview,
                                           # received_at, conversation_id, message_id, attachments[], project_matches[], matched_project,
                                           # classification, summary, signals{}, suggestion{}, source_file
list_triage(status="pending") -> list[dict]
approve_triage(triage_id, pid_or_new, *, workflow_id=None, status=None, work_type=None, new_name=None, actor="system") -> dict
dismiss_triage(triage_id, reason="", actor="system") -> None
# filing
file_correspondence(pid, email_obj: dict, attachment_paths: list[str]) -> str
# subtasks / notes / questions / drafts
add_subtask(pid, text, date_type="follow_up", follow_up_date=None, sla_hours=None, priority=None, source="system", category=None) -> str
open_subtasks(pid) -> list[dict]
complete_subtasks_matching(pid, text_prefix, completed_by="system") -> int
add_note(pid, text, source="system") -> None
add_question(pid, question, options: list[dict], recommend="", evidence="", unblocks="", asked_by="assistant") -> str
open_questions(pid=None) -> list[dict]
add_draft(pid, to, cc, subject, body, reply_to_corr_id=None, created_by="assistant") -> str
# ai bookkeeping
record_usage(task, provider, model, in_tok, out_tok, cost_usd, latency_ms, ok) -> None
usage_today() -> dict                      # {cost_usd, calls, by_task{}}
log_assistant(run_id, pid, item_type, outcome, detail: dict) -> None
assistant_log(limit=200) -> list[dict]
# misc
meta_get(key, default=None) / meta_set(key, value)
add_alert(kind, text, severity="warn") ; open_alerts()
```
