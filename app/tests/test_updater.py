"""Signed update check: manifest make + verify roundtrip, tampering, sha256 and
size enforcement, semver, endpoint shapes (network stubbed), Windows-only
install, and the desktop helpers added with it."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from plannerboard import __version__, create_app, desktop, updater  # noqa: E402
from plannerboard.license import client as lc  # noqa: E402
from plannerboard.updater import UpdateError, Updater  # noqa: E402

DEV_PRIV = APP_DIR.parent / "license_server" / "keys" / "DEV_ONLY_private.pem"


def _load_script():
    spec = importlib.util.spec_from_file_location("make_update_manifest", APP_DIR / "scripts" / "make_update_manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def script():
    return _load_script()


@pytest.fixture
def priv():
    return serialization.load_pem_private_key(DEV_PRIV.read_bytes(), password=None)


@pytest.fixture
def pub():
    return lc.load_public_key(lc.release_public_key_pem())


@pytest.fixture
def exe(tmp_path):
    p = tmp_path / "EmailBoss-9.9.9-Setup.exe"
    p.write_bytes(b"MZ" + b"installer-bytes" * 100)
    return p


def make_release(script, priv, exe, tmp_path, version="9.9.9", **kw):
    out = tmp_path / "rel"
    doc = script.build_manifest(version, exe, "https://example.com/dl/" + exe.name, **kw)
    script.write_signed(doc, priv, out)
    return doc, (out / "update.json").read_bytes(), (out / "update.json.sig").read_text()


class Net:
    """Stands in for http_get / http_download."""

    def __init__(self, manifest=b"", sig="", payload=b""):
        self.manifest, self.sig, self.payload = manifest, sig, payload
        self.urls = []

    def get(self, url, timeout=10, max_bytes=65536):
        self.urls.append(url)
        if url.endswith(".sig"):
            return self.sig.encode() if isinstance(self.sig, str) else self.sig
        return self.manifest

    def download(self, url, dest, size, sha, timeout=30):
        # real size/sha enforcement is covered by test_http_download_enforces_sha_and_size
        self.urls.append(url)
        dest.write_bytes(self.payload)


@pytest.fixture
def net(monkeypatch):
    n = Net()
    monkeypatch.setattr(updater, "http_get", n.get)
    monkeypatch.setattr(updater, "http_download", n.download)
    return n


def new_updater(tmp_path, pub, cfg=None, current="1.3.0"):
    cfg = cfg or {}
    return Updater(lambda k, d=None: cfg.get(k, d), tmp_path / "data", public_key=pub, current=current)


# ---------------------------------------------------------------- manifest

def test_manifest_roundtrip(script, priv, pub, exe, tmp_path):
    doc, raw, sig = make_release(script, priv, exe, tmp_path)
    assert raw == updater.canonical_manifest_bytes(doc)          # file IS the signed bytes
    assert b" " not in raw and b"\n" not in raw
    got = updater.verify_manifest(raw, sig, pub)
    assert got["version"] == "9.9.9" and got["channel"] == "stable"
    w = got["windows"]
    assert w["sha256"] == hashlib.sha256(exe.read_bytes()).hexdigest() and w["size"] == exe.stat().st_size
    assert got["notes_url"].endswith("/releases/tag/v9.9.9")
    assert "=" not in sig.strip()                                 # base64url, unpadded


def test_script_cli_and_verify_flag(script, exe, tmp_path, capsys, monkeypatch):
    out = tmp_path / "o"
    rc = script.main(["--version", "9.9.9", "--exe", str(exe), "--url", "https://example.com/x.exe",
                      "--key", str(DEV_PRIV), "--out", str(out), "--verify", "--dev-key"])
    assert rc == 0 and (out / "update.json").is_file() and (out / "update.json.sig").is_file()
    assert "verify ok" in capsys.readouterr().out
    # the production key must NOT verify a dev-signed manifest
    with pytest.raises(UpdateError):
        updater.verify_manifest((out / "update.json").read_bytes(), (out / "update.json.sig").read_text(),
                                lc.load_public_key(lc.RELEASE_PUBLIC_KEY_PEM))


def test_release_key_defaults_to_production_key():
    assert lc.RELEASE_PUBLIC_KEY_PEM == lc.DEFAULT_PUBLIC_KEY_PEM
    assert lc.release_public_key_pem() == lc.DEV_PUBLIC_KEY_PEM    # source checkout = dev channel


def test_tampered_manifest_rejected(script, priv, pub, exe, tmp_path):
    doc, raw, sig = make_release(script, priv, exe, tmp_path)
    evil = json.loads(raw)
    evil["windows"]["url"] = "https://evil.example/Setup.exe"
    with pytest.raises(UpdateError) as e:
        updater.verify_manifest(updater.canonical_manifest_bytes(evil), sig, pub)
    assert e.value.code == "bad_signature"
    with pytest.raises(UpdateError):
        updater.verify_manifest(raw.replace(b"9.9.9", b"9.9.8"), sig, pub)
    with pytest.raises(UpdateError):
        updater.verify_manifest(raw, "not base64 !!", pub)
    other = Ed25519PrivateKey.generate()
    with pytest.raises(UpdateError):
        updater.verify_manifest(raw, updater.sign_manifest_bytes(raw, other), pub)


def test_manifest_shape_validation(pub, priv):
    def signed(doc):
        raw = updater.canonical_manifest_bytes(doc)
        return raw, updater.sign_manifest_bytes(raw, priv)
    good = {"version": "1.4.0", "windows": {"url": "https://x.example/a.exe", "sha256": "a" * 64, "size": 10}}
    updater.verify_manifest(*signed(good), pub)
    for bad in ({"version": "banana"},
                dict(good, windows={"url": "http://x.example/a.exe", "sha256": "a" * 64, "size": 10}),
                dict(good, windows={"url": "https://x.example/a.exe", "sha256": "zz", "size": 10}),
                dict(good, windows={"url": "https://x.example/a.exe", "sha256": "a" * 64, "size": 0})):
        with pytest.raises(UpdateError):
            updater.verify_manifest(*signed(bad), pub)


# ------------------------------------------------------------------ semver

def test_semver_compare():
    assert updater.is_newer("1.4.0", "1.3.0")
    assert updater.is_newer("1.10.0", "1.9.9")
    assert updater.is_newer("2.0.0", "1.99.99")
    assert not updater.is_newer("1.3.0", "1.3.0")
    assert not updater.is_newer("1.2.9", "1.3.0")
    assert updater.is_newer("1.3.0", "1.3.0-rc.1")             # release beats its prerelease
    assert updater.is_newer("1.3.0-rc.2", "1.3.0-rc.1")
    assert updater.is_newer("1.3.0-rc.10", "1.3.0-rc.9")       # numeric identifiers
    assert not updater.is_newer("1.3.0+build5", "1.3.0")
    assert updater.is_newer("v1.4.0", "1.3.0")
    with pytest.raises(ValueError):
        updater.parse_semver("1.4")


# ---------------------------------------------------------- check / state

def test_check_available_and_not(script, priv, pub, exe, tmp_path, net):
    _, net.manifest, net.sig = make_release(script, priv, exe, tmp_path)
    u = new_updater(tmp_path, pub)
    st = u.check()
    assert net.urls == [updater.DEFAULT_URL, updater.DEFAULT_URL + ".sig"]
    assert st["available"] and st["latest"] == "9.9.9" and st["current"] == "1.3.0"
    assert st["url"].endswith(".exe") and st["notes_url"] and st["error"] is None and st["checked_at"]
    u2 = new_updater(tmp_path, pub, current="9.9.9")
    assert u2.check()["available"] is False
    assert u2.summary()["latest"] == "9.9.9"


def test_check_failures_are_reported_not_raised(script, priv, pub, exe, tmp_path, net, monkeypatch):
    doc, net.manifest, net.sig = make_release(script, priv, exe, tmp_path)
    net.sig = updater.sign_manifest_bytes(net.manifest, Ed25519PrivateKey.generate())
    st = new_updater(tmp_path, pub).check()
    assert st["available"] is False and "signature" in st["error"]

    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(updater, "http_get", boom)
    st = new_updater(tmp_path, pub).check()
    assert st["available"] is False and "could not reach" in st["error"]


def test_disabled_and_channel_and_custom_url(script, priv, pub, exe, tmp_path, net):
    _, net.manifest, net.sig = make_release(script, priv, exe, tmp_path)
    off = new_updater(tmp_path, pub, {"updates.enabled": False})
    assert off.check()["available"] is False and net.urls == []
    beta = new_updater(tmp_path, pub, {"updates.channel": "beta"})
    assert beta.check()["available"] is False                  # manifest says stable
    custom = new_updater(tmp_path, pub, {"updates.url": "https://example.com/u/update.json"})
    assert custom.check()["available"] is True
    assert net.urls[-2:] == ["https://example.com/u/update.json", "https://example.com/u/update.json.sig"]


def test_redirect_and_scheme_rules():
    assert updater._url_allowed("https://github.com/x")
    assert updater._url_allowed("http://127.0.0.1:9/x")
    assert not updater._url_allowed("http://example.com/x")
    assert not updater._url_allowed("file:///etc/passwd")
    with pytest.raises(UpdateError):
        updater.http_get("ftp://example.com/x")


# ----------------------------------------------------- real download path

class _Resp:
    def __init__(self, data):
        self._d, self._i = data, 0

    def read(self, n=-1):
        chunk = self._d[self._i:self._i + n]
        self._i += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_download_enforces_sha_and_size(tmp_path, monkeypatch):
    data = b"abc" * 1000
    sha = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(updater, "_open", lambda url, t: _Resp(data))
    dest = tmp_path / "S.exe"
    updater.http_download("https://x.example/S.exe", dest, len(data), sha)
    assert dest.read_bytes() == data and not (tmp_path / "S.exe.part").exists()
    d2 = tmp_path / "bad.exe"
    with pytest.raises(UpdateError) as e:
        updater.http_download("https://x.example/S.exe", d2, len(data), "0" * 64)
    assert e.value.code == "bad_sha256" and not d2.exists() and not (tmp_path / "bad.exe.part").exists()
    with pytest.raises(UpdateError) as e:
        updater.http_download("https://x.example/S.exe", d2, len(data) - 1, sha)
    assert e.value.code == "bad_size"
    with pytest.raises(UpdateError) as e:
        updater.http_download("https://x.example/S.exe", d2, len(data) + 1, sha)
    assert e.value.code == "bad_size" and not d2.exists()


def test_updater_download_flow_and_bad_sha(script, priv, pub, exe, tmp_path, net, monkeypatch):
    _, net.manifest, net.sig = make_release(script, priv, exe, tmp_path)
    u = new_updater(tmp_path, pub)
    with pytest.raises(UpdateError) as e:
        u.download()
    assert e.value.code == "none_available"                    # nothing checked yet
    u.check()
    net.payload = exe.read_bytes()
    p = u.download()
    assert p == tmp_path / "data" / "updates" / "EmailBoss-9.9.9-Setup.exe" and p.read_bytes() == net.payload
    n_urls = len(net.urls)
    assert u.download() == p and len(net.urls) == n_urls       # verified copy on disk: no second download
    assert u.state()["downloaded"] is True


def test_updater_download_rejects_wrong_bytes(script, priv, pub, exe, tmp_path, monkeypatch):
    doc, raw, sig = make_release(script, priv, exe, tmp_path)
    n = Net(raw, sig)
    monkeypatch.setattr(updater, "http_get", n.get)
    monkeypatch.setattr(updater, "_open", lambda url, t: _Resp(b"X" * doc["windows"]["size"]))
    u = new_updater(tmp_path, pub)
    u.check()
    with pytest.raises(UpdateError) as e:
        u.download()
    assert e.value.code == "bad_sha256"
    assert not list((tmp_path / "data" / "updates").glob("*"))  # nothing left behind
    assert u.downloaded is None


def test_install_windows_only_and_reverifies(script, priv, pub, exe, tmp_path, net, monkeypatch):
    _, net.manifest, net.sig = make_release(script, priv, exe, tmp_path)
    net.payload = exe.read_bytes()
    u = new_updater(tmp_path, pub)
    u.check()
    p = u.download()
    monkeypatch.setattr(updater, "_is_windows", lambda: False)
    with pytest.raises(UpdateError) as e:
        u.install()
    assert e.value.code == "windows_only" and e.value.http_status == 400

    launched, exits = [], []
    monkeypatch.setattr(updater, "_is_windows", lambda: True)
    monkeypatch.setattr(updater.subprocess, "Popen", lambda cmd, **kw: launched.append((cmd, kw)))
    monkeypatch.setattr(u, "schedule_exit", lambda *a, **k: exits.append(1))
    p.write_bytes(p.read_bytes() + b"tampered")                 # changed on disk after download
    with pytest.raises(UpdateError) as e:
        u.install()
    assert e.value.code == "bad_sha256" and not launched and not p.exists()
    with pytest.raises(UpdateError) as e:
        u.install()
    assert e.value.code == "not_downloaded"
    p = u.download()
    out = u.install()
    assert out["ok"] and launched and exits == [1]
    cmd = launched[0][0]
    assert cmd[0] == str(p) and "/SILENT" in cmd and "/CLOSEAPPLICATIONS" in cmd


# ----------------------------------------------------------- endpoints

@pytest.fixture
def app_client(data_root, monkeypatch):
    from tests.conftest import make_config
    app = create_app(config=make_config(data_root))
    return app, app.test_client()


def test_endpoint_shapes_network_stubbed(app_client, script, priv, exe, tmp_path, net, monkeypatch):
    app, c = app_client
    # health never touches the network and carries update.available
    h = c.get("/api/health").json
    assert h["update"]["available"] is False and h["update"]["current"] == __version__
    assert net.urls == []
    st = c.get("/api/update").json
    assert set(st) >= {"current", "latest", "available", "url", "notes_url", "checked_at", "error"}
    assert st["current"] == __version__ and st["available"] is False and st["checked_at"] is None and net.urls == []

    _, net.manifest, net.sig = make_release(script, priv, exe, tmp_path)
    st = c.get("/api/update?refresh=1").json
    assert st["available"] is True and st["latest"] == "9.9.9" and st["url"].endswith("-Setup.exe")
    assert st["checked_at"] and st["error"] is None
    assert c.get("/api/health").json["update"]["available"] is True

    r = c.post("/api/update/install", json={})                  # before any download
    assert r.status_code in (400, 409) and r.json["ok"] is False
    net.payload = exe.read_bytes()
    r = c.post("/api/update/download", json={})
    assert r.status_code == 200 and r.json["ok"] and r.json["path"].endswith("EmailBoss-9.9.9-Setup.exe")
    assert Path(r.json["path"]).parent.name == "updates"
    if sys.platform != "win32":
        r = c.post("/api/update/install", json={})
        assert r.status_code == 400 and r.json["code"] == "windows_only"


def test_endpoints_when_update_server_fails(app_client, monkeypatch):
    app, c = app_client

    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(updater, "http_get", boom)
    st = c.get("/api/update?refresh=1").json
    assert st["available"] is False and st["error"]
    assert c.post("/api/update/download", json={}).status_code == 409


def test_update_routes_open_in_read_only_license_mode():
    assert any("/api/update".startswith(p) or p == "/api/update" for p in lc._READ_ONLY_EXEMPT_PREFIXES)


# -------------------------------------------------------- desktop helpers

def test_wait_for_http_counts_auth_refusal_as_alive(data_root):
    """Regression: the engine's /api/health answers 401 to a cookie-less probe;
    the desktop shell used to treat that as 'engine did not answer'."""
    import os
    from tests.conftest import make_config
    os.environ["PB_ENFORCE_AUTH"] = "1"
    try:
        app = create_app(config=make_config(data_root))
    finally:
        del os.environ["PB_ENFORCE_AUTH"]
    port = desktop.free_port()
    srv = desktop.serve_engine(app, "127.0.0.1", port)
    try:
        assert desktop.wait_for_http(f"http://127.0.0.1:{port}/api/health", timeout=3) is True
    finally:
        srv.shutdown()


def test_stable_port_reuse_and_fallback(tmp_path):
    from flask import Flask
    app = Flask("x")
    srv, port = desktop.serve_stable(app, "127.0.0.1", tmp_path)
    try:
        assert desktop.preferred_port(tmp_path) == port
    finally:
        srv.shutdown()
        srv.server_close()
    srv2, port2 = desktop.serve_stable(app, "127.0.0.1", tmp_path)       # same port again
    try:
        assert port2 == port
        srv3, port3 = desktop.serve_stable(app, "127.0.0.1", tmp_path)   # taken: falls back
        assert port3 != port
        srv3.shutdown()
        srv3.server_close()
    finally:
        srv2.shutdown()
        srv2.server_close()


def test_webview2_check_messages(monkeypatch):
    shown = []
    monkeypatch.setattr(desktop, "webview2_version", lambda: None)
    monkeypatch.setattr(desktop, "show_message", lambda t, x, offer_url=None: shown.append((t, x, offer_url)))
    monkeypatch.delenv("PB_SKIP_WEBVIEW2_CHECK", raising=False)
    assert desktop.ensure_webview2() is False
    assert shown and "WebView2" in shown[0][0] and shown[0][2] == desktop.WEBVIEW2_URL
    assert desktop.WEBVIEW2_URL.startswith("https://developer.microsoft.com/")
    monkeypatch.setattr(desktop, "webview2_version", lambda: "130.0.2849.80")
    assert desktop.ensure_webview2() is True
    monkeypatch.setattr(desktop, "webview2_version", lambda: None)
    monkeypatch.setenv("PB_SKIP_WEBVIEW2_CHECK", "1")
    assert desktop.ensure_webview2() is True
    if sys.platform != "win32":
        monkeypatch.undo()
        assert desktop.webview2_version() == "n/a"
