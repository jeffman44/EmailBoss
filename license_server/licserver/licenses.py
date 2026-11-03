"""License issue / renew / revoke and the /v1 device API.

License key payload (signed, embedded in the key string):
    {"lid": "lic_...", "email": "...", "plan": "trial|solo|team", "seats": 1,
     "features": ["core", ...], "issued": "2026-10-05T12:00:00Z", "expires": "2027-10-05T12:00:00Z"}

Activation payload (signed, returned by /v1/activate and /v1/heartbeat):
    {"lid": "...", "fingerprint": "<sha256 hex>", "issued": "...", "valid_until": "<issued + 30d>",
     "plan": "...", "features": [...]}

Both are <base64url(payload_json)>.<base64url(sig)>; see keys.py.

Proof of possession: /v1/activate and /v1/trial return a random 32-byte
`device_secret` (hex) exactly once; the server keeps only
sha256(device_secret) on the activation row. Every later device call
(/v1/heartbeat, /v1/deactivate, /v1/ai/*, and /v1/activate for a device that
already holds a seat) carries
    ts     = unix seconds (within PROOF_WINDOW_S of server time)
    nonce  = random string, single use per license for NONCE_TTL_S (600 s)
    proof  = hex(HMAC-SHA256(device_key,
                 f"{lid}|{fingerprint}|{ts}|{path}|{nonce}|{body_sha256}"))
where device_key = sha256(device_secret) as hex text (the stored value, so
the server can recompute the MAC) and body_sha256 is the sha256 of the
canonical JSON (sort_keys, compact separators, ASCII) of the request body
WITHOUT ts, nonce and proof. A captured proof therefore authorizes exactly one
request. A stolen activation token alone does not work. Activations made
before 1.3.0 have no secret: one heartbeat that carries the license key
(body.key, verified, same lid) upgrades them and returns a device_secret.
A bare /v1/activate for a fingerprint that already holds a seat (with a
secret) is refused 409 device_bound; release the device in the portal first.

Error bodies are always {"detail": {"code": "...", "message": "..."}} so the
app client can surface a code without parsing prose.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .keys import Signer, SigningError
from .models import Database, iso, new_id, parse_iso, utcnow

log = logging.getLogger("licserver.licenses")
router = APIRouter(prefix="/v1", tags=["device"])

PLANS: dict[str, dict[str, Any]] = {
    "trial": {"seats": 1, "days": 14, "features": ["core", "ai", "assistant"]},
    "solo": {"seats": 2, "days": 365, "features": ["core", "ai", "assistant"]},
    "team": {"seats": 5, "days": 365, "features": ["core", "ai", "assistant", "multi_seat"]},
}


PROOF_WINDOW_S = 300
PROOF_FIELDS = ("ts", "nonce", "proof")
NONCE_MIN, NONCE_MAX = 8, 64


def secret_hash(device_secret: str) -> str:
    return hashlib.sha256(device_secret.encode("utf-8")).hexdigest()


def body_sha256(body: Any) -> str:
    """sha256 over the canonical JSON of the request body minus ts/nonce/proof."""
    d = {k: v for k, v in (body or {}).items() if k not in PROOF_FIELDS} if isinstance(body, dict) else {}
    raw = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def proof_message(lid: str, fingerprint: str, ts: int, path: str, nonce: str = "", body_sha: str = "") -> bytes:
    return f"{lid}|{fingerprint}|{int(ts)}|{path}|{nonce}|{body_sha}".encode("utf-8")


def compute_proof(device_key: str, lid: str, fingerprint: str, ts: int, path: str,
                  nonce: str = "", body_sha: str = "") -> str:
    """device_key is sha256(device_secret) hex; the client derives the same."""
    return hmac.new(device_key.encode("utf-8"), proof_message(lid, fingerprint, ts, path, nonce, body_sha),
                    hashlib.sha256).hexdigest()


def check_proof(act: Optional[dict], lid: str, fingerprint: str, ts: Any, proof: Any, path: str,
                now: float | None = None, *, nonce: Any = "", body: Any = None, db: Any = None) -> bool:
    """True when the activation row has a secret and ts/nonce/proof are valid
    for this path and body, and (when ``db`` is given) the nonce is unused.
    The nonce is consumed only after the MAC verifies, so garbage cannot fill
    the cache."""
    if not act or not act.get("secret_hash") or not proof:
        return False
    try:
        ts_i = int(ts)
    except (TypeError, ValueError):
        return False
    nonce = nonce if isinstance(nonce, str) else ""
    if not (NONCE_MIN <= len(nonce) <= NONCE_MAX):
        return False
    t_now = now if now is not None else time.time()
    if abs(t_now - ts_i) > PROOF_WINDOW_S:
        return False
    want = compute_proof(act["secret_hash"], lid, fingerprint, ts_i, path, nonce, body_sha256(body))
    if not hmac.compare_digest(want, str(proof).lower()):
        return False
    if db is not None and not db.claim_nonce(lid, nonce, int(t_now)):
        return False
    return True


async def raw_json(request: Request) -> dict:
    """The request body as the client sent it (the proof covers these exact
    fields, so the pydantic model, which fills defaults, cannot be used)."""
    try:
        d = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return d if isinstance(d, dict) else {}


def issue_device_secret(db, lid: str, fingerprint: str) -> str:
    """New random device secret; only its hash is stored. Returned once."""
    secret = secrets.token_hex(32)
    db.set_activation_secret(lid, fingerprint, secret_hash(secret))
    return secret


def _proof_denied() -> HTTPException:
    return _err(403, "proof_invalid", "Device proof missing, stale or wrong. Re-activate this device.")


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def signer_of(app) -> Signer:
    return app.state.signer


# ------------------------------------------------------------------ issue / renew / revoke

def build_key(signer: Signer, lic: dict) -> str:
    payload = {
        "lid": lic["lid"], "email": lic["email"], "plan": lic["plan"], "seats": int(lic["seats"]),
        "features": sorted(set(lic["features"])), "issued": lic["issued"], "expires": lic["expires"],
    }
    return signer.sign_payload(payload)


def issue_license(app, account: dict, plan: str = "solo", *, seats: int | None = None, days: int | None = None,
                  features: list[str] | None = None, stripe_sub_id: str | None = None, actor: str = "admin") -> dict:
    if plan not in PLANS:
        raise ValueError(f"unknown plan {plan!r}")
    db: Database = app.state.db
    defaults = PLANS[plan]
    now = utcnow()
    lic = {
        "lid": new_id("lic_"), "account_id": account["id"], "email": account["email"], "plan": plan,
        "seats": int(seats or defaults["seats"]), "features": list(features or defaults["features"]),
        "issued": iso(now), "expires": iso(now + timedelta(days=int(days or defaults["days"]))),
    }
    key = build_key(signer_of(app), lic)
    row = db.insert_license(lic["lid"], account["id"], plan, lic["seats"], lic["features"], lic["issued"],
                            lic["expires"], key, stripe_sub_id=stripe_sub_id)
    db.audit("license.issue", actor=actor, target=lic["lid"],
             detail={"email": account["email"], "plan": plan, "seats": lic["seats"], "expires": lic["expires"]})
    row["email"] = account["email"]
    return row


def renew_license(app, lid: str, days: int, *, actor: str = "admin", plan: str | None = None,
                  seats: int | None = None) -> dict:
    """Extend expiry (from the later of now and current expiry) and re-sign the key."""
    db: Database = app.state.db
    lic = db.get_license(lid)
    if not lic:
        raise KeyError(lid)
    acct = db.get_account(lic["account_id"])
    base = max(utcnow(), parse_iso(lic["expires"]) or utcnow())
    new_expires = iso(base + timedelta(days=int(days)))
    lic.update({"expires": new_expires, "email": acct["email"], "revoked": False})
    if plan and plan in PLANS:
        lic["plan"] = plan
    if seats:
        lic["seats"] = int(seats)
    key = build_key(signer_of(app), lic)
    row = db.update_license(lid, expires=new_expires, key=key, revoked=0, plan=lic["plan"], seats=lic["seats"])
    db.audit("license.renew", actor=actor, target=lid, detail={"expires": new_expires, "days": days})
    return row  # type: ignore[return-value]


def set_license_expiry(app, lid: str, expires_ts: int, *, actor: str = "stripe") -> dict:
    """Set expiry to an absolute instant (a Stripe period end) and re-sign.
    Never moves expiry backwards, so a late or replayed invoice is harmless."""
    from datetime import datetime, timezone
    db: Database = app.state.db
    lic = db.get_license(lid)
    if not lic:
        raise KeyError(lid)
    acct = db.get_account(lic["account_id"])
    target = datetime.fromtimestamp(int(expires_ts), timezone.utc)
    current = parse_iso(lic["expires"])
    if current and current >= target:
        return lic
    new_expires = iso(target)
    lic.update({"expires": new_expires, "email": acct["email"], "revoked": False})
    key = build_key(signer_of(app), lic)
    row = db.update_license(lid, expires=new_expires, key=key, revoked=0)
    db.audit("license.renew", actor=actor, target=lid, detail={"expires": new_expires, "period_end": True})
    return row  # type: ignore[return-value]


def revoke_license(app, lid: str, *, actor: str = "admin", reason: str = "") -> Optional[dict]:
    db: Database = app.state.db
    lic = db.get_license(lid)
    if not lic:
        return None
    db.update_license(lid, revoked=1)
    n = db.revoke_all_activations(lid)
    db.audit("license.revoke", actor=actor, target=lid, detail={"reason": reason, "activations_revoked": n})
    return db.get_license(lid)


def license_state(lic: dict) -> str:
    if lic.get("revoked"):
        return "revoked"
    exp = parse_iso(lic.get("expires"))
    if exp and exp < utcnow():
        return "expired"
    return "active"


# ------------------------------------------------------------------ activation signing

def sign_activation(app, lic: dict, fingerprint: str) -> tuple[str, dict]:
    settings = app.state.settings
    now = utcnow()
    payload = {
        "lid": lic["lid"], "fingerprint": fingerprint, "issued": iso(now),
        "valid_until": iso(now + timedelta(days=settings.activation_days)),
        "plan": lic["plan"], "features": sorted(set(lic["features"])),
        # The DB row is authoritative for expiry; carrying it here is how a
        # renewal reaches a client that still holds the old key string.
        "expires": lic["expires"],
    }
    return signer_of(app).sign_payload(payload), payload


def _check_license_live(lic: Optional[dict]) -> dict:
    if not lic:
        raise _err(404, "unknown_license", "No such license.")
    state = license_state(lic)
    if state == "revoked":
        raise _err(403, "revoked", "This license has been revoked.")
    if state == "expired":
        raise _err(403, "expired", f"This license expired {lic['expires']}.")
    return lic


# ------------------------------------------------------------------ /v1 device API

class ActivateIn(BaseModel):
    key: str = Field(min_length=20)
    fingerprint: str = Field(min_length=16, max_length=128)
    app_version: str = ""
    ts: Optional[int] = None
    nonce: str = Field(default="", max_length=64)
    proof: str = Field(default="", max_length=128)   # only for a device that already holds a seat


class HeartbeatIn(BaseModel):
    lid: str
    fingerprint: str = Field(min_length=16, max_length=128)
    app_version: str = ""
    ts: Optional[int] = None
    nonce: str = Field(default="", max_length=64)
    proof: str = Field(default="", max_length=128)
    key: str = Field(default="", max_length=8192)   # legacy upgrade path only


class DeactivateIn(BaseModel):
    lid: str
    fingerprint: str = Field(min_length=16, max_length=128)
    ts: Optional[int] = None
    nonce: str = Field(default="", max_length=64)
    proof: str = Field(default="", max_length=128)
    key: str = Field(default="", max_length=8192)   # legacy (no secret yet) only


class ValidateIn(BaseModel):
    key: str


def _key_matches(app, key: str, lid: str) -> bool:
    if not key:
        return False
    try:
        return str(signer_of(app).verify(key).get("lid", "")) == lid
    except SigningError:
        return False


def _activation_response(app, lic: dict, fingerprint: str, app_version: str, claim: bool = False,
                         new_secret: bool = False) -> dict:
    token, payload = sign_activation(app, lic, fingerprint)
    db: Database = app.state.db
    if claim:
        ok, used = db.claim_seat(lic["lid"], fingerprint, int(lic["seats"]), app_version, payload["valid_until"])
        if not ok:
            db.audit("activate.seat_limit", actor=fingerprint[:12], target=lic["lid"],
                     detail={"seats": lic["seats"], "used": used})
            raise _err(409, "seat_limit",
                       f"All {lic['seats']} seat(s) on this license are in use. Deactivate a device in the portal first.")
    else:
        db.upsert_activation(lic["lid"], fingerprint, app_version, payload["valid_until"])
    out = {"ok": True, "activation": token, "payload": payload}
    if new_secret:
        out["device_secret"] = issue_device_secret(db, lic["lid"], fingerprint)
    out["license"] = {"lid": lic["lid"], "plan": lic["plan"], "seats": lic["seats"],
                      "features": lic["features"], "expires": lic["expires"]}
    return out


@router.post("/activate")
def v1_activate(body: ActivateIn, request: Request, raw: dict = Depends(raw_json)):
    app = request.app
    db: Database = app.state.db
    try:
        payload = signer_of(app).verify(body.key)
    except SigningError as exc:
        raise _err(400, "bad_key", f"License key rejected: {exc}")
    lic = _check_license_live(db.get_license(str(payload.get("lid", ""))))
    # A fingerprint that already holds a seat (with a device secret) is bound
    # to that device: re-activating it needs the device's own proof, or a
    # portal action that releases the seat first. Otherwise anyone holding the
    # key could rotate the secret of someone else's device.
    existing = db.get_activation(lic["lid"], body.fingerprint)
    keep_secret = False
    if existing and not existing["revoked"] and existing.get("secret_hash"):
        if not body.proof:
            db.audit("activate.device_bound", actor=body.fingerprint[:12], target=lic["lid"])
            raise _err(409, "device_bound",
                       "This device already holds a seat on that license. Release it in the portal, "
                       "then activate again.")
        if not check_proof(existing, lic["lid"], body.fingerprint, body.ts, body.proof, "/v1/activate",
                           nonce=body.nonce, body=raw, db=db):
            db.audit("activate.proof_invalid", actor=body.fingerprint[:12], target=lic["lid"])
            raise _proof_denied()
        keep_secret = True
    # The signed key carries its own expiry, but the DB row is authoritative
    # (renewals re-sign; an old key string for a renewed license still works).
    # The seat count and the insert happen in one transaction (claim_seat).
    out = _activation_response(app, lic, body.fingerprint, body.app_version, claim=True,
                               new_secret=not keep_secret)
    db.audit("activate", actor=body.fingerprint[:12], target=lic["lid"], detail={"app_version": body.app_version})
    return out


class TrialIn(BaseModel):
    email: str = Field(max_length=254)
    fingerprint: str = Field(min_length=16, max_length=128)
    app_version: str = ""


TRIAL_GENERIC = "If this address can start a trial, you will get an email."


def _trial_net(ip: str) -> str:
    """The /24 (IPv4) or /64 (IPv6) the caller sits in, as text."""
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip or "?"
    prefix = 24 if addr.version == 4 else 64
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


@router.post("/trial")
def v1_trial(body: TrialIn, request: Request):
    """In-app free trial: no portal visit. One trial per email, new accounts only.

    The response never carries the license key: it goes to the inbox, together
    with the email-confirmation link (which is what funds AI trial credits) and
    a set-password link. This device is bound on the spot (activation and
    device secret come back). An address that already has an account, or has
    had a trial, gets the same 409 so the endpoint is no account oracle."""
    import sqlite3
    from . import mailer
    from .auth import create_account, valid_email
    from .web import client_ip
    app = request.app
    db: Database = app.state.db
    settings = app.state.settings
    if not settings.trial_enabled:
        raise _err(404, "trial_disabled", "Free trials are not available.")
    email = body.email.lower().strip()
    if not valid_email(email):
        raise _err(400, "bad_email", "Enter a valid email address.")
    ip = client_ip(request)
    limiter = app.state.login_limiter
    if not (limiter.hit("trial-ip:" + ip) and limiter.hit("trial-email:" + email)):
        raise _err(429, "rate_limited", "Too many attempts. Try again in a few minutes.")
    net = _trial_net(ip)
    total, from_net = db.trial_counts_today(net)
    if (settings.ai_trial_per_day > 0 and total >= settings.ai_trial_per_day) or \
            (settings.trial_per_ip_day > 0 and from_net >= settings.trial_per_ip_day):
        db.audit("trial.budget", actor=body.fingerprint[:12], target=net, detail={"total": total, "net": from_net})
        raise _err(429, "trial_budget", "Free trials are paused for today. Try again tomorrow.")

    def refuse(aid: str = "") -> HTTPException:
        db.audit("trial.refused", actor=body.fingerprint[:12], target=aid, detail={"email": email})
        return _err(409, "trial_used", TRIAL_GENERIC)

    existing = db.get_account_by_email(email)
    if existing:
        raise refuse(existing["id"])
    try:
        acct, _unused_temp = create_account(app, email, actor="trial")
    except sqlite3.IntegrityError:
        raise refuse()
    try:
        lic = issue_license(app, acct, plan="trial", days=settings.trial_days, actor="trial")
        out = _activation_response(app, lic, body.fingerprint, body.app_version, claim=True, new_secret=True)
        vtoken = db.create_verification(acct["id"])
        rtoken = db.create_reset(acct["id"])
        mailer.send_trial(email, settings.product_name, lic["key"], settings.base_url,
                          verify_url=f"{settings.base_url}/verify/{vtoken}",
                          set_password_url=f"{settings.base_url}/reset/{rtoken}")
    except Exception:
        # Nothing reached the inbox, so nothing must stay bound: drop the
        # account (cascade) so the user can simply try again.
        log.exception("trial start failed; rolling back account %s", acct["id"])
        db.delete_account(acct["id"])
        raise _err(503, "trial_unavailable", "Could not start the trial right now. Try again in a few minutes.")
    # No AI credits here: the wallet is funded when the email is confirmed.
    db.trial_log_add(net, lic["lid"])
    db.audit("trial.start", actor=body.fingerprint[:12], target=lic["lid"], detail={"email": email})
    return out


@router.post("/heartbeat")
def v1_heartbeat(body: HeartbeatIn, request: Request, raw: dict = Depends(raw_json)):
    app = request.app
    db: Database = app.state.db
    lic = _check_license_live(db.get_license(body.lid))
    act = db.get_activation(body.lid, body.fingerprint)
    if not act or act["revoked"]:
        raise _err(404, "not_activated", "This device is not activated on that license.")
    if act.get("secret_hash"):
        if not check_proof(act, body.lid, body.fingerprint, body.ts, body.proof, "/v1/heartbeat",
                           nonce=body.nonce, body=raw, db=db):
            db.audit("heartbeat.proof_invalid", actor=body.fingerprint[:12], target=body.lid)
            raise _proof_denied()
        return _activation_response(app, lic, body.fingerprint, body.app_version)
    # Pre-1.3.0 activation: accept the license key once and upgrade.
    if not _key_matches(app, body.key, body.lid):
        raise _proof_denied()
    out = _activation_response(app, lic, body.fingerprint, body.app_version, new_secret=True)
    db.audit("activation.upgrade", actor=body.fingerprint[:12], target=body.lid)
    return out


@router.post("/deactivate")
def v1_deactivate(body: DeactivateIn, request: Request, raw: dict = Depends(raw_json)):
    db: Database = request.app.state.db
    act = db.get_activation(body.lid, body.fingerprint)
    if act:
        authed = check_proof(act, body.lid, body.fingerprint, body.ts, body.proof, "/v1/deactivate",
                             nonce=body.nonce, body=raw, db=db) \
            if act.get("secret_hash") else _key_matches(request.app, body.key, body.lid)
        if not authed:
            raise _proof_denied()
    ok = db.revoke_activation(body.lid, body.fingerprint)
    db.audit("deactivate", actor=body.fingerprint[:12], target=body.lid, detail={"found": ok})
    return {"ok": True, "released": ok}


@router.post("/validate")
def v1_validate(body: ValidateIn, request: Request):
    app = request.app
    try:
        payload = signer_of(app).verify(body.key)
    except SigningError as exc:
        return JSONResponse({"valid": False, "reason": str(exc)}, status_code=200)
    lic = app.state.db.get_license(str(payload.get("lid", "")))
    if not lic:
        return {"valid": False, "reason": "unknown_license", "lid": payload.get("lid")}
    state = license_state(lic)
    return {"valid": state == "active", "state": state, "lid": lic["lid"], "plan": lic["plan"],
            "seats": lic["seats"], "expires": lic["expires"], "features": lic["features"],
            "seats_used": len(app.state.db.active_activations(lic["lid"]))}


@router.get("/public-key")
def v1_public_key(request: Request):
    s = signer_of(request.app)
    return {"algorithm": "Ed25519", "key_id": s.key_id(), "public_key_pem": s.public_key_pem(),
            "token_format": "base64url(payload_json).base64url(signature)"}
