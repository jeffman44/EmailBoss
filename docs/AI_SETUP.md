# AI setup

The AI layer is optional. With `ai.provider = none` the board runs on rules
alone: regex classification, contact and id-pattern matching, verbatim
summaries. Adding a model makes triage sharper and unlocks draft replies, the
project health rating and the assistant pass.

You bring your own key. Email Boss talks to the provider directly from your
machine over HTTPS; there is no relay and no account with us.

## Guarantees

- **Drafts only.** No AI feature calls a send API. The only send path in the app is the
  mail client's Send button, which a person presses, and which is refused until
  `mail.send_enabled` is on (see EMAIL_PROVIDERS.md, "Mail client and sending").
  Gmail is connected with `gmail.compose` (not `gmail.send`); Graph with
  `Mail.ReadWrite` (not `Mail.Send`). Draft replies land in your mailbox's
  Drafts folder (Gmail, Graph) or in the board's Drafts panel (IMAP, drop
  folder). You read, edit and send them yourself.
- The model never changes a project's status, money fields or contacts. It
  classifies, summarizes, picks among candidates you already have, and writes
  text you review.
- Every call is metered against a daily budget you set. When the budget is
  reached, calls stop for the rest of the day and the rules fallback takes
  over; `/api/health.ai.budget_hit` turns `true` and the board shows it.

## Providers

| `ai.provider` | needs | default model | notes |
|---|---|---|---|
| `anthropic` | API key from `https://console.anthropic.com/` | `claude-sonnet-4-5` | Messages API; structured output through tool-use schemas. `claude-haiku-4-5` is a good lower-cost choice for triage. |
| `openai` | API key from `https://platform.openai.com/` | `gpt-4o-mini` | Chat Completions with `response_format: json_schema`. |
| `openai` + `base_url` | key from the host | host-specific | Any OpenAI-compatible server. Presets: `groq`, `together`, `openrouter`, `lmstudio` (type the preset name in Base URL and the app expands it). |
| `ollama` | a running Ollama (`https://ollama.com/`) | `qwen3.5:4b` | Local, free, no key. `base_url` defaults to `http://localhost:11434`. Pull the model first (`ollama pull qwen3.5:4b`). |

Base URL presets:

```
openai      https://api.openai.com/v1
groq        https://api.groq.com/openai/v1
together    https://api.together.xyz/v1
openrouter  https://openrouter.ai/api/v1
lmstudio    http://localhost:1234/v1
```

For LM Studio start the local server in the app (Developer > Start Server),
load a model, and use any non-empty key. For OpenRouter the model name is
the full `vendor/model` string.

### Entering the key

Settings > AI: pick the provider, paste the key, optionally set model and
base URL, click **Test**. Test makes one tiny call and shows the latency, the
model that answered and the estimated cost. Save writes the key to the OS
keyring (service `EmailBoss`, key `ai_api_key`); `plannerboard.json` only
records the provider, model, base URL and feature flags.

Environment fallback: `PB_AI_API_KEY` is used when the keyring has no key
(headless installs, CI).

### Model choice

- Triage classification and summaries are short, structured calls. Small
  models do them well: `claude-haiku-4-5`, `gpt-4o-mini`, `gpt-4.1-mini`,
  `llama-3.3-70b` on Groq, or a local 4B to 8B model.
- Draft replies and the assistant pass benefit from a stronger model
  (`claude-sonnet-4-5`, `gpt-4.1`). All features share one model setting; pick
  for the drafts if you use them, otherwise pick for cost.
- Local models must return valid JSON for the structured prompts. Qwen and
  Llama 3.x instruct models do; very small models (under 3B) often fail the
  post-validators and fall back to rules, which is safe but wastes the call.

## Which features call the model

`ai.features` toggles each group independently. Everything has a rules
fallback.

| feature flag | prompts | when it runs | what it does |
|---|---|---|---|
| `classify` | `triage.classify`, `triage.suggest_project` | each new email in triage | picks one of the profession's triage classes; proposes a clean name, work type and scope for a new project (never creates one) |
| `summarize` | `triage.summarize_bullets` | each new email in triage | 2 to 4 fact bullets for the card; a grounding guard drops any bullet whose facts are not in the email |
| `match_rerank` | `triage.match_rerank` | only when an email matches 2 or more projects | tie-break among the existing candidates or abstain; cannot invent a project |
| `draft_reply` | `reply.no_reply_classifier`, `reply.draft`, `followup.draft` | the Draft reply / Draft follow-up buttons; the assistant pass | decides whether a reply is needed, writes a short body with no greeting, no signature and nothing the thread does not already say |
| `assistant` | `assistant.board_pass`, `project.health` | the scheduled assistant pass (`assistant.schedule_minutes`) and the Run now button | drains triage, rates the top N projects, drafts or asks a question; every item ends as `acted`, `drafted`, `question_posted`, `subtask_added` or `deferred` |
| (always on with a provider) | `project.health`, `search.parse`, `digest.weekly` | project drawer, search box, weekly digest | advisory health rating and next action; natural-language search to filters; a short weekly narrative |

Each prompt lives in `plannerboard/ai/prompts/<id>.yaml` with its system
text, user template, output schema, `max_tokens` (200 to 320) and
post-validators. You can read exactly what is asked.

## Daily budget

`ai.daily_budget_usd` (default 2.00) caps spend per calendar day, computed
from the price table in `plannerboard/ai/usage.py` (USD per million input
and output tokens by model prefix). The ledger is the `ai_usage` table;
Settings > AI > Usage and `GET /api/ai/usage` show today's cost, call count
and cost by task. The meter is persisted, so a restart cannot double-spend.

Models not in the table are metered at $0 and flagged "cost unknown" rather
than "free"; Ollama, LM Studio and anything prefixed `local/` are known to be
$0. If you use an unlisted hosted model, set the budget by call count instead
(watch Usage for a day) or add the model's price to the table.

## Cost expectations

Rough math for 100 inbound emails with `classify` and `summarize` on, using
the price table. Each email makes two calls of about 900 input tokens (system
prompt plus the first 1,500 characters of subject and body) and about 120
output tokens, so 1,800 in / 240 out per email.

| model | per 100 emails |
|---|---|
| `llama-3.1-8b` (Groq) | $0.01 |
| `gpt-4o-mini` | $0.04 |
| `gpt-5-mini` | $0.09 |
| `gpt-4.1-mini` | $0.11 |
| `llama-3.3-70b` (Groq) | $0.13 |
| `claude-haiku-4-5` | $0.30 |
| `claude-sonnet-4-5` | $0.90 |
| Ollama / LM Studio | $0 |

Reranks add one call for the few emails that match several projects. A draft
reply is about 700 in / 250 out: under a cent on `claude-sonnet-4-5`
($0.006), a fraction of that on `gpt-4o-mini`. The assistant pass costs one
`project.health` call per project it walks (`assistant.max_items`, default
10) plus any drafts, so an hourly pass on a 10-project board is roughly
$0.05 to $0.50 a day depending on the model.

A typical single-user inbox (30 to 60 relevant emails a day) therefore lands
between a few cents and about a dollar a day on hosted models. The default
$2.00 budget is a ceiling, not a target.

## Privacy: what text is sent

Only when a feature fires, and only to the provider you configured:

- Triage (`classify`, `summarize`, `match_rerank`): the email's subject,
  sender address, the first 1,200 to 1,500 characters of the body after
  signature and quoted-reply stripping, attachment names, and for reranks the
  names, locations and contact emails of the candidate projects.
- Drafts: the inbound message (same trimming), the project name, location,
  scope and the last three notes, plus your name, company and role from
  config. Customer-facing text may reference only the fields listed in the
  profession's `customer_visible_fields`.
- Project health / assistant: project name, status, open task texts, days
  since last activity, last three notes (120 characters each), contact
  emails, key reference numbers.
- Search: the search phrase you typed.
- Weekly digest: aggregate counts only.

Never sent: attachment contents, your API keys, the license key, the
database, mail you have already filed, or anything from projects not
involved in the call. Attachment text extraction (PDF, XLSX, CSV) is local.

Hosted providers see the text under their own terms. Anthropic and OpenAI
both publish API data-use policies that, at the time of writing, exclude API
traffic from model training by default; read the current policy for whichever
host you use, including Groq, Together and OpenRouter. Ollama and LM Studio
keep everything on your machine.

## Troubleshooting

| symptom | check |
|---|---|
| Test fails with 401 | key pasted with whitespace, wrong provider selected, or (OpenRouter / Groq) key for a different host |
| Test fails with "connection refused" on Ollama / LM Studio | the local server is not running, or `base_url` has the wrong port |
| Cards show verbatim summaries, no classification | `/api/health.ai`: `budget_hit` true, `ok` false, or the feature flag is off |
| JSON parse errors in the log | small local model; switch to a 7B+ instruct model or raise `max_tokens` in the prompt YAML |
| Cost shows $0 with a hosted model | model not in the price table; usage is still counted in tokens |
