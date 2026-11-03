"""License issue / renew / revoke and the /v1 device API.

License key payload (signed, embedded in the key string):
    {"lid": "lic_...", "email": "...", "plan": "trial|solo|team", "seats": 1,
     "features": ["core", ...], "issued": "2026-10-05T12:00:00Z", "expires": "2027-10-05T12:00:00Z"}

Activation payload (signed, returned by /v1/activate and /v1/heartbeat):
    {"lid": "...", "fingerprint": "<sha256 hex>", "issued": "...", "valid_until": "<issued + 30d>",
     "plan": "...", "features": [...]}

Both are <base64url(payload_json)>.<base64url(sig)>; see keys.py.

Error bodies are always {"detail": {"code": "...", "message": "..."}} so the
app client can surface a code without parsing prose.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
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


class HeartbeatIn(BaseModel):
    lid: str
    fingerprint: str = Field(min_length=16, max_length=128)
    app_version: str = ""


class DeactivateIn(BaseModel):
    lid: str
    fingerprint: str = Field(min_length=16, max_length=128)


class ValidateIn(BaseModel):
    key: str


def _activation_response(app, lic: dict, fingerprint: str, app_version: str, claim: bool = False) -> dict:
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
    return {"ok": True, "activation": token, "payload": payload,
            "license": {"lid": lic["lid"], "plan": lic["plan"], "seats": lic["seats"],
                        "features": lic["features"], "expires": lic["expires"]}}


@router.post("/activate")
def v1_activate(body: ActivateIn, request: Request):
    app = request.app
    db: Database = app.state.db
    try:
        payload = signer_of(app).verify(body.key)
    except SigningError as exc:
        raise _err(400, "bad_key", f"License key rejected: {exc}")
    lic = _check_license_live(db.get_license(str(payload.get("lid", ""))))
    # The signed key carries its own expiry, but the DB row is authoritative
    # (renewals re-sign; an old key string for a renewed license still works).
    # The seat count and the insert happen in one transaction (claim_seat).
    out = _activation_response(app, lic, body.fingerprint, body.app_version, claim=True)
    db.audit("activate", actor=body.fingerprint[:12], target=lic["lid"], detail={"app_version": body.app_version})
    return out


@router.post("/heartbeat")
def v1_heartbeat(body: HeartbeatIn, request: Request):
    app = request.app
    db: Database = app.state.db
    lic = _check_license_live(db.get_license(body.lid))
    act = db.get_activation(body.lid, body.fingerprint)
    if not act or act["revoked"]:
        raise _err(404, "not_activated", "This device is not activated on that license.")
    return _activation_response(app, lic, body.fingerprint, body.app_version)


@router.post("/deactivate")
def v1_deactivate(body: DeactivateIn, request: Request):
    db: Database = request.app.state.db
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
