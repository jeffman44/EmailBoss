"""
Config: ONE document, loaded and merged over generic defaults, written back by
the setup wizard. Secrets never enter the document: they live in the OS
keyring (service "EmailBoss") with an environment-variable fallback.

Ported and generalized from the reference install's config.py (lines 147-190
load / deep-merge, 338-348 write). Every install-specific default is gone: a
missing file means `setup_required`, not "someone's install".

Never import db, workflows or Flask from here: db.py may import this at
migration time, and the installer must be able to load it with nothing but the
standard library present.
"""

import json
import os
import re
import secrets
from copy import deepcopy
from pathlib import Path

from . import paths as _paths

SCHEMA_VERSION = 1
KEYRING_SERVICE = "EmailBoss"

# keyring key -> env fallback. Only these four names are ever stored.
SECRET_KEYS = {
    "imap_password": "PB_IMAP_PASSWORD",
    "gmail_token": "PB_GMAIL_TOKEN",
    "graph_token": "PB_GRAPH_TOKEN",
    "ai_api_key": "PB_AI_API_KEY",
}

MAIL_PROVIDERS = ("pa_folder", "imap", "gmail", "graph", "none")
AI_PROVIDERS = ("none", "anthropic", "openai", "ollama")

DEFAULTS = {
    "schema_version": SCHEMA_VERSION,
    "user": {"name": "", "email": "", "aliases": [], "company": "", "role": "",
             "internal_domains": []},
    "profession": {"template": "general", "overrides": {}},
    "paths": {"data_root": "", "mail_cache": "", "backups": ""},
    "server": {"port": 8080, "bind": "127.0.0.1", "api_token": ""},
    "mail": {
        "provider": "none",
        "poll_seconds": 300,
        "client_enabled": True,
        "send_enabled": False,
        "imap": {"host": "", "port": 993, "user": "", "inbox": "INBOX", "sent": "Sent",
                 "drafts": "", "trash": "", "display_name": ""},
        "smtp": {"host": "", "port": 587, "starttls": True, "ssl": False, "user": ""},
        "gmail": {"client_id": "", "client_secret_ref": "keyring"},
        "graph": {"client_id": "", "tenant": "common"},
    },
    "ai": {
        "provider": "none", "model": "", "base_url": "",
        "daily_budget_usd": 2.0,
        "features": {"classify": True, "summarize": True, "match_rerank": True,
                     "draft_reply": True, "assistant": False},
    },
    "assistant": {"schedule_minutes": 60, "max_items": 10, "drafts_to": "local",
                  "auto_create": False},
    "license": {"key": "", "server_url": "https://license.example.com", "activation": {}},
    "features": {"money": True, "game_layer": True, "analytics": True},
}

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def deep_merge(base, over):
    """Merge `over` onto a copy of `base`. Dicts recurse; scalars and lists in
    `over` replace wholesale. Unknown keys are kept (forward compatible).
    Port of config.py:161-170."""
    out = deepcopy(base) if isinstance(base, dict) else {}
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def _read_json(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("top level is not an object")
    return raw


# ── Secrets ─────────────────────────────────────────────────────────────────

def _keyring():
    try:
        import keyring  # noqa: F401
        return keyring
    except Exception:
        return None


def get_secret(key):
    """Keyring first, then the env fallback. Returns (value, source) where
    source is 'keyring' | 'env' | None."""
    if key not in SECRET_KEYS:
        raise KeyError(f"unknown secret '{key}'")
    kr = _keyring()
    if kr is not None:
        try:
            v = kr.get_password(KEYRING_SERVICE, key)
            if v:
                return v, "keyring"
        except Exception:
            pass
    env = os.environ.get(SECRET_KEYS[key])
    if env:
        return env, "env"
    return None, None


def set_secret(key, value):
    """Store in the keyring. Returns (ok, where, error). A value that cannot be
    stored is reported, never written to a file."""
    if key not in SECRET_KEYS:
        raise KeyError(f"unknown secret '{key}'")
    kr = _keyring()
    if kr is None:
        return False, None, "keyring library unavailable"
    try:
        if value is None or value == "":
            try:
                kr.delete_password(KEYRING_SERVICE, key)
            except Exception:
                pass
            return True, "keyring", None
        kr.set_password(KEYRING_SERVICE, key, value)
        return True, "keyring", None
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def keyring_available():
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.get_keyring()
        return True
    except Exception:
        return False


# ── User identity ──────────────────────────────────────────────────────────

def normalize_name(name):
    """The exact normalization the matcher uses: lower, drop spaces and dots."""
    return (name or "").lower().replace(" ", "").replace(".", "")


def auto_aliases(name, email, include_local=True):
    """Alias shapes derived from a display name + address so a user who types
    nothing still matches their own mail. Port of server.py:753-759.

    `include_local=False` leaves the mailbox local part out: the triage layer
    treats every stored alias as a signature boundary, and a first name such
    as "sam" would cut "Hi Sam," out of every inbound body. UserIdentity adds
    the local part back at runtime for the is_user() check."""
    email = (email or "").strip().lower()
    local = email.split("@")[0] if email else ""
    out = set()
    if local and include_local:
        out.add(local)
    if name:
        out.add(normalize_name(name))
        parts = [p for p in re.split(r"\s+", name.strip()) if p]
        if len(parts) >= 2:
            out.add((parts[-1] + "," + parts[0]).lower())
    return sorted(a for a in out if a)


class UserIdentity:
    """Is this message from the configured user? Port of config.py:277-291,
    kept permissive on purpose: some capture paths deliver an empty from_email
    with only a display name."""

    def __init__(self, user_block):
        u = user_block or {}
        self.name = u.get("name") or ""
        self.email = (u.get("email") or "").strip().lower()
        aliases = set(a.strip().lower() for a in (u.get("aliases") or []) if a)
        aliases |= set(auto_aliases(self.name, self.email))
        self.aliases = tuple(sorted(aliases))
        self.email_prefix = (self.email.split("@")[0] + "@") if "@" in self.email else self.email
        self.internal_domains = tuple(
            d.strip().lower().lstrip("@") for d in (u.get("internal_domains") or []) if d)
        if "@" in self.email:
            dom = self.email.split("@", 1)[1]
            if dom and dom not in self.internal_domains:
                self.internal_domains = self.internal_domains + (dom,)

    def is_user(self, from_email="", from_name=""):
        addr = (from_email or "").strip().lower()
        if self.email_prefix and addr.startswith(self.email_prefix):
            return True
        nm = normalize_name(from_name)
        return bool(nm) and nm in self.aliases

    def is_internal(self, email):
        addr = (email or "").strip().lower()
        if "@" not in addr:
            return False
        return addr.split("@", 1)[1] in self.internal_domains


# ── The document ────────────────────────────────────────────────────────────

class Config:
    """Loaded config document + resolved paths.

    `Config.load(path=None)` never raises: a missing file yields defaults with
    `setup_required=True`; a malformed file yields defaults plus `load_error`.
    """

    def __init__(self, doc, source_path=None, load_error=None):
        self.doc = deep_merge(DEFAULTS, doc or {})
        self.source_path = Path(source_path) if source_path else None
        self.load_error = load_error
        self._resolve()

    # -- loading ------------------------------------------------------------

    @classmethod
    def load(cls, path=None):
        target = Path(path) if path else _paths.config_path()
        if not target.is_file():
            return cls({}, None, None)
        try:
            return cls(_read_json(target), target, None)
        except Exception as e:
            return cls({}, None, f"{target}: {e}")

    def _resolve(self):
        p = self.doc.get("paths") or {}
        root = p.get("data_root") or _paths.default_data_root()
        self.paths = _paths.Paths(root, mail_cache=p.get("mail_cache") or None,
                                  backups=p.get("backups") or None,
                                  db_file=p.get("db_file") or None)
        self.user = UserIdentity(self.doc.get("user"))

    # -- derived -------------------------------------------------------------

    @property
    def configured(self):
        return self.source_path is not None

    @property
    def setup_required(self):
        if os.environ.get("PB_FORCE_SETUP") == "1":
            return True
        return not self.configured

    @property
    def port(self):
        env = os.environ.get("PB_PORT")
        try:
            return int(env) if env else int(self.doc["server"].get("port") or 8080)
        except (TypeError, ValueError):
            return 8080

    @property
    def bind(self):
        return (self.doc["server"].get("bind") or "127.0.0.1").strip()

    @property
    def api_token(self):
        return self.doc["server"].get("api_token") or ""

    @property
    def requires_token(self):
        """Non-loopback binds require a bearer token on every /api/* request."""
        return self.bind not in _LOOPBACK

    @property
    def features(self):
        return dict(self.doc.get("features") or {})

    def feature(self, name):
        """Unknown name is ON: a typo must never hide a working endpoint."""
        return bool((self.doc.get("features") or {}).get(name, True))

    @property
    def profession_template(self):
        return (self.doc.get("profession") or {}).get("template") or "general"

    @property
    def profession_overrides(self):
        return dict((self.doc.get("profession") or {}).get("overrides") or {})

    @property
    def mail_provider(self):
        v = str((self.doc.get("mail") or {}).get("provider") or "none").strip().lower()
        return v if v in MAIL_PROVIDERS else "none"

    @property
    def ai_provider(self):
        v = str((self.doc.get("ai") or {}).get("provider") or "none").strip().lower()
        return v if v in AI_PROVIDERS else "none"

    def get(self, dotted, default=None):
        cur = self.doc
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    # -- writing -------------------------------------------------------------

    def write(self, path=None):
        """Atomic write. Default target: <data_root>/plannerboard.json (or
        $PB_CONFIG). Returns the path."""
        target = Path(path) if path else _paths.config_path(self.paths.data_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.doc, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(target))
        self.source_path = target
        self.load_error = None
        return target

    def update(self, over):
        """Deep-merge `over` into the document and re-resolve. Does not write."""
        self.doc = deep_merge(self.doc, over)
        self._resolve()
        return self

    def set_overrides(self, overrides):
        self.doc.setdefault("profession", {})["overrides"] = dict(overrides or {})
        return self

    # -- public subset --------------------------------------------------------

    def public_dict(self):
        """The subset served by GET /api/config. No secrets, no paths beyond
        the data root (the browser is the owner's, on localhost)."""
        mail = self.doc.get("mail") or {}
        ai = self.doc.get("ai") or {}
        lic = self.doc.get("license") or {}
        return {
            "schema_version": SCHEMA_VERSION,
            "configured": self.configured,
            "setup_required": self.setup_required,
            "config_error": self.load_error,
            "user": {k: self.doc["user"].get(k) for k in
                     ("name", "email", "aliases", "company", "role", "internal_domains")},
            "profession": {"template": self.profession_template,
                           "overrides": self.profession_overrides},
            "server": {"port": self.port, "bind": self.bind,
                       "token_required": self.requires_token},
            "mail": {
                "provider": self.mail_provider,
                "poll_seconds": mail.get("poll_seconds", 300),
                "client_enabled": bool(mail.get("client_enabled", True)),
                "send_enabled": bool(mail.get("send_enabled", False)),
                "imap": {k: (mail.get("imap") or {}).get(k) for k in
                         ("host", "port", "user", "inbox", "sent", "drafts", "trash", "display_name", "preset")},
                "smtp": {k: (mail.get("smtp") or {}).get(k) for k in
                         ("host", "port", "starttls", "ssl", "user")},
                "gmail": {"client_id": (mail.get("gmail") or {}).get("client_id", "")},
                "graph": {"client_id": (mail.get("graph") or {}).get("client_id", ""),
                          "tenant": (mail.get("graph") or {}).get("tenant", "common")},
            },
            "ai": {"provider": self.ai_provider, "model": ai.get("model", ""),
                   "base_url": ai.get("base_url", ""),
                   "daily_budget_usd": ai.get("daily_budget_usd", 2.0),
                   "features": dict(ai.get("features") or {}),
                   "key_present": bool(get_secret("ai_api_key")[0])},
            "assistant": dict(self.doc.get("assistant") or {}),
            "license": {"server_url": lic.get("server_url", ""),
                        "key_present": bool(lic.get("key")),
                        "dev_mode": os.environ.get("PB_LICENSE_DEV") == "1"},
            "features": self.features,
            "data_root": str(self.paths.data_root),
        }


def new_api_token():
    return secrets.token_urlsafe(32)


def build_document(body, existing=None):
    """Turn a POST /api/setup body into a config document. Preserves the
    reference setup contract (server.py:725-834): name, email, aliases,
    data_root, port, plus the new profession / mail / ai / license blocks.
    Returns (doc, problems)."""
    problems = []
    body = body or {}
    base = deepcopy(existing.doc) if existing is not None else deepcopy(DEFAULTS)

    name = (body.get("name") or (body.get("user") or {}).get("name") or "").strip()
    email = (body.get("email") or (body.get("user") or {}).get("email") or "").strip().lower()
    aliases = body.get("aliases", (body.get("user") or {}).get("aliases"))
    if isinstance(aliases, str):
        aliases = [a.strip() for a in re.split(r"[,;\n]", aliases) if a.strip()]
    aliases = sorted(set(a.lower() for a in (aliases or []) if a)
                     | set(auto_aliases(name, email, include_local=False)))
    user_in = body.get("user") or {}
    internal = body.get("internal_domains", user_in.get("internal_domains")) or []
    if isinstance(internal, str):
        internal = [d.strip() for d in re.split(r"[,;\s]+", internal) if d.strip()]

    doc = deep_merge(base, {
        "schema_version": SCHEMA_VERSION,
        "user": {"name": name, "email": email, "aliases": aliases,
                 "company": (body.get("company") or user_in.get("company") or "").strip(),
                 "role": (body.get("role") or user_in.get("role") or "").strip(),
                 "internal_domains": [d.lower().lstrip("@") for d in internal]},
    })

    data_root = (body.get("data_root") or (body.get("paths") or {}).get("data_root") or "").strip()
    if data_root:
        doc["paths"]["data_root"] = data_root
    for k in ("mail_cache", "backups"):
        v = (body.get(k) or (body.get("paths") or {}).get(k) or "").strip()
        if v:
            doc["paths"][k] = v

    try:
        port = int(body.get("port") or (body.get("server") or {}).get("port") or doc["server"]["port"])
        if not (0 < port < 65536):
            raise ValueError
        doc["server"]["port"] = port
    except (TypeError, ValueError):
        problems.append("port ignored (not a valid port number)")
    bind = (body.get("bind") or (body.get("server") or {}).get("bind") or "").strip()
    if bind:
        doc["server"]["bind"] = bind
    if doc["server"]["bind"] not in _LOOPBACK and not doc["server"].get("api_token"):
        doc["server"]["api_token"] = new_api_token()

    prof = body.get("profession")
    if isinstance(prof, str):
        doc["profession"]["template"] = prof.strip() or "general"
    elif isinstance(prof, dict):
        if prof.get("template"):
            doc["profession"]["template"] = str(prof["template"]).strip()
        if isinstance(prof.get("overrides"), dict):
            doc["profession"]["overrides"] = prof["overrides"]
    elif body.get("preset"):
        doc["profession"]["template"] = str(body["preset"]).strip()

    mail = body.get("mail")
    if isinstance(mail, dict):
        safe = {k: v for k, v in mail.items() if k not in ("password", "imap_password")}
        if "provider" in safe:
            prov = str(safe["provider"] or "none").strip().lower()
            if prov not in MAIL_PROVIDERS:
                problems.append(f"mail.provider '{prov}' unknown; left on none")
                prov = "none"
            safe["provider"] = prov
        if isinstance(safe.get("imap"), dict):
            safe["imap"] = {k: v for k, v in safe["imap"].items() if k != "password"}
        doc["mail"] = deep_merge(doc["mail"], safe)
    elif body.get("mail_source"):
        legacy = str(body["mail_source"]).strip().lower()
        doc["mail"]["provider"] = {"pa": "pa_folder", "imap": "imap"}.get(legacy, legacy
                                                                           if legacy in MAIL_PROVIDERS else "none")
        if isinstance(body.get("imap"), dict):
            im = body["imap"]
            doc["mail"]["imap"] = deep_merge(doc["mail"]["imap"], {
                "host": im.get("host", ""), "port": im.get("port", 993),
                "user": im.get("user", ""), "inbox": im.get("inbox_folder") or im.get("inbox") or "INBOX",
                "sent": im.get("sent_folder") or im.get("sent") or "Sent"})
            if im.get("interval_min"):
                try:
                    doc["mail"]["poll_seconds"] = max(60, int(im["interval_min"]) * 60)
                except (TypeError, ValueError):
                    pass

    ai = body.get("ai")
    if isinstance(ai, dict):
        safe = {k: v for k, v in ai.items() if k not in ("api_key", "key")}
        if "provider" in safe:
            prov = str(safe["provider"] or "none").strip().lower()
            if prov not in AI_PROVIDERS:
                problems.append(f"ai.provider '{prov}' unknown; left on none")
                prov = "none"
            safe["provider"] = prov
        doc["ai"] = deep_merge(doc["ai"], safe)

    lic = body.get("license")
    if isinstance(lic, dict):
        doc["license"] = deep_merge(doc["license"], {k: v for k, v in lic.items()
                                                      if k in ("key", "server_url", "activation")})
    elif isinstance(lic, str) and lic.strip():
        doc["license"]["key"] = lic.strip()

    feats = body.get("features")
    if isinstance(feats, dict):
        for k, v in feats.items():
            if k in doc["features"]:
                doc["features"][k] = bool(v)

    if isinstance(body.get("assistant"), dict):
        doc["assistant"] = deep_merge(doc["assistant"], body["assistant"])

    return doc, problems
