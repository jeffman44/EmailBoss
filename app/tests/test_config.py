import json

from plannerboard import config as cfgmod
from plannerboard.config import Config, UserIdentity, auto_aliases, build_document, deep_merge


def test_deep_merge_dicts_recurse_and_lists_replace():
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 2}
    out = deep_merge(base, {"a": {"y": [9]}, "c": 3})
    assert out == {"a": {"x": 1, "y": [9]}, "b": 2, "c": 3}
    assert base["a"]["y"] == [1, 2]            # copy, not mutation


def test_missing_file_means_setup_required(data_root):
    cfg = Config.load()
    assert cfg.setup_required is True
    assert cfg.configured is False
    assert cfg.port == 8080
    assert cfg.bind == "127.0.0.1"
    assert cfg.requires_token is False
    assert cfg.paths.data_root == data_root


def test_malformed_file_records_error(data_root):
    data_root.mkdir(parents=True)
    (data_root / "plannerboard.json").write_text("{not json", encoding="utf-8")
    cfg = Config.load()
    assert cfg.load_error and "plannerboard.json" in cfg.load_error
    assert cfg.setup_required is True


def test_load_merge_write_roundtrip(data_root):
    data_root.mkdir(parents=True)
    p = data_root / "plannerboard.json"
    p.write_text(json.dumps({"user": {"name": "Pat", "email": "Pat@Example.com"},
                             "server": {"port": 9090}, "features": {"money": False}}), encoding="utf-8")
    cfg = Config.load()
    assert cfg.configured and not cfg.setup_required
    assert cfg.port == 9090
    assert cfg.feature("money") is False
    assert cfg.feature("unknown_flag") is True
    assert cfg.user.email == "pat@example.com"
    assert cfg.user.is_user("pat@example.com") and cfg.user.is_user("", "Pat")
    cfg.update({"ai": {"provider": "ollama", "model": "qwen"}})
    cfg.write()
    again = Config.load()
    assert again.ai_provider == "ollama"
    assert again.doc["ai"]["model"] == "qwen"
    assert again.doc["ai"]["daily_budget_usd"] == 2.0      # defaults kept
    pub = again.public_dict()
    assert "api_token" not in json.dumps(pub)
    assert pub["mail"]["provider"] == "none"


def test_env_overrides(data_root, monkeypatch):
    monkeypatch.setenv("PB_PORT", "7777")
    monkeypatch.setenv("PB_FORCE_SETUP", "1")
    cfg = Config({"user": {"name": "x", "email": "x@y.z"}}, source_path=data_root / "plannerboard.json")
    assert cfg.port == 7777
    assert cfg.setup_required is True


def test_build_document_reference_contract_and_new_shape():
    doc, problems = build_document({"name": "Pat Example", "email": "PAT@example.com", "aliases": "pe, pat e",
                                    "data_root": "/tmp/x", "port": 8081, "preset": "legal",
                                    "mail_source": "imap", "imap": {"host": "imap.example.com", "user": "pat@example.com",
                                                                     "interval_min": 10},
                                    "imap_password": "hunter2-pw"})
    assert not problems
    assert doc["user"]["email"] == "pat@example.com"
    assert "pe" in doc["user"]["aliases"] and "patexample" in doc["user"]["aliases"]
    assert doc["profession"]["template"] == "legal"
    assert doc["mail"]["provider"] == "imap" and doc["mail"]["imap"]["host"] == "imap.example.com"
    assert doc["mail"]["poll_seconds"] == 600
    assert "hunter2-pw" not in json.dumps(doc)          # the password never enters the document
    doc2, _ = build_document({"user": {"name": "A B", "email": "a@b.co"}, "profession": {"template": "it_msp"},
                              "server": {"bind": "0.0.0.0"}, "ai": {"provider": "anthropic", "api_key": "sk-x"},
                              "license": {"key": "LIC"}, "features": {"money": False}})
    assert doc2["server"]["api_token"]                       # non-loopback mints a token
    assert doc2["ai"]["provider"] == "anthropic" and "sk-x" not in json.dumps(doc2)
    assert doc2["license"]["key"] == "LIC"
    assert doc2["features"]["money"] is False


def test_user_identity_and_aliases():
    assert set(auto_aliases("Pat Example", "pat@example.com")) == {"pat", "patexample", "example,pat"}
    u = UserIdentity({"name": "Pat Example", "email": "pat@example.com", "internal_domains": ["corp.example"]})
    assert u.is_user("PAT@example.com")
    assert u.is_user("", "Example, Pat")
    assert not u.is_user("sue@client.com", "Sue")
    assert u.is_internal("x@corp.example") and u.is_internal("y@example.com")
    assert not u.is_internal("z@other.com")


def test_secrets_env_fallback(monkeypatch):
    monkeypatch.setenv("PB_AI_API_KEY", "from-env")
    monkeypatch.setattr(cfgmod, "_keyring", lambda: None)
    assert cfgmod.get_secret("ai_api_key") == ("from-env", "env")
    ok, where, err = cfgmod.set_secret("ai_api_key", "x")
    assert ok is False and err
