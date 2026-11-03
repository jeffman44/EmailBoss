"""Regressions from the Phase 0 adversarial review: each test reproduced a
loss or duplicate before its fix."""
import pytest

from plannerboard.mail.retry_queue import backoff_seconds
from .test_sync_truth import (FakeGmail, FakeGraph, _gmail, _thread, _cache_files,
                                   _graph_item, _mem_secrets)
from plannerboard.mail.graph import GraphProvider


def test_A_retry_done_before_consumer_takes_it(tmp_path):
    """Generator closed (or process killed) right after a retry is yielded."""
    now = [1_000_000.0]
    s = FakeGmail(); s.deliver("g1"); s.fail_fetch["g1"] = 1
    prov, cache = _gmail(tmp_path, s, clock=lambda: now[0])
    t = _thread(prov, cache)
    t.run_once()
    assert prov.retry.stats()["pending"] == 1
    now[0] += backoff_seconds(1) + 1
    gen = prov.fetch_new(t.state)
    msg = next(gen)          # the retried g1, not yet written by the consumer
    gen.close()              # consumer dies before write_record
    # g1 is now nowhere: not in retry queue, not in pending, not in the ledger
    assert prov.retry.stats()["pending"] == 1 or "g1" in (t.state.get("pending_ids") or []), \
        "retried message dropped: queue row deleted before the consumer took it"


def test_B_retry_of_archived_msg_marks_removed_then_never_recaptured(tmp_path):
    now = [1_000_000.0]
    s = FakeGmail(); s.deliver("g0")
    prov, cache = _gmail(tmp_path, s, clock=lambda: now[0])
    t = _thread(prov, cache); t.run_once()
    s.deliver("g1"); s.fail_fetch["g1"] = 1
    t.run_once()                                   # g1 fails -> retry queue
    s.relabel("g1", remove=("INBOX",))             # user archives it
    now[0] += backoff_seconds(1) + 1
    t.run_once()                                   # retry: not in inbox -> "gone on retry"
    s.relabel("g1", add=("INBOX",))                # user moves it back to inbox
    t.run_once(); t.run_once()
    row = prov.sync.get("gmail", "g1")
    assert row is not None and not row["removed"], "g1 sits in INBOX on the server but is never captured"


def test_C_graph_change_after_failed_convert_duplicates(tmp_path, monkeypatch):
    now = [1_000_000.0]
    g = FakeGraph()
    item = _graph_item("m9")
    g.pages = {
        "/me/mailFolders/inbox/messages/delta": {"value": [item],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d-inbox-2"},
        "/me/mailFolders/sentitems/messages/delta": {"value": [],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d-sent-2"},
        "/d-inbox-2": {"value": [_graph_item("m9", read=True)],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d-inbox-3"},
        "/d-inbox-3": {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d-inbox-3"},
        "/d-sent-2": {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d-sent-2"},
        "/me/messages/m9": _graph_item("m9"),
    }
    cache = tmp_path / "mail_cache"
    prov = GraphProvider({"mail": {"graph": {"client_id": "cid"}}}, cache, http=g)
    prov.tokens.save({"access_token": "AT", "expires_at": 9_999_999_999})
    prov.clock = lambda: now[0]
    real = prov._to_message
    calls = [0]
    def flaky(it, kind):
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("transient attachment fetch")
        return real(it, kind)
    monkeypatch.setattr(prov, "_to_message", flaky)
    t = _thread(prov, cache)
    t.run_once()                    # m9 fails -> retry
    t.run_once()                    # user read it: delta re-reports m9 -> captured
    now[0] += backoff_seconds(1) + 1
    t.run_once()                    # retry still queued -> captured again
    assert len(_cache_files(cache)) == 1, f"{len(_cache_files(cache))} cache files for one message"


def test_D_gmail_transient_history_error_loses_delete(tmp_path):
    s = FakeGmail()
    s.deliver("g1"); s.deliver("g2")
    prov, cache = _gmail(tmp_path, s)
    t = _thread(prov, cache); t.run_once()
    s.delete("g1")
    orig = s.__call__
    fail = [True]
    def http(method, url, **kw):
        if "/history" in url and fail[0]:
            fail[0] = False
            return 503, {"error": "backend"}
        return orig(method, url, **kw)
    prov.http = http
    t.run_once()                    # one 503 on /history
    t.run_once(); t.run_once()
    assert prov.sync.get("gmail", "g1")["removed"], "delete event skipped by the date fallback; cursor jumped"



def test_F_imap_folder_switch_same_uidvalidity_loses_mail(tmp_path):
    from plannerboard.mail import secrets
    from plannerboard.mail.imap import ImapProvider
    from .test_mail_client import RAW1, RAW2, FakeIMAP
    secrets.set_secret("imap_password", "pw", account="sam@acme.example")
    cache = tmp_path / "mail_cache"
    cfg = {"mail": {"imap": {"host": "imap.acme.example", "user": "sam@acme.example",
                             "preset": "custom", "sent_folder": "Sent"}}}
    prov = ImapProvider(cfg, cache)
    fake = FakeIMAP()
    fake.folders["Sent"] = {"uv": 1, "msgs": {1: [RAW1, ["\\Seen"]], 2: [RAW2, ["\\Seen"]]}}
    prov._connect = lambda timeout=45: fake
    t = _thread(prov, cache); t.run_once()
    assert len(_cache_files(cache, "sent")) == 2
    # user points sent at another mailbox; server reuses uidvalidity 1 (per-mailbox constant)
    fake.folders["Sent Items"] = {"uv": 1, "msgs": {1: [RAW2.replace(b"Subject:", b"Subject: other"), ["\\Seen"]],
                                                    2: [RAW1, ["\\Seen"]], 3: [RAW1, []]}}
    prov.settings["sent_folder"] = "Sent Items"
    t.run_once(); t.run_once()
    assert len(_cache_files(cache, "sent")) == 5, f"{len(_cache_files(cache, 'sent'))} files; new mailbox UIDs 1-2 skipped as already captured"


def test_G_imap_fetch_NO_is_silent_loss(tmp_path):
    from plannerboard.mail import secrets
    from plannerboard.mail.imap import ImapProvider
    from .test_mail_client import FakeIMAP
    secrets.set_secret("imap_password", "pw", account="sam@acme.example")
    cache = tmp_path / "mail_cache"
    prov = ImapProvider({"mail": {"imap": {"host": "imap.acme.example", "user": "sam@acme.example",
                                           "preset": "custom", "sent_folder": "Sent"}}}, cache)
    fake = FakeIMAP()
    real_uid = fake.uid
    hits = [0]
    def uid(cmd, *args):
        if cmd.upper() == "FETCH" and args[0] == "43" and "BODY.PEEK" in args[1] and hits[0] == 0:
            hits[0] += 1
            return "NO", [b"Some messages could not be FETCHed (Failure)"]
        return real_uid(cmd, *args)
    fake.uid = uid
    prov._connect = lambda timeout=45: fake
    t = _thread(prov, cache)
    for _ in range(4):
        t.run_once()
    assert prov.sync.has("imap", "inbox:7:43") or prov.retry.stats()["pending"] == 1, \
        "transient FETCH NO: uid 43 skipped, watermark past it, no retry, no warning"
