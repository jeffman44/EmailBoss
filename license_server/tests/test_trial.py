"""POST /v1/trial: in-app free trial (no key in the response, credits only after email confirmation)."""
import re
import time

from licserver import mailer
from licserver.config import Settings
from licserver.main import create_app
from starlette.testclient import TestClient

FP1 = "a" * 64
FP2 = "b" * 64
GENERIC = "If this address can start a trial, you will get an email."


def _post(client, email="new@example.test", fp=FP1):
    return client.post("/v1/trial", json={"email": email, "fingerprint": fp, "app_version": "1.3.0"})


def _mail(email):
    return [m for m in mailer.SENT if m["to"] == email][-1]


def test_trial_issues_once_and_never_returns_the_key(client, app):
    r = _post(client)
    assert r.status_code == 200, r.text
    j = r.json()
    assert "key" not in j and j["ok"] and j["activation"] and j["device_secret"]
    assert j["license"]["plan"] == "trial" and j["payload"]["fingerprint"] == FP1
    acct = app.state.db.get_account_by_email("new@example.test")
    assert acct and not acct.get("verified")
    body = _mail("new@example.test")["body"]
    lic = app.state.db.licenses_for_account(acct["id"])[0]
    assert lic["key"] in body and "/verify/" in body and "/reset/" in body
    assert "assword:" not in body and "emporary" not in body            # no password in the mail
    # the key from the mail activates normally; a second attempt is refused and audited
    assert client.post("/v1/activate", json={"key": lic["key"], "fingerprint": FP2}).status_code == 409  # seat
    r3 = _post(client, fp=FP2)
    assert r3.status_code == 409 and r3.json()["detail"]["code"] == "trial_used"
    assert any(e["action"] == "trial.refused" for e in app.state.db.audit_entries())


def test_existing_account_gets_the_same_generic_409(client, app):
    from licserver.auth import hash_password
    app.state.db.create_account("victim@example.test", hash_password("hunter2hunter2"), verified=True)
    a = _post(client, "victim@example.test")
    _post(client, "other@example.test")
    b = _post(client, "other@example.test", fp=FP2)          # trial already used
    assert a.status_code == b.status_code == 409
    assert a.json() == b.json() and a.json()["detail"]["message"] == GENERIC
    acct = app.state.db.get_account_by_email("victim@example.test")
    assert app.state.db.licenses_for_account(acct["id"]) == []   # nothing was issued on the victim's account


def test_no_ai_credits_until_the_email_is_confirmed(client, app):
    ai = app.state.settings
    ai.anthropic_api_key = "sk-test"
    j = _post(client, "credits@example.test").json()
    lid = j["license"]["lid"]
    assert (app.state.db.get_wallet(lid) or {"balance": 0})["balance"] == 0
    from tests.test_ai_relay import auth_body
    lic = dict(app.state.db.get_license(lid), device_secret=j["device_secret"])
    r = client.post("/v1/ai/balance", json=auth_body(app, lic, "/v1/ai/balance"))
    assert r.status_code == 200 and r.json()["balance"] == 0 and r.json()["pending_verification"] is True
    token = re.search(r"/verify/(\S+)", _mail("credits@example.test")["body"]).group(1)
    assert client.get(f"/verify/{token}").status_code in (200, 303)
    r = client.post("/v1/ai/balance", json=auth_body(app, lic, "/v1/ai/balance"))
    assert r.json()["balance"] == ai.ai_trial_credits and r.json()["pending_verification"] is False
    # the link is single use; confirming again funds nothing more
    assert client.get(f"/verify/{token}").status_code == 400


def test_set_password_link_works_and_also_confirms(client, app):
    _post(client, "pw@example.test")
    token = re.search(r"/reset/(\S+)", _mail("pw@example.test")["body"]).group(1)
    assert client.get(f"/reset/{token}").status_code == 200
    from tests.conftest import Portal
    p = Portal(client)
    r = p.post(f"/reset/{token}", password="brand-new-password", password2="brand-new-password")
    assert r.status_code == 303
    assert app.state.db.get_account_by_email("pw@example.test")["verified"]
    assert p.login("pw@example.test", "brand-new-password").status_code == 303


def test_global_trial_budget(tmp_path):
    from pathlib import Path
    key = Path(__file__).resolve().parents[1] / "keys" / "DEV_ONLY_private.pem"
    s = Settings(db_path=str(tmp_path / "b.db"), private_key_path=str(key), trial_enabled=True,
                 ai_trial_per_day=3, trial_per_ip_day=100, login_rate_limit=1000)
    c = TestClient(create_app(s))
    codes = [_post(c, f"g{i}@example.test", fp=("%02d" % i) * 32).status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert _post(c, "g9@example.test", fp="09" * 32).json()["detail"]["code"] == "trial_budget"


def test_per_network_trial_budget(tmp_path):
    from pathlib import Path
    key = Path(__file__).resolve().parents[1] / "keys" / "DEV_ONLY_private.pem"
    s = Settings(db_path=str(tmp_path / "n.db"), private_key_path=str(key), trial_enabled=True,
                 ai_trial_per_day=1000, trial_per_ip_day=5, login_rate_limit=1000)
    c = TestClient(create_app(s))
    codes = [_post(c, f"n{i}@example.test", fp=("%02d" % i) * 32).status_code for i in range(7)]
    assert codes == [200] * 5 + [429] * 2
    r = _post(c, "n9@example.test", fp="09" * 32)
    assert r.json()["detail"]["code"] == "trial_budget"


def test_trial_net_is_the_slash_24():
    from licserver.licenses import _trial_net
    assert _trial_net("203.0.113.7") == _trial_net("203.0.113.250") == "203.0.113.0/24"
    assert _trial_net("203.0.114.7") != _trial_net("203.0.113.7")
    assert _trial_net("2001:db8:1:2:aaaa::1") == _trial_net("2001:db8:1:2:bbbb::9")


def test_mail_failure_rolls_the_trial_back(client, app):
    orig = mailer.SENDER
    def boom(to, subject, body):
        raise RuntimeError("smtp down")
    mailer.SENDER = boom
    try:
        r = _post(client, "mailfail@example.test")
    finally:
        mailer.SENDER = orig
    assert r.status_code == 503 and r.json()["detail"]["code"] == "trial_unavailable"
    assert app.state.db.get_account_by_email("mailfail@example.test") is None
    assert _post(client, "mailfail@example.test").status_code == 200       # try again works


def test_trial_bad_email_400(client):
    assert _post(client, email="nope").status_code == 400


def test_trial_disabled_404(tmp_path):
    from pathlib import Path
    key = Path(__file__).resolve().parents[1] / "keys" / "DEV_ONLY_private.pem"
    s = Settings(db_path=str(tmp_path / "x.db"), private_key_path=str(key), trial_enabled=False)
    c = TestClient(create_app(s))
    assert _post(c).status_code == 404


def test_trial_rate_limited(client):
    codes = [_post(client, email=f"u{i}@example.test").status_code for i in range(7)]
    assert 429 in codes
