"""Shared fixtures for every builder's tests.

Core fixtures: data_root, pbcfg (a Config object), app, client, rt, store,
make_config(). Triage / mail / ai fixtures: cfg (the config DOCUMENT as a
dict), profession (effective hvac template), mail_cache (a tmp folder with
the inbox / sent / attachments layout), FIXTURES and drop_fixture().
"""
import base64
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plannerboard import create_app  # noqa: E402
from plannerboard import profession as _profession  # noqa: E402
from plannerboard.config import Config  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ── core ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("PB_DATA_ROOT", str(root))
    monkeypatch.setenv("PB_LICENSE_DEV", "1")        # writes stay open without a license key
    monkeypatch.setenv("PB_TESTING", "1")            # no background threads
    for var in ("PB_CONFIG", "PB_DB_PATH", "PB_FORCE_SETUP", "PB_PORT"):
        monkeypatch.delenv(var, raising=False)
    return root


def make_config(data_root, template="hvac_contractor", **over):
    doc = {"user": {"name": "Pat Example", "email": "pat@example.com"},
           "profession": {"template": template},
           "paths": {"data_root": str(data_root)}}
    for k, v in over.items():
        doc[k] = v
    cfg = Config(doc)
    cfg.paths.ensure()
    cfg.write()
    return Config.load(cfg.source_path)


@pytest.fixture
def pbcfg(data_root):
    return make_config(data_root)


@pytest.fixture
def app(pbcfg):
    application = create_app(config=pbcfg, testing=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def rt(app):
    return app.extensions["pb"]


@pytest.fixture
def store(rt):
    return rt.store


# ── triage / mail / ai ─────────────────────────────────────────────────────────

@pytest.fixture
def mail_cache(tmp_path):
    root = tmp_path / "mail_cache"
    for sub in ("inbox", "sent", "attachments"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def cfg(tmp_path, mail_cache):
    """The config DOCUMENT (a dict) the pipeline and providers read."""
    return {
        "schema_version": 1,
        "user": {"name": "Sam User", "email": "sam@acme.example", "aliases": ["samuser", "user,sam"],
                 "company": "Acme", "role": "account manager", "internal_domains": ["acme.example"]},
        "profession": {"template": "general", "overrides": {}},
        "paths": {"data_root": str(tmp_path / "data"), "mail_cache": str(mail_cache),
                  "backups": str(tmp_path / "data" / "backups")},
        "server": {"port": 8080, "bind": "127.0.0.1", "api_token": ""},
        "mail": {"provider": "pa_folder", "poll_seconds": 300,
                 "imap": {"host": "", "port": 993, "user": "", "inbox": "INBOX", "sent": "Sent"},
                 "gmail": {"client_id": "", "client_secret_ref": "keyring"},
                 "graph": {"client_id": "", "tenant": "common"}},
        "ai": {"provider": "none", "model": "", "base_url": "", "daily_budget_usd": 2.0,
               "features": {"classify": True, "summarize": True, "match_rerank": True,
                            "draft_reply": True, "assistant": False}},
        "assistant": {"schedule_minutes": 60, "max_items": 10, "drafts_to": "local", "auto_create": False},
        "license": {"key": "", "server_url": "https://license.example.com", "activation": {}},
        "features": {"money": True, "game_layer": True, "analytics": True},
    }


# The triage / ai unit tests were written against a generic template with a
# unique "reference_no" id (PR-10442), a NON-unique ticket id (TKT-5551) and
# a few vocabulary phrases. They exercise the profession override mechanism
# on top of the shipped `general` template.
TEST_PROFESSION_OVERRIDES = {
    "id_patterns": [
        {"key": "reference_no", "label": "Ref #", "regex": r"\b[A-Z]{2}-\d{4,6}\b", "unique_match": True},
        {"key": "ticket", "label": "Ticket", "regex": r"\bTKT-(\d{4,6})\b", "unique_match": False},
    ],
    "vocab": {"keywords": ["lobby renovation", "roof inspection", "roof repair", "tenant improvement"],
              "acronyms": ["COI", "RFP"], "scope_phrases": []},
}


@pytest.fixture
def profession(cfg):
    return _profession.effective("general", TEST_PROFESSION_OVERRIDES, user=cfg["user"], features=cfg["features"])


def drop_fixture(mail_cache, name, folder="inbox", with_attachments=True):
    """Copy tests/fixtures/<name> into <mail_cache>/<folder>/ and, when the
    record lists attachments with contentBytes, write them under
    attachments/<message id>/<name>. Returns the copied path."""
    mail_cache = Path(mail_cache)
    dest_dir = mail_cache / folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    src = FIXTURES / name
    dest = dest_dir / name
    shutil.copy2(str(src), str(dest))
    if with_attachments:
        try:
            rec = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            rec = {}
        mid = rec.get("id") or rec.get("message_id") or ""
        atts = rec.get("attachments") or []
        if mid and atts:
            adir = mail_cache / "attachments" / mid
            adir.mkdir(parents=True, exist_ok=True)
            for att in atts:
                if not isinstance(att, dict) or not att.get("name"):
                    continue
                raw = att.get("contentBytes")
                try:
                    data = base64.b64decode(raw) if raw else b""
                except Exception:
                    data = b""
                (adir / att["name"]).write_bytes(data)
    return dest
