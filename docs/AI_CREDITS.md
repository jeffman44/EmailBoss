# Email Boss AI (built-in, metered)

Users should never need an Anthropic or OpenAI account. The app ships with a
provider called **Email Boss AI**: the desktop app sends each prompt to the
license server, the license server forwards it to the model vendor with the
vendor's own key, meters the tokens, and charges the user's **credit wallet**
at a margin. Users buy credit packs from the portal (Stripe Checkout). A new
license starts with a small free allowance so the AI features work on day one.

Bring-your-own-key (Anthropic / OpenAI-compatible / Ollama) stays available
under "Advanced" for people who want it. Nothing else about the AI layer
changes: same prompt registry, same guards, same drafts-only rules.

## Money

- 1 credit = USD 0.01 at purchase. Packs come from `STRIPE_CREDIT_PACKS`
  (`price_id:credits,...`), e.g. 500 credits for $5, 3000 for $25.
- A call is charged `ceil(vendor_cost_usd * 100 * AI_MARKUP)` credits, minimum
  `AI_MIN_CREDITS_PER_CALL` (default 1). `AI_MARKUP` default 4.0, i.e. a
  Sonnet triage call that costs the vendor ~0.4 cents charges 2 credits.
  Haiku-tier calls are usually the 1-credit minimum. The markup is the margin.
- `AI_TRIAL_CREDITS` (default 300) are granted once per account, when the
  account's email is confirmed (never by `/v1/trial` itself). Admins can grant credits from the portal.
- The wallet can't go negative: a call is refused with 402 `no_credits` when
  the balance is below the reserve for the requested `max_tokens`; the app
  turns that into a "Buy AI credits" prompt and falls back to rules.

## Server (`license_server/licserver/ai_relay.py`)

Tables (`models.py`): `ai_wallets(lid PK, balance, trial_granted, updated)`,
`ai_ledger(id, lid, fingerprint, ts, task, tier, model, in_tok, out_tok,
vendor_usd, credits, ok, error)`, `ai_purchases(id, lid, stripe_session,
credits, usd_cents, ts)`.

Endpoints (`/v1/ai/*`, device API, no portal session):

- `POST /v1/ai/complete` body `{lid, fingerprint, activation, task, tier,
  system, user, max_tokens, temperature, json_schema}`.
  Auth: `activation` is the signed activation token the app already holds;
  the server verifies the signature, `lid`, `fingerprint` and `valid_until`,
  then checks the license is live. Rate limit per lid (`AI_RATE_PER_MIN`,
  default 60). `max_tokens` capped at `AI_MAX_TOKENS` (4000). `tier` is
  `fast` or `smart`; the server maps tiers to vendor models
  (`AI_MODEL_FAST`, `AI_MODEL_SMART`); the app never names a vendor model.
  Response `{ok, text, parsed, input_tokens, output_tokens, credits_charged,
  balance, model_tier, latency_ms}` or `{ok:false, error, code}` with
  402 `no_credits`, 403 `license`, 413 `input_too_large` / `schema_too_large`
  (`json_schema` is capped at 8 KB and counts toward the input cap and the
  hold; non-ASCII text is estimated at 1 token per character), 429
  `rate_limited` / `daily_cap` (holds still in flight count toward the cap),
  502 `vendor`.
  Prompt and completion text are NEVER stored or logged; the ledger holds
  metadata only.
- `GET /v1/ai/balance?lid=&fingerprint=&activation=` -> `{balance, trial_granted, pending_verification,
  packs:[{id,label,credits,usd_cents}], checkout_url_base}`.
- `POST /v1/ai/checkout {lid, fingerprint, activation, pack}` -> Stripe
  Checkout session URL (`mode=payment`, `metadata.lid`, `metadata.credits`).
  The existing Stripe webhook credits the wallet on
  `checkout.session.completed` when `metadata.kind == "ai_credits"`
  (idempotent by session id).
- Portal: "AI credits" card on the dashboard (balance, buy buttons, last 30
  days of ledger totals). Admin: grant credits, view per-license spend.

Vendor call: Anthropic Messages API with `ANTHROPIC_API_KEY`; JSON mode via
tool-use when `json_schema` is given (same contract as the app's own
`anthropic_provider.py`, reuse its request/response shaping). Timeouts 60 s.
If `ANTHROPIC_API_KEY` is unset the endpoint answers 503 `relay_disabled`.

## App

- `ai/emailboss_provider.py`: `EmailBossProvider(LLMProvider)`; reads `lid`,
  `fingerprint`, activation token and `server_url` from the license client;
  `complete()` POSTs to `/v1/ai/complete`; `LLMResult.cost_usd` =
  credits/100; errors map to categories (`credits`, `license`, `rate`,
  `vendor`, `network`). `available()` is true when the license status is
  `ok` or `grace`.
- `factory.py`: provider id `emailboss`, label "Email Boss AI (built in)",
  `needs_key: False`; it is the DEFAULT `ai.provider` for a fresh config.
- `usage.py`: records credits; `/api/ai/usage` adds `balance`,
  `credits_today`.
- `/api/ai/balance` (GET, cached 60 s) and `/api/ai/checkout` (POST, returns
  the URL; the UI opens it in the system browser through the desktop bridge).
- Health: `ai.provider`, `ai.balance`, `ai.low_credits` (< 50).
- Setup wizard AI step: default choice "Email Boss AI: works out of the box,
  300 free credits, then buy more inside the app". Advanced link reveals the
  BYO-key form unchanged.
- Settings > AI: balance, "Buy credits", usage this month, provider switch.
- When a call fails with `credits`, the UI shows one banner: "Out of AI
  credits. [Buy credits]" and the feature falls back to rules.

## Not in scope

Subscriptions for AI (packs only), per-seat wallets (wallet is per license),
vendor choice for the hosted relay (Anthropic only).
