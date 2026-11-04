"""Regressions from the Phase 1 adversarial pass: each one failed before its fix."""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plannerboard.mail import index, lifecycle, secrets
from plannerboard.mail import sync as backfill
from plannerboard.mail.runner import ProviderThread, build_provider
from plannerboard.mail.store import MailStore, forget_open, open_store, store_path

from .test_store_search import NOW, _ids, _msg, mailbox  # noqa: F401  (fixture)
from .test_sync_truth import FakeGmail, _cache_files


@pytest.fixture(autouse=True)
def _mem():
    secrets.use_memory_backend(True)
    yield
    secrets.use_memory_backend(False)
    forget_open()


class Board:
    def __init__(self, cutoff):
        self.c = cutoff

    def meta_get(self, k):
        return self.c if k == "install_cutoff" else None


def _gmail(cache, server):
    prov = build_provider({"paths": {"mail_cache": str(cache)}, "mail": {"gmail": {"client_id": "c"}}}, "gmail")
    prov.http = server
    prov.tokens.save({"access_token": "AT", "expires_at": 9e9})
    return prov


def _ms(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)


def test_phase0_rows_get_indexed_by_the_backfill(tmp_path):
    cache = tmp_path / "mc"
    cache.mkdir()
    c = sqlite3.connect(cache / "sync.sqlite")
    c.executescript("""CREATE TABLE mail_sync_ledger (provider TEXT, item_id TEXT, folder TEXT, labels TEXT, flags TEXT,
        message_id TEXT, removed INTEGER, removed_reason TEXT, first_seen REAL, updated_at REAL, PRIMARY KEY (provider, item_id));
        INSERT INTO mail_sync_ledger VALUES ('gmail','g1','inbox','["INBOX"]','[]','<g1@mail.example>',0,'',1,1);""")
    c.commit()
    c.close()
    server = FakeGmail()
    server.deliver("g1", subject="Chiller quote zanzibar")
    prov = _gmail(cache, server)
    st = open_store(cache)
    assert "not indexed yet" in index.coverage_for(st)["sentence"]
    assert backfill.step(prov, st, {"mail": {"store": {}}}, cutoff_ts=time.time(), pages=5)["done"]
    assert _ids(index.search(st, "zanzibar", now=NOW)) == ["g1"]
    assert st.captured("gmail", "g1")                           # still known to capture: no second card


def test_gate_uses_server_time_not_the_date_header(tmp_path):
    cutoff = "2026-11-04T00:00:00+00:00"
    server = FakeGmail()
    # Received by the server Oct 1 (before the cutoff); the sender's clock says 2027.
    server.deliver("old1", internal_ms=_ms(2026, 10, 1), date_hdr="Fri, 01 Jan 2027 10:00:00 +0000")
    cache = tmp_path / "mc"
    t = ProviderThread(_gmail(cache, server), {"paths": {"mail_cache": str(cache)},
                                                "mail": {"store": {"backfill": False}}}, Board(cutoff), {},
                       run_triage=False)
    assert t.run_once()["historical"] == 1 and not _cache_files(cache)
    # Received after the cutoff; the sender's clock says 2024. Triage must get it.
    server.deliver("new1", internal_ms=_ms(2026, 11, 5), date_hdr="Mon, 01 Jan 2024 10:00:00 +0000")
    t.run_once()
    assert len(_cache_files(cache)) == 1


def test_drop_folder_feeds_the_store_and_honours_the_cutoff(tmp_path):
    from plannerboard.mail.message import write_record
    cache = tmp_path / "mc"
    old = _msg("pa-old", "Old drop", "drop folder body old", day="2026-10-01")
    new = _msg("pa-new", "New drop", "drop folder body new", day="2026-11-10")
    p_old, _ = write_record(cache, old)
    write_record(cache, new)
    prov = build_provider({"paths": {"mail_cache": str(cache)}, "mail": {"pa_folder": {}}}, "pa_folder")
    t = ProviderThread(prov, {"paths": {"mail_cache": str(cache)}, "mail": {"store": {"backfill": False}}},
                       Board("2026-11-04T00:00:00+00:00"), {}, run_triage=False)
    s = t.run_once()
    assert s["historical"] == 1 and s["written"] == 0
    names = [p.name for p in _cache_files(cache)]
    assert len(names) == 1 and p_old.name not in names           # the old one is set aside, not triaged
    st = open_store(cache)
    assert {r["ref"] for r in index.search(st, "drop", now=NOW)["messages"]} == {
        st.ledger_rows("pa_folder")[0]["provider_id"], st.ledger_rows("pa_folder")[1]["provider_id"]}
    t.run_once()                                                 # nothing ingested twice
    assert len(st.ledger_rows("pa_folder")) == 2


def test_negated_phrase_and_quoted_operator_values(mailbox):
    assert _ids(index.search(mailbox, 'invoice -"net 30"', now=NOW)) == ["m06"]
    assert set(_ids(index.search(mailbox, 'subject:"lobby retrofit"', now=NOW))) == {"m01", "m02"}
    assert set(_ids(index.search(mailbox, 'from:"billing@fabrikam"', now=NOW))) == {"m04", "m06"}


def test_or_between_operators_and_account_case(mailbox):
    ids = _ids(index.search(mailbox, "from:dana OR from:billing", now=NOW))
    assert {"m01", "m04", "m06"} <= set(ids) and "m07" not in ids
    assert _ids(index.search(mailbox, "account:Gmail lobby", now=NOW))
    res = index.search(mailbox, "lobby OR from:dana", now=NOW)
    assert any("not a word and an operator" in h for h in res["hints"])


def test_gmail_label_by_name(tmp_path):
    st = MailStore(store_path(tmp_path))
    m = _msg("L1", "Tagged", "body")
    m.sync_labels = ["Label_12"]
    st.ingest("gmail", m)
    st.meta_set("labels:gmail", json.dumps({"Label_12": "Clients"}))
    assert _ids(index.search(st, "label:Clients", now=NOW)) == ["L1"]


@pytest.mark.parametrize("q", ["???", "-", "@", "\"", "()", "*", "''"])
def test_punctuation_only_is_not_everything(tmp_path, q):
    st = MailStore(store_path(tmp_path))
    for i in range(3):
        st.ingest("gmail", _msg(f"p{i}", f"Subj {i}", "words"))
    res = index.search(st, q, now=NOW)
    assert _ids(res) == [] and res["hints"]


def test_account_purge_is_exact(tmp_path):
    st = MailStore(store_path(tmp_path))
    backfill._save(st, "work2", {"cursor": "abc", "done": True, "stored": 5})
    st.delete_account("work")
    assert backfill.state(st, "work2")["done"]


def test_reingest_does_not_inflate_counts(tmp_path):
    st = MailStore(store_path(tmp_path))
    m = _msg("e1", "", "no subject here", sender="x@y.example")
    for _ in range(5):
        st.ingest("gmail", m)
    assert st.query("SELECT recv_count FROM mail_people WHERE email='x@y.example'")[0]["recv_count"] == 1
    assert st.query("SELECT message_count FROM mail_threads")[0]["message_count"] == 1


def test_trailing_space_ends_prefix(mailbox):
    assert _ids(index.search(mailbox, "lob", now=NOW, typing=True))
    assert _ids(index.search(mailbox, "lob", now=NOW, typing=False)) == []


def test_header_only_backfill_keeps_has_attachment(tmp_path):
    from .test_mail_client import RAW1, FakeIMAP
    from plannerboard.mail.imap import ImapProvider
    secrets.set_secret("imap_password", "pw", account="sam@acme.example")
    cache = tmp_path / "mc"
    prov = ImapProvider({"mail": {"imap": {"host": "h", "user": "sam@acme.example", "preset": "custom",
                                           "sent_folder": "Sent"}}}, cache)
    fake = FakeIMAP()
    fake.folders["Trash"]["msgs"][5] = [RAW1, ["\\Seen"]]       # multipart/mixed with a PDF
    prov._connect = lambda timeout=45: fake
    st = open_store(cache)
    backfill.step(prov, st, {"mail": {"store": {"body_days": 1}}}, cutoff_ts=time.time(), pages=20)
    row = st.get("imap", "x:Trash:10:5")
    assert row["body_state"] == "headers" and row["has_attachments"] == 1
