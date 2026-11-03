"""POST /v1/trial: in-app free trial."""
from licserver import mailer
from licserver.config import Settings
from licserver.main import create_app
from starlette.testclient import TestClient

FP1 = "a" * 64
FP2 = "b" * 64


def _post(client, email="new@example.test", fp=FP1):
    return client.post("/v1/trial", json={"email": email, "fingerprint": fp, "app_version": "1.3.0"})


def test_trial_issues_once(client, app):
    r = _post(client)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["key"] and j["activation"] and j["license"]["plan"] == "trial"
    assert j["payload"]["fingerprint"] == FP1
    acct = app.state.db.get_account_by_email("new@example.test")
    assert acct and not acct.get("verified")
    assert any(m["to"] == "new@example.test" and j["key"] in m["body"] for m in mailer.SENT)
    # the returned key activates normally
    r2 = client.post("/v1/activate", json={"key": j["key"], "fingerprint": FP1})
    assert r2.status_code == 200
    # second attempt (even from another machine) is refused and audited
    r3 = _post(client, fp=FP2)
    assert r3.status_code == 409 and r3.json()["detail"]["code"] == "trial_used"
    assert any(e["action"] == "trial.refused" for e in app.state.db.audit_entries())


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
