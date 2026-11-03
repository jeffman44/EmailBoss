"""Stripe webhook stub.

POST /webhooks/stripe
    Verifies the `Stripe-Signature` header (t=...,v1=... HMAC-SHA256 over
    "<t>.<raw body>"). With STRIPE_WEBHOOK_SECRET unset the endpoint answers
    503 (fail closed); LIC_ALLOW_UNSIGNED_STRIPE=1 accepts unsigned events for
    local development only. Every event id is recorded; a replay is
    acknowledged and ignored.

Events handled:
    checkout.session.completed      -> find/create account by customer email,
                                       issue a license (plan from STRIPE_PLAN_MAP by
                                       price id, metadata.plan, or "solo"), email the key.
    customer.subscription.deleted   -> revoke the license tied to that subscription id.
    invoice.paid                    -> renew the subscription's license to the
                                       invoice period end (fallback: +365 / +31
                                       days). The first invoice of a new
                                       subscription (billing_reason
                                       subscription_create) is skipped: the
                                       checkout event already issued it.
Everything else is acknowledged and logged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request

from . import mailer
from .auth import create_account
from .licenses import issue_license, renew_license, revoke_license

log = logging.getLogger("licserver.billing")
router = APIRouter()

TOLERANCE_SECONDS = 300


def verify_stripe_signature(payload: bytes, header: str, secret: str, now: float | None = None) -> bool:
    """Implements Stripe's v1 scheme without the SDK."""
    if not header or not secret:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts = parts.get("t")
    sigs = [v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1"]
    if not ts or not sigs:
        return False
    try:
        if abs((now or time.time()) - int(ts)) > TOLERANCE_SECONDS:
            return False
    except ValueError:
        return False
    expected = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def sign_for_test(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _plan_for(session: dict, plan_map: dict) -> tuple[str, int | None]:
    meta = session.get("metadata") or {}
    plan = meta.get("plan")
    seats = int(meta["seats"]) if str(meta.get("seats", "")).isdigit() else None
    for item in (session.get("line_items") or {}).get("data", []) or []:
        price = (item.get("price") or {}).get("id")
        if price in plan_map:
            p, s = plan_map[price]
            return p, seats or s
    return (plan if plan in ("trial", "solo", "team") else "solo"), seats


def _handle_ai_credits(app, session: dict) -> dict:
    """A paid credit-pack session: credit the wallet once per Stripe session id."""
    db, settings = app.state.db, app.state.settings
    meta = session.get("metadata") or {}
    lid, sid = str(meta.get("lid") or ""), str(session.get("id") or "")
    credits = int(meta["credits"]) if str(meta.get("credits", "")).isdigit() else 0
    if not lid or not sid or credits <= 0 or not db.get_license(lid):
        db.audit("ai.purchase_rejected", actor="stripe", target=lid, detail={"session": sid})
        return {"ok": False, "ignored": "bad ai_credits session"}
    if session.get("payment_status") not in (None, "paid", "no_payment_required"):
        return {"ok": True, "ignored": "not paid"}
    applied = db.record_purchase(lid, sid, credits, int(session.get("amount_total") or 0),
                                 trial_credits=settings.ai_trial_credits)
    if not applied:
        return {"ok": True, "ignored": "duplicate session"}
    return {"ok": True, "ai_credits": credits, "lid": lid}


def handle_event(app, event: dict) -> dict:
    """Claim the event id (so a concurrent duplicate is ignored), process it,
    and RELEASE the claim if processing raises, so Stripe's retry runs again
    instead of being swallowed as a duplicate (a mailer outage must not lose a
    license). Processing is idempotent per subscription / session id."""
    db = app.state.db
    etype = event.get("type", "")
    eid = str(event.get("id") or "")
    db.audit("stripe.event", actor="stripe", target=eid, detail={"type": etype})
    if not db.claim_webhook_event(eid, etype):
        return {"ok": True, "ignored": "duplicate event"}
    try:
        return _process_event(app, event, etype)
    except Exception:
        db.release_webhook_event(eid)
        log.exception("stripe event %s (%s) failed; claim released for retry", eid, etype)
        raise


def _process_event(app, event: dict, etype: str) -> dict:
    db, settings = app.state.db, app.state.settings
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "checkout.session.completed" and (obj.get("metadata") or {}).get("kind") == "ai_credits":
        return _handle_ai_credits(app, obj)

    if etype == "checkout.session.completed":
        email = ((obj.get("customer_details") or {}).get("email") or obj.get("customer_email") or "").lower().strip()
        if not email:
            return {"ok": False, "ignored": "no email on session"}
        acct = db.get_account_by_email(email)
        temp = None
        if not acct:
            acct, temp = create_account(app, email, actor="stripe")
        plan, seats = _plan_for(obj, settings.stripe_plan_map)
        sub_id = obj.get("subscription") if isinstance(obj.get("subscription"), str) else None
        # Idempotent across a retried event: reuse the license this subscription
        # (or, for a one-time payment, this checkout session) already got.
        ref = sub_id or (str(obj.get("id")) if obj.get("id") else None)
        lic = db.get_license_by_stripe_sub(ref) if ref else None
        if not lic:
            lic = issue_license(app, acct, plan, seats=seats, stripe_sub_id=ref, actor="stripe")
        if temp:
            mailer.send_welcome(email, settings.product_name, settings.base_url, temp_password=temp)
        mailer.send_license_key(email, settings.product_name, plan, lic["key"], settings.base_url)
        return {"ok": True, "issued": lic["lid"], "plan": plan}

    if etype == "customer.subscription.deleted":
        lic = db.get_license_by_stripe_sub(obj.get("id", ""))
        if not lic:
            return {"ok": True, "ignored": "no license for subscription"}
        revoke_license(app, lic["lid"], actor="stripe", reason="subscription deleted")
        return {"ok": True, "revoked": lic["lid"]}

    if etype == "invoice.paid":
        sub_id = obj.get("subscription") if isinstance(obj.get("subscription"), str) else ""
        lic = db.get_license_by_stripe_sub(sub_id) if sub_id else None
        if not lic:
            return {"ok": True, "ignored": "no license for invoice"}
        if obj.get("billing_reason") == "subscription_create":
            return {"ok": True, "ignored": "first invoice; issued at checkout"}
        interval, period_end = "", None
        for line in (obj.get("lines") or {}).get("data", []) or []:
            interval = ((line.get("price") or {}).get("recurring") or {}).get("interval") or interval
            pe = (line.get("period") or {}).get("end")
            if isinstance(pe, (int, float)):
                period_end = max(period_end or 0, int(pe))
        if period_end:
            from .licenses import set_license_expiry
            set_license_expiry(app, lic["lid"], period_end, actor="stripe")
            return {"ok": True, "renewed": lic["lid"], "expires_ts": period_end}
        days = 31 if interval == "month" else 365
        renew_license(app, lic["lid"], days, actor="stripe")
        return {"ok": True, "renewed": lic["lid"], "days": days}

    return {"ok": True, "ignored": etype}


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    raw = await request.body()
    settings = request.app.state.settings
    if settings.stripe_webhook_secret:
        if not verify_stripe_signature(raw, request.headers.get("stripe-signature", ""), settings.stripe_webhook_secret):
            raise HTTPException(status_code=400, detail={"code": "bad_signature", "message": "Stripe signature failed"})
    elif settings.allow_unsigned_stripe:
        log.warning("LIC_ALLOW_UNSIGNED_STRIPE=1; accepting unsigned webhook (development only)")
    else:
        # Fail closed: an unsigned webhook endpoint would let anyone mint licenses.
        raise HTTPException(status_code=503, detail={"code": "webhook_unconfigured",
                                                     "message": "STRIPE_WEBHOOK_SECRET is not set"})
    try:
        event = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "bad_json", "message": "body is not JSON"})
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail={"code": "bad_json", "message": "event must be an object"})
    return handle_event(request.app, event)
