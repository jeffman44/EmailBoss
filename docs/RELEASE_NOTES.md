# Release notes

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
