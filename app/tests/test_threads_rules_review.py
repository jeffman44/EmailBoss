"""Regressions from the Phase 3 adversarial review (threads, rules, waiting-on)."""
from plannerboard.mail import rules, secrets
from plannerboard.mail import threading as thr
from plannerboard.mail.imap import ImapProvider
from plannerboard.mail.store import open_store
from tests.test_mail_client import RAW1, RAW2, FakeIMAP
from tests.test_sync_truth import _thread
from tests.test_terminal import _api, _mem, _terminal_app  # noqa: F401
import pytest
import time

from tests.test_threads_rules import NOW_MS, _proj_snapshot, _project, _reply, _rule, _sync


# ── from the Phase 3 review: test_p3_threading ──
def _resync(rt, prov, st, gids):
    for gid in gids:
        st.ingest("gmail", prov.fetch_item(gid), historical=False, with_body=True, captured=True)


def test_split_of_middle_message_holds_on_resync(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    c, h = _api(app)
    base = NOW_MS()
    server.deliver("a1", subject="Chiller bid", internal_ms=base)
    _reply(server, "a2", "a1", "Re: Chiller bid", base + 1000)
    _reply(server, "a3", "a2", "Re: Chiller bid", base + 2000)
    _sync(rt)
    st = open_store(prov.cache_dir)
    key = lambda r: st.get("gmail", r)["thread_key"]  # noqa: E731
    c.post("/api/mail/thread/split", json={"ref": "a2"}, headers=h)
    before = (key("a1"), key("a2"), key("a3"))
    assert before[2] == before[0]                      # split took only a2
    _resync(rt, prov, st, ["a3"])                       # any re-ingest (reconcile, flag change, reinstall)
    after = (key("a1"), key("a2"), key("a3"))
    assert after == before, ("a3 silently followed a2 into the split thread on re-sync", before, after)


def test_split_of_root_holds_on_resync(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    c, h = _api(app)
    base = NOW_MS()
    server.deliver("a1", subject="Chiller bid", internal_ms=base)
    _reply(server, "a2", "a1", "Re: Chiller bid", base + 1000)
    _sync(rt)
    st = open_store(prov.cache_dir)
    key = lambda r: st.get("gmail", r)["thread_key"]  # noqa: E731
    c.post("/api/mail/thread/split", json={"ref": "a1"}, headers=h)
    k2 = key("a2")
    _resync(rt, prov, st, ["a2"])
    assert key("a2") == k2, "splitting a1 out pulls its reply a2 along after a re-sync"


def test_merge_undo_after_further_mail_is_complete(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    c, h = _api(app)
    base = NOW_MS()
    server.deliver("a1", subject="Chiller bid", internal_ms=base)
    server.deliver("b1", subject="Roof access", internal_ms=base + 1000)
    _sync(rt)
    st = open_store(prov.cache_dir)
    key = lambda r: st.get("gmail", r)["thread_key"]  # noqa: E731
    kb = key("b1")
    r = c.post("/api/mail/thread/merge", json={"ref": "b1", "into": "a1"}, headers=h).get_json()
    _reply(server, "b2", "b1", "Re: Roof access", base + 5000)   # reply to the merged message
    _sync(rt)
    assert key("b2") == key("a1")
    c.post(f"/api/mail/undo/{r['action_id']}", json={}, headers=h)
    assert key("b1") == kb
    assert key("b2") == kb, "after undo, b2 (a reply to b1) is stranded in the chiller thread"


def test_outsider_references_cannot_join_filed_thread(tmp_path, monkeypatch):
    """Anyone who knows (or guesses) a Message-ID can put their mail in a filed,
    waiting thread: it inherits the project and completes the nudge."""
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    c, h = _api(app)
    pid = _project(rt, closed=False)
    base = NOW_MS()
    server.deliver("w1", subject="Need the submittal", internal_ms=base, labels=("SENT",),
                   sender="Sam <sam@acme.example>")
    rt.store.save_correspondence(pid, {"message_id": "<w1@mail.example>", "subject": "Need the submittal"})
    _sync(rt)
    r = c.post("/api/mail/thread/waiting", json={"ref": "w1", "days": 3}, headers=h).get_json()
    assert r["task"]
    st = open_store(prov.cache_dir)
    # Unrelated sender, unrelated subject, only a References header.
    server.deliver("evil", subject="Cheap toner!!!", internal_ms=NOW_MS() + 60000,
                   sender="Spam <spam@evil.example>", headers=[("References", "<w1@mail.example>")])
    _sync(rt)
    k_evil, k_w1 = st.get("gmail", "evil")["thread_key"], st.get("gmail", "w1")["thread_key"]
    task = rt.store.conn.execute("SELECT status FROM subtasks WHERE id=?", (r["task"],)).fetchone()["status"]
    assert k_evil != k_w1 and task == "pending", ("outsider joined the project thread", k_evil == k_w1,
                                                  "nudge now", task)


def test_correspondence_link_reaches_backfilled_history(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    pid = _project(rt, closed=False)
    rt.store.save_correspondence(pid, {"message_id": "<old1@mail.example>", "subject": "History"})
    server.deliver("new1", subject="Fresh", internal_ms=NOW_MS() + 5000)
    _sync(rt)                                           # first post-cutoff batch: one-time pass runs now
    st = open_store(prov.cache_dir)
    assert st.meta_get("corr_link:gmail")
    server.deliver("old1", subject="History", internal_ms=1735689600000 - 86400000)
    st.ingest("gmail", prov.fetch_item("old1"), historical=True, with_body=True, captured=False)   # what backfill does
    from plannerboard.mail import sync as backfill
    backfill._save(st, "gmail", {**backfill.state(st, "gmail"), "done": True})
    _sync(rt)                                           # the next cycle sees the finished backfill
    k = st.get("gmail", "old1")["thread_key"]
    assert thr.get_state(st, "gmail", k)["project_id"] == pid, "backfilled history stays homeless forever"



# ── from the Phase 3 review: test_p3_imap_rerun ──
class FakeIMAPArchive(FakeIMAP):
    def __init__(self):
        super().__init__()
        self.folders["Archive"] = {"uv": 11, "msgs": {}}

    def list(self):
        typ, lines = super().list()
        return typ, lines + [b'(\\HasNoChildren \\Archive) "/" Archive']


@pytest.fixture
def imap(tmp_path):
    secrets.set_secret("imap_password", "pw", account="sam@acme.example")
    cache = tmp_path / "mail_cache"
    prov = ImapProvider({"mail": {"imap": {"host": "imap.acme.example", "user": "sam@acme.example",
                                           "preset": "custom", "sent_folder": "Sent", "archive": "Archive"}}}, cache)
    fake = FakeIMAPArchive()
    prov._connect = lambda timeout=45: fake
    return prov, fake, cache


def _imap_rule(st, actions):
    rule = {"name": "Lobby", "conditions": [{"field": "subject", "value": "Lobby"}], "actions": actions}
    r = rules.save_rule(st, rule, dry_run_ack=rules.dry_run(st, "imap", rule)["ack_token"])
    with st._conn() as c:   # the rule predates the mail (the fake's mail is dated Oct 2026)
        c.execute("UPDATE mail_rules SET created_at=0")
    return r


def test_uidvalidity_reset_does_not_rerun_rules(imap):
    prov, fake, cache = imap
    t = _thread(prov, cache)
    t.run_once()                      # create the store
    st = prov.sync
    _imap_rule(st, [{"type": "label", "value": "lobby"}, {"type": "star"}])
    fake.folders["INBOX"]["msgs"][44] = [RAW1.replace(b"<i1@", b"<i3@"), []]
    t.run_once()
    assert len(rules.recent_runs(st, "imap")) == 1
    fake.folders["INBOX"] = {"uv": 70, "msgs": {1: [RAW1, ["\\Seen"]], 2: [RAW2, []],
                                                3: [RAW1.replace(b"<i1@", b"<i3@"), []]}}
    t.run_once()
    runs = [r for r in rules.recent_runs(st, "imap")
            if (st.get("imap", r["ref"]) or {}).get("message_id") == "<i3@mail.example>"]
    # Same message, same Message-ID, already handled: a rule must not run twice on it.
    assert len(runs) == 1, [(r["ref"], r["done"]) for r in runs]


def test_undo_of_imap_archive_rule_sticks(imap):
    prov, fake, cache = imap
    t = _thread(prov, cache)
    t.run_once()
    st = prov.sync
    _imap_rule(st, [{"type": "archive"}])
    fake.folders["INBOX"]["msgs"][44] = [RAW1.replace(b"<i1@", b"<i3@"), []]
    t.run_once()
    runs = rules.recent_runs(st, "imap")
    assert len(runs) == 1 and runs[0]["done"][0].get("move"), runs
    from plannerboard.mail import terminal
    terminal.undo(st, prov, "imap", runs[0]["action_id"])
    assert any(b"<i3@" in raw for raw, _ in fake.folders["INBOX"]["msgs"].values())
    t.run_once()                      # next poll sees the "new" UID in INBOX
    in_inbox = any(b"<i3@" in raw for raw, _ in fake.folders["INBOX"]["msgs"].values())
    assert in_inbox, ("undo was reversed by the rule re-running on the new UID",
                      [(r["ref"], r["done"]) for r in rules.recent_runs(st, "imap")])



# ── from the Phase 3 review: test_p3_waiting_reactivates ──
def test_waiting_on_closed_project_leaves_status(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=3)
    c, h = _api(app)
    pid = _project(rt, closed=True)
    rt.store.save_correspondence(pid, {"message_id": "<t001@mail.example>", "subject": "Lobby item 001"})
    before = _proj_snapshot(rt, pid)
    r = c.post("/api/mail/thread/waiting", json={"ref": "t001", "days": 2}, headers=h).get_json()
    assert r.get("action_id"), r
    mid = _proj_snapshot(rt, pid)
    changed_by_waiting = {k: (before[k], mid[k]) for k in before if before[k] != mid[k]}
    assert not changed_by_waiting, changed_by_waiting           # a nudge never reopens a closed project
    u = c.post(f"/api/mail/undo/{r['action_id']}", json={}, headers=h).get_json()
    assert u.get("ok"), u
    after = _proj_snapshot(rt, pid)
    assert after == before, {"changed_by_waiting": changed_by_waiting,
                             "still_changed_after_undo": {k: (before[k], after[k]) for k in before if before[k] != after[k]}}



# ── from the Phase 3 review: test_p3_waiting_orphan ──
def test_reply_completes_nudge_when_project_came_from_routing(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    c, h = _api(app)
    pid = _project(rt, closed=False)
    base = NOW_MS()
    server.deliver("w1", subject="Need the submittal", internal_ms=base, labels=("SENT",),
                   sender="Sam <sam@acme.example>")
    _sync(rt)
    st = open_store(prov.cache_dir)
    key = st.get("gmail", "w1")["thread_key"]
    rt.store.routing_set(key, pid, source="approve")          # triage filed this thread
    r = c.post("/api/mail/thread/waiting", json={"ref": "w1", "days": 3}, headers=h).get_json()
    assert r["task"] and r["project_id"] == pid
    _reply(server, "w2", "w1", "Re: Need the submittal", NOW_MS() + 60000)
    _sync(rt)
    assert thr.get_state(st, "gmail", key)["waiting_until"] is None     # wait cleared ...
    status = rt.store.conn.execute("SELECT status FROM subtasks WHERE id=?", (r["task"],)).fetchone()["status"]
    assert status == "complete", "... but the nudge is still pending"



# ── from the Phase 3 review: test_p3_waiting_orphan2 ──
def _pending(rt, pid):
    return [r["text"] for r in rt.store.conn.execute(
        "SELECT text FROM subtasks WHERE project_id=? AND status='pending'", (pid,)).fetchall()]


def test_stop_waiting_completes_nudge(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=2)
    c, h = _api(app)
    pid = _project(rt, closed=False)
    rt.store.save_correspondence(pid, {"message_id": "<t001@mail.example>", "subject": "Lobby item 001"})
    st = open_store(prov.cache_dir); st.meta_set("corr_link:gmail", "")
    from plannerboard.mail import threading as thr
    thr.link_correspondence(st, rt.store, "gmail")
    r = c.post("/api/mail/thread/waiting", json={"ref": "t001", "days": 2}, headers=h).get_json()
    assert r["task"]
    c.post("/api/mail/thread/waiting", json={"ref": "t001", "off": True}, headers=h)
    assert _pending(rt, pid) == [], "the nudge outlives 'Stop waiting'"


def test_repick_waiting_moves_the_same_nudge(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=2)
    c, h = _api(app)
    pid = _project(rt, closed=False)
    rt.store.save_correspondence(pid, {"message_id": "<t001@mail.example>", "subject": "Lobby item 001"})
    st = open_store(prov.cache_dir); st.meta_set("corr_link:gmail", "")
    from plannerboard.mail import threading as thr
    thr.link_correspondence(st, rt.store, "gmail")
    r1 = c.post("/api/mail/thread/waiting", json={"ref": "t001", "days": 2}, headers=h).get_json()
    r2 = c.post("/api/mail/thread/waiting", json={"ref": "t001", "days": 7}, headers=h).get_json()
    key = st.get("gmail", "t001")["thread_key"]
    s = thr.get_state(st, "gmail", key)
    assert s["waiting_task"] == r1["task"], ("second pick dropped the link to the live nudge", r1["task"], r2["task"], s)



# ── from the Phase 3 review: test_p3_late_rerun ──
def test_rule_does_not_fire_late_on_reingest(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    c, h = _api(app)
    pid = _project(rt, closed=False)
    code, out = _rule(c, h, [{"field": "project", "value": "any"}], [{"type": "archive"}])
    assert code == 200, out
    server.deliver("m1", subject="Site walk Tuesday", internal_ms=NOW_MS() + 5000)
    _sync(rt)
    st = open_store(prov.cache_dir)
    assert rules.recent_runs(st, "gmail") == []          # no project yet: no match
    # Days later the user files the thread and stars the message in Gmail.
    thr.set_state(st, "gmail", st.get("gmail", "m1")["thread_key"], project_id=pid)
    server.relabel("m1", add=("STARRED",))
    _sync(rt)
    runs = rules.recent_runs(st, "gmail")
    assert runs == [], ("rule ran on a label-change re-ingest", st.get("gmail", "m1")["folder"],
                        [r["done"] for r in runs])



# ── from the Phase 3 review: test_p3_delete_probe ──
def test_gmail_delete_changes_state(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=2)
    c, h = _api(app)
    st = open_store(prov.cache_dir)
    b = (st.get("gmail", "t001")["folder"], sorted(server.msgs["t001"]["labels"]))
    r = c.post("/api/mail/client/delete", json={"ref": "t001", "undoable": True}, headers=h).get_json()
    a = (st.get("gmail", "t001")["folder"], sorted(server.msgs["t001"]["labels"]) if "t001" in server.msgs else None)
    assert a != b


# ── the dry-run gate is bound to the rule it previewed ──
def test_dry_run_confirmation_is_bound_to_the_rule(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=2)
    c, h = _api(app)
    narrow = {"name": "Lobby", "conditions": [{"field": "subject", "value": "Lobby item 001"}],
              "actions": [{"type": "archive"}]}
    token = c.post("/api/mail/rules/dry-run", json={"rule": narrow}, headers=h).get_json()["ack_token"]
    # A bare "true" is not a confirmation.
    assert c.post("/api/mail/rules", json={"rule": narrow, "dry_run_ack": True}, headers=h).status_code == 400
    # A token for a narrower rule does not confirm a broader one.
    broad = dict(narrow, conditions=[{"field": "subject", "value": "Lobby"}])
    assert c.post("/api/mail/rules", json={"rule": broad, "dry_run_ack": token}, headers=h).status_code == 400
    r = c.post("/api/mail/rules", json={"rule": narrow, "dry_run_ack": token}, headers=h).get_json()["rule"]
    # Off and on again, unchanged: stays confirmed. Broadened: needs a new dry run.
    off = c.put(f"/api/mail/rules/{r['id']}", json={"rule": dict(narrow, enabled=False)}, headers=h)
    on = c.put(f"/api/mail/rules/{r['id']}", json={"rule": dict(narrow, enabled=True)}, headers=h)
    assert off.status_code == 200 and on.status_code == 200
    assert c.put(f"/api/mail/rules/{r['id']}", json={"rule": broad}, headers=h).status_code == 400
