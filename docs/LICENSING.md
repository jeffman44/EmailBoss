# Licensing

How Email Boss proves it is licensed, what the hosted license server does,
and what an attacker can and cannot get away with.

Code: `app/plannerboard/license/` (client), `app/plannerboard/api/license_api.py`
(the `/api/license` blueprint), `license_server/` (portal + device API).

## Flow

```
   customer                 license server                        desktop app
   --------                 --------------                        -----------
   signup / Stripe  ---->   issue license
                            payload {lid,email,plan,seats,
                                     features,issued,expires}
                            key = b64u(payload).b64u(Ed25519 sig)
                   <----    key shown on dashboard / emailed

   paste key into Settings > License  ------------------------->  parse_key(key)
                                                                   verify sig with shipped PUBLIC key
                            POST /v1/activate              <----  {key, fingerprint, app_version}
                            verify key sig, lid live,
                            seats used < seats
                            activation {lid,fingerprint,
                              issued,valid_until=+30d,
                              plan,features} signed      ---->   store key + activation in app_meta
                                                                   status() = ok

   every 24 h               POST /v1/heartbeat             <----  {lid, fingerprint, app_version}
                            fresh activation (+30d)        ---->   replace stored activation

   offline                                                        now <= valid_until          ok
                                                                  valid_until < now <= +14d   grace (writes allowed)
                                                                  now > valid_until + 14d     expired -> read-only
                                                                                               (board loads, writes 402)

   portal "Deactivate device" / admin revoke / Stripe cancel      next heartbeat -> 403/404
                                                                   activation cleared -> invalid -> read-only
```

Timestamps are ISO 8601 UTC (`2026-10-05T12:00:00Z`). Tokens are
`base64url(payload_json).base64url(signature)` with canonical JSON
(`sort_keys=True`, separators `,` and `:`); the signature covers the exact
bytes in segment one.

## Status values (`GET /api/license`, `status()`)

| status | read_only | meaning |
|---|---|---|
| `dev` | no | `PB_LICENSE_DEV=1`; banner shown, nothing checked |
| `ok` | no | key verifies, activation verifies for this machine, `now <= valid_until` |
| `grace` | no | activation lapsed less than 14 days ago; `grace_days_left` counts down |
| `expired` | yes | license `expires` passed, or grace ran out |
| `unlicensed` | yes | no key, or key saved but never activated here |
| `invalid` | yes | bad signature, activation for another machine or another lid, or the server rejected the last heartbeat (revoked, seat released) |

Fields: `status, plan, email, expires, valid_until, grace_days_left, features[],
seats, message, read_only, fingerprint, server_url`.

Read-only mode: `license.middleware(app)` registers a Flask `before_request`
that answers `402 {"error": "license_required", "license": {...}}` to
`POST/PUT/PATCH/DELETE` under `/api/`, except `/api/license/*`, `/api/setup/*`,
`/api/health` and `/api/restart`, so the user can always fix the license.
Reads keep working; the board renders.

## Machine fingerprint

sha256 over `hostname`, primary MAC from `uuid.getnode()` (dropped when it is
the random multicast fallback), `platform.system|machine`, and the OS machine
id (`/etc/machine-id`, macOS `IOPlatformUUID`, Windows `MachineGuid`). Only
the hash leaves the machine. Renaming the host or swapping the NIC changes the
fingerprint; the user then deactivates the old device in the portal and
activates again (or an admin releases the seat).

## Threat model

### Key sharing (one key, many machines)

Enforced server-side. A key alone is not enough to run: the app needs a
signed activation bound to its fingerprint, and the server only signs one
when `active activations < seats`. Sharing a key hands out seats, and the
owner sees every device on the dashboard and can release any of them. The
30-day activation window means a leaked key that was later revoked keeps a
machine running for at most 30 + 14 days.

Not enforced: a determined user can copy a whole activated profile
(app_meta) onto an identical VM image whose fingerprint matches. That is
the ceiling for a client-side check and we accept it; heartbeats from two
"identical" machines still look like one seat.

### Clock rollback

Client-side mitigation. `LicenseClient` keeps a high-water mark
(`license.clock_hwm`) of the latest wall-clock time it has seen and computes
status against `max(now, hwm)`. Rolling the clock back after grace ran out
does not reopen writes. Resetting the clock *before* first use and never
moving it forward is still possible; the next successful heartbeat replaces
the activation with server time, and the server refuses heartbeats for
revoked or expired licenses, so the attack buys at most the offline window.
The mark is one row in `app_meta`; since 1.1.0 the client ALSO floors `now`
at the signed key's `issued` and the signed activation's `issued`, which
cannot be edited without breaking a signature and cannot be deleted without
losing the license itself. Deleting just the `clock_hwm` row therefore no
longer reopens writes.

Server-side the clock is authoritative: `expires` on the licenses row is what
`/v1/activate` and `/v1/heartbeat` check, not the `expires` inside the key.

### Renewals

A renewal (admin, Stripe `invoice.paid`) re-signs the key on the server. A
client that still holds the old key string keeps working: since 1.1.0 the
signed activation carries `expires` from the licenses row, the heartbeat
re-issues it daily, and the client uses the later of the key's and the
activation's `expires`. An activation can never shorten the key's own
expiry.

### Offline grace

14 days past `valid_until` (which is itself 30 days past the last heartbeat),
so a laptop can be off the network for up to 44 days after its last contact
before dropping to read-only. Grace is purely client-side and purely
time-based; no server call is needed to enter or leave it. When the network
comes back, `heartbeat()` (daily, or `POST /api/license/refresh`) restores
`ok`. Nothing is deleted on expiry: the data stays, writes return 402 until a
heartbeat succeeds.

### Forged keys and activations

Ed25519 over the payload bytes. The private key exists only on the license
server (`LIC_PRIVATE_KEY_PATH`); the app ships the public key
(`DEFAULT_PUBLIC_KEY_PEM`). A modified payload, a key signed by another pair,
or a truncated token fails `parse_key`. Patching the binary to accept
anything is the usual limit of client-side licensing and is out of scope.

The repository ships `license_server/keys/DEV_ONLY_private.pem` for
development and tests. Anyone with it can mint keys the dev public key
accepts, so it must be rotated before shipping (see "Key rotation" in
`license_server/README.md`).

### Replay and tampering with the server

`/v1/deactivate` takes `{lid, fingerprint}` without auth: knowing both lets
someone release a seat (a nuisance, not a bypass; the device re-activates on
its next heartbeat if a seat is free). `/v1/activate` needs the signed key.
Portal and admin forms use a session cookie (HttpOnly, SameSite=Lax) plus a
double-submit CSRF token. Login and signup are rate-limited per IP in memory.
Passwords use `hashlib.scrypt` (n=2^14, r=8, p=1, per-user salt). The Stripe
webhook verifies the `Stripe-Signature` HMAC when `STRIPE_WEBHOOK_SECRET` is
set and tolerates 5 minutes of skew.

## AI relay

The server also hosts the built-in "Email Boss AI" relay (full design in
`docs/AI_CREDITS.md`). All three endpoints authenticate with the signed
activation token the app already holds (signature, `lid`, `fingerprint`,
`valid_until`, license live, device not released); failures are
`403 {"ok":false,"code":"license","reason":...}`.

- `POST /v1/ai/complete` `{lid, fingerprint, activation, task, tier, system, user, max_tokens, temperature, json_schema}`:
  402 `no_credits`, 429 `rate_limited`, 502 `vendor`, 503 `relay_disabled`.
  Charged `ceil(vendor_usd * 100 * AI_MARKUP)` credits (minimum `AI_MIN_CREDITS_PER_CALL`).
  Prompt and completion text are never stored or logged.
- `GET /v1/ai/balance?lid=&fingerprint=&activation=` returns balance, packs and checkout base.
- `POST /v1/ai/checkout {lid, fingerprint, activation, pack}` returns a Stripe Checkout URL.
  `checkout.session.completed` with `metadata.kind == "ai_credits"` credits the wallet once per session id.

Env vars: `ANTHROPIC_API_KEY`, `AI_MARKUP`, `AI_MIN_CREDITS_PER_CALL`, `AI_TRIAL_CREDITS`,
`AI_RATE_PER_MIN`, `AI_MAX_TOKENS`, `AI_MODEL_FAST`, `AI_MODEL_SMART`, `STRIPE_CREDIT_PACKS`,
`STRIPE_SECRET_KEY` (table in `license_server/README.md`).

## What is enforced where

| check | client | server |
|---|---|---|
| key signature | yes (`parse_key`) | yes |
| key `expires` | yes (later of key and signed activation) | yes (DB row, authoritative) |
| seat count | no | yes (`/v1/activate`, count + insert in one transaction) |
| fingerprint binding | yes (activation must match this machine) | yes (activation row per `lid, fingerprint`) |
| revocation | only after a heartbeat says so | yes (activate + heartbeat refuse) |
| offline grace (14 d) | yes | not involved |
| clock rollback | high-water mark + signed `issued` floors | server time on every heartbeat |
| read-only enforcement (402) | yes (`middleware`) | not involved |
| dev bypass | `PB_LICENSE_DEV=1`, dev build channel only, visible banner | none |

## Developer notes

- Dev mode: `PB_LICENSE_DEV=1` (or the hidden `--dev-license` flag) makes
  `status()` return `dev` and the UI shows a banner. It only works when
  `plannerboard/_channel.py` says `CHANNEL = "dev"`, which is what a source
  checkout says; `build_release.sh` / `build_release.ps1` flip it to
  `"release"` for the wheel and bundle they produce and restore it after. A
  shipped build logs a warning and ignores the variable.
- Server hardening knobs (1.1.0): `LIC_TRUSTED_PROXY=1` is the only way
  `X-Forwarded-For` is honoured by the rate limiter (last hop); without a
  proxy the socket address is used. `/webhooks/stripe` answers 503 while
  `STRIPE_WEBHOOK_SECRET` is unset unless `LIC_ALLOW_UNSIGNED_STRIPE=1`
  (local development only). Every Stripe event id is recorded and a replay
  is ignored; `invoice.paid` sets expiry to the invoice period end and skips
  the subscription's first invoice (checkout already issued the license).
  Reset tokens are stored as SHA-256 digests and consumed with a conditional
  update.

### Known limits (not fixed in 1.1.0)

- Admin bootstrap is by signup: the first account when `LIC_ADMIN_EMAILS` is
  empty, or any signup matching that list. Set `LIC_ADMIN_EMAILS` BEFORE the
  server is reachable and create those accounts yourself first.
- CSRF for logged-in forms is a double-submit cookie (`lic_csrf`), not bound
  to the session row; `SameSite=Lax` and `LIC_SECURE_COOKIES=1` behind HTTPS
  are what hold it. Do not serve the portal on a domain that shares cookies
  with untrusted subdomains.
- Device proof (heartbeat, deactivate, `/v1/ai/*`, and `/v1/activate` for a
  device that already holds a seat): `proof = HMAC-SHA256(k, lid|fingerprint|ts|path|nonce|body_sha256)`
  with `k = sha256(device_secret)` as hex text, `body_sha256` over the
  canonical JSON (sorted keys, compact, ASCII) of the body without
  `ts`/`nonce`/`proof`. `ts` must be within 300 s; the server remembers each
  `(lid, nonce)` for 600 s (table `proof_nonces`) and rejects reuse, so a
  captured proof authorizes exactly one request. Known limitation: the key `k`
  is the hash the server stores, so a leaked database lets an attacker forge
  proofs for those devices. The upgrade path is a per-device Ed25519 key pair
  (the device keeps the private key, the server stores only the public key),
  which needs a client release.
- A bare `/v1/activate` (license key only) for a fingerprint that already
  holds a seat with a device secret is refused `409 device_bound`; the device
  must send its own proof, or the seat must be released in the portal first
  (this is also how a lost or reinstalled device recovers). Seats made before
  1.3.0 have no secret and still upgrade with the key once.
- `/v1/trial` never returns the license key. The key, the email confirmation
  link and a set-password link go to the inbox; the device is bound at once
  (activation and device secret come back) and the app runs from the signed
  activation until the key is pasted in. An address that already has an
  account answers the same generic 409 as one that already had a trial.
  AI trial credits are funded when the email is confirmed (`/verify/<token>`
  or a completed password reset), not by the trial call; until then
  `/v1/ai/balance` returns `balance: 0, pending_verification: true`.
  Budgets: `AI_TRIAL_PER_DAY` (200 trials a day, whole server) and
  `TRIAL_PER_IP_DAY` (5 a day per /24 IPv4 or /64 IPv6), both 429
  `trial_budget`.
- `/v1/deactivate` needs the device proof (or, for a seat made before 1.3.0,
  the license key). A lost laptop is released from the portal.
- Key rotation has no `kid`: both sides hold exactly one public key, so a
  rotation re-signs every live license and needs a client release that
  ships the new key.
- `STRIPE_PLAN_MAP` by price id only matches when the Checkout Session
  webhook includes expanded `line_items`, which Stripe does not do by
  default; set `metadata.plan` / `metadata.seats` on the Checkout Session.
- Config: `license.key` and `license.server_url` in `plannerboard.json`
  (`license.key` is copied into `app_meta` on first run); the activation and
  the heartbeat bookkeeping live in `app_meta` only.
- The app never stores the private key, customer passwords or anything
  about other customers. The only identifying datum sent to the server is
  the fingerprint hash and the app version.
- `app/tests/test_license_client.py` covers the state machine and grace math;
  `license_server/tests/` covers issue, seat limits, heartbeat, revoke, Stripe
  and the client end to end against the real server.
