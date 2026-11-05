# Email Boss 2.0: the email terminal

Status: v2 (2026-11-03). Synthesis of three brainstorms (UI + gamification, email terminal + search, competitive research), revised after an adversarial review (section 11 records what changed and why). Execution handoff: `docs/HANDOFF_2_0_OPUS.md`.

## 0. One sentence

Email Boss 2.0 is a local-first, keyboard-first email client where every thread belongs to a project card, search is instant and tells you exactly what it did and did not cover, and the only score that matters is the number of loops you closed today.

## 1. Why this shape (evidence)

- Outlook users leave for three reasons, in order: slowness (10 s open-from-notification on the WebView2 rebuild, ~10 processes), search they cannot trust (operators silently broken, re-index hidden), and features removed in a rewrite. Nobody is asking for a new feature set. We win on speed, trustworthy search, and not deleting things.
- Notion Mail died in September 2026 with over half its users never opening the inbox. "AI email client" alone does not hold. The board layer is the job to be done; mail is the feed.
- Superhuman's praised traits are keyboard speed, taught shortcuts, follow-up reminders and one-key email-to-event. Its complaints are price and AI gated behind the top tier.
- Streak research: broken streaks drive quitting, repairs undermine motivation, public leaderboards at work backfire. Gamification must be quiet, optional, tied to real work, with days off and a free skip.
- Pure vector search misses answers that do not share the query's wording. Hybrid (keyword + filters + optional embeddings) only.
- Thunderbird's indexer stalls silently and returns incomplete results; the bug was closed WORKSFORME. Index coverage must be visible and honest, which means it must be computable (section 5.1).
- Pricing norms: flat per-seat tiers $8 to $40, AI via quota and top-up (Spark, Shortwave) rather than open-ended credits. Users dislike unpredictable credit burn.

## 2. Product principles (non-negotiable, in priority order)

1. Local-first and fast. Targets are set from a measured 1.3 baseline (Phase 0), not borrowed: cold open, open-message, search on a real 50k mailbox, benchmarked on the Windows CI runner.
2. Nothing sends by itself. Every outbound is a draft the user clicks Send on. No scheduled send in any form; "Remind me to send at T" is a Today nudge that opens the draft.
3. Every post-install thread has a home. A project card or an explicit "no project". Historical mail (before the install cutoff) is search-only and never asks to be filed.
4. Search explains itself. Operators with inline hints, grouped results, a coverage footer ("body text searched since Oct 4; older mail matched on headers only"), a server-search fallback row on every results list, and a "not indexed" count computed from server totals minus local.
5. No feature removed without a migration. Every 1.x screen, field, stage, template, HP display and CSV import survives, with a parity test.
6. Game mode is Off by default everywhere. Off / Quiet / Game change words and visibility; Off records nothing. XP counts user-initiated closed loops only. Streak days come from the user's business hours. No leaderboards. No guilt copy.
7. AI is optional, metered, visible. Every AI action shows tier and cost before it runs, works without AI (plain fallback), never sends, and never touches historical mail unasked.
8. Privacy decided before the first store write. Encryption at rest, retention and purge are Phase 1 design inputs, not Phase 7 patches.
9. Modules per screen. No new JS file over 1,500 lines. board.js is touched only through named glue hooks.

## 3. The screens

### 3.1 Today (home)
Shipped in 1.3. Becomes the landing screen: Overdue, Reply today, Nudge today, Coming up (7 d), Decisions, New mail. Adds "What changed since you left" (new mail per project, replies received, deadlines moved, rule runs with one-click undo) and an end-of-hours wrap-up card with neutral copy ("3 loops closed, 2 waiting on others").

### 3.2 Mail (the terminal)
Three-pane with a collapsible rail. List and reader read from the local store; the provider is used for writes and refresh only. List is virtualized with fixed row height per density. Reader slides in over the list on narrow widths, docked on wide. Right context pane (`]`): Project, People, Money, Timeline for the open thread.

- Rows: sender, subject, snippet, project chip, waiting-on badge, attachment icon, snooze clock, age. One line. Density toggle and large text honoured.
- Reading pane header: Reply, Reply all, Forward, File to project, Set status, Snooze, Remind if no reply, Mute, Pin, Unsubscribe (RFC 8058 one-click when present, else a list-unsubscribe mailto draft).
- Body stays in the existing sandboxed iframe (`sandbox; default-src 'none'`) behind the existing image proxy. Remote images never load during indexing, preview or the What-changed block.
- Split inbox and VIP: 2.x.

### 3.3 Board
Unchanged 1.x kanban plus thread count and oldest-unanswered badge per card, drag a thread onto a card to file it, and the project timeline (every email, note, status change, money and date event on one vertical line). These are the only board.js changes in 2.0 and go through named hooks.

### 3.4 Search (Ctrl+K and the search bar are one thing)
Palette on `Ctrl+K`. Search as you type, grouped: Commands, Projects, People, Messages, Attachments, Saved searches, and always a last row "Search the server for this" (every provider already implements `search`). `>` forces commands, `@` people, `#` projects, `?` asks AI (section 6, separate opt-in).

Operators: `from: to: cc: subject: has:attachment filename: project: status: stage: before: after: newer_than: older_than: is:unread is:starred is:snoozed is:waiting label: in:sent in:drafts in:trash account:`. Quoted phrases, `-` negation, `OR`. Unknown operator shows an inline hint, never a silent zero.

Ranking: FTS5 `ORDER BY bm25()` with column weights (subject 3, from 2, body 1), `LIMIT 500`, then rerank in Python by recency decay (half-life 90 d) and contact affinity. Quoted text stripped before indexing. Tokenizer: `unicode61` plus a trigram side index for filenames, addresses and part numbers, and a prefix index for type-ahead. Attachment text indexed in its own FTS table. Local embeddings: 2.x, never alone.

Footer on every results list: coverage sentence, "N not on this computer" (server total minus local), and a Re-index button. Saved searches are smart folders in the rail.

### 3.5 Compose
Full-window or docked. Identities and signatures per account. `@name` adds a contact to To, `#project` links the draft to a card. Attachment reminder. Selection AI rewrite (shorter / friendlier / firmer) with diff and accept/reject. Reply-To mismatch warning (shipped). Undo send becomes a visible "Sending in 10 s" bar that says the send is dropped if the window closes. Templates/snippets, translate: 2.x.

### 3.6 Calendar
2.0 ships only: invite cards for .ics / iTIP in mail with Accept / Tentative / Decline that draft the iTIP reply (user sends; Graph uses the native accept API when the account is Graph so the organizer side updates), and `b` on a thread to create an event draft. Week view, read-only calendar sync, deadline push, free/busy: 2.1+ (they need `Calendars.Read` and a Google calendar scope, which means re-consent and more verification).

### 3.7 Settings
Existing tabs plus: Accounts (per-account config and secrets, from Phase 1), Search (coverage, re-index, retention, disk budget), Rules, Shortcuts (printable cheat sheet), Game (Off / Quiet / Game, business hours source).

## 4. Keyboard map (Gmail-compatible where one exists, taught in-app)

`j/k` next/prev, `o`/Enter open, `u` back, `e` archive, `#` trash, `r/a/f` reply/all/forward, `c` compose, `/` search, `Ctrl+K` palette, `s` star, `z` snooze, `w` waiting-on, `p` file to project, `t` set status, `b` create event, `m` mute, `x` select, `*a` select all, `g i/g t/g b/g p` go to Inbox/Today/Board/Projects, `?` cheat sheet (global; inside the palette `?` is the AI prefix), `]` context pane, `Ctrl+Z` undo. Focus guards: single-letter keys are inert in compose, contenteditable and inputs. Each key is introduced in the phase that ships its feature (section 9). Coach tips: 2.x.

## 5. Engine

### 5.1 Local mail store and sync truth
Tables `mail_accounts`, `mail_messages`, `mail_threads`, `mail_attachments`, `mail_people` (the existing project-scoped `contacts` table is untouched). `account_id` on every row, canonical identity = account + provider id + Message-ID. Config, keyring secrets, runner state and `processed_messages` dedup become per-account in Phase 1 (today they are one slot per provider).

Coverage: headers for the full history of every folder (inbox, sent, archive, custom, trash); bodies for a configurable window (default 90 d, option all). Delta sync: Gmail history with `messageAdded`, `messageDeleted`, `labelAdded`, `labelRemoved` and a cursor that never advances past unfetched ids; Graph delta including `@removed`; IMAP CONDSTORE/QRESYNC where advertised, else UID windows with a periodic header reconcile. A retry queue for failed fetches. A reconcile pass compares server counts (Gmail `messagesTotal` per label, Graph `totalItemCount`, IMAP `STATUS MESSAGES`) to local and feeds the "not indexed" number.

Known 1.3 defects fixed first (Phase 0): Gmail `max_per_cycle` cap advances `history_id` past unfetched messages (silent loss); failed fetches are skipped forever; Graph ignores `@removed`; Gmail history requests `messageAdded` only; IMAP has no change or deletion propagation.

Backfill policy: an install cutoff is recorded at upgrade. Mail older than it is store-and-search only; it never enters triage, rules, AI or "Needs a home". Store sync and triage are separate jobs.

Data lifecycle: encryption decision in Phase 1 (SQLCipher via a bundled wheel validated on the Windows runner, key in the OS keyring, `secure_delete` on, purge of `mail_cache/processed`, WAL and backups on enable). Retention setting purges FTS rows, the JSON cache and backups together. Disk budget shown in Settings. Purge on account removal. The privacy sentence in Settings names exactly what is on disk and how.

`pa_folder` (drop-folder) accounts feed the store from the folder; search only, no delta.

### 5.2 Rules engine
Runs locally on ingest for mail arriving after the rule was created; never on backfill. Conditions: from, to, subject, body contains, has attachment, list-id, project match. Actions: file to project, label, star, snooze, skip inbox, notify, archive (archive and any mailbox-changing action is gated per rule with a dry-run "would match N in the last 30 days"). No status, stage or money actions: inbound mail must never move a project. Every run audited and undoable from Today.

### 5.3 Threading
RFC 5322 References / In-Reply-To with subject fallback, per account. Manual split/merge overrides persist. Waiting-on with "remind if no reply by" creating a Today nudge. Snooze, pin, mute as thread flags. Existing `project_correspondence` rows are linked to threads by Message-ID in a one-time backfill so history does not show as homeless.

### 5.4 Board integration
File-to-project suggestions on open with a reason. Confirm-only extraction: dates and dollar amounts appear as chips the user clicks to attach to a field; each chip shows its source snippet on hover, including when the match sits in quoted text. Status set from the reading pane. "Needs a home" counts post-cutoff threads only, with bulk "no project".

### 5.5 Attachments
Preview PDF and images only, via bundled pdf.js inside the sandboxed iframe. No office conversion, no OCR in 2.0. Attachment browser via `has:attachment filename:`. Attachments on a filed thread land in the project's Files tab.

## 6. AI (Email Boss credits, relay shipped)

Tasks with tier and plain fallback: triage summary (fast), reply draft (smart), selection rewrite (fast), thread summary (fast), file-to-project rationale (fast), weekly report prose (fast). "?" ask over local search is a separate opt-in with its own consent sentence, per-account and per-folder exclusion, a payload preview before send, and at most 20 snippets. Daily meter, monthly estimate, user-set soft daily cap. Backfilled mail never reaches the relay. Google restricted-scope verification and the security assessment are tracked as a distribution prerequisite (section 10).

## 7. Game layer (2.x, design fixed now)

Unit: a user-initiated closed loop (replied, filed, status advanced by click, deadline met, draft sent). Rule runs and auto-archives earn nothing. Default Off for every profession; opt-in in Settings. Quiet shows counts; Game shows XP, levels, streak, label dictionary. Streak days follow `business_hours.py`; one freeze per week; no guilt copy; the report card is a Today block only, never a draft. HP stays visible in 1.x screens until an explicit migration ships with a parity test. Off mode writes no XP or streak rows.

## 8. Migration and compatibility

- `schema_version` in `app_meta`, forward-only numbered scripts, pre-upgrade backup, rollback = restore that backup with a warning that mail synced since will re-sync.
- Fixtures from real 1.0 through 1.3 databases in `tests/fixtures/db/`.
- PST import out of scope; documented.
- Parity test loads every 1.x route and asserts the named controls.

## 9. Phases (each ships, each is revertable, no time estimates)

| Phase | Scope | Exit criteria |
|---|---|---|
| 0 Baseline + sync truth | Measure 1.3 on a real 50k mailbox (cold open, open-message, sync time); fix the five Phase 0 defects; retry queue; reconcile pass; pytest added to `make check`; Windows benchmark job in CI | Gmail burst over the cap loses nothing; Graph removals and Gmail label/delete events reach the store; thresholds written into `tests/bench/` from the measurements |
| 1 Store + search | Per-account config/secrets/state; `mail_*` tables; install cutoff; encryption decision implemented; retention and purge; FTS5 + trigram + prefix indexes; operators; coverage footer; server fallback row; search bar and results list; `/` `Ctrl+K` | Search golden set passes against server results within coverage; backfill of 10k messages makes zero triage cards and zero `ai_usage` rows; purge removes FTS, cache and backups |
| 2 Terminal UI | Store-backed virtualized list and reader; context pane; undo stack framework; palette grouping; density and large text; mail.js split into `mail_list.js`, `mail_reader.js`, `mail_search.js`, `mail_compose.js`; keys `j k o u e # r a f c s x *a g* ? ]` | No mouse needed for list triage; in-app timing hook meets the Phase 0 thresholds on the Windows runner; CSP regression test green |
| 3 Threads + rules | Threading with overrides, correspondence backfill link, waiting-on, snooze, pin, mute, unsubscribe, rules with audit/dry-run/undo; keys `z w m`, `Ctrl+Z` covers rule runs | Crafted mail matching a rule cannot change status, stage, money or send; every list action is undoable |
| 4 Board glue | Suggestions with reasons, confirm-only chips with source snippet, timeline, drag-to-card, badges, Needs-a-home, What-changed, wrap-up card; keys `p t` | Every post-cutoff thread has a home or explicit none; quoted-text amount test passes |
| 5 Compose + invites | Identities, `@`/`#` mentions, attachment reminder, selection AI with diff, sending bar, invite cards with iTIP draft (Graph native accept), `b` event draft, PDF/image preview, Files tab auto-file | Invite round-trip test checks the organizer side on Gmail and Graph fixtures |
| 6 Release 2.0 | Migrations from 1.0 to 1.3 fixtures, rollback round-trip, privacy tests, parity test, WINDOWS_RUNTIME_CHECK, release notes, PST and pa_folder docs | Adversarial pass clean; no body text in logs |

2.x backlog, in order: game layer, split inbox and VIP, coach tips, templates/snippets, translate, "?" ask, calendar week view and sync, multi-account UI polish, OCR, embeddings. Each phase has a kill criterion: if its exit test cannot pass without touching the next phase, the phase is cut, not extended.

## 10. Distribution prerequisites (not code, tracked here)

Google restricted-scope verification and security assessment (the AI relay makes it likelier), Microsoft publisher verification and admin-consent guidance for SMB tenants, a code-signing certificate, a hosted license server, a privacy page on a real domain.

## 11. What the adversarial review changed

Cut: scheduled send (violated "nothing sends"); office conversion and OCR; split inbox, VIP, coach tips, snippets, translate and the game layer moved to 2.x; multi-account UI deferred but the data model and secrets go per-account in Phase 1. Added: Phase 0 with the five sync defects and a measured baseline; install cutoff and backfill policy; server-search fallback and coverage footer; reconcile against server counts; retry queue; data lifecycle and encryption decided in Phase 1; rules cannot touch status/stage/money and never run on backfill; `mail_*` table names (the existing `contacts` table collided); HP kept until migrated; correspondence-to-thread backfill; `pa_folder` path; pytest in `make check`; Windows benchmark job; nine acceptance tests now written into phase exits. Corrected: Gmail API has no scheduled send; the `data-on-*` dispatcher was not the performance risk, the live-provider list and per-open refetch were; `b` is the one key for create event.

### Phase 0 record

- "The store" in the Phase 0 exit criteria is the sync ledger, `mail_cache/sync.sqlite` (`mail_sync_ledger`, `mail_retry_queue`, `mail_reconcile`): captured server ids with folder, labels, flags and Message-ID, no bodies. Phase 1 folds it into `mail_messages`; nothing in the board database changed.
- The cap defect had a second site: the Gmail first sync (`_list_ids`) also stopped at `max_per_cycle` and dropped the rest of the window. Both paths now queue every discovered id in `state_gmail.json` before the cursor moves.
- Graph delta re-delivers an item when its read or flag state changes; 1.3 captured it again as a new record. A ledger hit is now a flag update, not a second capture.
- IMAP change propagation ships as the UID-window reconcile on every server (`UID SEARCH ALL` for expunges, `FLAGS` for the newest 2000 live UIDs). The CONDSTORE / QRESYNC fast path is an optimization, not a correctness fix, and moves to Phase 1 `mail/sync.py`.
- The bench measures the app's own cost against an in-process fake server (no network). Desktop first-paint timing needs the Phase 2 `window.__eb_timing` hook.
- Adversarial pass (fresh reviewer, code only) found six loss or duplicate paths; all fixed with regression tests in `tests/test_sync_truth_review.py`: retry row cleared before the consumer took the message; IMAP `FETCH` NO read as "no message"; any Gmail `/history` error treated as an expired cursor; a Gmail retry of a since-archived message marked removed and never recaptured; a retry captured a second time after the normal path got it; an IMAP mailbox switch with a reused UIDVALIDITY skipping mail.
- Carried to Phase 1 (not loss, visible today): given-up retries cannot be revived from the UI; a Gmail message labelled both INBOX and SENT counts as inbox locally but in both server totals, so the sent "not on this computer" number reads high by those messages.

### Phase 1 record

- The store is its own file, `mail_cache/mail_store.sqlite`, not tables in `plannerboard.db`: the board database, its backups and its migrations stay untouched, the store can be encrypted alone, and a 50k mailbox does not slow the board. `app_meta.install_cutoff` lives in the board database as planned; the store keeps its own `schema_version` in `mail_meta`. The Phase 0 ledger (`sync.sqlite`) is folded in on first open and deleted.
- Migrations are numbered entries in `mail/store.py` (`MIGRATIONS`), not files under `db/migrations/`: `plannerboard/db.py` already exists, so a `db/` package would shadow it, and a Python list needs no packaging rule for PyInstaller.
- Capture versus backfill: a row the backfill wrote first is not "captured", so new mail the backfill happens to see first still reaches triage. Capture skips only what capture already took.
- `processed_messages` dedup stays keyed by Message-ID across accounts on purpose: one email that reached two accounts is one triage card. Cursors, tokens, store rows, retry and reconcile rows are per account.
- A 1.x configuration becomes one account per provider whose id is the provider id, so an upgrade keeps `state_gmail.json`, the `gmail_token` keyring entry and its rows. New accounts use `mail.accounts` and `mail_token_<id>`. Multi-account UI is still 2.x.
- Initial-sync behaviour change: a fresh install's first 30 days of mail is now historical (before the cutoff), so it is searchable but makes no triage cards. 1.x made cards for it.
- IMAP backfill uses capture ids for the inbox and sent folders and `x:<folder>:<uidvalidity>:<uid>` for every other folder; search results from those folders cannot be opened in the reading pane yet (the reader's ref format differs). Phase 2 reads from the store and removes the gap.
- Board fixtures exist for 1.2 and 1.3 only; 1.0 and 1.1 predate this repository (`tests/fixtures/db/README.md`).
- Encryption: SQLCipher (`sqlcipher3` 0.6.4) is validated on windows-latest as a script and as a PyInstaller one-file exe (`sqlcipher` job). It ships in the Windows bundle and is ON by default (owner decision, 2026-11-04); `PB_STORE_ENCRYPT=0` opts out.
- The install-cutoff gate and the body window use the server's receive time (Gmail `internalDate`, Graph `receivedDateTime`, IMAP `INTERNALDATE`, the drop folder's `receivedDateTime`), never the sender's `Date:` header alone: a skewed sender clock can neither push old mail into triage nor keep new mail out of it.
- Drop-folder (`pa_folder`) records feed the store too; a record received before the cutoff is moved to `processed/<date>/historical/` so triage never sees it.
- Phase 1 adversarial pass (fresh reviewer, code only): three high and five medium findings fixed, each with a regression in `tests/test_store_search_review.py`: Phase 0 rows never indexed; the gate trusting the `Date:` header; `-"phrase"` turning positive; purge leaving body codes, attachment text and the WAL on disk; quoted operator values, `OR` between operators, `label:` by name and `account:` case; punctuation-only queries returning everything; backfill pacing (now a 20 s budget per cycle, one `UID SEARCH ALL` per IMAP folder walk, unreadable messages counted in the coverage line); long store locks (purge in batches, attachment text extracted outside the lock, coverage cached).
- Carried to Phase 2: an account purge route and keyring token removal (no multi-account UI yet); the search golden set compares against a local model of server search, the real provider `search` is exercised only by the fallback row.

### Phase 2 record

- The list and the reading pane read the store. The store now keeps the body as received inside the body window (`mail_bodies`, migration 2) so opening a message makes no server call; older mail opens from the server. Attachment bytes still come from the provider (not cached).
- `mail.js` is split into `mail_core / mail_list / mail_reader / mail_keys / mail_undo / mail_compose` plus the `mail.js` facade the board calls, and `mail_palette.js` for Ctrl+K. There is no `templates/mail.html`: the mail view is built by `mail_list.js` inside `board.html`, as in 1.x.
- `test_no_static_file_over_1500_lines` follows plan section 2 rule 9 ("no NEW JS file over 1,500 lines"): every 2.0 file is capped at 1,500; `board.js` (10,191) and `board.html` predate the rule, may not grow, and change only through the Phase 4 hooks. The handoff wording ("no file in static/") cannot hold without rewriting `board.js`, which invariant 6 forbids.
- `test_csp_no_inline_handlers` found `setup.html` still carried an inline script and 22 inline handlers with no CSP: the script moved to `setup.js`, the handlers to the `data-on-*` dispatcher, and the page gained the same `script-src 'self'` policy as the board.
- The bench reads `window.__eb_timing` through Playwright Chromium on windows-latest, not pywebview's JS bridge: WebView2 is Chromium, the page and server are the same, and a headless runner can drive it. New metrics: `ui_first_list_ms`, `ui_open_message_ms_p50`, `ui_search_ms_p50`, bounds set from the first Windows run. The Phase 0 bounds stay enforced on the server path; a browser open-message (render plus sandboxed frame) cannot be held to the 1 ms API bound.
- Ctrl+K is now the grouped palette (Commands, Projects, People, Messages, Attachments, the server row; `>` `@` `#` `?` prefixes; `?` says AI ask is a later version). `/` stays the search box. Saved searches are not in 2.0.
- Undo covers archive, move, trash (when the account can move) and star / read, single or bulk, from the toast or Ctrl+Z; send keeps its own 10 second window. A plain delete on an account that cannot move is not undoable and the toast says so by not offering Undo. An undo puts the message back where it really lived, read from its own row, not from the folder on screen. On Gmail it restores the exact labels (an archive drops user labels, so "move back to Inbox" is not enough) and every move re-reads the labels into the store. On Graph and IMAP the row follows the new id a move hands out; an IMAP move without COPYUID, or a Graph message the store only knows as "other", is not offered an Undo.
- The store answers a list only for inbox and sent (the capture path keeps them) until the backfill has walked the whole mailbox; any other folder reads the server until then, so a folder the store never synced cannot show as empty or partial.
- The list keeps up with the store: the periodic quiet load re-reads from the top (new mail shows, cursor and open message kept), scroll paging is its own load, and every load carries a sequence number so a folder switch during a page load never paints the old folder.
- The review regressions live in `tests/test_terminal_review.py` (server and browser). The `ui_*` bench bounds are frozen from the Phase 2 Windows run (measured x 1.5); `board.html`'s ceiling is its exact size at Phase 2.
- `sync_full_s` read 320 to 427 s on the Phase 2 runs against the 275 bound. The cause was the bench's fake Gmail, not the product: Phase 2 taught it `/labels`, which the capture cycle asks every pass, and it answered by counting the whole 50k mailbox ten times. With the counts cached per history id the local 50k run matches main (81.5 s against 82.0 s). The bound was not loosened.

### Phase 3 record

- Threading: a message takes, in order, its manual override, the thread of the message it replies to (In-Reply-To, then References newest first) when that message is stored, or the provider's key (Gmail and IMAP already hash the References root with a subject and participants fallback; Graph sends its conversation id). Overrides live in `mail_thread_overrides`, so a re-sync rewrites the message row but not its thread. A reply to a split message follows the split.
- Thread state sits on `mail_threads` (migration 3 columns): pin (top of the inbox), mute and snooze (hidden from the inbox, local only; the server is not touched), waiting-on, and the project the thread is filed to. Labels a rule adds are Email Boss labels on the message row (`local_labels`), not server labels, so a label is not a mailbox change.
- Waiting-on (`w`): on a thread filed to a project it adds a follow-up subtask "Nudge: no reply yet on ..." due on the remind date, which is what Today's "Nudge today" already lists; a reply from someone else completes it. Setting it is a user action, so `add_subtask`'s 1.x reactivation of a closed project still applies there; a reply arriving only completes the task (no status change). An unfiled thread keeps the wait on the thread and shows it in the list; Today lists unfiled waits in Phase 4 with the other board glue (no `board.js` change in Phase 3).
- Correspondence backfill: the first capture cycle after upgrade points every thread at the project its mail is already filed under (by Message-ID, marked done per account); afterwards each captured message is linked as it lands. A thread already filed keeps its project.
- Rules run right after capture stores new mail, never from the backfill job, never on historical mail, never on mail you sent, and only on `received_ts >= created_at` (editing a rule does not move `created_at`). Each (rule, message) runs once. Archive after skip inbox records a no-op, not a failure. Mailbox-changing actions are star, skip inbox and archive; file, label, snooze and notify are local. Filing adds the email to the project's correspondence through `save_correspondence`, which changes no status, stage, money or sent count; the crafted-mail test pins that against a closed project carrying money fields.
- Unsubscribe opens the sender's https page in the browser or turns a mailto into a draft; nothing is sent or fetched by Email Boss on its own.
- Ctrl+Z covers rule runs: the MAIL view polls `/api/mail/rules/runs` and puts each new run on the same undo stack as list actions (one toast per run, a summary toast past three). Gmail delete is now undoable too (Trash is a label); other accounts already trash through the undoable move.
- `mail_rules.js` loads from the same line of `board.html` as `mail_palette.js`, so `board.html` stays at its Phase 2 size.
