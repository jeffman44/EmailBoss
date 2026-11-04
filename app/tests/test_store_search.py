"""2.0 Phase 1 exit tests: the local store and search.

- test_search_golden_set
- test_backfill_creates_no_triage_or_ai_rows
- test_purge_removes_fts_cache_backups
- test_unknown_operator_hint
- test_per_account_secrets_two_gmail_accounts
plus the supporting behaviour (cutoff gate, quoted text, legacy ledger fold, upgrades).
"""
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plannerboard.mail import index, lifecycle, secrets
from plannerboard.mail import sync as backfill
from plannerboard.mail.message import Address, Attachment, Message
from plannerboard.mail.runner import ProviderThread, build_provider, start_all
from plannerboard.mail.store import MailStore, forget_open, open_store, store_path

from .test_sync_truth import FakeGmail, _cache_files

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def _mem_secrets():
    secrets.use_memory_backend(True)
    yield
    secrets.use_memory_backend(False)
    forget_open()


def _msg(pid, subject, body, sender="Dana <dana@northwind.example>", to="team@acme.example", day="2026-10-01",
         folder="inbox", read=True, starred=False, atts=()):
    m = Message(id=f"id-{pid}", received_at=datetime.fromisoformat(day + "T15:00:00+00:00"), subject=subject,
                body=body, is_html=False, sender=Address.parse_many(sender)[0], to=Address.parse_many(to),
                internet_message_id=f"<{pid}@mail.example>", conversation_id=f"c-{pid}", is_read=read,
                folder=folder, provider="gmail", provider_ref=pid,
                attachments=[Attachment(name=n, content_type="application/pdf", size=10, content=b"%PDF")
                             for n in atts])
    m.sync_id = pid
    m.flagged = starred
    m.sync_flags = (["\\Seen"] if read else []) + (["\\Flagged"] if starred else [])
    return m


MAILBOX = [
    _msg("m01", "Lobby retrofit kickoff", "Can we meet about the lobby retrofit Monday", to="Sam <sam@acme.example>"),
    _msg("m02", "Re: Lobby retrofit pricing",
         "Attached is pricing.\n\nOn Tue, Oct 1, 2026 at 9:00 AM Sam wrote:\n> rooftop unit question",
         day="2026-10-05", atts=["pricing.pdf"]),
    _msg("m03", "Lobby lobby lobby", "lobby lighting lobby", sender="ops@contoso.example", day="2026-10-30"),
    _msg("m04", "Invoice 2231", "Invoice attached, net 30", sender="billing@fabrikam.example", day="2026-10-10"),
    _msg("m05", "Site visit", "Visit notes", day="2026-10-12", read=False),
    _msg("m06", "Invoice 2232 overdue", "This invoice is overdue", sender="billing@fabrikam.example", day="2026-10-11"),
    _msg("m07", "Chiller maintenance", "Quarterly service", sender="svc@contoso.example", starred=True),
    _msg("m08", "Boiler inspection", "Annual inspection", sender="svc@contoso.example"),
    _msg("m09", "Drive submittal", "Submittal for VFD-4471 attached", sender="rep@vendor.example",
         atts=["VFD-4471 submittal.pdf"]),
    _msg("m10", "Order status", "Part VFD-4471 ships Friday", sender="rep@vendor.example", day="2026-10-25"),
    _msg("m11", "Following up", "Checking in on the schedule", sender="Sam <sam@acme.example>",
         to="dana@northwind.example", day="2026-10-28", folder="sent"),
    _msg("m12", "Parking lot", "parking", sender="mgr@contoso.example", day="2025-06-01"),
    _msg("m13", "Roof", "Please check the rooftop unit", sender="mgr@contoso.example", day="2026-10-02"),
    _msg("m14", "Thanks", "Thanks.\n\nOn Mon, Oct 5, 2026 at 8:00 AM Sam wrote:\n> rooftop unit",
         sender="mgr@contoso.example", day="2026-10-06"),
]


@pytest.fixture
def mailbox(tmp_path):
    st = MailStore(store_path(tmp_path))
    st.ensure_account("gmail", "gmail")
    for m in MAILBOX:
        body = (NOW - m.received_at.timestamp()) < 90 * 86400
        st.ingest("gmail", m, historical=True, with_body=body, attachment_text=index.attachment_text)
    yield st
    st.close()


def _ids(res):
    return [r["ref"] for r in res["messages"]]


def _naive_server(st, q):
    """A stand-in for a provider's own search over the same mail: whole
    words, case-insensitive, subject + sender + the message's own text,
    only for messages whose body is on this computer (inside coverage)."""
    words = [w.lower() for w in re.findall(r"\w+", q)]
    hits = []
    for m in MAILBOX:
        row = st.get("gmail", m.sync_id)
        if row["body_state"] != "full":
            continue
        hay = set(re.findall(r"\w+", " ".join([m.subject, m.sender.name, m.sender.email,
                                               index.strip_quoted(m.body)]).lower()))
        if all(w in hay for w in words):
            hits.append(m.sync_id)
    return hits


def test_search_golden_set(mailbox):
    golden = json.loads((FIX / "search_golden.json").read_text())["queries"]
    for g in golden:
        res = index.search(mailbox, g["q"], limit=50, now=NOW)
        ids = _ids(res)
        for want in g["expect"]:
            assert want in ids, f"{g['q']!r}: {want} missing from {ids}"
        for bad in g.get("absent", []):
            assert bad not in ids, f"{g['q']!r}: {bad} should not match ({ids})"
        if g.get("first"):
            assert ids[0] == g["first"], f"{g['q']!r}: {ids[0]} ranked first, wanted {g['first']}"
        if not any(":" in t for t in g["q"].split()) and "-" not in g["q"] and " OR " not in g["q"]:
            server = _naive_server(mailbox, g["q"])
            assert set(server) <= set(ids), f"{g['q']!r}: server found {set(server) - set(ids)} that local missed"


def test_quoted_text_is_not_indexed(mailbox):
    ids = _ids(index.search(mailbox, "rooftop", now=NOW))
    assert ids == ["m13"]


def test_unknown_operator_hint(mailbox):
    res = index.search(mailbox, "frm:dana lobby", now=NOW)
    assert any('"frm:" is not a search operator' in h for h in res["hints"])
    assert "m01" in _ids(res)                         # searched as text, not a silent zero
    res = index.search(mailbox, "is:snoozed", now=NOW)
    assert res["messages"] == [] and any("not in this version yet" in h for h in res["hints"])
    res = index.search(mailbox, "before:yesterday", now=NOW)
    assert any("is not a date" in h for h in res["hints"])
    res = index.search(mailbox, "zzzz-nothing", now=NOW)
    assert res["messages"] == [] and any("Search the server" in h for h in res["hints"])


def test_coverage_footer_and_server_row(mailbox):
    res = index.search(mailbox, "parking", now=NOW)
    cov = res["coverage"]
    assert cov["headers_only"] == 1 and "matched on headers only" in cov["sentence"]
    assert cov["unchecked"]                           # reconcile has not run: said, not hidden
    assert res["server_fallback"]["label"] == 'Search the server for "parking"'
    hit = res["messages"][0]
    assert hit["ref"] == "m12" and hit["body_searched"] is False


def test_ranking_prefers_recent_and_replied_senders(tmp_path):
    st = MailStore(store_path(tmp_path))
    st.ensure_account("gmail", "gmail")
    st.ingest("gmail", _msg("a1", "Budget", "budget numbers", sender="x@far.example", day="2026-03-01"))
    st.ingest("gmail", _msg("a2", "Budget", "budget numbers", sender="y@near.example", day="2026-10-30"))
    assert _ids(index.search(st, "budget", now=NOW))[0] == "a2"           # recency
    st.ingest("gmail", _msg("b1", "Plan", "plan", sender="p@one.example", day="2026-10-01"))
    st.ingest("gmail", _msg("b2", "Plan", "plan", sender="q@two.example", day="2026-10-01"))
    for i in range(3):                                                      # the user writes to q
        st.ingest("gmail", _msg(f"s{i}", "hi", "hi", sender="me@acme.example", to="q@two.example",
                                folder="sent", day="2026-09-01"))
    assert _ids(index.search(st, "plan", now=NOW))[0] == "b2"             # reply affinity


def test_purge_removes_fts_cache_backups(tmp_path):
    cache = tmp_path / "mail_cache"
    st = open_store(cache)
    old = _msg("o1", "Quarterly zebra review", "the zebramango figures", day="2026-01-05", atts=["zebrafile.txt"])
    old.attachments[0].content_type, old.attachments[0].content = "text/plain", b"zebrafile secret text"
    st.ingest("gmail", old, attachment_text=index.attachment_text)
    st.ingest("gmail", _msg("n1", "Fresh", "the zebramango update", day="2026-11-01"))
    assert set(_ids(index.search(st, "zebramango", now=NOW))) == {"o1", "n1"}
    assert index.search(st, "secret", now=NOW)["attachments"]

    proc_old = cache / "processed" / "2026-01-10"
    proc_new = cache / "processed" / "2026-11-02"
    for d in (proc_old, proc_new):
        d.mkdir(parents=True)
        (d / "rec.json").write_text("{}")
    backups = tmp_path / "backups"
    backups.mkdir()
    b_old, b_new = backups / "plannerboard_20260110.db", backups / "plannerboard_20261102.db"
    for b in (b_old, b_new):
        b.write_text("x")
    os.utime(b_old, (NOW - 200 * 86400, NOW - 200 * 86400))
    os.utime(b_new, (NOW - 2 * 86400, NOW - 2 * 86400))

    out = lifecycle.purge(st, {"mail": {"store": {"body_days": 90}}}, cache, backups, now=NOW)
    assert out["bodies_dropped"] == 1 and out["processed_dirs"] == 1 and out["backups"] == 1
    assert _ids(index.search(st, "zebramango", now=NOW)) == ["n1"]          # FTS body gone
    assert _ids(index.search(st, "zebra review", now=NOW)) == ["o1"]        # headers stay
    assert not index.search(st, "secret", now=NOW)["attachments"]           # attachment text gone
    row = st.get("gmail", "o1")
    assert row["snippet"] == "" and row["body_state"] == "headers"
    assert not proc_old.exists() and proc_new.exists()
    assert not b_old.exists() and b_new.exists()
    # The text is not left in the file either (secure_delete + optimize).
    st._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert b"zebramango figures" not in store_path(cache).read_bytes()


def test_purge_account(tmp_path):
    cache = tmp_path / "mail_cache"
    st = open_store(cache)
    st.ingest("work", _msg("w1", "Work thing", "work body"))
    st.ingest("home", _msg("h1", "Home thing", "home body"))
    (cache / "state_work.json").write_text("{}")
    out = lifecycle.purge_account(st, cache, "work")
    assert out["messages_removed"] == 1 and not (cache / "state_work.json").exists()
    assert _ids(index.search(st, "thing", now=NOW)) == ["h1"]


# ── per-account ──────────────────────────────────────────────────────────────

class TokenRouter:
    """Two Gmail mailboxes behind one fake transport, chosen by bearer token."""

    def __init__(self, servers):
        self.servers = servers
        self.seen_tokens = []

    def __call__(self, method, url, data=None, headers=None, form=False, timeout=30, raw=False):
        tok = ((headers or {}).get("Authorization") or "").replace("Bearer ", "")
        self.seen_tokens.append(tok)
        return self.servers[tok](method, url, data=data, headers=headers, form=form, timeout=timeout, raw=raw)


def test_per_account_secrets_two_gmail_accounts(tmp_path, monkeypatch):
    work, home = FakeGmail(), FakeGmail()
    work.deliver("g1", subject="Work order 17")
    home.deliver("g1", subject="Soccer schedule")            # same Gmail id, different mailbox
    router = TokenRouter({"AT-work": work, "AT-home": home})
    cache = tmp_path / "mail_cache"
    cfg = {"paths": {"mail_cache": str(cache)},
           "mail": {"gmail": {"client_id": "shared-cid"}, "store": {"backfill": False},
                    "accounts": [{"id": "work", "provider": "gmail", "email": "sam@work.example"},
                                 {"id": "home", "provider": "gmail", "email": "sam@home.example",
                                  "settings": {"client_id": "home-cid"}}]}}
    secrets.set_secret("mail_token_work", json.dumps({"access_token": "AT-work", "expires_at": 9e9}))
    secrets.set_secret("mail_token_home", json.dumps({"access_token": "AT-home", "expires_at": 9e9}))
    runner = start_all(cfg, store=None, profession={}, run_triage=False, autostart=False)
    assert set(runner.threads) == {"work", "home"}
    for t in runner.threads.values():
        t.provider.http = router
    assert runner.threads["home"].provider.client_id == "home-cid"
    assert runner.threads["work"].provider.client_id == "shared-cid"
    for t in runner.threads.values():
        t.run_once()
    st = open_store(cache)
    assert st.get("work", "g1")["subject"] == "Work order 17"
    assert st.get("home", "g1")["subject"] == "Soccer schedule"
    assert (cache / "state_work.json").exists() and (cache / "state_home.json").exists()
    assert set(router.seen_tokens) == {"AT-work", "AT-home"}
    assert secrets.get_secret("gmail_token")[0] is None      # nothing fell back to the 1.x slot
    assert _ids(index.search(st, "soccer", now=NOW, account="home")) == ["g1"]
    assert _ids(index.search(st, "soccer", now=NOW, account="work")) == []


def test_legacy_single_account_keeps_its_keys(tmp_path):
    cfg = {"paths": {"mail_cache": str(tmp_path)}, "mail": {"provider": "gmail", "gmail": {"client_id": "c"}}}
    runner = start_all(cfg, store=None, profession={}, run_triage=False, autostart=False)
    t = runner.threads["gmail"]
    assert t.account_id == "gmail" and t.state.path.name == "state_gmail.json"
    assert t.provider.tokens.key == "gmail_token"


# ── cutoff and backfill ──────────────────────────────────────────────────────

def test_capture_before_cutoff_is_store_only(app, tmp_path):
    rt = app.extensions["pb"]
    cutoff = rt.store.meta_get("install_cutoff")
    assert cutoff                                           # written on first start
    server = FakeGmail()
    server.deliver("old")                                   # Date header 2026-10-01: before the cutoff
    cache = tmp_path / "mc"
    prov = build_provider({"paths": {"mail_cache": str(cache)}, "mail": {"gmail": {"client_id": "c"}}}, "gmail")
    prov.http = server
    prov.tokens.save({"access_token": "AT", "expires_at": 9e9})
    t = ProviderThread(prov, {"paths": {"mail_cache": str(cache)}, "mail": {"store": {"backfill": False}}},
                       rt.store, {}, run_triage=False)
    s = t.run_once()
    assert s["historical"] == 1 and s["written"] == 0 and not _cache_files(cache)
    row = open_store(cache).get("gmail", "old")
    assert row["historical"] == 1 and row["captured"] == 1

    rt.store.meta_set("install_cutoff", "2026-09-01T00:00:00+00:00")
    server.deliver("new")
    t.run_once()
    assert len(_cache_files(cache)) == 1                    # after the cutoff: triage gets it


def test_backfill_creates_no_triage_or_ai_rows(app, tmp_path):
    rt = app.extensions["pb"]
    server = FakeGmail()
    for i in range(10_000):
        server.deliver(f"h{i:05d}", labels=("INBOX",) if i % 4 else ("Label_7",))
    cache = Path(rt.config.paths.mail_cache)
    prov = build_provider({"paths": {"mail_cache": str(cache)},
                           "mail": {"gmail": {"client_id": "c", "max_per_cycle": 500}}}, "gmail")
    prov.http = server
    prov.tokens.save({"access_token": "AT", "expires_at": 9e9})
    llm_calls = []

    class CountingLLM:
        def __getattr__(self, name):
            llm_calls.append(name)
            raise AssertionError(f"AI was called ({name})")
    cfg = dict(rt.config.doc)
    cfg["mail"] = {"provider": "gmail", "store": {"backfill": True, "backfill_page": 500}}
    t = ProviderThread(prov, cfg, rt.store, rt.profession(), llm=CountingLLM(), run_triage=True)
    for _ in range(60):
        t.run_once()
        if backfill.state(prov.sync, "gmail")["done"] and not t.state.get("pending_ids"):
            break
    st = prov.sync
    assert st.count("account_id='gmail' AND removed=0") == 10_000
    assert st.count("historical=1") == 10_000
    assert not _cache_files(cache)
    conn = rt.store.conn
    assert conn.execute("SELECT COUNT(*) FROM triage_cards").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 0
    assert llm_calls == []
    # The archived quarter (Label_7) came in through the backfill only, headers and body.
    row = st.get("gmail", "h00000")
    assert row["folder"] == "archive" and row["captured"] == 0 and row["body_state"] == "full"
    assert index.search(st, "\"Message h09999\"", now=NOW)["messages"]


def test_backfill_seen_first_still_reaches_triage(app, tmp_path):
    """New mail the backfill walks before the capture path must not be lost to triage."""
    rt = app.extensions["pb"]
    rt.store.meta_set("install_cutoff", "2026-09-01T00:00:00+00:00")
    server = FakeGmail()
    server.deliver("x1")
    cache = tmp_path / "mc"
    prov = build_provider({"paths": {"mail_cache": str(cache)}, "mail": {"gmail": {"client_id": "c"}}}, "gmail")
    prov.http = server
    prov.tokens.save({"access_token": "AT", "expires_at": 9e9})
    st = open_store(cache)
    msgs, _c, _d = prov.backfill_page(None, 50, None)
    for m in msgs:
        st.ingest("gmail", m)
    t = ProviderThread(prov, {"paths": {"mail_cache": str(cache)}, "mail": {"store": {"backfill": False}}},
                       rt.store, {}, run_triage=False)
    t.run_once()
    assert len(_cache_files(cache)) == 1 and st.get("gmail", "x1")["captured"] == 1


def test_imap_backfill_walks_every_folder(tmp_path):
    from .test_mail_client import RAW1, RAW2, FakeIMAP
    from plannerboard.mail.imap import ImapProvider
    secrets.set_secret("imap_password", "pw", account="sam@acme.example")
    cache = tmp_path / "mc"
    prov = ImapProvider({"mail": {"imap": {"host": "h", "user": "sam@acme.example", "preset": "custom",
                                           "sent_folder": "Sent"}}}, cache)
    fake = FakeIMAP()
    fake.folders["Trash"]["msgs"][5] = [RAW2, ["\\Seen"]]
    prov._connect = lambda timeout=45: fake
    st = open_store(cache)
    out = backfill.step(prov, st, {"mail": {"store": {"backfill_page": 1}}}, cutoff_ts=time.time(), pages=20)
    assert out["done"] and out["error"] is None
    ids = set(st.live_ids("imap"))
    assert {"inbox:7:42", "inbox:7:43", "x:Trash:10:5"} <= ids
    assert st.get("imap", "x:Trash:10:5")["folder"] == "trash"
    assert not any(c[0] == "STORE" for c in fake.log if isinstance(c, tuple))     # read-only walk


# ── store housekeeping ───────────────────────────────────────────────────────

def test_phase0_ledger_is_folded_in(tmp_path):
    legacy = tmp_path / "sync.sqlite"
    c = sqlite3.connect(legacy)
    c.executescript("""
        CREATE TABLE mail_sync_ledger (provider TEXT, item_id TEXT, folder TEXT, labels TEXT, flags TEXT,
            message_id TEXT, removed INTEGER, removed_reason TEXT, first_seen REAL, updated_at REAL,
            PRIMARY KEY (provider, item_id));
        INSERT INTO mail_sync_ledger VALUES ('gmail','g1','inbox','["INBOX"]','[]','<g1@x>',0,'',1,1);
        INSERT INTO mail_sync_ledger VALUES ('gmail','g2','',  '[]','[]','',1,'deleted',1,1);
    """)
    c.commit()
    c.close()
    st = MailStore(store_path(tmp_path))
    assert not legacy.exists()
    assert st.captured("gmail", "g1") and st.get("gmail", "g2")["removed"]
    assert st.schema_version() == 1


@pytest.mark.parametrize("version", ["1.2", "1.3"])
def test_upgrade_from_1x_database(tmp_path, monkeypatch, version):
    import shutil
    from plannerboard import create_app
    from tests.conftest import make_config
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("PB_DATA_ROOT", str(root))
    monkeypatch.setenv("PB_LICENSE_DEV", "1")
    monkeypatch.setenv("PB_TESTING", "1")
    cfg = make_config(root)
    shutil.copy(FIX / "db" / f"{version}.sqlite", cfg.paths.db_file)
    app = create_app(config=cfg, testing=True)
    rt = app.extensions["pb"]
    assert rt.store.get_project("proj_001")["name"] == "Lobby retrofit"
    assert rt.store.meta_get("install_cutoff")
    c = app.test_client()
    r = c.get("/api/mail/search?q=lobby")
    assert r.status_code == 200 and r.get_json()["ok"]


def test_search_api_routes(client):
    r = client.get("/api/mail/search?q=frm:x")
    body = r.get_json()
    assert r.status_code == 200 and body["hints"] and "coverage" in body
    u = client.get("/api/mail/store/usage").get_json()
    assert u["ok"] and "store" in u["bytes"] and "sentence" in u["coverage"]
    assert client.post("/api/mail/store/reindex", json={}).status_code in (200, 403)


# ── encryption (only where SQLCipher is installed; CI proves the Windows build) ──

def test_store_encryption_at_rest(tmp_path, monkeypatch):
    pytest.importorskip("sqlcipher3")
    p = store_path(tmp_path)
    plain = MailStore(p, encrypt=False)
    plain.ingest("gmail", _msg("e0", "Before encryption", "plainwords here"))
    plain.close()
    assert b"plainwords" in p.read_bytes() or b"plainwords" in Path(str(p) + "-wal").read_bytes()

    st = MailStore(p, encrypt=True)                        # converts the plaintext file in place
    assert st.encrypted
    st.ingest("gmail", _msg("e1", "Secret lobby", "cipherwords inside"))
    assert {r["ref"] for r in index.search(st, "plainwords OR cipherwords", now=NOW)["messages"]} == {"e0", "e1"}
    st._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    st.close()
    raw = p.read_bytes()
    assert not raw.startswith(b"SQLite format 3") and b"cipherwords" not in raw and b"plainwords" not in raw

    again = MailStore(p, encrypt=False)                    # an encrypted store stays encrypted
    assert again.encrypted and again.get("gmail", "e1")["subject"] == "Secret lobby"
    again.close()
    secrets.delete_secret("mail_store_key")
    with pytest.raises(RuntimeError, match="encrypted"):
        MailStore(p)                                        # key gone: refuse, never a silent fresh store
