"""Built-in Email Boss AI provider against a scripted relay."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from plannerboard.ai.emailboss_provider import (ERR_CREDITS, TIER_FAST, TIER_SMART,  # noqa: E402
                                                EmailBossProvider, tier_for)
from plannerboard.ai.factory import PROVIDER_CATALOG, make_provider  # noqa: E402
from plannerboard.ai.base import NullProvider  # noqa: E402
from tests.test_ai_providers import Recorder, _Resp  # noqa: E402

CREDS = {"lid": "L-1", "fingerprint": "f" * 32, "activation": "tok.sig", "server_url": "https://lic.test", "status": "ok"}


def _http_error(code, payload):
    import io
    return HTTPError("https://lic.test/v1/ai/complete", code, "x", {}, io.BytesIO(json.dumps(payload).encode()))


def test_catalog_has_builtin_first_and_default_config_uses_it():
    assert PROVIDER_CATALOG[0]["id"] == "emailboss" and PROVIDER_CATALOG[0]["needs_key"] is False
    from plannerboard.config import DEFAULTS
    assert DEFAULTS["ai"]["provider"] == "emailboss"


def test_factory_without_license_is_null():
    assert isinstance(make_provider({"ai": {"provider": "emailboss"}}), NullProvider)
    p = make_provider({"ai": {"provider": "emailboss"}}, license_creds=lambda: CREDS)
    assert isinstance(p, EmailBossProvider) and p.available()


def test_not_available_without_activation_or_bad_status():
    assert not EmailBossProvider(lambda: {**CREDS, "activation": ""}).available()
    assert not EmailBossProvider(lambda: {**CREDS, "status": "expired"}).available()
    assert EmailBossProvider(lambda: {**CREDS, "status": "grace"}).available()


def test_complete_sends_auth_and_tier_and_reads_credits():
    rec = Recorder([_Resp({"ok": True, "text": "hi", "parsed": {"a": 1}, "input_tokens": 10, "output_tokens": 5,
                           "credits_charged": 3, "balance": 297, "model_tier": "fast", "latency_ms": 80})])
    p = EmailBossProvider(lambda: CREDS, opener=rec)
    res = p.complete("sys", "usr", max_tokens=100, json_schema={"type": "object"}, task="triage.classify")
    assert res.ok and res.parsed == {"a": 1} and res.cost_usd == 0.03 and res.model == "emailboss/fast"
    req, _ = rec.requests[0]
    body = json.loads(req.data)
    assert req.full_url == "https://lic.test/v1/ai/complete"
    assert body["lid"] == "L-1" and body["activation"] == "tok.sig" and body["fingerprint"] == CREDS["fingerprint"]
    assert body["tier"] == TIER_FAST and body["json_schema"] == {"type": "object"} and body["task"] == "triage.classify"
    assert "x-api-key" not in {k.lower() for k in req.headers}
    assert p.last_balance == 297


def test_tiers():
    assert tier_for("triage.classify") == TIER_FAST
    assert tier_for("reply.draft") == TIER_SMART
    assert tier_for("") == TIER_SMART


def test_out_of_credits_maps_to_credits_error():
    rec = Recorder([_http_error(402, {"error": "no_credits", "balance": 0})])
    p = EmailBossProvider(lambda: CREDS, opener=rec)
    res = p.complete("s", "u")
    assert not res.ok and res.error.startswith(ERR_CREDITS) and p.last_balance == 0


def test_license_and_relay_off_errors():
    rec = Recorder([_http_error(403, {"message": "revoked"}), _http_error(503, {"error": "relay_disabled"})])
    p = EmailBossProvider(lambda: CREDS, opener=rec)
    assert p.complete("s", "u").error.startswith("license")
    assert p.complete("s", "u").error.startswith("relay_off")


def test_balance_and_checkout():
    rec = Recorder([_Resp({"balance": 120, "packs": [{"id": "p1", "credits": 500, "usd_cents": 500}]}),
                    _Resp({"url": "https://checkout.stripe.test/s"})])
    p = EmailBossProvider(lambda: CREDS, opener=rec)
    assert p.balance()["balance"] == 120 and p.last_balance == 120
    url, err = p.checkout_url("p1")
    assert url.startswith("https://checkout") and err == ""
    assert json.loads(rec.requests[1][0].data)["pack"] == "p1"


def test_llm_facade_marks_credits_exhausted(tmp_path):
    from plannerboard.ai.registry import LLM
    rec = Recorder([_http_error(402, {"error": "no_credits", "balance": 0})])
    llm = LLM(EmailBossProvider(lambda: CREDS, opener=rec), None, {"ai": {"provider": "emailboss",
              "features": {"classify": True}}}, {})
    assert llm.run("triage.classify", {"subject": "x", "body": "y", "from_name": "a", "from_email": "a@b.c"}) is None
    assert llm.credits_exhausted is True and llm.status()["credits_exhausted"] is True


def test_proof_of_possession_is_sent_when_the_client_signs():
    rec = Recorder([_Resp({"ok": True, "text": "x", "credits_charged": 1, "balance": 9})])
    seen = []
    def sign(path):
        seen.append(path)
        return {"ts": 1700000000, "proof": "abc"}
    p = EmailBossProvider(lambda: {**CREDS, "sign": sign}, opener=rec)
    assert p.complete("s", "u").ok
    body = json.loads(rec.requests[0][0].data)
    assert body["proof"] == "abc" and body["ts"] == 1700000000 and seen == ["/v1/ai/complete"]
