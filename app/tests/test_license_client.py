"""License client: parse_key, status state machine, offline grace math, clock rollback, middleware."""
from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from plannerboard.license import client as lc  # noqa: E402
from plannerboard.license.client import GRACE_DAYS, LicenseClient, LicenseError  # noqa: E402
from plannerboard.license.fingerprint import machine_fingerprint  # noqa: E402

DEV_PRIV = APP_DIR.parent / "license_server" / "keys" / "DEV_ONLY_private.pem"
T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Issuer:
    """Minimal signer mirroring licserver.keys (payload.sig over canonical JSON)."""

    def __init__(self, priv: Ed25519PrivateKey | None = None):
        if priv is None:
            priv = serialization.load_pem_private_key(DEV_PRIV.read_bytes(), password=None)
        self.priv = priv

    def sign(self, payload: dict) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"{_b64u(raw)}.{_b64u(self.priv.sign(raw))}"

    def key(self, **over) -> str:
        p = {"lid": "lic_test1", "email": "sam@example.test", "plan": "solo", "seats": 2,
             "features": ["ai", "core"], "issued": _iso(T0), "expires": _iso(T0 + timedelta(days=365))}
        p.update(over)
        return self.sign(p)

    def activation(self, fingerprint: str, valid_until: datetime, **over) -> str:
        p = {"lid": "lic_test1", "fingerprint": fingerprint, "issued": _iso(T0),
             "valid_until": _iso(valid_until), "plan": "solo", "features": ["ai", "core"]}
        p.update(over)
        return self.sign(p)


class Meta:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)


@pytest.fixture
def issuer():
    return Issuer()


@pytest.fixture
def meta():
    return Meta()


@pytest.fixture
def clock():
    return Clock()


def make_client(meta, clock, transport=None, cfg=None):
    return LicenseClient(cfg or {"license": {"server_url": "https://lic.example.test"}}, meta.get, meta.set,
                         transport=transport, now=clock, app_version="1.2.3")


def test_fingerprint_is_stable_sha256():
    fp = machine_fingerprint()
    assert len(fp) == 64 and fp == machine_fingerprint(force=True)


def test_parse_key_ok_and_rejections(issuer, meta, clock):
    c = make_client(meta, clock)
    key = issuer.key()
    p = c.parse_key(key)
    assert p["lid"] == "lic_test1" and p["plan"] == "solo" and p["features"] == ["ai", "core"] and p["seats"] == 2

    with pytest.raises(LicenseError):
        c.parse_key("garbage")
    with pytest.raises(LicenseError):
        c.parse_key(key + "x")  # signature segment damaged
    # payload altered: swap the plan inside the encoded JSON
    pb, sig = key.split(".")
    raw = base64.urlsafe_b64decode(pb + "=" * (-len(pb) % 4)).replace(b'"solo"', b'"team"')
    with pytest.raises(LicenseError, match="bad signature"):
        c.parse_key(f"{_b64u(raw)}.{sig}")
    # key signed by a different private key
    other = Issuer(Ed25519PrivateKey.generate())
    with pytest.raises(LicenseError, match="bad signature"):
        c.parse_key(other.key())
    # whitespace/newlines from copy-paste are tolerated
    assert c.parse_key(key[:20] + "\n" + key[20:] + " ")["lid"] == "lic_test1"


def test_status_unlicensed_invalid_dev(issuer, meta, clock, monkeypatch):
    c = make_client(meta, clock)
    st = c.status(use_cache=False)
    assert st["status"] == "unlicensed" and st["read_only"] is True
    meta.set("license.key", "nope.nope")
    assert c.status(use_cache=False)["status"] == "invalid"
    meta.set("license.key", issuer.key())
    st = c.status(use_cache=False)
    assert st["status"] == "unlicensed" and "not activated" in st["message"] and st["plan"] == "solo"
    monkeypatch.setenv("PB_LICENSE_DEV", "1")
    st = c.status(use_cache=False)
    assert st["status"] == "dev" and st["read_only"] is False


def test_status_grace_math(issuer, meta, clock):
    c = make_client(meta, clock)
    fp = machine_fingerprint()
    valid_until = T0 + timedelta(days=30)
    meta.set("license.key", issuer.key())
    meta.set("license.activation", issuer.activation(fp, valid_until))

    st = c.status(use_cache=False)
    assert st["status"] == "ok" and st["read_only"] is False and st["valid_until"] == _iso(valid_until)
    assert st["email"] == "sam@example.test" and st["seats"] == 2 and st["expires"].startswith("2027-10-05")

    clock.t = valid_until  # last second of validity
    assert c.status(use_cache=False)["status"] == "ok"

    clock.t = valid_until + timedelta(seconds=1)
    st = c.status(use_cache=False)
    assert st["status"] == "grace" and st["read_only"] is False and st["grace_days_left"] == GRACE_DAYS

    clock.t = valid_until + timedelta(days=13, hours=23)
    st = c.status(use_cache=False)
    assert st["status"] == "grace" and st["grace_days_left"] == 1

    clock.t = valid_until + timedelta(days=GRACE_DAYS)
    assert c.status(use_cache=False)["status"] == "grace"

    clock.t = valid_until + timedelta(days=GRACE_DAYS, seconds=1)
    st = c.status(use_cache=False)
    assert st["status"] == "expired" and st["read_only"] is True and "grace" in st["message"].lower()


def test_license_expiry_beats_activation(issuer, meta, clock):
    c = make_client(meta, clock)
    fp = machine_fingerprint()
    meta.set("license.key", issuer.key(expires=_iso(T0 + timedelta(days=1))))
    meta.set("license.activation", issuer.activation(fp, T0 + timedelta(days=30)))
    assert c.status(use_cache=False)["status"] == "ok"
    clock.advance(days=2)
    st = c.status(use_cache=False)
    assert st["status"] == "expired" and "Renew" in st["message"]


def test_activation_for_other_machine_or_other_lid_is_invalid(issuer, meta, clock):
    c = make_client(meta, clock)
    meta.set("license.key", issuer.key())
    meta.set("license.activation", issuer.activation("f" * 64, T0 + timedelta(days=30)))
    assert c.status(use_cache=False)["status"] == "invalid"
    meta.set("license.activation", issuer.activation(machine_fingerprint(), T0 + timedelta(days=30), lid="lic_other"))
    assert c.status(use_cache=False)["status"] == "invalid"


def test_clock_rollback_does_not_extend_grace(issuer, meta, clock):
    c = make_client(meta, clock)
    fp = machine_fingerprint()
    valid_until = T0 + timedelta(days=30)
    meta.set("license.key", issuer.key())
    meta.set("license.activation", issuer.activation(fp, valid_until))
    clock.t = valid_until + timedelta(days=GRACE_DAYS + 5)
    assert c.status(use_cache=False)["status"] == "expired"
    clock.t = T0  # user rolls the clock back 50 days
    st = c.status(use_cache=False)
    assert st["status"] == "expired"  # high-water mark holds
    assert meta.get("license.clock_hwm") == _iso(valid_until + timedelta(days=GRACE_DAYS + 5))


def test_activate_heartbeat_deactivate_with_fake_transport(issuer, meta, clock):
    import hashlib
    import hmac
    import time as _time
    fp = machine_fingerprint()
    calls = []

    nonces = set()

    def _check_proof(body, path):
        assert "key" not in body and abs(body["ts"] - _time.time()) < 5
        assert len(body["nonce"]) >= 8 and body["nonce"] not in nonces
        nonces.add(body["nonce"])
        device_key = hashlib.sha256(("5e" * 32).encode()).hexdigest().encode()
        digest = LicenseClient.body_digest(body)
        want_digest = hashlib.sha256(json.dumps(
            {k: v for k, v in body.items() if k not in ("ts", "nonce", "proof")},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert digest == want_digest
        msg = f"lic_test1|{fp}|{body['ts']}|{path}|{body['nonce']}|{digest}".encode()
        assert body["proof"] == hmac.new(device_key, msg, hashlib.sha256).hexdigest()

    def transport(url, body, timeout):
        calls.append((url, body))
        if url.endswith("/v1/activate"):
            assert body["key"] and body["fingerprint"] == fp and body["app_version"] == "1.2.3"
            return 200, {"ok": True, "activation": issuer.activation(fp, clock() + timedelta(days=30)),
                         "device_secret": "5e" * 32}
        if url.endswith("/v1/heartbeat"):
            assert {k: body[k] for k in ("lid", "fingerprint", "app_version")} == \
                {"lid": "lic_test1", "fingerprint": fp, "app_version": "1.2.3"}
            _check_proof(body, "/v1/heartbeat")
            return 200, {"ok": True, "activation": issuer.activation(fp, clock() + timedelta(days=30))}
        if url.endswith("/v1/deactivate"):
            _check_proof(body, "/v1/deactivate")
            return 200, {"ok": True, "released": True}
        raise AssertionError(url)

    c = make_client(meta, clock, transport=transport)
    res = c.activate(issuer.key())
    assert res["license"]["lid"] == "lic_test1" and res["activation"]["fingerprint"] == fp
    assert meta.get("license.key") and meta.get("license.activation")
    assert c.status(use_cache=False)["status"] == "ok"

    # daily cadence: a second heartbeat the same day is skipped
    assert c.heartbeat() == {"ok": True, "skipped": "recent"}
    clock.advance(hours=25)
    assert c.heartbeat() == {"ok": True}
    assert [u.rsplit("/", 1)[1] for u, _ in calls] == ["activate", "heartbeat"]

    # a server rejection flips status; a 403 clears the activation
    def rejecting(url, body, timeout):
        return 403, {"detail": {"code": "revoked", "message": "This license has been revoked."}}

    c._transport = rejecting
    hb = c.heartbeat(force=True)
    assert hb["ok"] is False and hb["code"] == "revoked"
    st = c.status(use_cache=False)
    assert st["status"] == "invalid" and "revoked" in st["message"]

    # offline failures are swallowed and do not change state
    c._transport = transport
    c.activate(issuer.key())
    assert c.status(use_cache=False)["status"] == "ok"

    def offline(url, body, timeout):
        raise LicenseError("license server unreachable", "offline")

    c._transport = offline
    hb = c.heartbeat(force=True)
    assert hb["ok"] is False and hb.get("offline") is True
    assert c.status(use_cache=False)["status"] == "ok"

    c._transport = transport
    assert c.deactivate()["ok"] is True
    assert c.status(use_cache=False)["status"] == "unlicensed"
    assert meta.get("license.key") == "" and meta.get("license.activation") == ""
    assert meta.get("license.device_secret") == ""


def test_sign_request_shape_and_legacy_upgrade(issuer, meta, clock):
    import hashlib
    import hmac
    fp = machine_fingerprint()
    sent = []

    def transport(url, body, timeout):
        sent.append((url, body))
        assert url.endswith("/v1/heartbeat") and body["key"]          # legacy: key once, no proof
        return 200, {"ok": True, "activation": issuer.activation(fp, clock() + timedelta(days=30)),
                     "device_secret": "ab" * 32}

    c = make_client(meta, clock, transport=transport)
    meta.set("license.key", issuer.key())
    meta.set("license.activation", issuer.activation(fp, clock() + timedelta(days=30)))
    sent_body = {"lid": "lic_test1", "fingerprint": fp, "activation": meta.get("license.activation"),
                 "user": "hello \u4e2d", "temperature": 0.2}
    out = c.sign_request("/v1/ai/complete", sent_body)
    assert set(out) == {"ts", "nonce", "proof"}
    assert len(sent) == 1 and meta.get("license.device_secret") == "ab" * 32
    key = hashlib.sha256(("ab" * 32).encode()).hexdigest().encode()
    digest = hashlib.sha256(json.dumps(sent_body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert out["proof"] == hmac.new(key, f"lic_test1|{fp}|{out['ts']}|/v1/ai/complete|{out['nonce']}|{digest}".encode(),
                                    hashlib.sha256).hexdigest()
    # the proof changes with the body and with every call (fresh nonce)
    again = c.sign_request("/v1/ai/complete", sent_body)
    assert again["nonce"] != out["nonce"] and again["proof"] != out["proof"]
    other = c.sign_request("/v1/ai/complete", {**sent_body, "user": "different"})
    assert other["proof"] != out["proof"]
    # no body: the identity body {lid, fingerprint, activation} (what /v1/ai/balance sends)
    bal = c.sign_request("/v1/ai/balance")
    idb = {"lid": "lic_test1", "fingerprint": fp, "activation": meta.get("license.activation")}
    assert bal["proof"] == hmac.new(
        key, f"lic_test1|{fp}|{bal['ts']}|/v1/ai/balance|{bal['nonce']}|{c.body_digest(idb)}".encode(),
        hashlib.sha256).hexdigest()
    assert len(sent) == 1                                              # no second upgrade



def test_activate_rejects_bad_key_before_network(issuer, meta, clock):
    def transport(url, body, timeout):
        raise AssertionError("network must not be touched for an unverifiable key")

    c = make_client(meta, clock, transport=transport)
    with pytest.raises(LicenseError):
        c.activate("bogus.key")


def test_flask_middleware_402_and_blueprint(issuer, meta, clock):
    flask = pytest.importorskip("flask")
    from plannerboard.api.license_api import bp
    from plannerboard.license.client import middleware

    fp = machine_fingerprint()

    class Store:
        meta_get = staticmethod(meta.get)
        meta_set = staticmethod(meta.set)

    app = flask.Flask("t")
    app.extensions["pb"] = {"store": Store(), "cfg": {"license": {"server_url": "https://lic.example.test"}}}
    app.register_blueprint(bp)
    middleware(app)

    @app.get("/api/projects")
    def _list():
        return flask.jsonify([])

    @app.post("/api/projects")
    def _create():
        return flask.jsonify({"ok": True})

    client = lc.client_from_app(app)
    client._now = clock
    tc = app.test_client()

    # unlicensed: reads work, writes are 402, license endpoints stay open
    assert tc.get("/api/projects").status_code == 200
    r = tc.post("/api/projects", json={})
    assert r.status_code == 402 and r.get_json()["error"] == "license_required"
    assert tc.get("/api/license").get_json()["status"] == "unlicensed"
    assert tc.post("/api/license/activate", json={}).status_code == 400

    # activate through the blueprint with a fake transport
    client._transport = lambda url, body, timeout: (200, {"activation": issuer.activation(fp, clock() + timedelta(days=30))})
    r = tc.post("/api/license/activate", json={"key": issuer.key()})
    assert r.status_code == 200 and r.get_json()["status"]["status"] == "ok"
    assert tc.post("/api/projects", json={}).status_code == 200

    # refresh (forced heartbeat) and deactivate
    r = tc.post("/api/license/refresh")
    assert r.get_json()["ok"] is True
    r = tc.post("/api/license/deactivate")
    assert r.get_json()["status"]["status"] == "unlicensed"
    assert tc.post("/api/projects", json={}).status_code == 402

    # grace keeps writes open; expired grace closes them
    meta.set("license.key", issuer.key())
    meta.set("license.activation", issuer.activation(fp, clock()))
    clock.advance(days=3)
    client._invalidate()  # meta was edited behind the client's back; the 30 s status cache is stale
    assert tc.post("/api/projects", json={}).status_code == 200
    clock.advance(days=GRACE_DAYS)
    client._invalidate()
    assert tc.post("/api/projects", json={}).status_code == 402


# ---------------------------------------------------------------- 1.1.0 hardening

def test_dev_mode_is_inert_on_the_release_channel(issuer, meta, clock, monkeypatch):
    from plannerboard import _channel
    c = make_client(meta, clock)
    monkeypatch.setenv("PB_LICENSE_DEV", "1")
    assert c.status(use_cache=False)["status"] == "dev"
    monkeypatch.setattr(_channel, "CHANNEL", "release")
    LicenseClient._dev_warned = False
    st = c.status(use_cache=False)
    assert st["status"] == "unlicensed" and st["read_only"] is True
    assert LicenseClient.dev_mode() is False


def test_renewal_reaches_a_client_holding_the_old_key(issuer, meta, clock):
    """The key string says 'expires in 1 day'; the signed activation (which
    the server re-issues on every heartbeat) says the license was renewed."""
    c = make_client(meta, clock)
    fp = machine_fingerprint()
    old_exp = T0 + timedelta(days=1)
    new_exp = T0 + timedelta(days=366)
    meta.set("license.key", issuer.key(expires=_iso(old_exp)))
    meta.set("license.activation", issuer.activation(fp, T0 + timedelta(days=30), expires=_iso(new_exp)))
    clock.t = T0 + timedelta(days=2)             # past the old key's expiry
    st = c.status(use_cache=False)
    assert st["status"] == "ok" and st["expires"] == _iso(new_exp)
    # an activation can never SHORTEN the key's own expiry (fresh state: the hwm above is at T0+2d)
    meta2, clock2 = Meta(), Clock(T0)
    c2 = make_client(meta2, clock2)
    meta2.set("license.key", issuer.key(expires=_iso(old_exp)))
    meta2.set("license.activation", issuer.activation(fp, T0 + timedelta(days=30), expires=_iso(T0 - timedelta(days=1))))
    st = c2.status(use_cache=False)
    assert st["status"] == "ok" and st["expires"] == _iso(old_exp)


def test_clock_rollback_floor_survives_deleting_the_hwm(issuer, meta, clock):
    c = make_client(meta, clock)
    fp = machine_fingerprint()
    valid_until = T0 + timedelta(days=30)
    late = valid_until + timedelta(days=GRACE_DAYS + 5)
    meta.set("license.key", issuer.key())
    # the server issued this activation at `late` (its clock is honest)
    meta.set("license.activation", issuer.activation(fp, valid_until, issued=_iso(late)))
    clock.t = late
    assert c.status(use_cache=False)["status"] == "expired"
    meta.d.pop("license.clock_hwm", None)         # attacker deletes the mark
    clock.t = T0                                   # and rolls the clock back
    assert c.status(use_cache=False)["status"] == "expired"     # floored at the activation's issue time


# ---------------------------------------------------------------- 1.2.0 production key
def test_release_channel_rejects_dev_signed_keys(monkeypatch):
    """A shipped build trusts only the production public key."""
    from plannerboard import _channel
    monkeypatch.setattr(_channel, "CHANNEL", "release")
    assert lc.trusted_public_key_pem() == lc.DEFAULT_PUBLIC_KEY_PEM
    assert lc.DEFAULT_PUBLIC_KEY_PEM != lc.DEV_PUBLIC_KEY_PEM
    token = Issuer().key()
    with pytest.raises(LicenseError):
        lc.verify_token(token, lc.load_public_key(lc.DEFAULT_PUBLIC_KEY_PEM))


def test_start_trial_stores_activation_and_secret_but_no_key(issuer, meta, clock):
    fp = machine_fingerprint()

    def transport(url, body, timeout):
        assert url.endswith("/v1/trial")
        assert body == {"email": "sam@example.test", "fingerprint": fp, "app_version": "1.2.3"}
        return 200, {"ok": True, "device_secret": "cd" * 32,
                     "activation": issuer.activation(fp, clock() + timedelta(days=30), plan="trial",
                                                     expires=_iso(T0 + timedelta(days=14))),
                     "license": {"lid": "lic_test1", "plan": "trial"}}

    c = make_client(meta, clock, transport=transport)
    res = c.start_trial("sam@example.test")
    assert "key" not in res and res["activation"]["fingerprint"] == fp
    assert not meta.get("license.key") and meta.get("license.activation")
    assert meta.get("license.device_secret") == "cd" * 32
    st = c.status(use_cache=False)
    assert st["status"] == "ok" and st["trial"] is True and st["days_left"] == 14 and st["lid"] == "lic_test1"

    # the daily heartbeat works from the activation alone, with a proof
    seen = []

    def hb(url, body, timeout):
        seen.append(body)
        assert url.endswith("/v1/heartbeat") and "key" not in body and body["proof"] and body["nonce"]
        return 200, {"ok": True, "activation": issuer.activation(fp, clock() + timedelta(days=30), plan="trial",
                                                                 expires=_iso(T0 + timedelta(days=14)))}

    c._transport = hb
    assert c.heartbeat(force=True) == {"ok": True} and len(seen) == 1


def test_activate_after_trial_carries_proof_and_device_bound_surfaces(issuer, meta, clock):
    fp = machine_fingerprint()
    meta.set("license.device_secret", "cd" * 32)
    meta.set("license.activation", issuer.activation(fp, clock() + timedelta(days=30), plan="trial"))

    def transport(url, body, timeout):
        assert url.endswith("/v1/activate") and body["proof"] and body["nonce"] and body["ts"]
        return 200, {"ok": True, "activation": issuer.activation(fp, clock() + timedelta(days=30))}

    c = make_client(meta, clock, transport=transport)
    c.activate(issuer.key())
    assert meta.get("license.device_secret") == "cd" * 32            # kept: the server did not rotate it

    # lost secret: heartbeat's proof is refused, the key alone is refused, the error says why
    def refusing(url, body, timeout):
        if url.endswith("/v1/heartbeat"):
            return 403, {"detail": {"code": "proof_invalid", "message": "bad proof"}}
        assert "proof" not in body                                     # recovery attempt carries none
        return 409, {"detail": {"code": "device_bound", "message": "Release it in the portal."}}

    c._transport = refusing
    hb = c.heartbeat(force=True)
    assert hb["ok"] is False and hb["code"] == "device_bound"
    assert "device_bound" in meta.get("license.revoked")


def test_start_trial_server_error_surfaces(issuer, meta, clock):
    def transport(url, body, timeout):
        return 409, {"detail": {"code": "trial_used", "message": "This email has already had a free trial."}}

    c = make_client(meta, clock, transport=transport)
    with pytest.raises(LicenseError) as ei:
        c.start_trial("sam@example.test")
    assert ei.value.code == "trial_used" and ei.value.http_status == 409
    assert not meta.get("license.key")
    with pytest.raises(LicenseError):
        c.start_trial("not-an-email")


def test_status_not_trial_for_paid(issuer, meta, clock):
    c = make_client(meta, clock)
    meta.set("license.key", issuer.key())
    st = c.status(use_cache=False)
    assert st["trial"] is False and st["days_left"] == 0
