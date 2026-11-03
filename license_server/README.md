# Email Boss License Server

FastAPI + SQLite + Ed25519. Issues signed license keys, activates devices,
hands out signed 30-day activations, and hosts the customer portal and admin
pages. No Docker required.

Full protocol and threat model: `../docs/LICENSING.md`.

## Layout

```
licserver/
  main.py        app factory; `uvicorn licserver.main:app`
  config.py      Settings from environment
  keys.py        Ed25519 load/sign/verify (token format shared with the app client)
  models.py      sqlite3 schema + queries (WAL, one connection per call)
  auth.py        signup/login/logout, scrypt passwords, reset tokens, rate limit, admin gate
  licenses.py    issue/renew/revoke + /v1/activate, /v1/heartbeat, /v1/deactivate, /v1/validate, /v1/public-key
  billing.py     Stripe webhook stub
  admin.py       /admin pages
  portal.py      customer pages (dashboard, download, account lives in auth.py)
  web.py         templates, sessions, CSRF, redirects, RateLimiter
  mailer.py      pluggable email (console logger by default)
  templates/     Jinja, dark theme
  static/        portal.css
scripts/gen_keys.py
keys/DEV_ONLY_private.pem   dev signing key. Never deploy it.
tests/
```

## Quick start (dev)

```bash
cd license_server
pip install -e ".[test]"
TRIAL_ENABLED=1 uvicorn licserver.main:app --reload --port 8600
# http://localhost:8600  -> create an account; the first account becomes admin
#                          when LIC_ADMIN_EMAILS is empty
python -m pytest -q
```

## Environment variables

| var | default | meaning |
|---|---|---|
| `LIC_PRIVATE_KEY_PATH` | `keys/DEV_ONLY_private.pem` | Ed25519 PKCS8 PEM used to sign keys and activations |
| `LIC_DB_PATH` | `license_server/licserver.db` | SQLite file (WAL). Back it up. |
| `LIC_BASE_URL` | `http://localhost:8600` | public URL, used in emails (reset links, portal link) |
| `LIC_PRODUCT_NAME` | `Email Boss` | shown in the UI and emails |
| `LIC_SECURE_COOKIES` | `0` | set `1` behind HTTPS (adds `Secure` to cookies) |
| `LIC_ADMIN_EMAILS` | empty | comma list; those accounts are admins. Empty = first signup is admin |
| `TRIAL_ENABLED` | `0` | `1` issues a trial license at signup |
| `TRIAL_DAYS` | `14` | trial length |
| `LIC_ACTIVATION_DAYS` | `30` | activation `valid_until` horizon (client grace is 14 days past it) |
| `LIC_LOGIN_RATE_LIMIT` / `LIC_LOGIN_RATE_WINDOW` | `10` / `300` | attempts per IP per window (in memory, per process) |
| `STRIPE_WEBHOOK_SECRET` | empty | `/webhooks/stripe` verifies `Stripe-Signature` with it; while empty the endpoint answers 503 (fail closed) |
| `LIC_ALLOW_UNSIGNED_STRIPE` | `0` | `1` accepts unsigned webhook events. Local development only; never in production |
| `LIC_TRUSTED_PROXY` | `0` | `1` when a reverse proxy in front of the server appends `X-Forwarded-For`; the rate limiter then keys on that (last hop). Leave `0` when clients connect directly, or the header can be spoofed |
| `STRIPE_PLAN_MAP` | empty | `price_abc:solo:2,price_def:team:5` maps Stripe price ids to plan and seats |
| `ANTHROPIC_API_KEY` | empty | vendor key for the AI relay; while empty `/v1/ai/complete` answers 503 `relay_disabled` |
| `AI_MARKUP` | `4.0` | credits charged = `ceil(vendor_usd * 100 * AI_MARKUP)` |
| `AI_MIN_CREDITS_PER_CALL` | `1` | minimum charge per successful call |
| `AI_TRIAL_CREDITS` | `300` | free credits granted once per license on first AI use |
| `AI_RATE_PER_MIN` | `60` | AI calls per license per minute (in memory, per process) |
| `AI_MAX_TOKENS` | `4000` | cap applied to the requested `max_tokens` |
| `AI_MODEL_FAST` / `AI_MODEL_SMART` | `claude-haiku-4-5` / `claude-sonnet-4-5` | vendor models behind the `fast` and `smart` tiers |
| `STRIPE_CREDIT_PACKS` | empty | `price_id:credits:usd_cents,...` e.g. `price_a:500:500,price_b:3000:2500` |
| `STRIPE_SECRET_KEY` | empty | used to create Checkout sessions for credit packs (REST, no SDK); empty = 503 `billing_unconfigured` |
| `DOWNLOAD_URL_WINDOWS`, `DOWNLOAD_URL_MACOS`, `DOWNLOAD_URL_LINUX` | empty | links on the Download page (any `DOWNLOAD_URL_*` is listed) |

Plans (`licenses.PLANS`): `trial` 1 seat / 14 days, `solo` 2 seats / 365 days,
`team` 5 seats / 365 days. Admin issue form can override seats, days and features.

## Deploy without Docker (systemd + uvicorn behind Caddy or nginx)

```bash
# as root
useradd --system --home /opt/licserver --shell /usr/sbin/nologin licserver
mkdir -p /opt/licserver /var/lib/licserver /etc/licserver
cp -r license_server/* /opt/licserver/
cd /opt/licserver && python3 -m venv venv && venv/bin/pip install -e .
venv/bin/python scripts/gen_keys.py --out /etc/licserver --name prod   # prints the public PEM
chown -R licserver:licserver /opt/licserver /var/lib/licserver /etc/licserver
chmod 600 /etc/licserver/prod_private.pem
```

`/etc/licserver/env`:

```
LIC_PRIVATE_KEY_PATH=/etc/licserver/prod_private.pem
LIC_DB_PATH=/var/lib/licserver/licserver.db
LIC_BASE_URL=https://license.example.com
LIC_SECURE_COOKIES=1
LIC_ADMIN_EMAILS=you@example.com
LIC_TRUSTED_PROXY=1
TRIAL_ENABLED=1
STRIPE_WEBHOOK_SECRET=whsec_...
DOWNLOAD_URL_WINDOWS=https://downloads.example.com/EmailBoss-Setup.exe
```

`/etc/systemd/system/licserver.service`:

```
[Unit]
Description=Email Boss license server
After=network.target

[Service]
User=licserver
Group=licserver
WorkingDirectory=/opt/licserver
EnvironmentFile=/etc/licserver/env
ExecStart=/opt/licserver/venv/bin/uvicorn licserver.main:app --host 127.0.0.1 --port 8600 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/licserver
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now licserver
```

Caddyfile (automatic TLS):

```
license.example.com {
    reverse_proxy 127.0.0.1:8600
}
```

nginx equivalent:

```
server {
    listen 443 ssl http2;
    server_name license.example.com;
    ssl_certificate     /etc/letsencrypt/live/license.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/license.example.com/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8600;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Run one uvicorn worker. The login rate limiter is in-process memory and
SQLite writes are serialized per process; one worker handles thousands of
heartbeats a day without effort. Back up `LIC_DB_PATH` (sqlite3 `.backup`)
nightly; the signing key is the other thing that must never be lost.

Stripe: point the webhook at `https://license.example.com/webhooks/stripe`
with events `checkout.session.completed`, `customer.subscription.deleted`,
`invoice.paid`. Checkout sessions should carry `metadata.plan` (or set
`STRIPE_PLAN_MAP`) and the customer email.

Email: `licserver.mailer.SENDER` is a callable `(to, subject, body)`; the
default logs to the journal. Replace it in a small sitecustomize or a wrapper
module that imports `licserver.main` and sets `mailer.SENDER` before uvicorn
starts.

## Key rotation

1. `python scripts/gen_keys.py --out /etc/licserver --name prod2`.
2. Ship an app release whose `DEFAULT_PUBLIC_KEY_PEM` (in
   `app/plannerboard/license/client.py`) is the new public PEM. Clients only
   trust one key, so keys and activations signed by the old key stop
   verifying on the new build.
3. Point `LIC_PRIVATE_KEY_PATH` at the new private key and restart.
4. Re-sign every live license with the new key (`renew_license` with
   `days=0` re-signs without moving expiry; a small script over
   `db.list_licenses()` does it) so the portal shows keys the new build
   accepts. Activations re-sign themselves on the next heartbeat.
5. Tell customers on old builds to update; their next heartbeat on the new
   build pulls a new activation.

`keys/DEV_ONLY_private.pem` is checked in for development and tests only. It
must be rotated before production: anyone with it can mint keys the shipped
dev public key accepts.

## Endpoints

Device API (JSON):

| method | path | body | returns |
|---|---|---|---|
| POST | `/v1/activate` | `{key, fingerprint, app_version}` | `{ok, activation, payload, license}` or 400 `bad_key`, 403 `revoked|expired`, 404 `unknown_license`, 409 `seat_limit` |
| POST | `/v1/heartbeat` | `{lid, fingerprint, app_version}` | same as activate; 404 `not_activated` |
| POST | `/v1/deactivate` | `{lid, fingerprint}` | `{ok, released}` |
| POST | `/v1/validate` | `{key}` | `{valid, state, lid, plan, seats, seats_used, expires, features}` |
| GET | `/v1/public-key` | | `{algorithm, key_id, public_key_pem, token_format}` |
| POST | `/webhooks/stripe` | Stripe event | `{ok, ...}` |
| GET | `/healthz` | | `{ok, accounts, key_id}` |

Errors are `{"detail": {"code": "...", "message": "..."}}`.

Portal (HTML, CSRF on every POST): `/`, `/signup`, `/login`, `/logout`,
`/forgot`, `/reset/{token}`, `/dashboard`, `/download`, `/account`,
`/account/password`, `/licenses/{lid}/devices/{id}/deactivate`.

Admin (`is_admin` accounts): `/admin`, `/admin/issue`,
`/admin/licenses/{lid}/revoke`, `/admin/licenses/{lid}/renew`,
`/admin/accounts/{id}/admin`, `/admin/activations`,
`/admin/activations/{lid}/{id}/revoke`, `/admin/audit`.
