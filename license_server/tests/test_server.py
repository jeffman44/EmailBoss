"""License server tests: auth, issue, activate (seat limit), heartbeat, revoke, webhook, admin."""
import json

from starlette.testclient import TestClient

from licserver.main import create_app

from licserver import mailer
from licserver.billing import sign_for_test
from licserver.keys import verify_token
from licserver.licenses import issue_license, revoke_license

FP1 = "a" * 64
FP2 = "b" * 64
FP3 = "c" * 64


def _account(app, email):
    return app.state.db.get_account_by_email(email)


# ---------------------------------------------------------------- auth

def test_signup_login_logout_and_trial(client, portal, app):
    r = portal.signup("sam@example.test")
    assert r.status_code == 303 and r.headers["location"].startswith("/dashboard")
    assert "lic_session" in client.cookies
    acct = _account(app, "sam@example.test")
    assert acct and acct["is_admin"] == 0  # admin list is set, so no auto-admin
    lics = app.state.db.licenses_for_account(acct["id"])
    assert len(lics) == 1 and lics[0]["plan"] == "trial"
    # trial key was "emailed"
    assert any(m["to"] == "sam@example.test" and lics[0]["key"] in m["body"] for m in mailer.SENT)

    r = client.get("/dashboard")
    assert r.status_code == 200 and lics[0]["lid"] in r.text and lics[0]["key"] in r.text

    r = portal.logout()
    assert r.status_code == 303
    r = client.get("/dashboard")
    assert r.status_code == 303 and "/login" in r.headers["location"]

    r = portal.login("sam@example.test", "wrong-password")
    assert r.status_code == 401
    r = portal.login("sam@example.test")
    assert r.status_code == 303
    assert client.get("/dashboard").status_code == 200


def test_signup_rejects_bad_input_and_csrf(client, portal):
    r = client.post("/signup", data={"email": "x@example.test", "password": "hunter2hunter2", "csrf_token": "nope"})
    assert r.status_code == 400
    r = portal.signup("not-an-email")
    assert r.status_code == 400
    r = portal.signup("short@example.test", password="short")
    assert r.status_code == 400
    portal.signup("dup@example.test")
    portal.logout()
    r = portal.signup("dup@example.test")
    assert r.status_code == 400 and "already" in r.text


def test_login_rate_limit(client, portal):
    portal.signup("rl@example.test")
    portal.logout()
    codes = [portal.login("rl@example.test", "bad-bad-bad").status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429


def test_password_reset_flow(client, portal, app):
    portal.signup("pw@example.test")
    portal.logout()
    mailer.SENT.clear()
    r = portal.post("/forgot", email="pw@example.test")
    assert r.status_code == 303
    link = [m for m in mailer.SENT if "reset" in m["subject"].lower()][0]["body"]
    token = link.split("/reset/")[1].split()[0]
    assert client.get(f"/reset/{token}").status_code == 200
    r = portal.post(f"/reset/{token}", password="newpassword123", password2="newpassword123")
    assert r.status_code == 303
    assert portal.login("pw@example.test", "hunter2hunter2").status_code == 401
    assert portal.login("pw@example.test", "newpassword123").status_code == 303
    # token is single use
    assert client.get(f"/reset/{token}").status_code == 400


def test_admin_gate_and_issue(client, portal, app):
    portal.signup("nobody@example.test")
    assert client.get("/admin").status_code == 403
    portal.logout()
    portal.signup("admin@example.test")
    assert client.get("/admin").status_code == 200
    r = portal.post("/admin/issue", email="cust@example.test", plan="team", seats="3", days="0", features="", send_email="1")
    assert r.status_code == 303, r.text
    acct = _account(app, "cust@example.test")
    assert acct is not None
    lic = app.state.db.licenses_for_account(acct["id"])[0]
    assert lic["plan"] == "team" and lic["seats"] == 3 and "multi_seat" in lic["features"]
    payload = verify_token(lic["key"], app.state.signer.public_key)
    assert payload == {"lid": lic["lid"], "email": "cust@example.test", "plan": "team", "seats": 3,
                       "features": sorted(lic["features"]), "issued": lic["issued"], "expires": lic["expires"]}
    assert client.get("/admin/activations").status_code == 200
    assert client.get("/admin/audit").status_code == 200
    assert client.get("/download").status_code == 200


# ---------------------------------------------------------------- /v1 device API

def _issued(app, email="dev@example.test", plan="solo", seats=2):
    db = app.state.db
    from licserver.auth import hash_password
    acct = db.get_account_by_email(email) or db.create_account(email, hash_password("hunter2hunter2"))
    return issue_license(app, acct, plan, seats=seats)


def test_activate_heartbeat_seat_limit_deactivate(client, app):
    lic = _issued(app, seats=2)
    r = client.post("/v1/activate", json={"key": lic["key"], "fingerprint": FP1, "app_version": "1.0.0"})
    assert r.status_code == 200, r.text
    body = r.json()
    act = verify_token(body["activation"], app.state.signer.public_key)
    assert set(act) == {"lid", "fingerprint", "issued", "valid_until", "plan", "features", "expires"}
    assert act["expires"] == lic["expires"]
    assert act["lid"] == lic["lid"] and act["fingerprint"] == FP1 and act["plan"] == "solo"
    assert act["valid_until"] > act["issued"]
    assert body["payload"] == act

    # same device again: idempotent, no new seat
    assert client.post("/v1/activate", json={"key": lic["key"], "fingerprint": FP1}).status_code == 200
    # second device fills the license
    assert client.post("/v1/activate", json={"key": lic["key"], "fingerprint": FP2}).status_code == 200
    # third is refused
    r = client.post("/v1/activate", json={"key": lic["key"], "fingerprint": FP3})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "seat_limit"

    # heartbeat refreshes the activation for a known device
    r = client.post("/v1/heartbeat", json={"lid": lic["lid"], "fingerprint": FP1, "app_version": "1.0.1"})
    assert r.status_code == 200 and verify_token(r.json()["activation"], app.state.signer.public_key)["fingerprint"] == FP1
    # heartbeat from an unknown device is refused
    r = client.post("/v1/heartbeat", json={"lid": lic["lid"], "fingerprint": FP3})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_activated"

    # release a seat, third device gets in
    r = client.post("/v1/deactivate", json={"lid": lic["lid"], "fingerprint": FP2})
    assert r.status_code == 200 and r.json()["released"] is True
    assert client.post("/v1/activate", json={"key": lic["key"], "fingerprint": FP3}).status_code == 200
    r = client.post("/v1/heartbeat", json={"lid": lic["lid"], "fingerprint": FP2})
    assert r.status_code == 404

    # validate + public key
    r = client.post("/v1/validate", json={"key": lic["key"]})
    assert r.json()["valid"] is True and r.json()["seats_used"] == 2
    r = client.get("/v1/public-key")
    assert "BEGIN PUBLIC KEY" in r.json()["public_key_pem"]


def test_bad_key_and_tampered_key(client, app):
    lic = _issued(app)
    r = client.post("/v1/activate", json={"key": "not.a.key.at.all" * 3, "fingerprint": FP1})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_key"
    p, s = lic["key"].split(".")
    tampered = p[:-2] + ("AA" if p[-2:] != "AA" else "BB") + "." + s
    r = client.post("/v1/activate", json={"key": tampered, "fingerprint": FP1})
    assert r.status_code == 400
    r = client.post("/v1/validate", json={"key": tampered})
    assert r.json()["valid"] is False


def test_revoke_blocks_activate_and_heartbeat(client, app):
    lic = _issued(app, email="rev@example.test")
    assert client.post("/v1/activate", json={"key": lic["key"], "fingerprint": FP1}).status_code == 200
    revoke_license(app, lic["lid"], reason="test")
    r = client.post("/v1/activate", json={"key": lic["key"], "fingerprint": FP2})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "revoked"
    r = client.post("/v1/heartbeat", json={"lid": lic["lid"], "fingerprint": FP1})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "revoked"
    assert client.post("/v1/validate", json={"key": lic["key"]}).json()["state"] == "revoked"


def test_expired_license_refused(client, app):
    lic = _issued(app, email="old@example.test")
    app.state.db.update_license(lic["lid"], expires="2020-01-01T00:00:00Z")
    r = client.post("/v1/activate", json={"key": lic["key"], "fingerprint": FP1})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "expired"


def test_portal_deactivate_device(client, portal, app):
    portal.signup("dev2@example.test")
    acct = _account(app, "dev2@example.test")
    lic = app.state.db.licenses_for_account(acct["id"])[0]
    assert client.post("/v1/activate", json={"key": lic["key"], "fingerprint": FP1}).status_code == 200
    act = app.state.db.get_activation(lic["lid"], FP1)
    r = portal.post(f"/licenses/{lic['lid']}/devices/{act['id']}/deactivate")
    assert r.status_code == 303 and "notice=" in r.headers["location"]
    assert client.post("/v1/heartbeat", json={"lid": lic["lid"], "fingerprint": FP1}).status_code == 404


# ---------------------------------------------------------------- stripe webhook

def test_stripe_webhook_issue_and_revoke(client, app):
    event = {"id": "evt_1", "type": "checkout.session.completed",
             "data": {"object": {"customer_details": {"email": "buyer@example.test"}, "subscription": "sub_123",
                                 "metadata": {"plan": "team", "seats": "4"}}}}
    raw = json.dumps(event).encode()
    r = client.post("/webhooks/stripe", content=raw, headers={"stripe-signature": "t=1,v1=bad", "content-type": "application/json"})
    assert r.status_code == 400
    r = client.post("/webhooks/stripe", content=raw, headers={"stripe-signature": sign_for_test(raw, "whsec_test"),
                                                              "content-type": "application/json"})
    assert r.status_code == 200 and r.json()["plan"] == "team", r.text
    lic = app.state.db.get_license_by_stripe_sub("sub_123")
    assert lic and lic["seats"] == 4 and not lic["revoked"]
    assert any(m["to"] == "buyer@example.test" and lic["key"] in m["body"] for m in mailer.SENT)

    event2 = {"id": "evt_2", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_123"}}}
    raw2 = json.dumps(event2).encode()
    r = client.post("/webhooks/stripe", content=raw2, headers={"stripe-signature": sign_for_test(raw2, "whsec_test"),
                                                               "content-type": "application/json"})
    assert r.status_code == 200 and r.json()["revoked"] == lic["lid"]
    assert app.state.db.get_license(lic["lid"])["revoked"] is True
    r = client.post("/v1/activate", json={"key": lic["key"], "fingerprint": FP1})
    assert r.status_code == 403


# ---------------------------------------------------------------- hardening (1.1.0 review)

def _signed(client, event):
    raw = json.dumps(event).encode()
    return client.post("/webhooks/stripe", content=raw,
                       headers={"stripe-signature": sign_for_test(raw, "whsec_test"), "content-type": "application/json"})


def test_seat_claim_is_atomic_under_concurrency(app):
    """12 parallel activations on a 2-seat license: exactly 2 win."""
    import threading
    lic = _issued(app, email="race@example.test", seats=2)
    results = []
    from licserver.licenses import v1_activate, ActivateIn
    from starlette.testclient import TestClient

    def go(i):
        c = TestClient(app)
        r = c.post("/v1/activate", json={"key": lic["key"], "fingerprint": chr(97 + i) * 64})
        results.append(r.status_code)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(200) == 2 and results.count(409) == 10, results
    assert len(app.state.db.active_activations(lic["lid"])) == 2
    # a device that already holds a seat re-activates fine even when full
    c = TestClient(app)
    winner = [fp for fp in (chr(97 + i) * 64 for i in range(12))
              if app.state.db.get_activation(lic["lid"], fp) and not app.state.db.get_activation(lic["lid"], fp)["revoked"]][0]
    assert c.post("/v1/activate", json={"key": lic["key"], "fingerprint": winner}).status_code == 200


def test_activation_carries_expires_and_renewal_reaches_old_key(client, app):
    from licserver.licenses import renew_license
    lic = _issued(app, email="renew@example.test", seats=1)
    r = client.post("/v1/activate", json={"key": lic["key"], "fingerprint": FP1})
    assert r.json()["payload"]["expires"] == lic["expires"]
    renew_license(app, lic["lid"], 365)
    newer = app.state.db.get_license(lic["lid"])["expires"]
    assert newer > lic["expires"]
    # heartbeat (and activate with the OLD key string) both carry the renewed expiry
    r = client.post("/v1/heartbeat", json={"lid": lic["lid"], "fingerprint": FP1})
    assert r.status_code == 200 and r.json()["payload"]["expires"] == newer
    r = client.post("/v1/activate", json={"key": lic["key"], "fingerprint": FP1})
    assert r.status_code == 200 and r.json()["payload"]["expires"] == newer
    act = verify_token(r.json()["activation"], app.state.signer.public_key)
    assert act["expires"] == newer


def test_stripe_fails_closed_without_secret(settings, tmp_path):
    settings.stripe_webhook_secret = ""
    settings.db_path = str(tmp_path / "noweb.db")
    c = TestClient(create_app(settings))
    raw = json.dumps({"id": "evt_x", "type": "checkout.session.completed",
                      "data": {"object": {"customer_email": "attacker@example.test", "metadata": {"plan": "team", "seats": "500"}}}}).encode()
    r = c.post("/webhooks/stripe", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 503
    settings.allow_unsigned_stripe = True
    settings.db_path = str(tmp_path / "noweb2.db")
    c = TestClient(create_app(settings))
    assert c.post("/webhooks/stripe", content=raw, headers={"content-type": "application/json"}).status_code == 200


def test_stripe_events_idempotent_and_period_end(client, app):
    ev = {"id": "evt_c1", "type": "checkout.session.completed",
          "data": {"object": {"customer_details": {"email": "idem@example.test"}, "subscription": "sub_idem",
                              "metadata": {"plan": "solo"}}}}
    assert _signed(client, ev).json()["issued"]
    assert _signed(client, ev).json()["ignored"] == "duplicate event"
    lic = app.state.db.get_license_by_stripe_sub("sub_idem")
    first_exp = lic["expires"]
    # the subscription's first invoice does not double-grant
    inv0 = {"id": "evt_i0", "type": "invoice.paid", "data": {"object": {"subscription": "sub_idem", "billing_reason": "subscription_create",
                                                                        "lines": {"data": [{"price": {"recurring": {"interval": "year"}}}]}}}}
    assert "ignored" in _signed(client, inv0).json()
    assert app.state.db.get_license(lic["lid"])["expires"] == first_exp
    # a renewal invoice sets expiry to the period end, and a replay changes nothing
    import time as _t
    period_end = int(_t.time()) + 400 * 86400
    inv1 = {"id": "evt_i1", "type": "invoice.paid", "data": {"object": {"subscription": "sub_idem", "billing_reason": "subscription_cycle",
                                                                        "lines": {"data": [{"period": {"end": period_end}, "price": {"recurring": {"interval": "year"}}}]}}}}
    assert _signed(client, inv1).json()["expires_ts"] == period_end
    exp1 = app.state.db.get_license(lic["lid"])["expires"]
    assert exp1 > first_exp
    assert _signed(client, inv1).json()["ignored"] == "duplicate event"
    inv2 = dict(inv1, id="evt_i2")
    _signed(client, inv2)
    assert app.state.db.get_license(lic["lid"])["expires"] == exp1          # same period end: no stacking


def test_rate_limit_ignores_spoofed_forwarded_for(client, portal):
    portal.signup("xff@example.test")
    portal.logout()
    codes = []
    for i in range(8):
        r = client.post("/login", data={"email": "xff@example.test", "password": "bad-bad-bad", "csrf_token": portal.csrf()},
                        headers={"x-forwarded-for": f"10.0.0.{i}"})
        codes.append(r.status_code)
    assert codes[:5] == [401] * 5 and codes[5] == 429, codes


def test_reset_tokens_hashed_and_single_use(app):
    db = app.state.db
    from licserver.auth import hash_password
    acct = db.create_account("reset@example.test", hash_password("hunter2hunter2"))
    token = db.create_reset(acct["id"])
    row = db._one("SELECT token FROM password_resets")
    assert row["token"] != token and len(row["token"]) == 64
    assert db.peek_reset(token)
    assert db.consume_reset(token) == acct["id"]
    assert db.consume_reset(token) is None and db.peek_reset(token) is None
