# 1.3.0 revision plan (from the 12-persona simulation, 2026-11-03)

Twelve simulated users (plumber, property manager, GC, insurance agent,
attorney, recruiter, MSP owner, realtor, consultant, photographer, HVAC AE,
church admin) walked the real UI. Average happiness 2 of 5. The same walls
came up for nearly everyone, so this release is about removing them, not
adding breadth.

## The walls, in order of damage

1. Connecting mail is impossible for normal people. Gmail API and Microsoft
   365 need the user's own OAuth app ("optional" in the wizard is false);
   the wizard posts `client_id` in the wrong shape; OAuth starts before
   config is saved. IMAP + app password is the only working path and it is
   not explained for someone who has only used Gmail on a phone.
2. The license step says "Skip for now (developer mode)" and then the app
   is silently read-only (402 on every write, no handler in the board).
3. AI exists only as a bring-your-own-key form. Nothing in the board or
   mail client calls draft-reply, follow-up, health, search or digest.
   Assistant drafts land in an invisible outbox. Answering an assistant
   question does nothing.
4. The product still looks like the author's private tool: "// PLANNER
   BOARD", skull boss, HP/MP bars, "$ QR", urgency numbers like "187",
   money fields on a church funeral, timezone America/New_York, jargon
   everywhere (APPROVE / SKIP / IGNORE, SLA, Internal email domains).
5. Sending is one click once enabled, Reply-To silently overrides From,
   Reply all has no guard, no undo, Delete/Archive no undo, IGNORE on a
   triage card hides a whole live project, APPROVE ALL swallows failures.

## What 1.3.0 does

### Setup (3 screens, not 7)
- "You and your email": name, email, "Which email do you use?" (Gmail /
  Outlook or Microsoft 365 / Yahoo / iCloud / Other). Gmail and Microsoft
  get the built-in sign-in (shipped OAuth client ids, PKCE, config saved
  before redirect); every choice also has "Use an app password instead"
  with a numbered, screenshot-free guide. Timezone from the browser.
- "Your work": profession cards with plain labels ("What kind of work do
  you do?"), "pick the nearest, you can rename anything later".
- "Done": free 14-day trial starts automatically (one call to the license
  server with the email; key emailed and stored), Email Boss AI on with
  free credits, one privacy sentence ("Your mail stays on this computer.
  When AI helps, the text of that one email goes to Email Boss AI."),
  "Open Email Boss". Data folder, advanced mail settings, BYO AI key,
  license key paste all move to Settings > Advanced.

### Board
- Brand: "Email Boss" everywhere. Game layer and money fields off unless
  the profession turns them on (hvac_contractor, construction_gc keep
  money). Settings > Display toggles for both plus large-text mode (14 px
  minimum) and light theme.
- Today view is the default tab: "Reply today", "Nudge today", "Decisions
  for you", "New mail to sort", "Overdue".
- Decisions for you: one-at-a-time modal over open questions, number keys
  1-4, "Something else", skip. An answer feeds the assistant
  (`direction_note`) and drafts immediately; "Draft ready, open it".
- AI on every card and in the composer: "Write a reply for me", "Write a
  follow-up", "What's the status?", "Ask the board" in the search box,
  "Summarize my week". When AI is off the buttons stay visible with a
  one-line "Turn on AI" link.
- Drafts tab: everything in the board outbox plus what the assistant wrote,
  with "Open in my mail" and "Copy".
- Triage: APPROVE -> "Add to board", SKIP -> "Not now", IGNORE -> "Not
  work, hide" with a confirm when a live project would be hidden; "Add
  all" skips unmatched cards and reports counts; the leave-reason from the
  assistant shows on the card; "$ QR" -> "Needs a quote" and the badge and
  checkbox agree.
- Urgency numbers become words ("Due today", "3 days late"). Dates use the
  local day, not UTC.
- 402 from the engine opens Settings > License with a plain banner.
- Settings > AI: Email Boss AI balance, "Buy credits", usage this month,
  "Use my own key (advanced)" with the key field right there.
- Settings > Fields: add a field (name, type, show on card) without JSON;
  stage presets with plain role names; "Mark won / lost" buttons.
- Timezone and business hours editable; 24x7 option.

### Mail client
- Send confirms with the recipients named; Reply all with more than 3
  recipients confirms; a Reply-To that differs from From is shown in amber
  and From is the default; 10-second undo for send; undo toast for trash
  and archive; autosave of the composer; plain-language errors; Drafts
  folder in the client shows the board outbox too; "Write with AI" in the
  composer.

### Engine and templates
- Questions: several open per project; answers drive the next action.
- Follow-up drafts for follow_up tasks (vendor silent, nudge after N days).
- Deadline extraction from "due / deadline / by <date>" into a deadline
  subtask; `sla` shows as "Reply by" in the date picker.
- First fetch 30 days.
- Templates: `features` block per profession (money, game_layer); legal
  gets Hearing date; real_estate gets Showing scheduled; insurance renewal
  date arms the Renewal stage 30 days out; property_management drops the
  duplicate tenant field; new templates: trades (plumbing/electrical),
  photographer/events, nonprofit/church. Renamed money labels per
  profession.
- CSV import ("Add many from a spreadsheet").
- Weekly digest as a scheduled draft to self.
- Email Boss AI relay + credit wallet (docs/AI_CREDITS.md).

### Explicitly not in 1.3.0
Phone app, cross-folder mail search, calendar, per-priority SLA matrices.
