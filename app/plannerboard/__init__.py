"""
Email Boss: app factory.

    from plannerboard import create_app
    app = create_app()            # config from $PB_CONFIG / <data_root>/plannerboard.json
    app.run(host=app.extensions["pb"].config.bind, port=...)

The Runtime object on `app.extensions["pb"]` is the one place blueprints and
background threads reach for config, the store, the workflow document and the
effective profession.
"""

import importlib
import os
import sys
import threading
import time
from pathlib import Path

from flask import redirect, Flask, Response, jsonify, request, send_file

from . import paths as _paths
from . import profession as _profession
from . import workflows as wfm
from .business_hours import BusinessHours
from .config import Config
from .db import Store, StoreContext

__version__ = "1.2.0"


class Runtime:
    """Everything the request handlers and loops share. One per process."""

    def __init__(self, config):
        self.config = config
        self.started_at = time.time()
        self.paths = config.paths
        self._prof_cache = None
        self._lock = threading.RLock()
        self.watchers = {}          # name -> {kind, expected_every_min, last_seen, note}
        self.triage_state = {"last_run": None, "run_count": 0, "error_count": 0, "last_error": None}
        self.backup_state = {"last_run": None, "last_error": None, "last_file": None}
        self.restart_requested = False
        # Hooks other layers replace (mail / ai / license / triage / assistant).
        self.mail_status = lambda: {"provider": self.config.mail_provider, "ok": None,
                                    "last_fetch": None, "error": None}
        self.ai_status = lambda: {"provider": self.config.ai_provider, "ok": None,
                                  "today_cost_usd": 0.0, "budget_usd": self.config.get("ai.daily_budget_usd", 2.0),
                                  "budget_hit": False}
        self.license_status = lambda: {"status": "dev" if os.environ.get("PB_LICENSE_DEV") == "1" else "unknown",
                                       "plan": None, "expires": None, "grace_days_left": None}
        self.run_triage_cycle = None      # callable set by the mail/triage layer
        self.run_assistant = None         # callable set by the assistant layer
        # Slots the other layers fill (ai.wiring.install / mail.runner.wire_runtime /
        # license.client.client_from_app). None until installed.
        self.llm = None
        self.mail = None                  # a MailProvider that can create drafts, when one exists
        self.mail_runner = None
        self.license = None
        self.secret_getter = None
        self.assistant = None
        self.assistant_lock = None
        self.assistant_status = None
        self.testing = False
        self.hours = BusinessHours.from_profession(self._raw_profession())
        seed = self.profession().get("workflows")
        self.workflows = wfm.WorkflowStore(self.paths.workflows_file, self.paths.workflow_backups, seed=seed,
                                           date_types=_profession.date_type_ids(self.profession()))
        self.store = Store(self.paths.db_file, StoreContext(
            workflows=self.workflows, profession=self.profession, hours=self.hours, user=self.config.user))

    # -- profession ---------------------------------------------------------

    def _raw_profession(self):
        return _profession.effective(self.config.profession_template, self.config.profession_overrides,
                                     user=self.config.doc.get("user"), features=self.config.features)

    def profession(self):
        with self._lock:
            if self._prof_cache is None:
                self._prof_cache = self._raw_profession()
            return self._prof_cache

    def reload(self, config=None):
        """Re-read config (and the profession behind it). Keeps the store."""
        with self._lock:
            if config is not None:
                self.config = config
            else:
                self.config = Config.load(self.config.source_path) if self.config.source_path else Config.load()
            self.paths = self.config.paths
            self._prof_cache = None
            self.hours = BusinessHours.from_profession(self.profession())
            self.workflows.set_seed(self.profession().get("workflows"))
            self.workflows.date_types = set(_profession.date_type_ids(self.profession()))
            self.store.ctx.hours = self.hours
            self.store.ctx.user = self.config.user

    def rewire(self):
        """Rebuild the AI and mail layers on the current config (after setup
        or a config PUT). The license client is dropped so it re-reads the
        server URL on its next use. Threads only start outside tests."""
        with self._lock:
            old_sched = self.assistant
            if old_sched is not None and hasattr(old_sched, "stop"):
                try:
                    old_sched.stop()
                except Exception:
                    pass
            if self.mail_runner is not None:
                try:
                    self.mail_runner.stop_all()
                except Exception:
                    pass
            self.assistant = None
            self.license = None
            try:
                from .ai.wiring import install as _install_ai
                _install_ai(self, self.secret_getter or default_secret_getter, start_scheduler=not self.testing)
            except Exception as e:
                print(f"[ai] rewire failed: {type(e).__name__}: {e}")
            try:
                from .mail.runner import wire_runtime as _wire_mail
                autostart = (not self.testing) and self.config.mail_provider not in ("none", "")
                _wire_mail(self, llm=self.llm, autostart=autostart)
            except Exception as e:
                print(f"[mail] rewire failed: {type(e).__name__}: {e}")

    # -- watchers -----------------------------------------------------------------

    def register_watcher(self, name, expected_every_min, kind="loop", note=""):
        with self._lock:
            w = self.watchers.setdefault(name, {"kind": kind, "expected_every_min": expected_every_min,
                                                "last_seen": None, "note": note})
            w["expected_every_min"] = expected_every_min
            w["kind"] = kind
            if note:
                w["note"] = note

    def heartbeat(self, name, note=None):
        with self._lock:
            w = self.watchers.setdefault(name, {"kind": "loop", "expected_every_min": None,
                                                "last_seen": None, "note": ""})
            w["last_seen"] = self.hours.now_iso()
            if note is not None:
                w["note"] = note

    def uptime_s(self):
        return int(time.time() - self.started_at)

    # -- dict view ------------------------------------------------------------
    # The mail, ai and license layers were written against
    # `app.extensions["pb"]` as a dict {store, cfg, profession, llm, mail,
    # secret_getter, license, version, ...}. The Runtime answers the same
    # mapping protocol so either shape works.

    _VIEW_KEYS = ("store", "cfg", "profession", "llm", "mail", "mail_runner", "secret_getter", "license",
                  "version", "assistant", "assistant_lock", "assistant_status", "config", "paths", "hours")

    def _view_value(self, key):
        if key == "cfg":
            return self.config.doc
        if key == "profession":
            return self.profession()
        if key == "version":
            return __version__
        if key == "secret_getter" and self.secret_getter is None:
            return default_secret_getter
        return getattr(self, key)

    def __contains__(self, key):
        return key in self._VIEW_KEYS or hasattr(self, key)

    def __getitem__(self, key):
        if key not in self:
            raise KeyError(key)
        return self._view_value(key)

    def get(self, key, default=None):
        if key not in self:
            return default
        val = self._view_value(key)
        return default if val is None else val

    _DERIVED = frozenset({"cfg", "profession", "version"})

    def __setattr__(self, key, value):
        # ai.wiring.resolve() copies its view back with setattr; the derived
        # keys (and the profession() method) must survive that.
        if key in Runtime._DERIVED:
            raise AttributeError(f"{key} is derived from the Runtime and cannot be assigned")
        object.__setattr__(self, key, value)

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def setdefault(self, key, default=None):
        cur = self.get(key)
        if cur is None:
            self[key] = default
            return default
        return cur

    def keys(self):
        return list(self._VIEW_KEYS)


def default_secret_getter(name):
    """Keyring (service EmailBoss) then the env fallback; '' when absent or
    when the name is not one of the four secrets this app keeps."""
    from .config import get_secret
    try:
        return get_secret(name)[0] or ""
    except KeyError:
        return ""


def _api_package():
    """Import plannerboard.api, surviving a test harness that pre-seeded a bare
    namespace module under that name (tests/fakes_ai.import_api_module)."""
    mod = sys.modules.get(__name__ + ".api")
    if mod is not None and not hasattr(mod, "register_blueprints"):
        mod = importlib.reload(mod)
    elif mod is None:
        mod = importlib.import_module(__name__ + ".api")
    return mod


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_JSON_TYPES = ("application/json",)
_FORM_TYPES = ("multipart/form-data",)
SESSION_COOKIE = "pb_session"


def _host_name(host: str) -> str:
    h = (host or "").strip().lower()
    if h.startswith("["):
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _constant_eq(a: str, b: str) -> bool:
    import hmac
    return bool(a) and bool(b) and hmac.compare_digest(a, b)


def _token_ok(cfg, session_token: str = "") -> bool:
    """Every /api/* request needs proof it came from this app's own window:
    the per-launch session cookie (set by /auth/<token>), or a bearer that
    equals the session token or, off loopback, the configured api_token."""
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:].strip() if auth.startswith("Bearer ") else ""
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if session_token and (_constant_eq(cookie, session_token) or _constant_eq(bearer, session_token)):
        return True
    if cfg.requires_token and cfg.api_token and (_constant_eq(bearer, cfg.api_token) or _constant_eq(cookie, cfg.api_token)):
        return True
    return False


def _host_ok(cfg) -> bool:
    """Reject DNS-rebinding: the Host header must be the address we serve on."""
    host = _host_name(request.host)
    if not cfg.requires_token:  # loopback bind
        return host in _LOOPBACK_HOSTS
    return host in _LOOPBACK_HOSTS or host == str(cfg.bind).lower()


def _origin_ok() -> bool:
    """A present Origin must be our own origin (same host as Host)."""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    if origin == "null":
        return False
    from urllib.parse import urlsplit
    o = urlsplit(origin)
    return _host_name(o.netloc) == _host_name(request.host)


def _content_type_ok() -> bool:
    if request.method not in _WRITE_METHODS:
        return True
    ct = (request.content_type or "").split(";")[0].strip().lower()
    if not ct and (request.content_length or 0) == 0:
        return True  # empty-body POST (e.g. /api/restart)
    return ct in _JSON_TYPES or ct in _FORM_TYPES


def create_app(config_path=None, config=None, testing=None):
    """Build the Flask app and wire every layer onto the Runtime.

    `testing` (default: FLASK_TESTING / PB_TESTING env, or False) keeps the
    background threads (mail capture, assistant scheduler) from starting.
    """
    cfg = config if config is not None else Config.load(config_path)
    cfg.paths.ensure()
    if testing is None:
        testing = os.environ.get("PB_TESTING") == "1" or os.environ.get("FLASK_TESTING") == "1" \
            or "pytest" in sys.modules
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False
    app.config["TESTING"] = bool(testing)
    rt = Runtime(cfg)
    rt.secret_getter = default_secret_getter
    rt.testing = bool(testing)
    app.extensions["pb"] = rt

    # -- licensing: 402 on writes while read-only; status hook for /api/health
    from .license import client as lic
    lic.middleware(app)

    def _license_status():
        try:
            return lic.client_from_app(app).status()
        except Exception as e:
            return {"status": "error", "plan": None, "expires": None, "grace_days_left": None, "error": str(e)}
    rt.license_status = _license_status

    # -- AI: provider from config, assistant scheduler (not started under test)
    try:
        from .ai.wiring import install as _install_ai
        _install_ai(rt, default_secret_getter, start_scheduler=not testing)
    except Exception as e:  # the board must come up without AI
        print(f"[ai] layer not installed: {type(e).__name__}: {e}")

    # -- mail + triage: capture threads per configured provider, run_triage_cycle hook
    try:
        from .mail.runner import wire_runtime as _wire_mail
        autostart = (not testing) and cfg.mail_provider not in ("none", "")
        _wire_mail(rt, llm=rt.llm, autostart=autostart)
    except Exception as e:
        print(f"[mail] layer not installed: {type(e).__name__}: {e}")

    _api_package().register_blueprints(app)

    import secrets as _secrets
    rt.session_token = _secrets.token_urlsafe(32)
    # Tests drive the API with Flask's test client and no window; the gate is
    # off there unless a test turns it on (PB_ENFORCE_AUTH=1 or app.config).
    app.config["PB_ENFORCE_AUTH"] = (not testing) or os.environ.get("PB_ENFORCE_AUTH") == "1"

    @app.before_request
    def _api_gate():
        path = request.path or ""
        if not path.startswith("/api/"):
            return None
        if not _host_ok(rt.config):
            return jsonify({"ok": False, "error": "bad_host"}), 400
        if not _origin_ok():
            return jsonify({"ok": False, "error": "bad_origin"}), 403
        if not _content_type_ok():
            return jsonify({"ok": False, "error": "unsupported_media_type",
                            "message": "Send JSON (Content-Type: application/json)."}), 415
        if app.config.get("PB_ENFORCE_AUTH") and not _token_ok(rt.config, rt.session_token):
            return jsonify({"ok": False, "error": "unauthorized",
                            "message": "Open Email Boss from the app window."}), 401
        return None

    @app.route("/auth/<token>")
    def auth_bootstrap(token):
        """The app window (or the browser the server opened) lands here once per
        launch; the matching cookie is what every /api/* call then carries."""
        import time as _t
        ok = _constant_eq(token, rt.session_token) or \
            (rt.config.requires_token and _constant_eq(token, rt.config.api_token or ""))
        if not ok:
            _t.sleep(0.5)
            return Response("Not a valid Email Boss link for this session.", status=403, mimetype="text/plain")
        nxt = request.args.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
            nxt = "/"
        resp = redirect(nxt)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Lax",
                        secure=False, path="/", max_age=60 * 60 * 24 * 30)
        return resp

    @app.teardown_request
    def _release_stranded(exc=None):
        try:
            Store.release_all_stranded("request teardown")
        except Exception:
            pass

    def _serve_static(name):
        p = _paths.STATIC_DIR / name
        if not p.exists():
            return Response(f"{name} not found", status=404, mimetype="text/plain")
        html = p.read_text(encoding="utf-8")
        # No token is ever written into the page (it used to be, off loopback);
        # the session cookie from /auth/<token> is the only credential.
        resp = Response(html, mimetype="text/html")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.route("/")
    def serve_board():
        if rt.config.setup_required:
            return _serve_static("setup.html")
        return _serve_static("board.html")

    @app.route("/setup")
    def serve_setup():
        return _serve_static("setup.html")

    @app.route("/manifest.webmanifest")
    def serve_manifest():
        """Web app manifest. Name follows the user's company (or the profession
        label) so the installed PWA tile reads as theirs; icon is an inline SVG."""
        import json as _json
        company = ""
        label = ""
        try:
            company = str(rt.config.get("user.company", "") or "").strip()
            label = str((rt.profession() or {}).get("label") or "").strip()
        except Exception:
            pass
        if company:
            name = f"{company} Email Boss"
        elif label and label.lower() != "general":
            name = f"{label} Email Boss"
        else:
            name = "Email Boss"
        icon = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
                "%3Crect width='64' height='64' rx='12' fill='%230f1115'/%3E"
                "%3Crect x='12' y='14' width='40' height='6' rx='3' fill='%234fc3f7'/%3E"
                "%3Crect x='12' y='29' width='40' height='6' rx='3' fill='%23fac775'/%3E"
                "%3Crect x='12' y='44' width='26' height='6' rx='3' fill='%23c0dd97'/%3E%3C/svg%3E")
        manifest = {
            "name": name, "short_name": "Planner", "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#0f1115", "theme_color": "#0f1115",
            "description": "Email-driven project board",
            "icons": [{"src": icon, "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
        }
        return Response(_json.dumps(manifest), mimetype="application/manifest+json")

    @app.route("/static/<path:name>")
    def serve_asset(name):
        p = (_paths.STATIC_DIR / name).resolve()
        try:
            p.relative_to(_paths.STATIC_DIR.resolve())
        except ValueError:
            return Response("forbidden", status=403)
        if not p.is_file():
            return Response("not found", status=404)
        return send_file(str(p))

    return app
