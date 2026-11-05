# Architecture

Email Boss is one Python process: a Flask app serving a single-page board,
an SQLite store in the user's data root, and a few background threads (mail
capture, triage, assistant pass, backups, license heartbeat). Everything binds
to `127.0.0.1` unless configured otherwise.

## Data flow

```
 +------------------+     +---------------------------+     +----------------------+
 | Mail provider    |     | <data_root>/mail_cache/   |     | triage pipeline      |
 |  pa_folder       | --> |   inbox/<stamp>-inbox.json| --> |  normalize           |
 |  imap            |     |   sent/<stamp>-sent.json  |     |  match (contacts,    |
 |  gmail           |     |   attachments/<id>/...    |     |   id patterns,       |
 |  graph           |     |  (PA record = contract)   |     |   thread routing)    |
 +------------------+     +---------------------------+     |  signals / classify  |
        ^                                                   |  (rules, LLM opt.)   |
        | OAuth / IMAP / files                              +----------+-----------+
        |                                                              |
 +------+-----------+                                                  v
 | mail/runner.py   |     +---------------------------+     +----------------------+
 | one thread per   |     | SQLite (db.Store, WAL)    | <-- | triage cards         |
 | provider; writes |     |  projects, subtasks,      |     |  unique match ->     |
 | PA files, then   |     |  triage_cards,            |     |   auto-commit        |
 | calls run_cycle  |     |  project_correspondence,  |     |  else -> pending     |
 +------------------+     |  correspondence_files,    |     +----------------------+
                          |  routing, messages_seen,  |
                          |  drafts, questions,       |     +----------------------+
                          |  ai_usage, assistant_log  | <-> | ai/ layer            |
                          +-------------+-------------+     |  registry (prompts)  |
                                        |                   |  providers           |
                                        v                   |  usage meter/budget  |
                          +---------------------------+     |  assistant pass      |
                          | api/ blueprints (Flask)   |     +----------------------+
                          |  /api/projects, /triage,  |
                          |  /correspondence, /drafts |     +----------------------+
                          |  /ai, /assistant, /mail,  | <-> | license/ client      |
                          |  /license, /health ...    |     |  Ed25519 verify,     |
                          +-------------+-------------+     |  heartbeat, grace,   |
                                        |                   |  402 on writes when  |
                                        v                   |  read-only           |
                          +---------------------------+     +----------+-----------+
                          | static/board.html         |                |
                          | static/setup.html         |                v
                          +---------------------------+     hosted license server
```

1. A mail provider produces one JSON file per message in the Power Automate
   (PA) record shape (`mail/message.py`), plus attachment bytes under
   `attachments/<id>/`. The drop-folder provider does nothing: an external
   flow writes the files. IMAP, Gmail and Graph fetch and write them.
2. `triage/pipeline.run_cycle` scans `inbox/` and `sent/`, normalizes each
   record into an `email_obj`, deduplicates by message id, and matches it to
   projects by thread routing, contact email, profession id patterns and
   keywords. Rules (and optionally the model) classify and summarize.
3. A unique match on a known thread auto-commits: the email is filed as
   correspondence, attachments are copied under
   `project_files/<project>/attachments/`, completion signals close matching
   subtasks. Anything else becomes a pending triage card the user approves,
   skips or ignores on the board. Handled source files move to
   `mail_cache/processed/<date>/`.
4. `board.html` reads `/api/projects`, `/api/triage`, `/api/board-tabs` and
   the rest; the stage roster, columns, fields and vocabulary come from
   `/api/profession` and `/api/workflows`.

## Modules

| path | job |
|---|---|
| `plannerboard/__init__.py` | `create_app()`, the `Runtime` object (`app.extensions["pb"]`), static routes, `/manifest.webmanifest`, bearer gate for non-loopback binds |
| `config.py`, `paths.py` | config load/merge/write; `RESOURCE_DIR` (read-only, inside the install or PyInstaller bundle) vs `DATA_ROOT` (writable, never inside the bundle) |
| `db.py`, `schema.sql` | `Store`: per-thread SQLite connections, WAL, migrations, every query the API and the pipelines use |
| `workflows.py`, `ladder.py` | stage roster with roles, owning tasks, advance-on-complete ladders, won/revert cascades |
| `profession.py`, `professions/*.json` | shipped templates, deep merge with user overrides, `/api/profession(s)` |
| `api/*.py` | one blueprint per area; `_common.py` holds the shared helpers (`rt()`, `store()`, `body()`, `error_response()`) |
| `mail/message.py` | `Message` / `Attachment`, `to_pa_record()` / `from_pa_record()`, `validate_record()`, atomic `write_pa_record()` |
| `mail/base.py`, `pa_folder.py`, `imap.py`, `gmail.py`, `graph.py`, `oauth.py`, `runner.py`, `secrets.py` | provider protocol, the four providers, PKCE helpers, capture threads, keyring wrapper |
| `mail/store.py` | the local mail store (`mail_cache/mail_store.sqlite`): `mail_*` tables, numbered forward-only migrations, per-account identity `(account_id, provider_id)`, ingest with FTS5 (subject/sender/body), trigram side index (addresses, filenames, part numbers), attachments, threads, people; optional SQLCipher |
| `mail/index.py` | query language (operators, phrases, `-`, `OR`, inline hints), bm25 search with recency and reply-affinity rerank, coverage sentence |
| `mail/sync.py` | store backfill: the whole mailbox, newest first, headers always and bodies inside the window; never triage |
| `mail/lifecycle.py` | retention purge (store bodies, processed cache, backups together), per-account purge, disk usage |
| `mail/accounts.py` | per-account config (`mail.accounts`), 1.x slots as legacy accounts, keyring token keys |
| `mail/syncdb.py`, `mail/retry_queue.py`, `mail/reconcile.py` | Phase 0 sync truth, now inside the store: retry queue with backoff, server-count reconcile |
| `mail/terminal.py`, `api/mail_terminal_api.py` | the MAIL view's store-backed list and reading pane (`/api/mail/store/list`, `/message`), the context pane (`/context`), store updates after every write and the undo log (`/api/mail/undo/<action_id>`) |
| `static/mail_core.js`, `mail_list.js`, `mail_reader.js`, `mail_keys.js`, `mail_undo.js`, `mail_compose.js`, `mail.js`, `mail_palette.js` | the MAIL view split by concern (shared state on `window.EBMailCore`; `mail.js` keeps the `PBMail` facade): virtualized list with fixed row heights, reader and context pane, Gmail-style keys with focus guards, undo stack, composer, Ctrl+K palette; `window.__eb_timing` for the bench |
| `mail/threading.py`, `mail/rules.py`, `api/rules_api.py`, `static/mail_rules.js` | threads by RFC 5322 linkage with persisted split / merge overrides, thread state (pin, mute, snooze, waiting-on with a project nudge), the one-time correspondence-to-thread link; rules (frozen action tuple, never on backfill or before the rule existed, dry run plus confirmation for mailbox changes, every run audited and undoable); keys `z w m`, palette commands, the Rules editor |
| `api/search_api.py`, `static/mail_search.js` | `/api/mail/search` (+ `/server` fallback), `/api/mail/store/usage`, `/reindex`, `/purge`; the MAIL view's search box, `/` and Ctrl+K |
| `mail/compose.py`, `mail/smtp.py` | outbound message assembly (quoting, threading headers, text alternative, attachments) and SMTP submission for the IMAP provider |
| `api/mail_client_api.py`, `static/mail.js`, `static/mail.css` | the mail client: `/api/mail/client/*` (folders, list, read, attachment, flags, move, delete, compose, draft, send, stage, file) and the MAIL view that drives it |
| `triage/*.py` | normalize, text cleanup, match, signals, attachment text extraction, classify, `pipeline.run_cycle` |
| `ai/base.py`, `*_provider.py`, `registry.py`, `prompts/*.yaml`, `usage.py`, `guards.py`, `assistant.py`, `wiring.py` | provider protocol, Anthropic / OpenAI-compatible / Ollama clients, prompt registry with post-validators, usage ledger and daily budget, grounding guards, the assistant pass, runtime wiring |
| `license/client.py`, `fingerprint.py` | signed key + activation verification, heartbeat, offline grace, read-only middleware |
| `static/board.html`, `setup.html` | the UI and the first-run wizard |
| `start.py`, `__main__.py`, `tray.py` | entry point (lock, logging, threads, serve), `python -m plannerboard`, tray icon |

## Mail client

The capture path above is one-way: provider -> files -> triage. The mail
client is the other direction: a person browsing the mailbox from the board.
Each provider reports `capabilities()`; the API answers 501 for anything the
provider cannot do and the UI hides the control. Two config gates:
`mail.client_enabled` (default on) and `mail.send_enabled` (default OFF,
checked on every `/api/mail/client/send`). The IMAP provider holds one
client connection per process behind a lock and reconnects when the server
drops it; Gmail and Graph are stateless HTTPS. Two bridges feed the board:
`stage` writes the open message into `mail_cache/` and runs one triage cycle
(same dedup, same matching as captured mail); `file` saves it straight onto
a project as correspondence and registers the thread.

## Threads

| thread | interval | note |
|---|---|---|
| mail capture (one per provider) | `mail.poll_seconds` (min 60) | fetch -> PA files -> `run_triage_cycle` |
| triage loop | `mail.poll_seconds` | safety net and the drop-folder case |
| assistant scheduler | `assistant.schedule_minutes` | only when `ai.features.assistant` is on |
| db backup | daily | `sqlite3` backup API into `backups/`, 30-day retention |
| license heartbeat | every 6 h, effective daily | skipped in `PB_LICENSE_DEV=1` |

Each thread registers a watcher; `/api/health.watchers[]` reports the last
beat and `watchers_down[]` names any that missed two intervals.

## AI layer

`ai/registry.py` loads `prompts/*.yaml` (id, version, system, user template,
output schema, max tokens, temperature, post-validators) and renders them with
profile variables from the profession and user config. `LLM.run(prompt_id,
vars)` checks the feature flag, provider availability and the daily budget,
calls the provider, parses JSON when a schema is declared, runs the
post-validators (grounding, label whitelist) and records a row in `ai_usage`.
When the budget is hit every call short-circuits to the rules fallback and
`/api/health.ai.budget_hit` is `true`.

The assistant pass (`ai/assistant.py`) is a deterministic loop that uses the
model for judgment only: drain triage (approve unique matches, dismiss junk,
rerank ambiguous cards and approve only at confidence 0.85 or better with a
single candidate), then walk the top N open projects, rate health, decide
clear / draft / ask, and write drafts via the provider's `create_draft` or
`/api/drafts`. Every item ends in `acted | drafted | question_posted |
subtask_added | deferred` and is logged to `assistant_log` and
`<data_root>/logs/assistant_YYYY-MM-DD.jsonl`.

## License

The app ships the server's Ed25519 public key. A license key is
`base64url(payload).base64url(signature)`; activation binds it to a machine
fingerprint for 30 days and the heartbeat refreshes it. Past `valid_until`
the app allows 14 days of grace, then becomes read-only (board loads, writes
answer 402). Details: `LICENSING.md`.

## Where things live on disk

```
<data_root>/                      $PB_DATA_ROOT or ~/EmailBoss
  plannerboard.json               config (no secrets; those are in the OS keyring)
  plannerboard.db (+ -wal, -shm)  the database
  workflows.json                  stage roster after first run (Settings editor writes it)
  mail_cache/inbox|sent|attachments|processed
  mail_cache/mail_store.sqlite    local mail store + search indexes, retry queue, reconcile counts
  mail_cache/state_<account>.json cursors (Gmail historyId + queued ids, Graph deltaLinks, IMAP UIDs)
  project_files/<project>/attachments|emails|intake
  backups/                        daily db snapshots, workflow backups
  logs/server.log, assistant_*.jsonl
  board.lock                      single-instance lock
```
