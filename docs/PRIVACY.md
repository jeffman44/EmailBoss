# Privacy

Email Boss is a local application. Your email, projects, notes, drafts
and attachments are stored on your own computer and are never uploaded to
us. This page lists every network connection the app makes and what each
one carries.

## What stays on your machine

Everything in the data root (`$PB_DATA_ROOT` or `~/EmailBoss`):

- `plannerboard.db`: projects, contacts, correspondence, triage cards,
  drafts, questions, notes, AI usage ledger, assistant log.
- `mail_cache/`: raw mail records and attachment bytes as captured, then
  moved to `processed/` and purged after 90 days.
- `project_files/`: attachments and intake files copied onto projects.
- `backups/`, `logs/`, `workflows.json`, `plannerboard.json`.

Secrets (mailbox password, OAuth tokens, AI API key) are stored in the
operating system keyring (Windows Credential Manager, macOS Keychain, Secret
Service on Linux) under service `EmailBoss`. They are never written to
the config file, the database or the logs. The one exception is the license:
your license key and the per-device license secret are kept in the app's
`app_meta` table inside `plannerboard.db` (they are not mailbox or vendor
credentials, and the secret only proves this device to the license server).
Deleting the data root removes them; deactivating the device in the
portal invalidates them.

The web UI is served on `127.0.0.1` only. If you change `server.bind`, every
API request needs the bearer token the app generates, and you are responsible
for the network you expose it on.

## Connections the app makes

| destination | when | what is sent | what comes back |
|---|---|---|---|
| Your mail provider (IMAP server, Gmail API, Microsoft Graph) | every poll (`mail.poll_seconds`) | credentials or OAuth token, folder and watermark queries; for drafts, the draft text | your mail |
| Your AI provider (Anthropic, OpenAI or compatible host, or a local Ollama / LM Studio) | only when an AI feature fires | the trimmed email text, project summary fields and your name / company / role as listed in `AI_SETUP.md` under "Privacy: what text is sent" | classifications, summaries, draft text |
| Email Boss AI (the built-in AI, `ai.provider = emailboss`) | only when an AI feature fires | the text of the email being worked on (trimmed), the project summary fields and your name / company / role, plus your license id, machine fingerprint hash and a signed device proof | classifications, summaries, draft text, your credit balance |
| The license server (`license.server_url`) | activation, then one heartbeat a day | license key, machine fingerprint (a SHA-256 hash of hostname, MAC, OS and machine id; the raw values never leave the machine), app version | a signed activation |
| Nothing else | | | |

There is no telemetry, crash reporting, update check or analytics beacon. The
app does not phone home with usage statistics. Download links on the license
portal are plain HTTPS downloads.

With `ai.provider = none` the only outbound connections are your mailbox and
the license server. With the drop-folder mail provider and `PB_LICENSE_DEV=1`
(development only) the app makes no outbound connections at all.

## What the license server stores about you

Account email, scrypt password hash, license records (plan, seats, expiry,
features), activations (machine fingerprint hash, app version, last
heartbeat time), an audit log of portal actions, and when billing is enabled
the Stripe subscription id. Payment card details are handled
by Stripe and never reach the license server. Details: `LICENSING.md`.

## Third parties

- Mail providers and AI providers see the data you send them under their
  own terms. The app lets you choose which ones, including fully local
  options (IMAP to your own server, Ollama).
- The license server is operated by us. Its license endpoints never
  receive mail content or project data.
- **Email Boss AI.** When you use the built-in AI, the text of the email
  being worked on (and the project fields listed above) is sent to our AI
  relay and from there to the model vendor (Anthropic) to produce the
  answer. The relay does not store or log that text: it keeps only
  metadata for billing (time, task name, model tier, token counts, credits
  charged). The vendor processes it under its API terms, which do not use
  API traffic for training. If you do not want any email text to leave
  your computer, set AI to "Off" or use a local model (Ollama) in
  Settings > AI.

## Deleting your data

Uninstall and delete the data root. On Windows
`uninstall_windows.ps1 -PurgeData` does both. Remove the keyring entries
under service `EmailBoss` with your OS credential manager if you want
the tokens gone too; deactivating the device in the license portal releases
the seat. To delete your license account, contact support with the account
email; the account, its licenses and activations are removed.

## Changes

This document describes version 1.1.0. Material changes to what the app
sends or stores will be noted in the release notes and here.

- 1.1.0: the mail client. With `mail.client_enabled` (default on) the app
  reads any folder of the connected mailbox on demand, changes read / flag
  state and moves or trashes messages when you ask it to. With
  `mail.send_enabled` (default OFF) it sends a message you wrote, through
  your own mailbox: SMTP for IMAP accounts, Gmail's or Microsoft Graph's
  send call otherwise. A sent message linked to a project is stored on that
  project as outbound correspondence. Gmail now asks for the `gmail.modify`
  scope; Microsoft 365 asks for `Mail.Send` only when sending is on. Nothing
  about what the app transmits to the license server or to AI providers
  changed.
