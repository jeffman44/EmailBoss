"""The desktop app's LicenseClient against the real server (TestClient as transport)."""
from datetime import datetime, timedelta, timezone

from licserver.auth import hash_password
from licserver.licenses import issue_license, revoke_license
from plannerboard.license.client import GRACE_DAYS, LicenseClient
from plannerboard.license.fingerprint import machine_fingerprint


class Meta(dict):
    def get_(self, k, default=None):
        return dict.get(self, k, default)

    def set_(self, k, v):
        self[k] = v


class Clock:
    def __init__(self):
        self.t = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.t


def _transport(client):
    def send(url, body, timeout):
        path = url.split("testserver", 1)[1]
        r = client.post(path, json=body)
        return r.status_code, r.json()
    return send


def _lic(app, seats=1):
    db = app.state.db
    acct = db.create_account("e2e@example.test", hash_password("hunter2hunter2"))
    return issue_license(app, acct, "solo", seats=seats)


def test_client_end_to_end(client, app):
    lic = _lic(app, seats=1)
    meta, clock = Meta(), Clock()
    lc = LicenseClient({"license": {}}, meta.get_, meta.set_, server_url="http://testserver",
                       public_key_pem=app.state.signer.public_key_pem(), transport=_transport(client), now=clock)

    assert lc.status(use_cache=False)["status"] == "unlicensed"
    res = lc.activate(lic["key"])
    assert res["activation"]["fingerprint"] == machine_fingerprint()
    st = lc.status(use_cache=False)
    assert st["status"] == "ok" and st["plan"] == "solo" and st["email"] == "e2e@example.test"
    assert st["valid_until"] == res["activation"]["valid_until"]

    # a second machine cannot take the single seat
    other = LicenseClient({"license": {}}, Meta().get_, Meta().set_, server_url="http://testserver",
                          public_key_pem=app.state.signer.public_key_pem(), transport=_transport(client), now=clock)
    other_fp = "9" * 64
    from plannerboard.license import client as mod
    orig = mod.machine_fingerprint
    mod.machine_fingerprint = lambda force=False: other_fp
    try:
        try:
            other.activate(lic["key"])
            assert False, "seat limit not enforced"
        except mod.LicenseError as exc:
            assert exc.code == "seat_limit" and exc.http_status == 409
    finally:
        mod.machine_fingerprint = orig

    # heartbeat after 31 client-days (grace) refreshes the activation. The server
    # runs on real time, so the new valid_until is real-now + 30 d, which leaves
    # the (artificially advanced) client 13 grace days instead of 1.
    old_valid_until = st["valid_until"]
    clock.t = clock.t + timedelta(days=31)
    st = lc.status(use_cache=False)
    assert st["status"] == "grace" and st["grace_days_left"] in (13, 14)  # 14 when the server's second ticked over
    assert lc.heartbeat() == {"ok": True}
    st = lc.status(use_cache=False)
    assert st["valid_until"] >= old_valid_until and st["status"] == "grace" and st["read_only"] is False
    act_row = app.state.db.get_activation(lic["lid"], machine_fingerprint())
    assert act_row["valid_until"] == st["valid_until"] and act_row["last_seen"] >= act_row["first_seen"]

    # revoke on the server: next heartbeat drops the client to invalid
    revoke_license(app, lic["lid"])
    hb = lc.heartbeat(force=True)
    assert hb["ok"] is False and hb["code"] == "revoked"
    st = lc.status(use_cache=False)
    assert st["status"] == "invalid" and st["read_only"] is True

    # and a fresh activate with the same key fails too
    try:
        lc.activate(lic["key"])
        assert False
    except mod.LicenseError as exc:
        assert exc.code == "revoked"

    # grace math constant is what the docs promise
    assert GRACE_DAYS == 14
