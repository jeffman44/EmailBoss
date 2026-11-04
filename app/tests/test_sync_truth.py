"""2.0 Phase 0 exit tests: sync truth.

Stateful fake servers (no network) for Gmail, Graph and IMAP, driven
through the real capture path (``ProviderThread.run_once``, triage off) so
the assertions are about what reaches the sync ledger and the mail cache.
"""
import base64
import urllib.parse
from pathlib import Path

import pytest

from plannerboard.mail import reconcile, secrets
from plannerboard.mail.gmail import GmailProvider
from plannerboard.mail.graph import GraphProvider
from plannerboard.mail.imap import ImapProvider
from plannerboard.mail.retry_queue import backoff_seconds
from plannerboard.mail.runner import ProviderThread

from .test_mail_client import RAW1, RAW2, FakeIMAP


@pytest.fixture(autouse=True)
def _mem_secrets():
    secrets.use_memory_backend(True)
    yield
    secrets.use_memory_backend(False)


def _b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _cache_files(cache: Path, folder="inbox"):
    d = cache / folder
    return sorted(p for p in d.glob("*.json")) if d.is_dir() else []


def _thread(prov, cache, **mail):
    # The capture path only: the store backfill job has its own tests.
    mail.setdefault("store", {"backfill": False})
    cfg = {"paths": {"mail_cache": str(cache)}, "mail": dict(mail)}
    return ProviderThread(prov, cfg, store=None, profession={}, run_triage=False, poll_seconds=60)


# ── a stateful Gmail ─────────────────────────────────────────────────────────

class FakeGmail:
    """messages + an append-only history log, the way users.history works."""

    def __init__(self):
        self.msgs: dict[str, dict] = {}
        self.history: list[dict] = []     # each {"id": int, ...event}
        self.hid = 1000
        self.calls: list[str] = []
        self.fail_fetch: dict[str, int] = {}   # gid -> remaining failures

    # server-side mutations
    def deliver(self, gid, labels=("INBOX", "UNREAD"), subject=None):
        self.msgs[gid] = {"labels": set(labels), "subject": subject or f"Message {gid}"}
        self.hid += 1
        self.history.append({"id": self.hid, "messagesAdded": [
            {"message": {"id": gid, "labelIds": sorted(labels)}}]})

    def delete(self, gid):
        self.msgs.pop(gid, None)
        self.hid += 1
        self.history.append({"id": self.hid, "messagesDeleted": [{"message": {"id": gid}}]})

    def relabel(self, gid, add=(), remove=()):
        m = self.msgs[gid]
        m["labels"] = (m["labels"] | set(add)) - set(remove)
        self.hid += 1
        ev = {"id": self.hid}
        msg = {"id": gid, "labelIds": sorted(m["labels"])}
        if add:
            ev["labelsAdded"] = [{"message": msg, "labelIds": list(add)}]
        if remove:
            ev["labelsRemoved"] = [{"message": msg, "labelIds": list(remove)}]
        self.history.append(ev)

    def _full(self, gid):
        m = self.msgs[gid]
        return {"id": gid, "threadId": f"t{gid}", "labelIds": sorted(m["labels"]),
                "internalDate": "1759330000000", "snippet": m["subject"],
                "payload": {"mimeType": "text/plain",
                            "headers": [{"name": "From", "value": "Dana <dana@northwind.example>"},
                                        {"name": "To", "value": "sam@acme.example"},
                                        {"name": "Subject", "value": m["subject"]},
                                        {"name": "Message-ID", "value": f"<{gid}@mail.example>"},
                                        {"name": "Date", "value": "Wed, 01 Oct 2026 10:00:00 -0600"}],
                            "body": {"data": _b64url(f"body of {gid}".encode())}}}

    def __call__(self, method, url, data=None, headers=None, form=False, timeout=30, raw=False):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.split("/gmail/v1/users/me", 1)[-1]
        q = urllib.parse.parse_qs(parsed.query)
        self.calls.append(url)
        if path == "/profile":
            return 200, {"emailAddress": "sam@acme.example", "historyId": str(self.hid),
                         "messagesTotal": len(self.msgs)}
        if path.startswith("/labels/"):
            label = path.rsplit("/", 1)[1]
            return 200, {"id": label, "messagesTotal": sum(1 for m in self.msgs.values() if label in m["labels"])}
        if path == "/history":
            start = int(q["startHistoryId"][0])
            page_size = int(q.get("maxResults", ["500"])[0])
            offset = int(q.get("pageToken", ["0"])[0])
            rows = [h for h in self.history if h["id"] > start]
            page = rows[offset:offset + page_size]
            body = {"history": [{k: v for k, v in h.items() if k != "id"} for h in page],
                    "historyId": str(self.hid)}
            if offset + page_size < len(rows):
                body["nextPageToken"] = str(offset + page_size)
            return 200, body
        if path == "/messages":
            if "q" in q:          # the capture path's dated window: inbox and sent only
                ids = [g for g, m in self.msgs.items() if m["labels"] & {"INBOX", "SENT"}]
            else:                 # no query: the whole mailbox (backfill, list views)
                ids = list(self.msgs)
            ids.reverse()   # newest first, like the API
            page_size = int(q.get("maxResults", ["100"])[0])
            offset = int(q.get("pageToken", ["0"])[0])
            body = {"messages": [{"id": g} for g in ids[offset:offset + page_size]]}
            if offset + page_size < len(ids):
                body["nextPageToken"] = str(offset + page_size)
            return 200, body
        if path.startswith("/messages/"):
            gid = path.split("/")[2]
            if self.fail_fetch.get(gid, 0) > 0:
                self.fail_fetch[gid] -= 1
                return 500, {"error": "backend error"}
            if gid not in self.msgs:
                return 404, {"error": "not found"}
            return 200, self._full(gid)
        return 404, {"error": f"no route {method} {url}"}


def _gmail(tmp_path, server, cap=100, clock=None):
    cache = tmp_path / "mail_cache"
    prov = GmailProvider({"mail": {"gmail": {"client_id": "cid", "max_per_cycle": cap}}}, cache, http=server)
    prov.tokens.save({"access_token": "AT", "expires_at": 9_999_999_999})
    if clock is not None:
        prov.clock = clock
    return prov, cache


def _drain(thread, max_cycles=50):
    for _ in range(max_cycles):
        thread.run_once()
        if not thread.state.get("pending_ids"):
            return
    raise AssertionError("queue never drained")


def test_gmail_burst_over_cap_loses_nothing(tmp_path):
    server = FakeGmail()
    server.deliver("g0")
    prov, cache = _gmail(tmp_path, server, cap=100)
    t = _thread(prov, cache)
    t.run_once()                                   # first sync: g0 captured, cursor set
    assert len(_cache_files(cache)) == 1

    for i in range(1, 251):                        # a burst of 250 against a cap of 100
        server.deliver(f"g{i}")
    t.run_once()
    assert len(_cache_files(cache)) == 101
    assert t.status()["queued_ids"] == 150         # the rest is queued, not dropped
    assert t.state.get("history_id") == str(server.hid)

    for i in range(251, 261):                      # more mail lands while the queue drains
        server.deliver(f"g{i}")
    _drain(t)

    live = prov.sync.live_ids("gmail", folder="inbox")
    assert sorted(live) == sorted(f"g{i}" for i in range(261))
    assert len(_cache_files(cache)) == 261         # every message captured exactly once
    t.run_once()                                   # a quiet cycle captures nothing more
    assert len(_cache_files(cache)) == 261


def test_gmail_first_sync_over_cap_loses_nothing(tmp_path):
    server = FakeGmail()
    for i in range(230):
        server.deliver(f"g{i}")
    prov, cache = _gmail(tmp_path, server, cap=100)
    t = _thread(prov, cache)
    _drain(t)
    assert len(_cache_files(cache)) == 230


def test_gmail_label_and_delete_events(tmp_path):
    server = FakeGmail()
    for gid in ("g1", "g2", "g3"):
        server.deliver(gid)
    server.deliver("g4", labels=("Label_9",))      # never captured: not inbox or sent
    prov, cache = _gmail(tmp_path, server)
    t = _thread(prov, cache)
    t.run_once()
    assert sorted(prov.sync.live_ids("gmail")) == ["g1", "g2", "g3"]

    server.delete("g1")
    server.relabel("g2", remove=("INBOX",))        # archived
    server.relabel("g3", add=("STARRED",), remove=("UNREAD",))
    server.relabel("g4", add=("INBOX",))           # moved into the inbox: now captured
    t.run_once()

    hist_calls = [u for u in server.calls if "/history" in u]
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(hist_calls[-1]).query)
    assert set(qs["historyTypes"]) == {"messageAdded", "messageDeleted", "labelAdded", "labelRemoved"}

    g1, g2, g3, g4 = (prov.sync.get("gmail", g) for g in ("g1", "g2", "g3", "g4"))
    assert g1["removed"] and g1["removed_reason"] == "deleted"
    assert not g2["removed"] and g2["folder"] == "archive" and "INBOX" not in g2["labels"]
    assert "STARRED" in g3["labels"] and "UNREAD" not in g3["labels"] and g3["folder"] == "inbox"
    assert g4 is not None and not g4["removed"] and g4["folder"] == "inbox"
    assert prov.sync.local_counts("gmail") == {"inbox": 2, "archive": 1}


def test_failed_fetch_retried(tmp_path):
    now = [1_000_000.0]
    server = FakeGmail()
    server.deliver("g1")
    server.deliver("g2")
    server.fail_fetch["g2"] = 2                    # fails twice, then works
    prov, cache = _gmail(tmp_path, server, clock=lambda: now[0])
    t = _thread(prov, cache)

    t.run_once()
    assert prov.sync.live_ids("gmail") == ["g1"]
    stats = t.status()["sync"]["retry"]
    assert stats["pending"] == 1 and stats["gave_up"] == 0

    t.run_once()                                   # backoff not elapsed: not asked for again
    assert [u for u in server.calls if u.endswith("/messages/g2?format=full")].__len__() == 1

    now[0] += backoff_seconds(1) + 1
    t.run_once()                                   # second failure, longer backoff
    assert prov.retry.stats()["pending"] == 1
    now[0] += backoff_seconds(2) + 1
    t.run_once()                                   # third try succeeds
    assert sorted(prov.sync.live_ids("gmail")) == ["g1", "g2"]
    assert prov.retry.stats() == {"pending": 0, "gave_up": 0, "next_retry_at": None, "gave_up_items": []}
    assert len(_cache_files(cache)) == 2


def test_failed_fetch_gives_up_visibly(tmp_path):
    now = [1_000_000.0]
    server = FakeGmail()
    server.deliver("g1")
    server.fail_fetch["g1"] = 99
    prov, cache = _gmail(tmp_path, server, clock=lambda: now[0])
    t = _thread(prov, cache)
    for attempt in range(1, 12):
        t.run_once()
        now[0] += backoff_seconds(attempt) + 1
    stats = t.status()["sync"]["retry"]
    assert stats["pending"] == 0 and stats["gave_up"] == 1
    assert stats["gave_up_items"][0]["item_id"] == "g1"
    assert any("gave up" in w for w in t.status()["warnings"])


def test_retry_of_deleted_message_marks_it_removed(tmp_path):
    now = [1_000_000.0]
    server = FakeGmail()
    server.deliver("g1")
    server.fail_fetch["g1"] = 1
    prov, cache = _gmail(tmp_path, server, clock=lambda: now[0])
    t = _thread(prov, cache)
    t.run_once()
    server.msgs.pop("g1")                          # gone before the retry (no history event)
    now[0] += backoff_seconds(1) + 1
    t.run_once()
    assert prov.retry.stats()["pending"] == 0
    assert prov.sync.get("gmail", "g1")["removed"]


def test_write_failure_requeues_fetch(tmp_path, monkeypatch):
    now = [1_000_000.0]
    server = FakeGmail()
    server.deliver("g1")
    prov, cache = _gmail(tmp_path, server, clock=lambda: now[0])
    t = _thread(prov, cache)
    from plannerboard.mail import runner as runner_mod
    real = runner_mod.write_record
    monkeypatch.setattr(runner_mod, "write_record", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    t.run_once()
    assert prov.retry.stats()["pending"] == 1 and not prov.sync.live_ids("gmail")
    monkeypatch.setattr(runner_mod, "write_record", real)
    now[0] += backoff_seconds(1) + 1
    t.run_once()
    assert prov.sync.live_ids("gmail") == ["g1"] and len(_cache_files(cache)) == 1


def test_reconcile_counts_server_vs_local(tmp_path):
    server = FakeGmail()
    for i in range(5):
        server.deliver(f"g{i}")
    server.deliver("s1", labels=("SENT",))
    prov, cache = _gmail(tmp_path, server, cap=3)
    t = _thread(prov, cache)
    t.run_once()                                   # 3 of 6 captured, reconcile runs once
    rec = reconcile.latest(prov.sync, "gmail")
    assert rec["inbox"]["server_total"] == 5 and rec["inbox"]["not_local"] == 5 - rec["inbox"]["local_total"]
    assert rec["sent"]["server_total"] == 1
    _drain(t)
    out = reconcile.run(prov, prov.sync)
    assert out["folders"]["inbox"] == {"server_total": 5, "local_total": 5, "not_local": 0, "error": ""}
    assert out["folders"]["sent"]["not_local"] == 0


def test_reconcile_records_error_not_zero(tmp_path):
    class Broken:
        id = "gmail"

        def server_counts(self):
            raise RuntimeError("quota")
    from plannerboard.mail.syncdb import SyncDB
    sync = SyncDB(tmp_path)
    out = reconcile.run(Broken(), sync)
    assert out["folders"]["inbox"]["not_local"] is None and "quota" in out["folders"]["inbox"]["error"]


# ── Graph delta ──────────────────────────────────────────────────────────────

def _graph_item(gid, read=False, subject="Lease renewal"):
    return {"id": gid, "receivedDateTime": "2026-10-01T16:00:00Z", "hasAttachments": False,
            "internetMessageId": f"<{gid}@mail.example>", "subject": subject, "bodyPreview": "hi",
            "importance": "normal", "conversationId": "c1", "isRead": read, "isDraft": False,
            "flag": {"flagStatus": "notFlagged"},
            "body": {"contentType": "text", "content": "hi"},
            "from": {"emailAddress": {"address": "dana@northwind.example", "name": "Dana"}},
            "toRecipients": [{"emailAddress": {"address": "sam@acme.example"}}], "attachments": []}


class FakeGraph:
    def __init__(self):
        self.pages = {}            # url suffix -> page
        self.calls = []

    def __call__(self, method, url, data=None, headers=None, form=False, timeout=30, raw=False):
        self.calls.append(url)
        path = url.split("?")[0]
        for suffix, page in self.pages.items():
            if path.endswith(suffix) or url.endswith(suffix):
                return (200, page) if not callable(page) else page()
        return 404, {"error": f"no route {url}"}


def test_graph_removed_reaches_store(tmp_path):
    g = FakeGraph()
    g.pages = {
        "/me/mailFolders/inbox/messages/delta": {"value": [_graph_item("m1"), _graph_item("m2")],
                                                 "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d-inbox-2"},
        "/me/mailFolders/sentitems/messages/delta": {"value": [],
                                                     "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d-sent-2"},
        "/d-sent-2": {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d-sent-2"},
        "/d-inbox-2": {"value": [{"id": "m1", "@removed": {"reason": "deleted"}},
                                 _graph_item("m2", read=True)],
                       "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d-inbox-3"},
        "/me/mailFolders/inbox": {"totalItemCount": 1},
        "/me/mailFolders/sentitems": {"totalItemCount": 0},
    }
    cache = tmp_path / "mail_cache"
    prov = GraphProvider({"mail": {"graph": {"client_id": "cid"}}}, cache, http=g)
    prov.tokens.save({"access_token": "AT", "expires_at": 9_999_999_999})
    t = _thread(prov, cache)
    t.run_once()
    assert sorted(prov.sync.live_ids("graph")) == ["m1", "m2"]
    t.run_once()
    m1, m2 = prov.sync.get("graph", "m1"), prov.sync.get("graph", "m2")
    assert m1["removed"] and m1["removed_reason"] == "graph deleted"
    assert not m2["removed"] and "\\Seen" in m2["flags"]
    assert len(_cache_files(cache)) == 2           # the read-state change was not a second capture
    assert prov.sync.local_counts("graph") == {"inbox": 1}
    assert reconcile.run(prov, prov.sync)["folders"]["inbox"]["not_local"] == 0


def test_graph_bad_item_is_retried(tmp_path):
    now = [1_000_000.0]
    bad = _graph_item("m9")
    bad["receivedDateTime"] = None
    bad["sentDateTime"] = None
    bad["from"] = "not a dict"                     # makes conversion raise
    g = FakeGraph()
    g.pages = {
        "/me/mailFolders/inbox/messages/delta": {"value": [bad],
                                                 "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d-inbox-2"},
        "/me/mailFolders/sentitems/messages/delta": {"value": [],
                                                     "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d-sent-2"},
        "/d-inbox-2": {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d-inbox-2"},
        "/d-sent-2": {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d-sent-2"},
        "/me/messages/m9": _graph_item("m9"),
    }
    cache = tmp_path / "mail_cache"
    prov = GraphProvider({"mail": {"graph": {"client_id": "cid"}}}, cache, http=g)
    prov.tokens.save({"access_token": "AT", "expires_at": 9_999_999_999})
    prov.clock = lambda: now[0]
    t = _thread(prov, cache)
    t.run_once()
    assert prov.retry.stats()["pending"] == 1
    now[0] += backoff_seconds(1) + 1
    t.run_once()
    assert prov.sync.live_ids("graph") == ["m9"] and prov.retry.stats()["pending"] == 0


# ── IMAP ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def imap(tmp_path):
    secrets.set_secret("imap_password", "pw", account="sam@acme.example")
    cache = tmp_path / "mail_cache"
    prov = ImapProvider({"mail": {"imap": {"host": "imap.acme.example", "user": "sam@acme.example",
                                           "preset": "custom", "sent_folder": "Sent"}}}, cache)
    fake = FakeIMAP()
    prov._connect = lambda timeout=45: fake
    return prov, fake, cache


def test_imap_uidvalidity_reset_reindexes(imap):
    prov, fake, cache = imap
    t = _thread(prov, cache)
    t.run_once()
    assert sorted(prov.sync.live_ids("imap", folder="inbox")) == ["inbox:7:42", "inbox:7:43"]

    # The server rebuilt the mailbox: new UIDVALIDITY, renumbered UIDs.
    fake.folders["INBOX"] = {"uv": 70, "msgs": {1: [RAW1, ["\\Seen"]], 2: [RAW2, []]}}
    t.run_once()
    live = sorted(prov.sync.live_ids("imap", folder="inbox"))
    assert live == ["inbox:70:1", "inbox:70:2"]
    for old in ("inbox:7:42", "inbox:7:43"):
        row = prov.sync.get("imap", old)
        assert row["removed"] and row["removed_reason"] == "uidvalidity reset"
    assert t.state.folder("inbox")["uidvalidity"] == 70
    assert prov.sync.local_counts("imap") == {"inbox": 2}
    assert any("UIDVALIDITY changed" in w for w in t.status()["warnings"])


def test_imap_expunge_and_flags_propagate(imap):
    prov, fake, cache = imap
    t = _thread(prov, cache)
    t.run_once()
    fake.folders["INBOX"]["msgs"].pop(42)           # expunged by another client
    fake.folders["INBOX"]["msgs"][43][1].append("\\Flagged")
    t.run_once()
    assert prov.sync.get("imap", "inbox:7:42")["removed"]
    assert "\\Flagged" in prov.sync.get("imap", "inbox:7:43")["flags"]
    assert not any(c[0] == "EXPUNGE" for c in fake.log if isinstance(c, tuple))
    assert len(_cache_files(cache)) == 2            # nothing captured twice


def test_imap_server_counts(imap):
    prov, fake, cache = imap
    assert prov.server_counts() == {"inbox": 2, "sent": 0}
