# Handoff: Email Boss 2.0 execution

For: the implementing agent (Opus). Read `docs/PLAN_2_0_EMAIL_TERMINAL.md` first; this document is the operating manual for building it. The plan is the what, this is the how. When the two disagree, the plan wins and you flag the disagreement in your report.

## 1. Repo and environment

- Repo: `jeffman44/emailboss` (private). Working clone `/home/claude/emailboss`, main at `0a3872c` or later. Branch per phase: `phase-0-sync-truth`, `phase-1-store-search`, and so on. One PR per phase. Never push a tag; the owner tags releases in the GitHub UI.
- Layout: engine `app/plannerboard/` (Flask, SQLite, package name stays `plannerboard`), native shell `app/plannerboard/desktop.py`, entry `app/scripts/launch.py`, PyInstaller spec `app/scripts/build_pyinstaller.spec`, installer `app/installer/emailboss.iss`, release workflow `.github/workflows/release.yml`, license server `license_server/`, tests `app/tests/`.
- Venv for tests: `/home/claude/ev`. Run `make check` and `make test` from `app/` before every commit. CI tests run on ubuntu-latest; Phase 0 adds a Windows benchmark job.
- The owner's work machine runs SentinelOne; unsigned installers get blocked. You never need to install anything on his machine; the Windows CI runner is your Windows.
- Commit trailer (required on every commit):
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01X8WbRqHPS5GRFUsETN5n9G
  ```

## 2. Invariants (a PR that breaks one is rejected)

1. Nothing sends by itself. No code path calls a send API without a user click in the same request. No scheduled send, no auto-reply, no rule action that sends. Test: grep for `send` call sites in `mail/` and assert every one is reached only from `/api/mail/client/send` with the session gate.
2. The API gate stays: session cookie from `/auth/<token>`, exact Origin check incl. port, `X-Requested-With: EmailBoss` on writes, JSON-only writes except the listed multipart upload routes, Sec-Fetch-Site and Host checks. New routes register under the gate; `_GATE_EXEMPT` gains nothing without a written reason in the PR.
3. CSP `script-src 'self'`. No inline handlers, no `unsafe-inline`, no `eval`. New JS uses the `data-on-*` dispatcher in `static/board_boot.js` or `addEventListener`. The body iframe keeps `sandbox; default-src 'none'` and the image proxy's public-IP checks.
4. IMAP never expunges. UID EXPUNGE paths return 409 as today. UIDVALIDITY change re-indexes.
5. Banned literals anywhere in the repo: the list in the `check` target of the top-level `Makefile` (employer name, internal system names, the owner's work paths). No em dashes in any file, code or docs. `make check` enforces both and, from Phase 0, runs pytest too.
6. No file in `static/` over 1,500 lines after Phase 2. `board.js` changes only inside the hooks named in section 5.
7. Historical mail (before `app_meta.install_cutoff`) never enters triage, rules, AI or "Needs a home".
8. Every new table is `mail_*`. The existing `contacts`, `hp_history`, `project_correspondence` tables are not renamed or dropped in 2.0.
9. Every AI call shows tier and estimated cost before running and has a plain fallback. The relay never receives backfilled mail.
10. Every migration is forward-only, numbered, preceded by a backup, and has a fixture test from 1.0, 1.1, 1.2 and 1.3 databases.
11. No time estimates in any report, doc or commit message.
12. No customer-facing or user-facing copy that condescends ("go home", "you got this"). Neutral counts.

## 3. Phase order and exit tests

Work phases strictly in order. A phase is done when its exit tests pass on CI (ubuntu tests, Windows benchmarks). If an exit test cannot pass without work from a later phase, stop and report; do not pull the later work forward.

### Phase 0: baseline and sync truth
Files: `mail/gmail.py`, `mail/graph.py`, `mail/imap.py`, `mail/runner.py`, `Makefile`, `.github/workflows/release.yml`, `tests/bench/`.
- Fix: Gmail `max_per_cycle` cap advancing `history_id` past unfetched ids (gmail.py ~222 and ~259); skipped fetches never retried (gmail.py ~213); Graph ignoring `@removed` (graph.py ~220); Gmail history requesting `messageAdded` only (gmail.py ~249); IMAP with no deletion or flag propagation (imap.py ~426).
- Add `mail/retry_queue.py` (sqlite-backed, exponential backoff, max attempts, surfaced in health).
- Add `mail/reconcile.py`: per-account server totals (Gmail `messagesTotal` per label, Graph `totalItemCount`, IMAP `STATUS MESSAGES`) vs local counts; result stored for the coverage footer.
- Add pytest to `make check`. Add a `bench` job on `windows-latest` that runs `tests/bench/` against a generated 50k-message fixture and writes `bench.json` as an artifact; thresholds live in `tests/bench/thresholds.json` and are set from the first real run, then frozen.
- Exit tests: `test_gmail_burst_over_cap_loses_nothing`, `test_graph_removed_reaches_store`, `test_gmail_label_and_delete_events`, `test_imap_uidvalidity_reset_reindexes`, `test_failed_fetch_retried`.

### Phase 1: store and search
Files: new `mail/store.py`, `mail/index.py`, `mail/sync.py`, `mail/lifecycle.py`, `api/search_api.py`, `db/migrations/`, `static/mail_search.js`, `config.py`, `mail/secrets.py`.
- Per-account config, keyring secrets (`PB_MAIL_TOKEN_<account_id>`), runner state and dedup keys. Canonical message identity: `(account_id, provider_id, message_id_header)`.
- Tables: `mail_accounts`, `mail_messages`, `mail_threads`, `mail_attachments`, `mail_people`, `mail_messages_fts` (FTS5, `unicode61`, columns subject/from/body with weights 3/2/1), `mail_attachments_fts`, trigram side index for filenames/addresses/part numbers, prefix index for type-ahead. Quoted text stripped before indexing.
- `app_meta.install_cutoff` written on first 2.0 start. `app_meta.schema_version`.
- Encryption: validate a SQLCipher wheel on the Windows runner. If it bundles cleanly, ship it behind `PB_STORE_ENCRYPT` with the key in the keyring and `secure_delete=ON`. If it does not bundle, ship plaintext with `secure_delete=ON`, the retention default at 90 days, and the privacy sentence stating plaintext. Either way the decision is recorded in `docs/PRIVACY.md` before Phase 1 merges.
- Retention and purge: one function purges `mail_messages`, FTS rows, `mail_cache/processed`, and backups older than the window together. Disk usage reported at `/api/mail/store/usage`.
- `pa_folder` accounts: store fed from the drop folder, no delta, search only.
- Operators per plan 3.4 with inline hint on unknown operator. Ranking: `ORDER BY bm25()` `LIMIT 500`, Python rerank by recency half-life 90 d and reply-affinity. Server fallback row calls the provider's existing `search`.
- Coverage footer data from `reconcile.py`.
- Exit tests: `test_search_golden_set` (fixtures of queries with expected ids, local vs provider `search` within coverage), `test_backfill_creates_no_triage_or_ai_rows`, `test_purge_removes_fts_cache_backups`, `test_unknown_operator_hint`, `test_per_account_secrets_two_gmail_accounts`.

### Phase 2: terminal UI
Files: `static/mail.js` split into `mail_list.js`, `mail_reader.js`, `mail_search.js` (from Phase 1), `mail_compose.js`, `mail_keys.js`, `mail_undo.js`; `templates/mail.html`; `api/mail_client_api.py` reading from the store.
- List and reader read from the store; provider only on explicit refresh and writes. Virtualized list with fixed row height per density (compact 32 px, comfortable 44 px, large text adds 8 px). Attachment downloads come from `mail_attachments` when cached, provider otherwise.
- Undo stack framework (`mail_undo.js`): every mutating list action pushes an inverse; `Ctrl+Z` pops. Server side: `/api/mail/undo/<action_id>`.
- Palette grouping, `]` context pane, keys `j k o u e # r a f c s x *a g* ? ]` with focus guards.
- In-app timing hook: `window.__eb_timing` records cold-open-to-first-paint, open-message and search latency; the bench job reads it through pywebview's JS bridge on the Windows runner.
- Exit tests: `test_list_triage_without_mouse` (Playwright, keys only), bench thresholds, `test_csp_no_inline_handlers` (grep every template and static file for `on[a-z]+=` and `javascript:`), `test_no_static_file_over_1500_lines`.

### Phase 3: threads and rules
Files: `mail/threading.py`, `mail/rules.py`, `api/rules_api.py`, `static/mail_rules.js`, migration for `mail_rules`, `mail_rule_runs`, `mail_thread_overrides`.
- Threading per plan 5.3; one-time backfill linking `project_correspondence` to `mail_threads` by Message-ID.
- Rules: conditions and actions per plan 5.2. Action whitelist is a frozen tuple in `rules.py`; status, stage, money and send are absent and a test asserts the tuple. Rules run only on messages with `received_at >= rule.created_at`. Mailbox-changing actions require `dry_run_ack`. Every run writes `mail_rule_runs` and is undoable.
- Keys `z w m`; `Ctrl+Z` covers rule runs.
- Exit tests: `test_crafted_mail_cannot_change_project` (a message matching a rule with every action; assert project status, stage, money fields and sent count unchanged), `test_rules_skip_backfill`, `test_every_list_action_undoable`, `test_threading_overrides_persist`.

### Phase 4: board glue
Files: `api/projects.py` (suggestion endpoint), `mail/extract.py`, `static/mail_reader.js`, `static/board_glue.js`, `board.js` hooks only: `window.PB.hooks.cardBadge(projectId)`, `window.PB.hooks.onThreadDrop(projectId, threadId)`, `window.PB.hooks.timelineTab(projectId)`.
- Suggestions with a reason string from local data (sender history on cards, subject tokens matching card titles); AI rationale only when the user has AI on and clicks.
- Confirm-only chips for dates and amounts; each chip carries its source snippet and whether it came from quoted text; nothing is written without a click.
- Needs-a-home counts post-cutoff only. What-changed block on Today includes rule runs with undo. Wrap-up card with neutral counts.
- Exit tests: `test_every_post_cutoff_thread_has_home_or_none`, `test_chip_shows_source_snippet_quoted_case`, `test_board_js_diff_limited_to_hooks` (diff of `board.js` against the Phase 3 tag touches only the three hook sites).

### Phase 5: compose and invites
Files: `static/mail_compose.js`, `mail/compose.py` (sanitizer stays), `mail/itip.py`, `mail/calendar_graph.py`, `api/ai_api.py` (selection rewrite), `static/pdfjs/` (bundled), `api/files.py` (auto-file).
- Identities and signatures per account; `@` and `#` mentions; attachment reminder; selection rewrite with diff; "Sending in 10 s" bar that states the send is dropped on window close.
- Invite cards: parse `text/calendar` and `.ics`; Accept / Tentative / Decline drafts the iTIP reply for Gmail and IMAP; for Graph call the native accept/decline API after the click. `b` creates an event draft (Gmail Calendar / Graph event) with title, attendees and suggested time pre-filled; scopes added only if the account consents in Settings.
- PDF and image preview with bundled pdf.js inside the sandboxed iframe; nothing else previews.
- Exit tests: `test_invite_roundtrip_gmail_fixture`, `test_invite_roundtrip_graph_organizer_side`, `test_attachment_reminder`, `test_preview_rejects_non_pdf_image`.

### Phase 6: release 2.0
Files: `db/migrations/`, `tests/fixtures/db/{1.0,1.1,1.2,1.3}.sqlite`, `docs/RELEASE_NOTES.md`, `docs/PRIVACY.md`, `docs/INSTALL_WINDOWS.md`, `docs/WINDOWS_RUNTIME_CHECK.md`, `tests/test_parity.py`, `tests/test_privacy.py`.
- Exit tests: migration from each fixture, upgrade-then-rollback round-trip, `test_no_body_text_in_logs`, `test_ai_payload_excludes_opted_out_accounts`, `test_parity_every_1x_route`, `test_game_off_writes_nothing`, and an adversarial review by a separate agent that has not seen the diffs.

## 4. Agent workflow

- You own the plan of record. Use Sonnet subagents for implementation inside one file set at a time; you review every diff. Use a fresh Opus agent for each phase's adversarial pass; give it the code and the exit tests, not your conclusions.
- Partition by file ownership. Two agents never edit the same file in the same phase. `board.js` is owned by one agent in Phase 4 and nobody else, ever.
- Never `git stash`. Never force-push. Never rewrite history on a shared branch.
- Every subagent report ends with the test command it ran and the result. A report without a passing test run is not done.
- When a phase exit test cannot pass without later work, stop, write the finding into `docs/PLAN_2_0_EMAIL_TERMINAL.md` section 11, and report. Do not expand scope.

## 5. Reporting to the owner

- He wants deliverables, not summaries. Each phase report: branch name, PR link, exit tests and their status, anything cut and why, and the one decision only he can make (if any) as a single question.
- Never a time estimate. Never an action he could do himself that you can do. Questions one at a time.
- Keep the owner's employer and its systems out of every file, string, fixture and test name.
- No em dashes anywhere, including this report back to him.

## 6. Open decisions for the owner (ask one at a time, only when the phase reaches them)

1. Phase 1: if SQLCipher bundles on Windows, encrypt by default or opt-in? (Default proposal: on by default, key in keyring.)
2. Phase 1: body retention default 90 days or all? (Default proposal: 90 days, "all" one click away.)
3. Phase 5: request calendar scopes at install or only when the user first presses `b`? (Default proposal: on first press.)
4. 2.x: game layer default stays Off for every profession, including the trades templates? (Default proposal: yes.)
