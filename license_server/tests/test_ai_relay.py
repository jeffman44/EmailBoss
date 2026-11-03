"""AI relay: activation auth, wallet, charge math, rate limit, checkout, webhook credits."""
import json
import re

import pytest

from licserver import ai_relay
from licserver.ai_relay import credits_for, reserve_credits, vendor_call, VendorError
from licserver.auth import hash_password
from licserver.billing import sign_for_test
from licserver.licenses import issue_license, revoke_license, sign_activation

FP = "a" * 64
PROMPT = "SECRET-PROMPT-TEXT-12345"
COMPLETION = "SECRET-COMPLETION-TEXT-67890"


class FakeVendor:
    def __init__(self, in_tok=1000, out_tok=200, model="claude-sonnet-4-5", status=200, content=None):
        self.calls, self.status = [], status
        self.in_tok, self.out_tok, self.model = in_tok, out_tok, model
        self.content = content or [{"type": "text", "text": COMPLETION}]

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(body)))
        if self.status != 200:
            return self.status, '{"error":{"message":"%s"}}' % PROMPT
        return 200, json.dumps({"model": self.model, "content": self.content,
                                "usage": {"input_tokens": self.in_tok, "output_tokens": self.out_tok}})


@pytest.fixture
def ai(app):
    s = app.state.settings
    s.anthropic_api_key = "sk-test"
    s.stripe_secret_key = "sk_stripe"
    s.stripe_credit_packs = {"price_small": {"credits": 500, "usd_cents": 500},
                             "price_big": {"credits": 3000, "usd_cents": 2500}}
    app.state.ai_transport = FakeVendor()
    return app


@pytest.fixture
def lic(ai):
    db = ai.state.db
    acct = db.create_account("ai@example.test", hash_password("hunter2hunter2"))
    row = issue_license(ai, acct, "solo", seats=2)
    db.upsert_activation(row["lid"], FP, "1.0", "2099-01-01T00:00:00Z")
    return row


def act_token(app, lic, fp=FP):
    return sign_activation(app, lic, fp)[0]


def body(app, lic, **kw):
    d = {"lid": lic["lid"], "fingerprint": FP, "activation": act_token(app, lic), "task": "triage",
         "tier": "smart", "system": "sys", "user": PROMPT, "max_tokens": 500, "temperature": 0.2}
    d.update(kw)
    return d


def test_happy_path_and_trial_granted_once(client, ai, lic):
    r = client.post("/v1/ai/complete", json=body(ai, lic))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["text"] == COMPLETION and j["input_tokens"] == 1000 and j["output_tokens"] == 200
    # sonnet: 1000*3 + 200*15 = 6000/1e6 = 0.006 usd -> 0.6c * 4 = 2.4 -> 3 credits
    assert j["credits_charged"] == 3 and j["balance"] == 297 and j["model_tier"] == "smart"
    sent = ai.state.ai_transport.calls[0][2]
    assert sent["model"] == "claude-sonnet-4-5" and sent["messages"][0]["content"] == PROMPT
    # second call does not grant the trial again
    r = client.post("/v1/ai/complete", json=body(ai, lic))
    assert r.json()["balance"] == 294
    assert ai.state.db.get_wallet(lic["lid"])["trial_granted"] is True
    assert ai.state.db.ensure_wallet(lic["lid"], 300)["balance"] == 294


def test_fast_tier_minimum_charge(client, ai, lic):
    ai.state.ai_transport = FakeVendor(in_tok=100, out_tok=20, model="claude-haiku-4-5")
    j = client.post("/v1/ai/complete", json=body(ai, lic, tier="fast")).json()
    assert j["credits_charged"] == 1
    assert ai.state.ai_transport.calls[0][2]["model"] == "claude-haiku-4-5"


def test_charge_math():
    assert credits_for(0.004, 4.0, 1) == 2
    assert credits_for(0.0, 4.0, 1) == 1
    assert credits_for(0.0001, 4.0, 3) == 3
    assert credits_for(0.0025, 4.0, 1) == 1   # exactly 1.0, float noise must not round up
    assert credits_for(0.01, 2.0, 1) == 2


def test_json_schema_tool_use(client, ai, lic):
    ai.state.ai_transport = FakeVendor(content=[{"type": "tool_use", "name": "emit_result", "input": {"result": [1, 2]}}])
    j = client.post("/v1/ai/complete", json=body(ai, lic, json_schema={"type": "array"})).json()
    assert j["parsed"] == [1, 2]
    sent = ai.state.ai_transport.calls[0][2]
    assert sent["tool_choice"] == {"type": "tool", "name": "emit_result"}
    assert sent["tools"][0]["input_schema"]["properties"]["result"] == {"type": "array"}


@pytest.mark.parametrize("case", ["bad_signature", "wrong_fingerprint", "wrong_lid", "expired", "revoked", "released"])
def test_auth_rejections(client, ai, lic, case):
    b = body(ai, lic)
    if case == "bad_signature":
        p, s = b["activation"].split(".")
        b["activation"] = p + "." + ("A" if s[0] != "A" else "B") + s[1:]
    elif case == "wrong_fingerprint":
        b["fingerprint"] = "b" * 64
    elif case == "wrong_lid":
        other = issue_license(ai, ai.state.db.get_account_by_email("ai@example.test"), "solo")
        b["lid"] = other["lid"]
    elif case == "expired":
        b["activation"] = ai.state.signer.sign_payload(
            {"lid": lic["lid"], "fingerprint": FP, "issued": "2020-01-01T00:00:00Z", "valid_until": "2020-02-01T00:00:00Z"})
    elif case == "revoked":
        revoke_license(ai, lic["lid"])
    elif case == "released":
        ai.state.db.revoke_activation(lic["lid"], FP)
    r = client.post("/v1/ai/complete", json=b)
    assert r.status_code == 403, r.text
    j = r.json()
    assert j["ok"] is False and j["code"] == "license"
    assert ai.state.ai_transport.calls == []
    assert ai.state.db.get_wallet(lic["lid"]) is None  # rejected before any wallet side effect


def test_revoked_reason(client, ai, lic):
    revoke_license(ai, lic["lid"])
    assert client.post("/v1/ai/complete", json=body(ai, lic)).json()["reason"] == "revoked"


def test_no_credits_402(client, ai, lic):
    db = ai.state.db
    db.ensure_wallet(lic["lid"], 0)  # trial already consumed: empty wallet
    assert db.get_wallet(lic["lid"])["balance"] == 0
    r = client.post("/v1/ai/complete", json=body(ai, lic))
    assert r.status_code == 402 and r.json()["code"] == "no_credits"
    assert ai.state.ai_transport.calls == []
    # reserve: 4000 tokens of sonnet output = 6c * 4 = 24
    assert reserve_credits(ai.state.settings, 4000) == 24
    db.credit(lic["lid"], 24, "test")
    assert client.post("/v1/ai/complete", json=body(ai, lic, max_tokens=99999)).status_code == 200


def test_debit_never_negative(app):
    db = app.state.db
    acct = db.create_account("w@example.test", hash_password("hunter2hunter2"))
    row = issue_license(app, acct, "solo")
    db.ensure_wallet(row["lid"], 5)
    assert db.debit(row["lid"], 6) is None
    assert db.get_wallet(row["lid"])["balance"] == 5
    assert db.debit(row["lid"], 5) == 0
    assert db.debit_upto(row["lid"], 9) == (0, 0)


def test_ledger_has_no_prompt_text(client, ai, lic):
    client.post("/v1/ai/complete", json=body(ai, lic))
    ai.state.ai_transport = FakeVendor(status=500)
    r = client.post("/v1/ai/complete", json=body(ai, lic))
    assert r.status_code == 502 and r.json()["code"] == "vendor"
    assert PROMPT not in r.text
    db = ai.state.db
    with db.connect() as c:
        dump = json.dumps([list(r) for r in c.execute("SELECT * FROM ai_ledger")])
        audit = json.dumps([list(r) for r in c.execute("SELECT * FROM audit_log")])
    assert PROMPT not in dump and COMPLETION not in dump
    assert PROMPT not in audit and COMPLETION not in audit
    assert db.ledger_totals(lic["lid"])["calls"] == 2
    assert db.ledger_totals(lic["lid"])["ok_calls"] == 1
    # failed vendor call is free
    assert db.get_wallet(lic["lid"])["balance"] == 297


def test_rate_limit(client, ai, lic):
    ai.state.settings.ai_rate_per_min = 3
    ai.state.ai_limiter = None
    codes = [client.post("/v1/ai/complete", json=body(ai, lic)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert client.post("/v1/ai/complete", json=body(ai, lic)).json()["code"] == "rate_limited"


def test_relay_disabled_and_bad_tier(client, ai, lic):
    assert client.post("/v1/ai/complete", json=body(ai, lic, tier="huge")).status_code == 400
    ai.state.settings.anthropic_api_key = ""
    r = client.post("/v1/ai/complete", json=body(ai, lic))
    assert r.status_code == 503 and r.json()["code"] == "relay_disabled"


def test_max_tokens_capped(client, ai, lic):
    client.post("/v1/ai/complete", json=body(ai, lic, max_tokens=99999))
    assert ai.state.ai_transport.calls[0][2]["max_tokens"] == 4000


def test_vendor_call_transport_errors():
    def boom(url, headers, b, t):
        raise OSError("down")
    with pytest.raises(VendorError) as e:
        vendor_call("m", "", "u", 10, 0.1, None, api_key="k", transport=boom)
    assert e.value.kind == "network"


def test_balance_shape(client, ai, lic):
    r = client.get("/v1/ai/balance", params={"lid": lic["lid"], "fingerprint": FP, "activation": act_token(ai, lic)})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["balance"] == 300 and j["trial_granted"] is True
    assert j["packs"][0] == {"id": "price_small", "label": "500 credits ($5.00)", "credits": 500, "usd_cents": 500}
    assert j["checkout_url_base"].startswith("http")
    bad = client.get("/v1/ai/balance", params={"lid": lic["lid"], "fingerprint": "b" * 64, "activation": act_token(ai, lic)})
    assert bad.status_code == 403


def test_checkout_session_creation(client, ai, lic):
    seen = {}

    def fake_stripe(url, form, secret):
        seen.update(url=url, form=form, secret=secret)
        return {"id": "cs_test_1", "url": "https://checkout.stripe.test/cs_test_1"}
    ai.state.stripe_transport = fake_stripe
    r = client.post("/v1/ai/checkout", json={"lid": lic["lid"], "fingerprint": FP, "activation": act_token(ai, lic),
                                             "pack": "price_big"})
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://checkout.stripe.test/cs_test_1"
    f = seen["form"]
    assert seen["url"].endswith("/v1/checkout/sessions") and seen["secret"] == "sk_stripe"
    assert f["mode"] == "payment" and f["line_items[0][price]"] == "price_big"
    assert f["metadata[kind]"] == "ai_credits" and f["metadata[lid]"] == lic["lid"] and f["metadata[credits]"] == "3000"
    bad = client.post("/v1/ai/checkout", json={"lid": lic["lid"], "fingerprint": FP, "activation": act_token(ai, lic),
                                               "pack": "price_nope"})
    assert bad.status_code == 400 and bad.json()["code"] == "bad_pack"
    ai.state.settings.stripe_secret_key = ""
    r = client.post("/v1/ai/checkout", json={"lid": lic["lid"], "fingerprint": FP, "activation": act_token(ai, lic),
                                             "pack": "price_big"})
    assert r.status_code == 503


def _hook(client, event):
    raw = json.dumps(event).encode()
    return client.post("/webhooks/stripe", content=raw,
                       headers={"stripe-signature": sign_for_test(raw, "whsec_test"), "content-type": "application/json"})


def _event(eid, lid, sid="cs_test_1", credits=500):
    return {"id": eid, "type": "checkout.session.completed", "data": {"object": {
        "id": sid, "payment_status": "paid", "amount_total": 500, "customer_email": "ai@example.test",
        "metadata": {"kind": "ai_credits", "lid": lid, "credits": str(credits)}}}}


def test_webhook_credits_wallet_once(client, ai, lic):
    db = ai.state.db
    n_lic = len(db.licenses_for_account(lic["account_id"]))
    r = _hook(client, _event("evt_1", lic["lid"]))
    assert r.status_code == 200 and r.json()["ai_credits"] == 500
    assert db.get_wallet(lic["lid"])["balance"] == 800  # 300 trial + 500
    # replay: same event id, then a new event id for the same session
    assert _hook(client, _event("evt_1", lic["lid"])).json()["ignored"] == "duplicate event"
    assert _hook(client, _event("evt_2", lic["lid"])).json()["ignored"] == "duplicate session"
    assert db.get_wallet(lic["lid"])["balance"] == 800
    assert len(db.licenses_for_account(lic["account_id"])) == n_lic  # no license issued
    assert db._one("SELECT COUNT(*) AS n FROM ai_purchases")["n"] == 1
    r = _hook(client, _event("evt_3", "lic_missing", sid="cs_other"))
    assert r.json()["ok"] is False


def test_portal_card_and_admin_grant(client, portal, ai, lic):
    portal.signup("admin@example.test")  # listed in admin_emails
    html = client.get("/dashboard").text
    assert "AI credits" in html
    db = ai.state.db
    acct = db.get_account_by_email("admin@example.test")
    own = issue_license(ai, acct, "solo")
    html = client.get("/dashboard").text
    assert "Buy 500 credits ($5.00)" in html and own["lid"] in html
    # buy button creates a session and redirects
    ai.state.stripe_transport = lambda u, f, s: {"id": "cs_1", "url": "https://checkout.stripe.test/cs_1"}
    r = portal.post("/ai/checkout", lid=own["lid"], pack="price_small")
    assert r.status_code == 303 and r.headers["location"] == "https://checkout.stripe.test/cs_1"
    r = portal.post("/ai/checkout", lid=lic["lid"], pack="price_small")  # someone else's license
    assert "error=" in r.headers["location"]
    # admin grant
    r = portal.post(f"/admin/licenses/{lic['lid']}/credits", credits="250", note="goodwill")
    assert r.status_code == 303 and "notice=" in r.headers["location"]
    assert db.get_wallet(lic["lid"])["balance"] == 550
    assert "550" in client.get("/admin").text
    r = portal.post(f"/admin/licenses/{lic['lid']}/credits", credits="-5")
    assert "error=" in r.headers["location"]
