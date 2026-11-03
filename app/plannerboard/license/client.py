"""License client: signed key + signed activation, heartbeat, offline grace.

Token format (shared with license_server/licserver/keys.py):

    <base64url(payload_json)>.<base64url(ed25519_signature)>

    payload_json is canonical JSON: sort_keys=True, separators=(",", ":").
    The signature is over the exact payload bytes that were base64url-encoded,
    so verification decodes segment 1 and verifies the signature against those
    bytes (no re-serialization on the client).

License key payload:
    {"lid", "email", "plan", "seats", "features": [...], "issued", "expires"}
Activation payload:
    {"lid", "fingerprint", "issued", "valid_until", "plan", "features": [...]}

All timestamps are ISO 8601 UTC with a trailing Z ("2026-10-05T12:00:00Z").

State lives in app_meta through the two callables passed in:
    license.key            the license key string
    license.activation     the signed activation token
    license.last_heartbeat ISO timestamp of the last successful heartbeat
    license.clock_hwm      highest wall-clock time this client has observed
    license.revoked        non-empty reason string when the server said no
    license.device_secret  per-device secret from /v1/activate or /v1/trial
                           (proof of possession, see sign_request)

Proof of possession: heartbeat, deactivate, every /v1/ai/* call (and
/v1/activate when this device already holds a seat) carry
    ts    = unix seconds
    nonce = random, single use (the server remembers it for 600 s)
    proof = hex(HMAC-SHA256(device_key,
                f"{lid}|{fingerprint}|{ts}|{path}|{nonce}|{body_sha256}"))
with device_key = sha256(device_secret) as hex text (the value the server
stores, so it can recompute the MAC without ever holding the secret) and
body_sha256 the sha256 of the canonical JSON of the request body without
ts, nonce and proof. One proof therefore authorizes one request.
Limitation: the server's stored hash is the MAC key, so a leaked server
database can forge proofs; per-device Ed25519 keys are the upgrade path.

The license key and device secret are kept in the app's meta table (the
local database), not the keyring; see docs/PRIVACY.md.

Dependencies: stdlib + cryptography (Ed25519).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .fingerprint import machine_fingerprint

# PRODUCTION public key (pair generated 2026-11-03; the private half lives
# only on the license server, LIC_PRIVATE_KEY_PATH, never in this repo).
# To rotate: license_server/scripts/gen_keys.py, paste the new public PEM
# here, follow "Key rotation" in license_server/README.md.
DEFAULT_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEACY8p7b/50i2KwWCdfNDBfSWhKYOorQIZmZO5YFdE8BQ=
-----END PUBLIC KEY-----
"""

# Public half of license_server/keys/DEV_ONLY_private.pem. Trusted ONLY on
# the "dev" build channel (source checkouts, the test suite); the release
# scripts flip _channel.CHANNEL to "release", after which only the
# production key above is accepted.
DEV_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAThvAs8MHRRxs1qGYEV7alc+IhvBCtmfaqBu4GZjxbaQ=
-----END PUBLIC KEY-----
"""


def trusted_public_key_pem() -> str:
    from .._channel import CHANNEL
    return DEV_PUBLIC_KEY_PEM if CHANNEL == "dev" else DEFAULT_PUBLIC_KEY_PEM


GRACE_DAYS = 14
HEARTBEAT_INTERVAL = timedelta(hours=24)
HTTP_TIMEOUT = 10
STATUS_CACHE_SECONDS = 30

META_KEY = "license.key"
META_ACTIVATION = "license.activation"
META_LAST_HEARTBEAT = "license.last_heartbeat"
META_CLOCK_HWM = "license.clock_hwm"
META_REVOKED = "license.revoked"
META_DEVICE_SECRET = "license.device_secret"

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Write paths that must keep working in read-only mode so the user can fix it.
_READ_ONLY_EXEMPT_PREFIXES = ("/api/license", "/api/setup", "/api/health", "/api/restart")


class LicenseError(Exception):
    """Raised for malformed or badly signed tokens and failed server calls."""

    def __init__(self, message: str, code: str = "invalid", http_status: int = 0):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


# ---------------------------------------------------------------- helpers

def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        s2 = s.strip()
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def load_public_key(pem: str) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(pem.encode("utf-8"))
    if not isinstance(key, Ed25519PublicKey):
        raise LicenseError("public key is not Ed25519", "config")
    return key


def verify_token(token: str, public_key: Ed25519PublicKey) -> dict:
    """Verify `<b64u(payload)>.<b64u(sig)>` and return the payload dict."""
    if not isinstance(token, str):
        raise LicenseError("token must be a string")
    token = token.strip().replace("\n", "").replace(" ", "")
    if token.count(".") != 1:
        raise LicenseError("malformed token: expected payload.sig")
    p_b64, s_b64 = token.split(".", 1)
    try:
        payload_bytes = _b64u_decode(p_b64)
        sig = _b64u_decode(s_b64)
    except (ValueError, TypeError) as exc:
        raise LicenseError(f"malformed token: {exc}") from exc
    try:
        public_key.verify(sig, payload_bytes)
    except InvalidSignature as exc:
        raise LicenseError("bad signature") from exc
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LicenseError("payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise LicenseError("payload is not an object")
    return payload


# ---------------------------------------------------------------- client

class LicenseClient:
    """See module docstring for the storage and token contracts.

    `transport` is an optional callable `(url, body_dict, timeout) -> (status, dict)`
    used by tests to stand in for HTTP.
    """

    def __init__(
        self,
        cfg: dict | None,
        store_meta_get: Callable[..., Any],
        store_meta_set: Callable[[str, Any], Any],
        server_url: str | None = None,
        public_key_pem: str | None = None,
        *,
        app_version: str = "",
        transport: Callable[..., tuple[int, dict]] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.cfg = cfg or {}
        lic_cfg = self.cfg.get("license", {}) if isinstance(self.cfg, dict) else {}
        self._meta_get = store_meta_get
        self._meta_set = store_meta_set
        self.server_url = (server_url or lic_cfg.get("server_url") or "").rstrip("/")
        self.public_key = load_public_key(public_key_pem or trusted_public_key_pem())
        self.app_version = app_version or str(self.cfg.get("version", "") or "")
        self._transport = transport or self._http_post
        self._now = now or utcnow
        self._lock = threading.RLock()
        self._status_cache: tuple[float, dict] | None = None
        # A key placed in config but never activated gets copied into meta so
        # there is one place to look.
        if lic_cfg.get("key") and not self._meta_get(META_KEY, None):
            self._meta_set(META_KEY, lic_cfg["key"].strip())

    # -------------------------------------------------- key handling

    def parse_key(self, key: str) -> dict:
        """Verify the Ed25519 signature on a license key and return its payload."""
        payload = verify_token(key, self.public_key)
        for field in ("lid", "plan", "expires"):
            if field not in payload:
                raise LicenseError(f"license payload missing {field}")
        payload.setdefault("features", [])
        payload.setdefault("seats", 1)
        payload.setdefault("email", "")
        return payload

    def parse_activation(self, token: str) -> dict:
        payload = verify_token(token, self.public_key)
        for field in ("lid", "fingerprint", "valid_until"):
            if field not in payload:
                raise LicenseError(f"activation payload missing {field}")
        return payload

    # -------------------------------------------------- state access

    @property
    def fingerprint(self) -> str:
        return machine_fingerprint()

    def stored_key(self) -> str:
        return (self._meta_get(META_KEY, None) or "").strip()

    def stored_activation(self) -> str:
        return (self._meta_get(META_ACTIVATION, None) or "").strip()

    def _invalidate(self) -> None:
        self._status_cache = None

    def _effective_now(self) -> datetime:
        """Wall clock, floored at the highest time we have ever seen.

        Rolling the system clock back cannot extend grace: the high-water mark
        only moves forward, and status math uses max(now, hwm).
        """
        now = self._now()
        hwm = parse_iso(self._meta_get(META_CLOCK_HWM, None))
        if hwm and hwm > now:
            return hwm
        if not hwm or now > hwm:
            try:
                self._meta_set(META_CLOCK_HWM, iso(now))
            except Exception:  # meta store is best effort for the hwm
                pass
        return now

    # -------------------------------------------------- server calls

    def _http_post(self, url: str, body: dict, timeout: int = HTTP_TIMEOUT) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": f"EmailBoss/{self.app_version or 'dev'}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except ValueError:
                parsed = {"detail": raw[:200]}
            return exc.code, parsed if isinstance(parsed, dict) else {"detail": parsed}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LicenseError(f"license server unreachable: {exc}", "offline") from exc

    def _post(self, path: str, body: dict) -> dict:
        if not self.server_url:
            raise LicenseError("license.server_url is not configured", "config")
        status, data = self._transport(f"{self.server_url}{path}", body, HTTP_TIMEOUT)
        if status >= 400:
            # Server errors arrive as {"detail": {"code": ..., "message": ...}}
            # (FastAPI HTTPException with a dict detail) or {"detail": "text"}.
            detail = data.get("detail") if isinstance(data, dict) else None
            code, msg = "server", f"HTTP {status}"
            if isinstance(detail, dict):
                code = str(detail.get("code") or code)
                msg = str(detail.get("message") or msg)
            elif detail:
                msg = str(detail)
            raise LicenseError(msg, code, http_status=status)
        return data if isinstance(data, dict) else {}

    def _store_activation(self, data: dict) -> dict:
        token = data.get("activation")
        if not token:
            raise LicenseError("server response had no activation token", "server")
        payload = self.parse_activation(token)
        if payload.get("fingerprint") != self.fingerprint:
            raise LicenseError("activation is for a different machine", "fingerprint")
        with self._lock:
            if data.get("device_secret"):
                self._meta_set(META_DEVICE_SECRET, str(data["device_secret"]))
            self._meta_set(META_ACTIVATION, token)
            self._meta_set(META_LAST_HEARTBEAT, iso(self._now()))
            self._meta_set(META_REVOKED, "")
            self._invalidate()
        return payload

    # -------------------------------------------------- proof of possession

    def device_secret(self) -> str:
        return (self._meta_get(META_DEVICE_SECRET, None) or "").strip()

    def _lid(self) -> str:
        key = self.stored_key()
        if key:
            try:
                return str(self.parse_key(key).get("lid") or "")
            except LicenseError:
                pass
        act = self.stored_activation()
        if act:
            try:
                return str(self.parse_activation(act).get("lid") or "")
            except LicenseError:
                pass
        return ""

    @staticmethod
    def body_digest(body: dict) -> str:
        """sha256 of the canonical JSON of ``body`` minus ts, nonce and proof
        (must match licserver.licenses.body_sha256)."""
        d = {k: v for k, v in (body or {}).items() if k not in ("ts", "nonce", "proof")}
        raw = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _proof(self, path: str, lid: str, secret: str, body: dict | None = None) -> dict:
        ts = int(time.time())   # real wall clock: the server checks it within 300 s
        nonce = secrets.token_hex(12)
        device_key = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        msg = f"{lid}|{self.fingerprint}|{ts}|{path}|{nonce}|{self.body_digest(body or {})}".encode("utf-8")
        return {"ts": ts, "nonce": nonce, "proof": hmac.new(device_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()}

    def sign_request(self, path: str, body: dict | None = None) -> dict:
        """{ts, nonce, proof} for a device call to `path` (e.g. "/v1/ai/complete").

        ``body`` is the dict that will be sent (every field except ts, nonce
        and proof; the proof covers its hash, so it must not change after
        signing). Omitted, it is the plain identity body {lid, fingerprint,
        activation}, which is all /v1/ai/balance sends.

        An activation from before 1.3.0 has no device secret yet; the first
        call upgrades it with one forced heartbeat (key-based, once). Returns
        {} when no secret can be had (the server then answers proof_invalid)."""
        secret = self.device_secret()
        if not secret and self.stored_key():
            try:
                self.heartbeat(force=True)
            except Exception:  # noqa: BLE001 - signing must never raise into the caller
                pass
            secret = self.device_secret()
        lid = self._lid()
        if not secret or not lid:
            return {}
        if body is None:
            body = {"lid": lid, "fingerprint": self.fingerprint, "activation": self.stored_activation()}
        return self._proof(path, lid, secret, body)

    def activate(self, key: str, use_proof: bool = True) -> dict:
        """Verify the key locally, then POST /v1/activate and store the activation."""
        key = (key or "").strip()
        payload = self.parse_key(key)
        body = {"key": key, "fingerprint": self.fingerprint, "app_version": self.app_version}
        secret = self.device_secret()
        if secret and use_proof and self._lid() == payload["lid"]:
            # This device may already hold the seat; the server then wants its proof.
            body.update(self._proof("/v1/activate", payload["lid"], secret, body))
        data = self._post("/v1/activate", body)
        with self._lock:
            self._meta_set(META_KEY, key)
        act = self._store_activation(data)
        return {"license": payload, "activation": act}

    def start_trial(self, email: str) -> dict:
        """POST /v1/trial: the server creates the account and a trial license,
        binds this machine and returns the activation and device secret. The
        license KEY is only emailed (with a confirmation link that funds the
        AI trial credits), so this stores an activation and no key; status
        works from the signed activation until the key is pasted in."""
        email = (email or "").strip()
        if not email or "@" not in email:
            raise LicenseError("Enter a valid email address.", "bad_email", http_status=400)
        data = self._post("/v1/trial", {
            "email": email, "fingerprint": self.fingerprint, "app_version": self.app_version,
        })
        act = self._store_activation(data)
        return {"license": data.get("license") or {}, "activation": act}

    def heartbeat(self, force: bool = False) -> dict:
        """Daily refresh of the activation. Returns {"ok", "skipped"?, "error"?}.

        Network failures are swallowed (offline grace covers them); a server
        rejection (revoked, expired, seat removed) is recorded so status() can
        report it, and a 404 for a deactivated seat clears the activation.
        """
        key = self.stored_key()
        lid = ""
        if key:
            try:
                lid = str(self.parse_key(key)["lid"])
            except LicenseError as exc:
                return {"ok": False, "error": str(exc)}
        else:
            lid = self._lid()      # trial: activation only, the key is in the mailbox
            if not lid:
                return {"ok": False, "skipped": "no_key"}
        last = parse_iso(self._meta_get(META_LAST_HEARTBEAT, None))
        if not force and last and self._now() - last < HEARTBEAT_INTERVAL:
            return {"ok": True, "skipped": "recent"}
        body = {"lid": lid, "fingerprint": self.fingerprint, "app_version": self.app_version}
        secret = self.device_secret()
        if secret:
            body.update(self._proof("/v1/heartbeat", lid, secret, body))
        elif key:
            body["key"] = key   # pre-1.3.0 activation: the server upgrades it once and returns a secret
        else:
            return {"ok": False, "skipped": "no_secret"}
        try:
            data = self._post("/v1/heartbeat", body)
        except LicenseError as exc:
            if exc.code == "offline" or exc.http_status >= 500:
                return {"ok": False, "offline": True, "error": str(exc)}
            if exc.code == "proof_invalid" and key:
                # Lost or rotated secret. The server will not hand a bound
                # device a new secret on the key alone (device_bound): the
                # user has to release this device in the portal first. If they
                # already did, this succeeds.
                try:
                    self.activate(key, use_proof=False)
                    return {"ok": True, "reactivated": True}
                except LicenseError as exc2:
                    exc = exc2
            with self._lock:
                self._meta_set(META_REVOKED, f"{exc.code}: {exc}")
                if exc.http_status in (403, 404, 410):
                    self._meta_set(META_ACTIVATION, "")
                self._invalidate()
            return {"ok": False, "error": str(exc), "code": exc.code}
        try:
            self._store_activation(data)
        except LicenseError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def deactivate(self) -> dict:
        """Release this machine's seat on the server and forget local state."""
        key = self.stored_key()
        result: dict = {"ok": True}
        lid = self._lid()
        if lid:
            try:
                body = {"lid": lid, "fingerprint": self.fingerprint}
                secret = self.device_secret()
                body.update(self._proof("/v1/deactivate", lid, secret, body) if secret else {"key": key})
                self._post("/v1/deactivate", body)
            except LicenseError as exc:
                # Local state is cleared regardless; report the server side.
                result = {"ok": False, "error": str(exc), "code": exc.code}
        with self._lock:
            self._meta_set(META_ACTIVATION, "")
            self._meta_set(META_KEY, "")
            self._meta_set(META_LAST_HEARTBEAT, "")
            self._meta_set(META_REVOKED, "")
            self._meta_set(META_DEVICE_SECRET, "")
            self._invalidate()
        return result

    # -------------------------------------------------- status

    _dev_warned = False

    @classmethod
    def dev_mode(cls) -> bool:
        """PB_LICENSE_DEV=1 skips license checks, on the dev build channel
        only. A release build ignores it (and says so once in the log)."""
        if os.environ.get("PB_LICENSE_DEV", "") != "1":
            return False
        from .._channel import CHANNEL
        if CHANNEL == "dev":
            return True
        if not cls._dev_warned:
            cls._dev_warned = True
            import logging
            logging.getLogger("plannerboard.license").warning(
                "PB_LICENSE_DEV is set but this is a release build; license checks stay on")
        return False

    def _trial_fields(self, st: dict) -> dict:
        is_trial = st.get("plan") == "trial"
        days_left = 0
        exp = parse_iso(st.get("expires"))
        if is_trial and exp:
            delta = exp - self._effective_now()
            days_left = max(0, delta.days + (1 if delta.seconds else 0))
        st["trial"] = is_trial
        st["days_left"] = days_left
        return st

    def status(self, use_cache: bool = True) -> dict:
        if use_cache and self._status_cache and time.monotonic() - self._status_cache[0] < STATUS_CACHE_SECONDS:
            return dict(self._status_cache[1])
        st = self._trial_fields(self._compute_status())
        self._status_cache = (time.monotonic(), st)
        return dict(st)

    def _base(self, status: str, message: str, **extra: Any) -> dict:
        out = {
            "status": status, "plan": "", "email": "", "expires": "", "valid_until": "",
            "grace_days_left": 0, "features": [], "seats": 0, "message": message,
            "read_only": status not in ("ok", "grace", "dev"),
            "fingerprint": self.fingerprint, "server_url": self.server_url,
        }
        out.update(extra)
        return out

    def _license_from_activation(self) -> Optional[dict]:
        """A trial started in the app has a signed activation but no key (the
        key is only emailed). Its payload carries what status needs."""
        token = self.stored_activation()
        if not token:
            return None
        try:
            act = self.parse_activation(token)
        except LicenseError:
            return None
        if not act.get("expires"):
            return None
        return {"lid": act["lid"], "plan": act.get("plan", ""), "expires": act["expires"], "email": "",
                "features": list(act.get("features") or []), "seats": 1, "issued": act.get("issued", "")}

    def _compute_status(self) -> dict:
        if self.dev_mode():
            return self._base("dev", "Developer mode: license checks skipped (PB_LICENSE_DEV=1).",
                              plan="dev", features=["*"], seats=1)
        key = self.stored_key()
        if key:
            try:
                lic = self.parse_key(key)
            except LicenseError as exc:
                return self._base("invalid", f"License key rejected: {exc}")
        else:
            lic = self._license_from_activation()
            if lic is None:
                return self._base("unlicensed", "No license key. Enter one in Settings or start a trial.")

        now = self._effective_now()
        # Clock-rollback floor that cannot be deleted without losing the
        # license itself: nothing is ever "before" the key was issued, and an
        # activation (checked below) floors it again at ITS issue time.
        key_issued = parse_iso(lic.get("issued"))
        if key_issued and key_issued > now:
            now = key_issued
        expires = parse_iso(lic.get("expires"))
        expires_str = lic.get("expires", "")
        token = self.stored_activation()
        # A renewal re-signs the key on the server, but this machine may still
        # hold the old key string. The signed activation carries the
        # server-authoritative expiry (1.1.0+), so the later of the two wins.
        act_pre = None
        if token:
            try:
                act_pre = self.parse_activation(token)
            except LicenseError:
                act_pre = None
        if act_pre and act_pre.get("lid") == lic.get("lid"):
            act_exp = parse_iso(act_pre.get("expires"))
            if act_exp and (not expires or act_exp > expires):
                expires, expires_str = act_exp, str(act_pre.get("expires"))
            act_issued = parse_iso(act_pre.get("issued"))
            if act_issued and act_issued > now:
                now = act_issued
        common = dict(plan=lic.get("plan", ""), email=lic.get("email", ""), expires=expires_str,
                      features=list(lic.get("features") or []), seats=int(lic.get("seats") or 1), lid=lic.get("lid"))
        if expires and now > expires:
            return self._base("expired", f"License expired {expires_str}. Renew it in the portal.", **common)

        revoked = self._meta_get(META_REVOKED, None) or ""
        if not token:
            msg = f"License was rejected by the server ({revoked}). Re-activate." if revoked else \
                "License key saved but this machine is not activated yet."
            return self._base("invalid" if revoked else "unlicensed", msg, **common)
        try:
            act = self.parse_activation(token)
        except LicenseError as exc:
            return self._base("invalid", f"Stored activation rejected: {exc}", **common)
        if act.get("fingerprint") != self.fingerprint:
            return self._base("invalid", "Activation belongs to a different machine. Re-activate here.", **common)
        if act.get("lid") != lic.get("lid"):
            return self._base("invalid", "Activation does not match the stored license key. Re-activate.", **common)

        valid_until = parse_iso(act.get("valid_until"))
        if not valid_until:
            return self._base("invalid", "Activation has no valid_until.", **common)
        common["valid_until"] = act.get("valid_until", "")
        if act.get("features"):
            common["features"] = list(act["features"])
        if act.get("plan"):
            common["plan"] = act["plan"]

        if revoked:
            return self._base("invalid", f"Server rejected the last heartbeat ({revoked}).", **common)
        if now <= valid_until:
            return self._base("ok", "Licensed.", **common)
        grace_end = valid_until + timedelta(days=GRACE_DAYS)
        if now <= grace_end:
            days_left = max(0, (grace_end - now).days + (1 if (grace_end - now).seconds else 0))
            return self._base("grace", f"Could not reach the license server since {act.get('valid_until')}. "
                                       f"{days_left} day(s) of offline grace left.", grace_days_left=days_left, **common)
        return self._base("expired", "Offline grace period is over. Connect to the internet and refresh the license.",
                          **common)


# ---------------------------------------------------------------- flask glue

def client_from_app(app) -> LicenseClient:
    """Build (once) a LicenseClient from flask app.extensions['pb'] = {store, cfg}."""
    pb = app.extensions.setdefault("pb", {})
    client = pb.get("license")
    if client is None:
        store = pb.get("store")
        cfg = pb.get("cfg") or {}
        if store is None:
            raise RuntimeError("app.extensions['pb']['store'] is not set")
        client = LicenseClient(
            cfg, store.meta_get, store.meta_set,
            server_url=(cfg.get("license") or {}).get("server_url"),
            app_version=str(pb.get("version") or getattr(app, "version", "") or ""),
        )
        pb["license"] = client
    return client


def middleware(app) -> None:
    """Register a before_request hook: 402 JSON on write methods while read-only."""
    from flask import jsonify, request

    @app.before_request
    def _license_gate():  # pragma: no cover - exercised through tests via the client
        if request.method not in _WRITE_METHODS:
            return None
        path = request.path or ""
        if not path.startswith("/api/") or path.startswith(_READ_ONLY_EXEMPT_PREFIXES):
            return None
        try:
            st = client_from_app(app).status()
        except Exception as exc:  # misconfigured app: do not brick reads, do block writes
            st = {"status": "invalid", "read_only": True, "message": str(exc)}
        if st.get("read_only"):
            resp = jsonify({"error": "license_required", "license": st,
                            "message": st.get("message", "License required for changes.")})
            resp.status_code = 402
            return resp
        return None
