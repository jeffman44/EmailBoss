# Email providers

Email Boss reads your mail through one of four providers. All four write
the same record shape into `<data_root>/mail_cache/` (see `POWER_AUTOMATE.md`
for the schema); the triage pipeline never knows which one produced a file.

| provider | id | what it needs | mail client | drafts | send | best for |
|---|---|---|---|---|---|---|
| Drop folder | `pa_folder` | a flow that writes JSON files | no | no (drafts stay in the board) | no | locked-down Microsoft 365 tenants, anything that can write a file |
| IMAP | `imap` | host, user, app password | yes | yes (APPEND to Drafts) | yes (SMTP) | any mailbox, no cloud project needed |
| Gmail API | `gmail` | nothing (built-in sign-in); your own OAuth client optional | yes | yes (`drafts.create`) | yes (`messages.send`) | Gmail and Google Workspace |
| Microsoft Graph | `graph` | nothing (built-in sign-in); your own app registration optional | yes | yes (`createReply`) | yes (`sendMail`) | Microsoft 365 |

"Mail client" is the MAIL view on the board: folders, a message list, a
reading pane and a composer. "Send" means a person pressing Send there, and
it is refused until `mail.send_enabled` is on (see the last section).

Pick the provider in the setup wizard or later in Settings > Mail. Settings >
Mail > Test checks the connection; `/api/health.mail` shows `provider, ok,
last_fetch, error` afterwards. Secrets (passwords, OAuth tokens) are stored in
the OS keyring under service `EmailBoss`, never in `plannerboard.json`.

Polling interval is `mail.poll_seconds` (default 300, minimum 60). On first
connect the IMAP, Gmail and Graph providers look back 30 days, then keep a
watermark (UID, Gmail history id, Graph delta token) so nothing is fetched
twice.

## Built-in sign-in (1.3.0)

"Sign in with Google" and "Sign in with Microsoft" work out of the box: the
app ships with Email Boss's own OAuth client ids (`PB_GMAIL_CLIENT_ID`,
`PB_GMAIL_CLIENT_SECRET`, `PB_GRAPH_CLIENT_ID`, set at build time; the Google
"secret" for an installed app is not confidential, Google says so in its
docs). Both flows use PKCE on a loopback redirect. A user who prefers their
own cloud project can still paste a client id under Advanced; the steps below
describe that path. The wizard also offers "Use an app password instead" for
every provider.

## Gmail API

Google requires an OAuth client that belongs to you. One-time setup, about ten
minutes:

1. Go to `https://console.cloud.google.com/`, create a project (any name).
2. APIs & Services > Library: enable **Gmail API**.
3. APIs & Services > OAuth consent screen: User type **External** (or Internal
   for Workspace), app name "Email Boss", your email as support and
   developer contact. Add the scopes
   `https://www.googleapis.com/auth/gmail.readonly` and
   `https://www.googleapis.com/auth/gmail.compose`. While the app is in
   "Testing" status add your own address under Test users; that is enough for
   personal use and avoids Google verification.
4. APIs & Services > Credentials > Create credentials > **OAuth client ID**,
   application type **Desktop app**. Copy the client id and client secret.
5. In Email Boss: Settings > Mail > Gmail, paste the client id and secret,
   click **Connect**. A browser tab opens Google's consent page; after you
   approve, Google redirects to
   `http://127.0.0.1:<port>/api/mail/oauth/callback` on the board's own port
   (loopback redirect, PKCE). Desktop-app clients accept any loopback port, so
   nothing has to be registered for it.

Scopes requested: `gmail.modify` (read, labels, trash, drafts and send) when
the mail client is on, which is the default; `gmail.readonly` plus
`gmail.compose` when `mail.client_enabled` is false. Google has no scope that
allows labels and drafts but not send, so with the client on the TOKEN could
send; whether the APP sends is `mail.send_enabled`, checked on every call to
`/api/mail/client/send`. If you turn the client on or off after connecting,
click Connect again so the token carries the matching scope.

Watermark: `users.history.list` from the last history id, labels `INBOX` and
`SENT`. Tokens live in the keyring under `gmail_token`.

## Microsoft 365 (Graph)

Needs an app registration in Microsoft Entra ID (Azure AD). If your tenant
lets users register apps this takes five minutes. If it does not, see the
caveat below and use IMAP or the drop folder instead.

1. `https://entra.microsoft.com/` > Applications > App registrations > **New
   registration**. Name "Email Boss". Supported account types: pick
   "Accounts in any organizational directory and personal Microsoft accounts"
   if you want the `common` tenant to work, or "this organizational directory
   only" for a single-tenant app.
2. Redirect URI: platform **Mobile and desktop applications** (public client),
   value `http://localhost:<port>/api/mail/oauth/callback` where `<port>` is
   the board port (8080 unless you changed it). Entra accepts `http://localhost`
   redirects only for public clients; `http://127.0.0.1:<port>/...` works too
   and is what the app sends, so register both if in doubt.
3. Authentication > Advanced settings > **Allow public client flows: Yes**.
   This also enables the device-code fallback the app uses when a browser
   redirect is not possible.
4. API permissions > Add a permission > Microsoft Graph > **Delegated**:
   `Mail.Read`, `Mail.ReadWrite` (drafts, flags and moves need write;
   `Mail.ReadWrite` includes read), `User.Read`, `offline_access`. Add
   `Mail.Send` only if you intend to turn "Allow sending" on. Do not add any
   Application permissions.
5. Copy the **Application (client) ID**. No client secret is used (public
   client + PKCE).
6. In Email Boss: Settings > Mail > Microsoft 365, paste the client id,
   set Tenant to `common` (multi-tenant / personal accounts), `organizations`,
   or your tenant id or domain for single-tenant, and click **Connect**.

Scopes requested by the app: `offline_access Mail.ReadWrite User.Read`, plus
`Mail.Send` only while `mail.send_enabled` is on (turning it on tells you to
sign in again so the token gains the scope; turning it off and signing in
again drops it). Sync: delta queries on `mailFolders/inbox` and
`mailFolders/sentitems`. Drafts: `createReply` on the inbound message, saved
as a draft in your mailbox. Send: `createReply` + `/send` for replies (keeps
Outlook's conversation threading), `/me/sendMail` otherwise, always with
`saveToSentItems`. Tokens live in the keyring under `graph_token`.

**Admin consent caveat.** Many corporate tenants block users from consenting
to delegated permissions, or block app registration altogether. Symptoms: the
consent page says "Need admin approval", or the portal hides "New
registration". Delegated `Mail.ReadWrite` is a user-consentable permission by
default, but a tenant policy can require admin consent for every app. Your
options, in order of effort:

- Ask an administrator to grant tenant-wide consent to your registration
  (API permissions > Grant admin consent), or to add your account to a group
  that may consent.
- Use IMAP with an app password (if the tenant allows IMAP and app passwords;
  many disable both).
- Use the drop folder with Power Automate (`POWER_AUTOMATE.md`). Power
  Automate's Office 365 Outlook connector is already consented in almost every
  tenant, so this path needs no new approval.

## IMAP

Works with any mailbox. Use an app password; most providers reject the normal
password for IMAP once two-step verification is on.

| preset | host | sent folder | where to make an app password |
|---|---|---|---|
| Gmail / Google Workspace | `imap.gmail.com:993` | `[Gmail]/Sent Mail` | Google account > Security > 2-Step Verification > App passwords. Also turn IMAP on under Gmail Settings > Forwarding and POP/IMAP. |
| Outlook.com / Hotmail / Microsoft 365 | `outlook.office365.com:993` | `Sent` | account.microsoft.com > Security > Advanced security options > App passwords (two-step verification must be on). Microsoft 365 work accounts: your admin must allow IMAP and legacy auth; many tenants do not. |
| iCloud Mail | `imap.mail.me.com:993` | `Sent Messages` | appleid.apple.com > Sign-In and Security > App-Specific Passwords. |
| Other | your host, 993 TLS | `Sent` (adjust) | whatever your provider calls an application or device password |

Settings: host, port, user, inbox folder, sent folder, and optionally
drafts / trash folder names and your display name (used on the From line).
The password goes to the keyring as `imap_password` (env fallback
`PB_IMAP_PASSWORD`). The capture path uses `BODY.PEEK` so it never marks
mail as read, keeps a UID watermark per folder, caps each cycle at 50
messages, and writes attachments before the JSON so a card never appears
without its files. XOAUTH2 is supported for providers that need it.

Drafts go into the mailbox's Drafts folder by IMAP `APPEND` (the folder is
found by its `\Drafts` attribute or its name; set `mail.imap.drafts` if the
server names it something unusual). Sending uses SMTP; see below.

## Mail client and sending

The MAIL view (board header, next to LIST / KANBAN) is a three-pane mail
client on the configured provider: folders on the left, the message list in
the middle, the reading pane on the right, and a composer. It is on by
default (`mail.client_enabled`) for IMAP, Gmail and Microsoft 365; the drop
folder has no mailbox to browse, so the view is hidden there.

What it does, by provider capability (`GET /api/mail/client/capabilities`):

- **Folders** with unread counts; Gmail labels are folders, "All Mail" is
  the archive. Microsoft 365 kinds come from the well-known folder ids, so a
  German or French mailbox still gets the right icons.
- **List** newest first, paged (Load more), with a per-folder search box:
  IMAP `SEARCH TEXT`, Gmail `q=`, Graph `$search`.
- **Read**: the body renders in a sandboxed iframe (no scripts, no
  same-origin access); `cid:` images are served through
  `/api/mail/client/attachment`. Opening a message marks it read.
- **Flag / Unread / Archive / Delete** (delete = move to Trash, never a
  permanent delete).
- **Reply / Reply all / Forward** open the composer pre-filled: recipients
  (Reply-To honoured, your own address dropped), `Re:` / `Fw:` subject, the
  quoted original, forwarded attachments, and `In-Reply-To` / `References`
  so threads stay intact in everyone's client.
- **Save draft** writes into the provider's Drafts folder (IMAP APPEND,
  Gmail `drafts.create`, Graph `createReply` / `/me/messages`).
- **File** puts the open message on a project as correspondence,
  attachments included, and registers its thread so the rest of the
  conversation auto-files. **Triage** writes it into the mail cache and runs
  one triage cycle, so it lands on the triage panel exactly like captured
  mail; a message the board has already processed says so instead.
- **Send**: see below.

### Allow sending (`mail.send_enabled`, default off)

Off is the shipping default: the composer has Save draft only, and
`POST /api/mail/client/send` answers 403. Settings > Mail > "Allow sending"
turns it on. Then, and only then, the Send button appears and a message a
person wrote goes out through their own mailbox:

| provider | how it sends | copy in Sent |
|---|---|---|
| IMAP | SMTP submission, port 587 STARTTLS by default (465 implicit TLS if `mail.smtp.ssl`), same login as IMAP (app password or XOAUTH2). Host blank = the preset matching the IMAP preset (`smtp.gmail.com`, `smtp.office365.com`, `smtp.mail.me.com`) or `smtp.<rest of the IMAP host>`. | Gmail and Outlook.com file it themselves; for other servers the app APPENDs the sent message to the Sent folder |
| Gmail API | `users.messages.send` with the thread id on replies | Gmail does it |
| Microsoft Graph | `createReply` + `/send` for replies, `/me/sendMail` otherwise | `saveToSentItems: true` |

Bcc is honoured (envelope recipient, stripped from the headers). Attachments
up to 25 MB per message. A message sent with a project linked in the
composer is stored on that project as outbound correspondence and its thread
is registered, so the reply files itself.

The assistant, the triage pipeline and every scheduled pass have no path to
this endpoint; they write drafts. Only a person pressing Send in the MAIL
view sends, and only after sending was switched on.

Off-loopback note: when the board is bound to a non-loopback address and
requires the bearer token, inline images inside the sandboxed reading pane
cannot carry the token and will not load; attachments opened from the chip
still work because the page's own fetch wrapper adds it.

## Drop folder (Power Automate or any flow)

Set `mail.provider` to `pa_folder` and point the drop folder at the directory
your flow writes into (the wizard asks for it; it defaults to
`<data_root>/mail_cache`). The provider fetches nothing; it reports whether
the folder exists, how many records are waiting and how old the newest one is.
The record schema, file naming and the three Power Automate flows are in
`POWER_AUTOMATE.md`.

## Switching providers

Change the provider in Settings > Mail, test, save, and restart when the board
asks (`/api/health.restart_needed`). Records already in `mail_cache/` are
provider-neutral and keep working. Deduplication is by internet message id,
so overlapping fetches after a switch do not create duplicate cards.
