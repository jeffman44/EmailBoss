"""Email Boss AI relay: metered model calls charged against a per-license credit wallet.

Endpoints (device API, authenticated by the signed activation token AND
the device proof of possession, see licenses.py):
    POST /v1/ai/complete   forward one prompt to the vendor, charge the wallet
    POST /v1/ai/balance    wallet balance and the packs on sale
    POST /v1/ai/checkout   create a Stripe Checkout session for a credit pack

Abuse limits on /complete: `task` must be a known prompt id; input (system +
user + the JSON of json_schema) is capped per tier (413 input_too_large) and
json_schema alone at 8 KB (413 schema_too_large); a reserve (input estimate
at 1 token per non-ASCII char and 1 per 4 other chars, plus max_tokens of
output, at the tier's prices) is debited as a hold before the vendor call and
settled to the real cost after; vendor spend per license per UTC day, counting
holds still in flight, is capped (AI_DAILY_USD_PER_LID, 429 daily_cap).

Prompt and completion text are never stored or logged; the ledger holds
metadata only. Errors are {"ok": false, "error": "<message>", "code": "<code>"}.
"""
from __future__ import annotations

import json
import logging
import math
import socket
import threading
import time
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .keys import SigningError
from .licenses import _check_license_live, check_proof, raw_json, signer_of
from .models import parse_iso, utcnow
from .web import RateLimiter

log = logging.getLogger("licserver.ai")
router = APIRouter(prefix="/v1/ai", tags=["ai"])

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
TIMEOUT_S = 60
JSON_TOOL_NAME = "emit_result"
STRIPE_SESSIONS_URL = "https://api.stripe.com/v1/checkout/sessions"

# Task ids the relay serves: the app's prompt ids (app/plannerboard/ai/prompts/*.yaml)
# plus the provider self-test. Anything else is refused with 400.
ALLOWED_TASKS = frozenset({
    "assistant.board_pass", "digest.weekly", "followup.draft", "project.health", "reply.draft",
    "reply.no_reply_classifier", "search.parse", "triage.classify", "triage.match_rerank",
    "triage.suggest_project", "triage.summarize_bullets", "ai.test",
})

# Input cap (system + user + schema characters) per tier.
INPUT_CAP_CHARS = {"fast": 40_000, "smart": 120_000}
# A structured-output schema is a few hundred bytes in practice.
SCHEMA_CAP_CHARS = 8 * 1024

# USD per million tokens (input, output). Anthropic rows copied from the app's usage.PRICE_TABLE.
PRICE_TABLE: dict[str, tuple[float, float]] = {
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-3-7-sonnet": (3.0, 15.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.80, 4.0),
    "claude-3-haiku": (0.25, 1.25),
}


class AiError(Exception):
    def __init__(self, status: int, code: str, message: str, reason: str = ""):
        super().__init__(message)
        self.status, self.code, self.message, self.reason = status, code, message, reason


class VendorError(Exception):
    """Vendor call failed. ``kind`` is a short category safe to store (no vendor text)."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _fail(e: AiError) -> JSONResponse:
    body: dict[str, Any] = {"ok": False, "error": e.message, "code": e.code}
    if e.reason:
        body["reason"] = e.reason
    return JSONResponse(body, status_code=e.status)


# ------------------------------------------------------------------ pricing

def lookup_price(model: str) -> Optional[tuple[float, float]]:
    m = (model or "").lower().strip()
    best = None
    for prefix, price in PRICE_TABLE.items():
        if m.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, price)
    return best[1] if best else None


def vendor_cost_usd(model: str, in_tok: int, out_tok: int, fallback_model: str = "") -> float:
    price = lookup_price(model) or lookup_price(fallback_model) or (3.0, 15.0)
    return round((max(0, in_tok) * price[0] + max(0, out_tok) * price[1]) / 1_000_000.0, 6)


def credits_for(vendor_usd: float, markup: float, minimum: int) -> int:
    """ceil(vendor_usd * 100 * markup), never below the per-call minimum."""
    raw = round(float(vendor_usd) * 100.0 * float(markup), 6)  # round away float noise before ceil
    return max(int(minimum), int(math.ceil(raw)))


def tier_model(settings, tier: str) -> str:
    return settings.ai_model_smart if tier == "smart" else settings.ai_model_fast


def estimate_input_tokens(*texts: str) -> int:
    """1 token per non-ASCII character (CJK and friends), 1 per 4 other characters."""
    ascii_n = other = 0
    for t in texts:
        n = len(t or "")
        a = len((t or "").encode("ascii", "ignore"))
        ascii_n += a
        other += n - a
    return other + int(math.ceil(ascii_n / 4.0))


def reserve_usd(settings, max_tokens: int, input_chars: int = 0, tier: str = "smart",
                input_tokens: int | None = None) -> float:
    """Worst case for a call at the tier's prices: the input estimated at
    chars/4 tokens (or ``input_tokens`` when the caller measured it) plus
    max_tokens of output."""
    price = lookup_price(tier_model(settings, tier)) or (3.0, 15.0)
    in_tok = int(input_tokens) if input_tokens is not None else int(math.ceil(max(0, int(input_chars)) / 4.0))
    return (in_tok * price[0] + int(max_tokens) * price[1]) / 1_000_000.0


def reserve_credits(settings, max_tokens: int, input_chars: int = 0, tier: str = "smart",
                    input_tokens: int | None = None) -> int:
    return credits_for(reserve_usd(settings, max_tokens, input_chars, tier, input_tokens), settings.ai_markup,
                       settings.ai_min_credits)


class InFlight:
    """USD held by calls that are past the cap check but not yet in the ledger,
    per license. Makes the daily cap a hard bound under concurrency (within
    one process)."""

    def __init__(self):
        self._usd: dict[str, float] = {}
        self._lock = threading.Lock()

    def try_add(self, lid: str, usd: float, spent_today: float, cap: float) -> bool:
        with self._lock:
            held = self._usd.get(lid, 0.0)
            if cap > 0 and spent_today + held + usd > cap:
                return False
            self._usd[lid] = held + usd
            return True

    def release(self, lid: str, usd: float) -> None:
        with self._lock:
            left = self._usd.get(lid, 0.0) - usd
            if left > 1e-9:
                self._usd[lid] = left
            else:
                self._usd.pop(lid, None)


# ------------------------------------------------------------------ vendor

Transport = Callable[[str, dict, bytes, int], tuple[int, str]]


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: int) -> tuple[int, str]:
    req = UrlRequest(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""


def _ensure_object_schema(schema: dict) -> tuple[dict, bool]:
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema, False
    return {"type": "object", "properties": {"result": schema}, "required": ["result"]}, True


def vendor_call(model: str, system: str, user: str, max_tokens: int, temperature: float,
                json_schema: dict | None, *, api_key: str, transport: Transport | None = None,
                timeout: int = TIMEOUT_S) -> dict:
    """One Anthropic Messages call (same shaping as the app's anthropic_provider).
    Returns {text, parsed, input_tokens, output_tokens, model}. Raises VendorError.
    ``transport(url, headers, body_bytes, timeout) -> (status, text)`` is injectable."""
    body: dict = {
        "model": model,
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "messages": [{"role": "user", "content": user}],
    }
    if system:
        body["system"] = system
    wrapped = False
    if json_schema:
        tool_schema, wrapped = _ensure_object_schema(json_schema)
        body["tools"] = [{
            "name": JSON_TOOL_NAME,
            "description": "Return the result as structured data matching the schema.",
            "input_schema": tool_schema,
        }]
        body["tool_choice"] = {"type": "tool", "name": JSON_TOOL_NAME}
    headers = {"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": API_VERSION}
    send = transport or _urllib_transport
    try:
        status, raw = send(API_URL, headers, json.dumps(body).encode("utf-8"), timeout)
    except (socket.timeout, TimeoutError):
        raise VendorError("timeout")
    except (URLError, OSError):
        raise VendorError("network")
    if status != 200:
        raise VendorError(f"http_{status}")
    try:
        data = json.loads(raw)
    except ValueError:
        raise VendorError("parse")
    text_parts: list[str] = []
    parsed = None
    for block in data.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use" and block.get("name") == JSON_TOOL_NAME:
            parsed = block.get("input")
            if wrapped and isinstance(parsed, dict) and "result" in parsed:
                parsed = parsed["result"]
    text = "\n".join(p for p in text_parts if p).strip()
    if parsed is not None and not text:
        text = json.dumps(parsed)
    usage = data.get("usage") or {}
    return {"text": text, "parsed": parsed, "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0), "model": data.get("model") or model}


# ------------------------------------------------------------------ auth

def authenticate(app, lid: str, fingerprint: str, activation: str, ts=None, proof: str = "",
                 path: str = "", nonce: str = "", raw: dict | None = None) -> dict:
    """Verify the signed activation token, that the license is live, and the
    device proof (HMAC over lid|fingerprint|ts|path|nonce|body_sha256 with the
    device key; ``raw`` is the request body dict, the nonce is single use).
    Returns the license row; raises AiError(403, ...) otherwise."""
    def deny(reason: str, message: str) -> AiError:
        return AiError(403, "license", message, reason)

    try:
        payload = signer_of(app).verify(activation)
    except SigningError:
        raise deny("bad_signature", "Activation token rejected.")
    if payload.get("lid") != lid:
        raise deny("lid_mismatch", "Activation does not belong to this license.")
    if payload.get("fingerprint") != fingerprint:
        raise deny("fingerprint_mismatch", "Activation does not belong to this device.")
    until = parse_iso(payload.get("valid_until"))
    if not until or until < utcnow():
        raise deny("activation_expired", "Activation expired; connect to refresh it.")
    db = app.state.db
    try:
        lic = _check_license_live(db.get_license(lid))
    except HTTPException as exc:
        d = exc.detail if isinstance(exc.detail, dict) else {}
        raise deny(str(d.get("code") or "license"), str(d.get("message") or "License is not active."))
    act = db.get_activation(lid, fingerprint)
    if not act or act["revoked"]:
        raise deny("not_activated", "This device is not activated on that license.")
    if not check_proof(act, lid, fingerprint, ts, proof, path, nonce=nonce, body=raw, db=db):
        raise AiError(403, "proof_invalid", "Device proof missing, stale or wrong. Refresh the license.",
                      "proof_invalid")
    return lic


def _packs(settings) -> list[dict]:
    out = []
    for pid, p in settings.stripe_credit_packs.items():
        out.append({"id": pid, "label": f"{p['credits']} credits (${p['usd_cents'] / 100:.2f})",
                    "credits": p["credits"], "usd_cents": p["usd_cents"]})
    return out


# ------------------------------------------------------------------ models

class CompleteIn(BaseModel):
    lid: str = Field(min_length=1, max_length=64)
    fingerprint: str = Field(min_length=16, max_length=128)
    activation: str = Field(min_length=20, max_length=8192)
    ts: Optional[int] = None
    nonce: str = Field(default="", max_length=64)
    proof: str = Field(default="", max_length=128)
    task: str = Field(default="", max_length=64)
    tier: str = "fast"
    system: str = Field(default="", max_length=200_000)
    user: str = Field(min_length=1, max_length=400_000)
    max_tokens: int = 800
    temperature: float = 0.2
    json_schema: Optional[dict] = None


class BalanceIn(BaseModel):
    lid: str = Field(min_length=1, max_length=64)
    fingerprint: str = Field(min_length=16, max_length=128)
    activation: str = Field(min_length=20, max_length=8192)
    ts: Optional[int] = None
    nonce: str = Field(default="", max_length=64)
    proof: str = Field(default="", max_length=128)


class CheckoutIn(BaseModel):
    lid: str = Field(min_length=1, max_length=64)
    fingerprint: str = Field(min_length=16, max_length=128)
    activation: str = Field(min_length=20, max_length=8192)
    ts: Optional[int] = None
    nonce: str = Field(default="", max_length=64)
    proof: str = Field(default="", max_length=128)
    pack: str = Field(min_length=1, max_length=128)


# ------------------------------------------------------------------ complete

def _inflight(app) -> InFlight:
    inf = getattr(app.state, "ai_inflight", None)
    if inf is None:
        inf = app.state.ai_inflight = InFlight()
    return inf


def _limiter(app) -> RateLimiter:
    lim = getattr(app.state, "ai_limiter", None)
    if lim is None:
        lim = RateLimiter(max(1, app.state.settings.ai_rate_per_min), 60)
        app.state.ai_limiter = lim
    return lim


@router.post("/complete")
def ai_complete(body: CompleteIn, request: Request, raw: dict = Depends(raw_json)):
    app = request.app
    settings, db = app.state.settings, app.state.db
    held_usd, inflight = 0.0, _inflight(app)
    try:
        authenticate(app, body.lid, body.fingerprint, body.activation, body.ts, body.proof, "/v1/ai/complete",
                     body.nonce, raw)
        if body.tier not in ("fast", "smart"):
            raise AiError(400, "bad_request", "tier must be 'fast' or 'smart'.")
        if body.task not in ALLOWED_TASKS:
            raise AiError(400, "bad_task", "Unknown task.")
        schema_text = json.dumps(body.json_schema) if body.json_schema else ""
        if len(schema_text) > SCHEMA_CAP_CHARS:
            raise AiError(413, "schema_too_large", f"json_schema is too large ({SCHEMA_CAP_CHARS} characters max).")
        input_chars = len(body.system or "") + len(body.user or "") + len(schema_text)
        if input_chars > INPUT_CAP_CHARS[body.tier]:
            raise AiError(413, "input_too_large",
                          f"Input is too long for the {body.tier} tier ({INPUT_CAP_CHARS[body.tier]} characters max).")
        if not _limiter(app).hit(body.lid):
            raise AiError(429, "rate_limited", "Too many AI requests; slow down.")
        if not settings.anthropic_api_key:
            raise AiError(503, "relay_disabled", "The AI relay is not configured on this server.")
        max_tokens = max(1, min(int(body.max_tokens), settings.ai_max_tokens))
        temperature = min(1.0, max(0.0, float(body.temperature)))
        model = tier_model(settings, body.tier)
        in_tok = estimate_input_tokens(body.system, body.user, schema_text)
        cap = float(getattr(settings, "ai_daily_usd_per_lid", 5.0) or 0.0)
        want_usd = reserve_usd(settings, max_tokens, input_chars, body.tier, in_tok)
        if not inflight.try_add(body.lid, want_usd, db.vendor_usd_today(body.lid), cap):
            raise AiError(429, "daily_cap", "Daily AI limit for this license reached. It resets at midnight UTC.")
        held_usd = want_usd
        wallet = db.ensure_wallet(body.lid, settings.ai_trial_credits)
        hold = reserve_credits(settings, max_tokens, input_chars, body.tier, in_tok)
        # The hold is debited BEFORE the vendor call, so concurrent calls can
        # never spend credits the wallet does not have.
        if db.debit(body.lid, hold) is None:
            raise AiError(402, "no_credits", f"Not enough AI credits (need {hold}, have {wallet['balance']}).")
    except AiError as e:
        if held_usd:
            inflight.release(body.lid, held_usd)
        return _fail(e)
    try:
        return _complete_vendor(app, body, settings, db, model, max_tokens, temperature, hold)
    finally:
        inflight.release(body.lid, held_usd)


def _complete_vendor(app, body: "CompleteIn", settings, db, model: str, max_tokens: int, temperature: float,
                     hold: int):
    transport = getattr(app.state, "ai_transport", None)
    t0 = time.monotonic()
    try:
        res = vendor_call(model, body.system, body.user, max_tokens, temperature, body.json_schema,
                          api_key=settings.anthropic_api_key, transport=transport)
    except VendorError as ve:
        db.refund(body.lid, hold)
        db.ledger_insert(body.lid, body.fingerprint, body.task, body.tier, model, 0, 0, 0.0, 0, False, ve.kind)
        log.warning("vendor call failed lid=%s kind=%s", body.lid, ve.kind)
        return _fail(AiError(502, "vendor", "The AI vendor call failed."))
    latency = int((time.monotonic() - t0) * 1000)

    usd = vendor_cost_usd(res["model"], res["input_tokens"], res["output_tokens"], fallback_model=model)
    want = credits_for(usd, settings.ai_markup, settings.ai_min_credits)
    # Settle the hold: refund the unused part, or take the overrun from what
    # is left (the input estimate can undershoot; never go negative).
    if want <= hold:
        charged, balance = want, db.refund(body.lid, hold - want)
    else:
        extra, balance = db.debit_upto(body.lid, want - hold)
        charged = hold + extra
    db.ledger_insert(body.lid, body.fingerprint, body.task, body.tier, res["model"], res["input_tokens"],
                     res["output_tokens"], usd, charged, True, "" if charged == want else "short_charge")
    return {"ok": True, "text": res["text"], "parsed": res["parsed"], "input_tokens": res["input_tokens"],
            "output_tokens": res["output_tokens"], "credits_charged": charged, "balance": balance,
            "model_tier": body.tier, "latency_ms": latency}


# ------------------------------------------------------------------ balance

@router.post("/balance")
def ai_balance(body: BalanceIn, request: Request, raw: dict = Depends(raw_json)):
    app = request.app
    settings, db = app.state.settings, app.state.db
    lid = body.lid
    try:
        lic = authenticate(app, lid, body.fingerprint, body.activation, body.ts, body.proof, "/v1/ai/balance",
                           body.nonce, raw)
    except AiError as e:
        return _fail(e)
    wallet = db.ensure_wallet(lid, settings.ai_trial_credits)
    acct = db.get_account(lic["account_id"]) or {}
    # An unconfirmed address has no trial credits yet; the app can say why.
    pending = not acct.get("verified") and not acct.get("ai_trial_granted")
    return {"ok": True, "balance": wallet["balance"], "trial_granted": wallet["trial_granted"],
            "pending_verification": bool(pending),
            "packs": _packs(settings), "checkout_url_base": f"{settings.base_url}/v1/ai/checkout"}


# ------------------------------------------------------------------ checkout

def _urllib_stripe_transport(url: str, form: dict, secret: str) -> dict:
    req = UrlRequest(url, data=urlencode(form).encode("utf-8"), method="POST", headers={
        "Authorization": f"Bearer {secret}", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        raise AiError(502, "stripe", f"Stripe refused the checkout request ({e.code}).")
    except (URLError, OSError, ValueError):
        raise AiError(502, "stripe", "Could not reach Stripe.")


def create_checkout_session(app, lid: str, pack_id: str) -> dict:
    """Create a Stripe Checkout session (mode=payment) for a credit pack. Raises AiError."""
    settings = app.state.settings
    pack = settings.stripe_credit_packs.get(pack_id)
    if not pack:
        raise AiError(400, "bad_pack", "Unknown credit pack.")
    if not settings.stripe_secret_key:
        raise AiError(503, "billing_unconfigured", "Credit purchases are not configured on this server.")
    form = {
        "mode": "payment",
        "line_items[0][price]": pack_id,
        "line_items[0][quantity]": "1",
        "client_reference_id": lid,
        "success_url": f"{settings.base_url}/dashboard?notice=Payment+received.+Credits+arrive+in+a+moment.",
        "cancel_url": f"{settings.base_url}/dashboard",
        "metadata[kind]": "ai_credits",
        "metadata[lid]": lid,
        "metadata[credits]": str(pack["credits"]),
    }
    send = getattr(app.state, "stripe_transport", None) or _urllib_stripe_transport
    data = send(STRIPE_SESSIONS_URL, form, settings.stripe_secret_key)
    url = data.get("url") if isinstance(data, dict) else None
    if not url:
        raise AiError(502, "stripe", "Stripe returned no checkout URL.")
    app.state.db.audit("ai.checkout", actor=lid, target=lid,
                       detail={"pack": pack_id, "credits": pack["credits"], "session": data.get("id", "")})
    return {"url": url, "session_id": data.get("id", "")}


@router.post("/checkout")
def ai_checkout(body: CheckoutIn, request: Request, raw: dict = Depends(raw_json)):
    app = request.app
    try:
        authenticate(app, body.lid, body.fingerprint, body.activation, body.ts, body.proof, "/v1/ai/checkout",
                     body.nonce, raw)
        out = create_checkout_session(app, body.lid, body.pack)
    except AiError as e:
        return _fail(e)
    return {"ok": True, "url": out["url"], "session_id": out["session_id"]}
