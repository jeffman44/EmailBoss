# Power Automate drop folder

The drop-folder provider (`mail.provider = pa_folder`) lets any flow feed the
board by writing one JSON file per email. Power Automate with the Office 365
Outlook connector is the usual producer: it is already consented in nearly
every Microsoft 365 tenant, needs no app registration and no admin, and runs
in the cloud while your PC is off (files land when OneDrive syncs).

Three flows:

| flow | trigger | writes to | purpose |
|---|---|---|---|
| Inbox capture | When a new email arrives (V3), folder Inbox | `<drop>/inbox/` | every inbound message becomes a triage card or files onto its project |
| Sent capture | When a new email arrives (V3), folder Sent Items | `<drop>/sent/` | your replies close "Respond" tasks and start follow-up timers |
| Sent reconcile | Recurrence, every 15 minutes | `<drop>/sent/` | catches sent mail the trigger missed (the Sent Items trigger is unreliable for mail sent from phones and shared mailboxes) |

`<drop>` is the folder you pick in the setup wizard ("Drop folder path"). It
defaults to `<data_root>/mail_cache` (`~/EmailBoss/mail_cache`). The board
expects three subfolders under it: `inbox/`, `sent/`, `attachments/`.

## Landing files on the local disk: the OneDrive sync trick

Power Automate cloud flows cannot write to your PC directly. Pick a folder
inside your OneDrive (for example `OneDrive/Apps/EmailBoss/mail_cache`),
make sure that folder is set to **Always keep on this device** in the OneDrive
client, and point the board's drop folder at the synced local path (for
example `C:\Users\<you>\OneDrive\Apps\EmailBoss\mail_cache`). The flow
writes to OneDrive; the OneDrive client copies the file down within seconds;
the board picks it up on its next poll (`mail.poll_seconds`, default 300).

Handled files are moved by the board into `<drop>/processed/<date>/` and
purged after 90 days, so the OneDrive folder does not grow without bound.

The on-premises data gateway with the "File System" connector is the
alternative when OneDrive is not allowed; the flow steps are the same except
the "Create file" action.

## Record schema

One JSON object per message. Keys are exactly the required set plus the
optional ones present. The built-in providers run `validate_record()` on
every file they write; the triage scanner reads external files leniently, so
a wrong key name shows up as a blank field on the card rather than an error.
Match the names exactly.

Required (`PA_REQUIRED_KEYS` in `plannerboard/mail/message.py`):

| key | type | source in the V3 trigger | notes |
|---|---|---|---|
| `id` | string | Message Id | also the attachments folder name |
| `receivedDateTime` | string | Received Time | ISO 8601 with numeric offset: `2026-10-01T15:12:03+00:00`. The trigger gives `...Z`; convert with `formatDateTime(..., 'yyyy-MM-ddTHH:mm:ssK')` and replace `Z` with `+00:00`, or use the Compose expression below |
| `hasAttachments` | bool | Has Attachment | Graph semantics: inline-only images report `false` |
| `internetMessageId` | string | Internet Message Id | `<...@...>` form, used for deduplication and threading |
| `subject` | string | Subject | |
| `bodyPreview` | string | Body Preview | 255 characters max |
| `importance` | string | Importance | `low`, `normal` or `high` |
| `conversationId` | string | Conversation Id | threads replies onto the same project |
| `isRead` | bool | Is Read | |
| `internetMessageHeaders` | array of `{name, value}` | Internet Message Headers | at least `From`, `To`, `Cc` with display names (`"Dana Whitfield <dana@example.com>"`); the board reads names from here because the address fields below are bare |
| `isHtml` | bool | Is HTML | |
| `body` | string | Body | HTML or text; 400,000 characters max |
| `from` | string | From | bare address |
| `toRecipients` | string | To | bare addresses, `;` separated |
| `attachments` | array | Attachments | see below; `[]` when none |

Optional (`PA_OPTIONAL_KEYS`): `ccRecipients` (string, bare `;` separated),
`replyTo` (string). Tolerated for older producers but never required:
`from_display_name`, `bccRecipients`.

Each entry of `attachments` has exactly these keys (`PA_ATTACHMENT_KEYS`):

```json
{
  "@odata.type": "#microsoft.graph.fileAttachment",
  "id": "AAMkAG...-1",
  "lastModifiedDateTime": "2026-10-01T15:12:03+00:00",
  "name": "Signed Proposal.pdf",
  "contentType": "application/pdf",
  "size": 24,
  "isInline": false,
  "contentId": null,
  "contentBytes": "JVBERi0xLjQg..."
}
```

`contentBytes` may be an empty string when the flow writes the file bytes to
the attachments folder instead (see below); `name`, `size`, `isInline` must
still be right because the board matches files to declared names.

Complete example: `app/tests/fixtures/inbox_approval.json`.

## File naming

- Records: `<drop>/inbox/<yyyyMMddHHmmssfff>-inbox.json` and
  `<drop>/sent/<yyyyMMddHHmmssfff>-sent.json`. Any `.json` name works; this
  pattern sorts chronologically and never collides across folders. Expression:
  `concat(formatDateTime(utcNow(),'yyyyMMddHHmmssfff'),'-inbox.json')`.
- Attachment bytes: `<drop>/attachments/<id>/<file name>` where `<id>` is the
  record's `id`. Write the files before the JSON so a card never shows up
  with missing attachments. Keep the original file name; the board sanitizes
  Windows-unsafe characters itself.
- Write the JSON in one action (no append), UTF-8, no BOM.

## Flow 1: Inbox capture (step by step, Power Automate designer)

1. **Trigger**: Office 365 Outlook > *When a new email arrives (V3)*.
   Folder `Inbox`, Include Attachments `Yes`, Only with Attachments `No`.
   Leave the other filters empty so every message is captured; the board
   decides what is junk.
2. **Compose** (name it `ReceivedIso`):
   `replace(formatDateTime(triggerOutputs()?['body/receivedDateTime'],'yyyy-MM-ddTHH:mm:ssK'),'Z','+00:00')`
3. **Select** (name it `Headers`): From = `triggerOutputs()?['body/internetMessageHeaders']`,
   Map: `name` -> `item()?['name']`, `value` -> `item()?['value']`.
   If your tenant's trigger does not expose headers, build them by hand with
   a Compose:
   `[{"name":"From","value":"@{triggerOutputs()?['body/from']}"},{"name":"To","value":"@{triggerOutputs()?['body/toRecipients']}"},{"name":"Cc","value":"@{triggerOutputs()?['body/ccRecipients']}"}]`
4. **Select** (name it `Attachments`): From = `triggerOutputs()?['body/attachments']`,
   Map:
   - `@odata.type` -> `#microsoft.graph.fileAttachment`
   - `id` -> `item()?['id']`
   - `lastModifiedDateTime` -> `outputs('ReceivedIso')`
   - `name` -> `item()?['name']`
   - `contentType` -> `item()?['contentType']`
   - `size` -> `item()?['size']`
   - `isInline` -> `item()?['isInline']`
   - `contentId` -> `item()?['contentId']`
   - `contentBytes` -> `item()?['contentBytes']`
5. **Apply to each** over `triggerOutputs()?['body/attachments']` (skip this
   step if you keep `contentBytes` in the JSON only): OneDrive for Business >
   *Create file*, folder `/Apps/EmailBoss/mail_cache/attachments/@{triggerOutputs()?['body/id']}`,
   name `items('Apply_to_each')?['name']`, content
   `base64ToBinary(items('Apply_to_each')?['contentBytes'])`.
6. **Compose** (name it `Record`):

   ```
   {
     "id": "@{triggerOutputs()?['body/id']}",
     "receivedDateTime": "@{outputs('ReceivedIso')}",
     "hasAttachments": @{triggerOutputs()?['body/hasAttachments']},
     "internetMessageId": "@{triggerOutputs()?['body/internetMessageId']}",
     "subject": @{json(string(coalesce(triggerOutputs()?['body/subject'],'')))},
     "bodyPreview": @{json(substring(string(coalesce(triggerOutputs()?['body/bodyPreview'],'')),0,min(255,length(string(coalesce(triggerOutputs()?['body/bodyPreview'],''))))))},
     "importance": "@{toLower(coalesce(triggerOutputs()?['body/importance'],'normal'))}",
     "conversationId": "@{triggerOutputs()?['body/conversationId']}",
     "isRead": @{triggerOutputs()?['body/isRead']},
     "internetMessageHeaders": @{body('Headers')},
     "isHtml": @{triggerOutputs()?['body/isHtml']},
     "body": @{json(string(coalesce(triggerOutputs()?['body/body'],'')))},
     "from": "@{triggerOutputs()?['body/from']}",
     "toRecipients": "@{triggerOutputs()?['body/toRecipients']}",
     "ccRecipients": "@{coalesce(triggerOutputs()?['body/ccRecipients'],'')}",
     "attachments": @{body('Attachments')}
   }
   ```

   The `json(string(...))` wrapper around free text produces a correctly
   escaped JSON string (quotes, newlines). Do not paste subject or body
   directly into the template.
7. OneDrive for Business > **Create file**: folder
   `/Apps/EmailBoss/mail_cache/inbox`, file name
   `@{concat(formatDateTime(utcNow(),'yyyyMMddHHmmssfff'),'-inbox.json')}`,
   content `@{outputs('Record')}`.
8. Save, send yourself a test email, and confirm a file appears in the local
   synced folder. In the board, Settings > Mail > Test should report the
   folder, the record count and the age of the newest file; the triage card
   shows within one poll.

## Flow 2: Sent capture

Same as Flow 1 with these changes: trigger folder `Sent Items`; output folder
`/Apps/EmailBoss/mail_cache/sent`; file name suffix `-sent.json`. The
board marks a `sent/` record as outbound, sets `isRead` true, and uses the
`conversationId` / `In-Reply-To` headers to find the project.

## Flow 3: Sent reconcile (every 15 minutes)

The Sent Items trigger misses mail sent from mobile clients, shared
mailboxes and some add-ins. A polling flow fills the gaps:

1. **Trigger**: Schedule > *Recurrence*, every 15 minutes.
2. Office 365 Outlook > **Get emails (V3)**: folder `Sent Items`, Fetch Only
   Unread `No`, Include Attachments `Yes`, Top `25`, Search Query
   `received>=@{formatDateTime(addMinutes(utcNow(),-20),'yyyy-MM-ddTHH:mm:ssZ')}`.
3. **Apply to each** over the result: the same Compose / Select / Create file
   steps as Flow 2, reading from `items('Apply_to_each')?['...']` instead of
   `triggerOutputs()?['body/...']`.

Overlap between Flow 2 and Flow 3 is expected. The board deduplicates on
`internetMessageId`, so a message captured twice produces one card and one
correspondence row; the second file goes straight to `processed/`.

## Checklist when nothing shows up

- The drop folder in Settings > Mail is the **local synced path**, not the
  OneDrive web URL.
- The folder is marked Always keep on this device; files that are
  "online-only" are invisible to the board.
- The JSON parses. A file that is not valid JSON is left in place for retry
  and raises the board alert "unreadable mail record(s) left on disk"; open
  the file and check the escaping of subject and body. A record missing keys
  is read leniently (missing fields default to empty), so a card with a blank
  sender or body usually means a wrong key name.
- `receivedDateTime` ends in `+00:00`, not `Z`.
- Booleans are unquoted (`true`, not `"true"`).
- `/api/health.mail` shows `ok: true` and a recent `last_fetch`.
