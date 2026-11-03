# Release notes

## 1.3.2

- Fixes the 1.3.1 installer: the packaged program failed on start with
  "attempted relative import with no known parent package". The bundle now
  starts through a launcher that imports the package properly.


## 1.3.1

- Fixes the 1.3.0 desktop launch: the window's health probe was refused by
  the new session gate, so the app gave up after 20 seconds. The window now
  also keeps the same local port between launches so settings persist, and
  a missing WebView2 runtime shows a plain message with the download link
  instead of exiting silently.
- Signed update check: the app looks for a new release once a day, verifies
  the signed manifest, and offers "Update now" (two clicks, never automatic).
  Settings > General > "Check for updates now".
- Built-in "Sign in with Google" and "Sign in with Microsoft" ship in this
  build (Google app in Testing mode: only listed test users until Google
  verifies the app).
- Windows runtime review: PyInstaller bundle includes the WebView2 and
  keyring backends, installer never asks for admin, restart works in
  desktop mode. Owner checklist in docs/WINDOWS_RUNTIME_CHECK.md.


## 1.3.0

Built from a simulation of twelve kinds of users (plumber, property manager,
general contractor, insurance agent, attorney, recruiter, IT provider,
realtor, consultant, photographer, service contractor, church office) walking
the app in a browser, and two adversarial security reviews. Average
"would I keep using this" score went from 2.0 to 2.9 of 5 between rounds.

### For everyone

- Setup is three screens: you and your email, what kind of work you do,
  done. "Sign in with Google" and "Sign in with Microsoft" when the build
  carries the publisher's app ids; every provider also has an app-password
  path with a numbered guide, and you cannot finish setup until mail is
  actually connected.
- A free 14-day trial starts from the wizard. The license key is emailed;
  nothing is pasted.
- Email Boss AI is built in: no API key. 300 free credits once your email
  address is confirmed, then credit packs bought inside the app. Your own key
  (Anthropic, OpenAI-compatible, Ollama) is still available under Advanced.
- Today view is the first thing you see: Overdue, Reply today, Nudge today,
  Coming up (7 days), Decisions for you, New mail to sort.
- Decisions for you: the helper's questions, one at a time, answered with
  number keys. An answer writes the draft immediately.
- AI on every card: Write a reply for me, Write a follow-up, What's the
  status?, Ask (in the search box), Summarize my week. A Drafts tab shows
  everything the helper wrote, with "Open in my mail".
- Plain words everywhere: Email Boss brand, "Add to board / Not now / Not
  for me", "3 days late" instead of a number, time zone taken from the
  computer, light theme and large text, money and game features off unless
  your profession uses them.
- Mail client safety: send confirms the recipients, Reply all with more
  than three people confirms, a Reply-To that differs from the sender is
  shown, ten seconds to undo a send, undo for archive and trash, drafts
  autosave, remote images blocked until you ask.
- Settings: Display, Fields (rename, hide, add), Stages (basic and
  advanced), Privacy (what leaves this computer, Turn AI off), time zone and
  working hours, weekly summary schedule.
- "Add many from a spreadsheet" (CSV import) with a sample file.
- Dated fields (court date, renewal date, bid due, showing, closing) keep a
  reminder in step with them. Moving to a stage arms that stage's task.
- New professions: trades, events, nonprofit. Legal gets a hearing date;
  real estate gets a showing stage; insurance renewals arm 30 days out.

### Security (two review passes)

- The engine answers only its own window: per-launch session cookie, exact
  origin check, app header on every write, JSON-only writes, no token in
  the page.
- Email HTML can no longer run script in the app: inert parsing, a
  server-side sanitizer for quoted mail, no inline scripts or handlers in
  the board, Content-Security-Policy with script-src 'self'.
- The helper cannot be talked into misfiling or drafting to a stranger by
  the text of an email. Drafts only ever go to a project's own contacts.
- IMAP never expunges; deletes go to Trash or are refused.
- License: device-bound activations with a proof of possession, trial and
  relay abuse limits, verified admins, no open redirects.

### Publisher

- Relay: `ANTHROPIC_API_KEY`, `AI_MARKUP`, `STRIPE_CREDIT_PACKS`,
  `AI_DAILY_USD_PER_LID`, trial budgets. See docs/AI_CREDITS.md and
  docs/LICENSING.md.
- Built-in sign-in: docs/OAUTH_SETUP.md.
- Releases publish SHA256SUMS.txt beside the installer.


## 1.2.0

### Desktop app

Email Boss now runs as a real program, not a browser tab. `--desktop`
(the default for the packaged build and the Windows installer) serves the
engine on an ephemeral loopback port chosen at launch and shows it in a
native window (Edge WebView2 on Windows, WKWebView on macOS, WebKitGTK on
Linux). No fixed port, nothing to open in a browser, nothing listening that
the user has to know about. Ported from the EmailBoss prototype shell.

- Second launch while a window is open attaches a new window to the running
  engine (`<data_root>/desktop.json`) instead of starting a second one.
- JS bridge on `window.pywebview.api`: `pick_folder`, `pick_files`,
  `save_file`, `open_external`, `notify`, `app_info`.
- Tray icon follows the chosen port. Closing the window stops the engine.
- Server mode is unchanged: `python -m plannerboard.start --port 8080`, or
  `PB_SERVER_MODE=1` on the packaged executable.
- Installer: `install_windows.ps1` takes `-TaskName` so a second install on
  the same machine does not replace another board's autostart task, and
  `-Port` now sets `PB_PORT` for server mode.
- New extra: `pip install plannerboard[desktop]` (pywebview, plyer).


## 1.1.0

### The mail client

The board can now be the mailbox. A MAIL view sits next to LIST / KANBAN /
ANALYTICS for IMAP, Gmail and Microsoft 365 accounts:

- Folders with unread counts (Gmail labels; Microsoft 365 well-known folders
  resolved by id so localized mailboxes get the right kinds).
- Message list, newest first, paged, with a per-folder search.
- Reading pane in a sandboxed iframe (no scripts), inline images resolved,
  attachments downloadable one at a time.
- Flag, unread, archive, delete (to Trash, never permanent).
- Reply, reply all, forward: pre-filled recipients, quoted original,
  forwarded attachments, `In-Reply-To` / `References` threading.
- Save draft into the mailbox's own Drafts folder. IMAP gained this too
  (APPEND), so no provider is left with board-only drafts any more.
- File: put a message on a project as correspondence, attachments included,
  and register its thread. Triage: push a message onto the triage panel
  through the normal pipeline (same dedup, same matching).
- Send, off by default. Settings > Mail > "Allow sending" turns it on. IMAP
  accounts send over SMTP (587 STARTTLS, presets for Gmail / Outlook.com /
  iCloud, app password or XOAUTH2); Gmail through `messages.send`; Microsoft
  365 through `sendMail` / `createReply` + send. The assistant and every
  automated pass still only write drafts.

### Config

- `mail.client_enabled` (default true), `mail.send_enabled` (default false),
  `mail.smtp {host, port, starttls, ssl, user}`, `mail.imap.drafts`,
  `mail.imap.trash`, `mail.imap.display_name`.
- `PUT /api/config` with a `mail` block rebuilds the client provider.

### Scopes (re-consent needed)

- Gmail requests `gmail.modify` while the client is on (was `gmail.readonly`
  + `gmail.compose`). Click Connect again after upgrading.
- Microsoft 365 requests `Mail.Send` only while sending is on; the app asks
  you to sign in again when you flip the switch.

### Licensing hardening (from an independent code review)

- Seat limit: count + activation insert now happen in one `BEGIN IMMEDIATE`
  transaction; parallel activations cannot overshoot `seats`.
- Renewals reach clients holding the old key string: the signed activation
  carries the server's `expires`; the client uses the later of key and
  activation (never shorter).
- Dev mode (`PB_LICENSE_DEV`, `--dev-license`) only works on the dev build
  channel; release builds ignore it. `--dev-license` is hidden from `--help`.
- Clock rollback: `now` is also floored at the signed key's and activation's
  `issued`, so deleting the high-water-mark row no longer reopens writes.
- Stripe: fail closed (503) without `STRIPE_WEBHOOK_SECRET`
  (`LIC_ALLOW_UNSIGNED_STRIPE=1` for dev); event ids deduplicated; renewals
  set expiry to the invoice period end; the subscription's first invoice no
  longer double-grants.
- Rate limiter honours `X-Forwarded-For` only with `LIC_TRUSTED_PROXY=1`.
- Password reset tokens stored hashed; single-use enforced atomically.
- Remaining known limits are listed in `docs/LICENSING.md`.

### Hygiene

- The banned-literal scan (`make check`, `tasks.ps1 check`) now also bans the
  reference install's company abbreviation and scans `.js` / `.css`.
- A 7-day analytics test that pinned a calendar date was made relative.

### Known limits

- Off-loopback with a bearer token: inline images in the reading pane do not
  carry the token and will not load (attachments opened from the chip do).
- IMAP list rows have no preview text (headers-only fetch); the body shows
  when opened.
- No IMAP IDLE yet; the list refreshes every 90 s while the view is open.

## 1.0.0

Initial release: profession templates, drop-folder / IMAP / Gmail / Graph
capture, triage pipeline, AI layer (drafts only), license client and portal.
