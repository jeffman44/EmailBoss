"""
Signed update check.

The release workflow publishes two assets next to the installer:

    update.json       canonical JSON (sorted keys, no spaces), exactly the bytes signed
    update.json.sig   base64url Ed25519 signature over those bytes

The app fetches both from the GitHub "latest release" redirect, verifies the
signature with the release public key (license.client.release_public_key_pem,
by default the production license key), compares semver with __version__ and
reports. Nothing is ever installed without the user asking: download and
install are separate POSTs, the installer's sha256 and size must match the
signed manifest, and install is Windows only.

Manifest:
    {"version": "1.4.0", "published": "2026-11-03T12:00:00Z", "channel": "stable",
     "windows": {"url": "https://.../EmailBoss-1.4.0-Setup.exe", "sha256": "<hex>", "size": 123},
     "notes_url": "https://github.com/.../releases/tag/v1.4.0"}

Config (plannerboard.json): updates.enabled (default true), updates.channel
(default "stable"; a manifest for another channel is ignored), updates.url
(default DEFAULT_URL). Only https URLs are followed (plain http to a loopback
host is allowed so a test server can stand in).

Dependencies: stdlib + cryptography.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature

from . import __version__

DEFAULT_URL = "https://github.com/jeffman44/emailboss/releases/latest/download/update.json"
HTTP_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 30          # per socket read while streaming the installer
MAX_MANIFEST_BYTES = 64 * 1024
MAX_INSTALLER_BYTES = 1024 * 1024 * 1024
CHECK_INTERVAL_S = 24 * 3600
INSTALL_ARGS = ["/SILENT", "/CLOSEAPPLICATIONS", "/RELAUNCH=1"]

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(Exception):
    def __init__(self, message: str, code: str = "error", http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


# ----------------------------------------------------------------- manifest

def canonical_manifest_bytes(manifest: dict) -> bytes:
    """The exact bytes that are written to update.json and signed."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_manifest_bytes(raw: bytes, private_key) -> str:
    return _b64u_encode(private_key.sign(raw))


def verify_manifest(raw: bytes, sig_b64u: Any, public_key) -> dict:
    """Check the signature over the raw bytes, then parse and validate shape."""
    if isinstance(sig_b64u, (bytes, bytearray)):
        sig_b64u = bytes(sig_b64u).decode("ascii", "ignore")
    try:
        sig = _b64u_decode(str(sig_b64u or ""))
    except (ValueError, TypeError) as e:
        raise UpdateError(f"malformed signature: {e}", "bad_signature") from e
    try:
        public_key.verify(sig, raw)
    except InvalidSignature as e:
        raise UpdateError("update manifest signature is not valid", "bad_signature") from e
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise UpdateError("update manifest is not JSON", "bad_manifest") from e
    return validate_manifest(doc)


def validate_manifest(doc: Any) -> dict:
    if not isinstance(doc, dict):
        raise UpdateError("update manifest is not an object", "bad_manifest")
    ver = doc.get("version")
    try:
        parse_semver(ver)
    except ValueError as e:
        raise UpdateError(f"update manifest version: {e}", "bad_manifest") from e
    win = doc.get("windows")
    if win is not None:
        if not isinstance(win, dict):
            raise UpdateError("update manifest windows entry is not an object", "bad_manifest")
        if not _url_allowed(str(win.get("url") or "")):
            raise UpdateError("update manifest installer url must be https", "bad_manifest")
        if not _SHA256.match(str(win.get("sha256") or "").lower()):
            raise UpdateError("update manifest sha256 is not a hex digest", "bad_manifest")
        size = win.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or not (0 < size <= MAX_INSTALLER_BYTES):
            raise UpdateError("update manifest size is not valid", "bad_manifest")
    return doc


# ------------------------------------------------------------------- semver

def parse_semver(v: Any) -> tuple:
    """(major, minor, patch, prerelease-key). Releases sort above their own
    prereleases; build metadata is ignored."""
    m = _SEMVER.match(str(v or "").strip())
    if not m:
        raise ValueError(f"not a semantic version: {v!r}")
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre = m.group(4)
    if pre is None:
        return (major, minor, patch, (1,))
    parts = []
    for ident in pre.split("."):
        parts.append((0, int(ident), "") if ident.isdigit() else (1, 0, ident))
    return (major, minor, patch, (0, tuple(parts)))


def is_newer(latest: str, current: str) -> bool:
    return parse_semver(latest) > parse_semver(current)


# --------------------------------------------------------------------- http

def _url_allowed(url: str) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if u.scheme == "https" and u.hostname:
        return True
    return u.scheme == "http" and (u.hostname or "") in _LOOPBACK


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_allowed(newurl):
            raise urllib.error.URLError(f"redirect to a non-https address refused: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float):
    if not _url_allowed(url):
        raise UpdateError("update address must be https", "bad_url")
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": f"EmailBoss/{__version__}"})
    return opener.open(req, timeout=timeout)


def http_get(url: str, timeout: float = HTTP_TIMEOUT, max_bytes: int = MAX_MANIFEST_BYTES) -> bytes:
    """GET with redirects (https only). Tests replace this function."""
    with _open(url, timeout) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UpdateError("response too large", "too_large")
    return data


def http_download(url: str, dest: Path, expect_size: int, expect_sha256: str,
                  timeout: float = DOWNLOAD_TIMEOUT) -> None:
    """Stream url to dest (via dest.part), enforcing size and sha256. Tests
    replace this function."""
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    n = 0
    try:
        with _open(url, timeout) as r, open(part, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                n += len(chunk)
                if n > expect_size:
                    raise UpdateError("installer is larger than the signed size", "bad_size")
                h.update(chunk)
                f.write(chunk)
        if n != expect_size:
            raise UpdateError(f"installer size {n} does not match the signed size {expect_size}", "bad_size")
        if h.hexdigest() != expect_sha256.lower():
            raise UpdateError("installer sha256 does not match the signed manifest", "bad_sha256")
        os.replace(part, dest)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_windows() -> bool:
    return sys.platform == "win32"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ updater

class Updater:
    """One per app. `cfg_get(dotted, default)` reads the config document,
    `data_root` is where updates/ lives, `public_key` verifies manifests."""

    def __init__(self, cfg_get, data_root, public_key=None, current: str = __version__):
        self._cfg_get = cfg_get
        self.data_root = Path(data_root)
        self._public_key = public_key
        self.current = current
        self._lock = threading.Lock()
        self._dl_lock = threading.Lock()
        self.checked_at: Optional[str] = None
        self.checked_epoch = 0.0
        self.manifest: Optional[dict] = None
        self.error: Optional[str] = None
        self.downloaded: Optional[Path] = None
        self.exit_hook = None            # desktop.run sets: closes the window, engine follows

    # -- config -------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self._cfg_get("updates.enabled", True))

    @property
    def channel(self) -> str:
        return str(self._cfg_get("updates.channel", "stable") or "stable")

    @property
    def url(self) -> str:
        return str(self._cfg_get("updates.url", "") or "").strip() or DEFAULT_URL

    @property
    def updates_dir(self) -> Path:
        return self.data_root / "updates"

    def key(self):
        if self._public_key is None:
            from .license.client import load_public_key, release_public_key_pem
            self._public_key = load_public_key(release_public_key_pem())
        return self._public_key

    # -- check --------------------------------------------------------------
    def check(self) -> dict:
        """Fetch + verify the manifest. Never raises; the outcome is in
        state() (error set, available false)."""
        with self._lock:
            self.checked_at = _now_iso()
            self.checked_epoch = time.time()
            if not self.enabled:
                self.manifest, self.error = None, None
                return self.state()
            try:
                base = self.url
                raw = http_get(base)
                sig = http_get(base + ".sig", max_bytes=1024)
                doc = verify_manifest(raw, sig, self.key())
                chan = str(doc.get("channel") or "stable")
                if chan != self.channel:
                    self.manifest, self.error = None, None
                else:
                    self.manifest, self.error = doc, None
            except UpdateError as e:
                self.manifest, self.error = None, str(e)
            except Exception as e:  # noqa: BLE001 - network is best effort
                self.manifest, self.error = None, f"could not reach the update server: {type(e).__name__}"
            return self.state()

    def latest_version(self) -> str:
        return str((self.manifest or {}).get("version") or self.current)

    def _available(self) -> bool:
        m = self.manifest
        if not m:
            return False
        try:
            return is_newer(m["version"], self.current)
        except ValueError:
            return False

    def state(self) -> dict:
        m = self.manifest or {}
        avail = self._available()
        return {
            "ok": True,
            "enabled": self.enabled,
            "channel": self.channel,
            "current": self.current,
            "latest": m.get("version") or self.current,
            "available": avail,
            "url": (m.get("windows") or {}).get("url") if avail else None,
            "notes_url": m.get("notes_url") if avail else None,
            "published": m.get("published") if avail else None,
            "can_install": _is_windows(),
            "downloaded": bool(self.downloaded and self.downloaded.is_file()) if avail else False,
            "checked_at": self.checked_at,
            "error": self.error,
        }

    def summary(self) -> dict:
        """Health payload: cached only, never touches the network."""
        s = self.state()
        return {k: s[k] for k in ("available", "latest", "current", "checked_at", "error", "notes_url", "can_install")}

    # -- download / install ---------------------------------------------------
    def _target(self) -> tuple:
        if not self.enabled:
            raise UpdateError("update checks are turned off", "disabled", 409)
        if not self._available():
            raise UpdateError("no update is available", "none_available", 409)
        win = (self.manifest or {}).get("windows")
        if not win:
            raise UpdateError("this release has no Windows installer", "no_installer", 409)
        ver = str(self.manifest["version"])
        name = "EmailBoss-" + re.sub(r"[^0-9A-Za-z.+-]", "_", ver) + "-Setup.exe"
        return win, self.updates_dir / name

    def download(self) -> Path:
        win, dest = self._target()
        if not self._dl_lock.acquire(blocking=False):
            raise UpdateError("a download is already running", "busy", 409)
        try:
            self.updates_dir.mkdir(parents=True, exist_ok=True)
            if dest.is_file() and dest.stat().st_size == win["size"] and file_sha256(dest) == win["sha256"].lower():
                self.downloaded = dest
                return dest
            try:
                dest.unlink()
            except OSError:
                pass
            try:
                http_download(win["url"], dest, win["size"], win["sha256"])
            except UpdateError:
                raise
            except Exception as e:  # noqa: BLE001
                raise UpdateError(f"download failed: {type(e).__name__}", "download_failed", 502) from e
            self.downloaded = dest
            self._prune(keep=dest)
            return dest
        finally:
            self._dl_lock.release()

    def _prune(self, keep: Path) -> None:
        for f in self.updates_dir.glob("EmailBoss-*-Setup.exe*"):
            if f != keep:
                try:
                    f.unlink()
                except OSError:
                    pass

    def install(self) -> dict:
        """Launch the verified installer and ask the app to exit. Windows only."""
        if not _is_windows():
            raise UpdateError("installing updates from inside the app is only supported on Windows",
                              "windows_only", 400)
        win, dest = self._target()
        if self.downloaded != dest or not dest.is_file():
            raise UpdateError("download the update first", "not_downloaded", 409)
        # Re-verify at the last moment: the file sat on disk since the download.
        if dest.stat().st_size != win["size"] or file_sha256(dest) != win["sha256"].lower():
            try:
                dest.unlink()
            except OSError:
                pass
            self.downloaded = None
            raise UpdateError("the downloaded installer no longer matches the signed manifest; download it again",
                              "bad_sha256", 409)
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
            flags |= getattr(subprocess, name, 0)
        subprocess.Popen([str(dest)] + INSTALL_ARGS, close_fds=True, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.schedule_exit()
        return {"ok": True, "installing": True, "path": str(dest)}

    def schedule_exit(self, delay: float = 1.0) -> None:
        """Answer the HTTP request first, then shut down cleanly: the desktop
        hook closes the window (the engine, tray and mail threads follow in
        desktop.run's cleanup); without a window the process exits itself."""
        def go():
            time.sleep(delay)
            hook = self.exit_hook
            if hook:
                try:
                    hook()
                except Exception as e:  # noqa: BLE001
                    print(f"[update] exit hook failed: {e}")
                time.sleep(8)           # window did not close in time: do not block the installer
            try:
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                pass
            os._exit(0)
        threading.Thread(target=go, daemon=True, name="update-exit").start()


# ------------------------------------------------------------------ wiring

def updater_from_app(app) -> Updater:
    u = app.extensions.get("pb_updater")
    if u is None:
        from .api._common import cfg_get
        ext = app.extensions["pb"]
        paths = ext.get("paths") if isinstance(ext, dict) else getattr(ext, "paths", None)
        root = getattr(paths, "data_root", None) or os.environ.get("PB_DATA_ROOT") or (Path.home() / "EmailBoss")

        def _get(dotted, default=None):
            with app.app_context():
                return cfg_get(dotted, default)
        u = Updater(_get, root)

        def _exit():
            hook = app.extensions.get("pb_exit_hook")
            if hook:
                hook()
            else:                       # server mode: no window to close
                runner = getattr(ext, "mail_runner", None)
                if runner is not None:
                    runner.stop_all()
        u.exit_hook = _exit
        app.extensions["pb_updater"] = u
    return u


def start_update_thread(rt, app) -> bool:
    """Check once shortly after launch, then whenever 24 h have passed."""
    u = updater_from_app(app)
    if not u.enabled:
        print("[update] checks are turned off (updates.enabled = false)")
        return False
    rt.register_watcher("update_check", 24 * 60, note="signed release manifest")

    def loop():
        time.sleep(60)
        while True:
            try:
                if u.enabled and time.time() - u.checked_epoch >= CHECK_INTERVAL_S:
                    u.check()
                note = u.error or ("update available: " + str(u.latest_version()) if u._available() else "up to date")
                rt.heartbeat("update_check", note=note)
            except Exception as e:  # noqa: BLE001
                print(f"[update] check failed: {e}")
            time.sleep(3600)

    threading.Thread(target=loop, daemon=True, name="update-check").start()
    return True

