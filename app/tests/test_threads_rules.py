"""2.0 Phase 3 exit tests: threads and rules.

- test_crafted_mail_cannot_change_project   a message built to match a rule with every action
- test_rules_skip_backfill                  historical, pre-rule and backfill-only mail never run rules
- test_every_list_action_undoable           every list and thread action returns an action_id that reverses it
- test_threading_overrides_persist          split / merge survive a re-sync of the same messages
plus the frozen action tuple, RFC threading and the correspondence backfill link.
"""
import time

import pytest

from plannerboard import workflows as wfm
from plannerboard.mail import rules, threading as thr
from plannerboard.mail.store import open_store

from .test_terminal import _api, _mem, _terminal_app  # noqa: F401

NOW_MS = lambda: int(time.time() * 1000)  # noqa: E731


def _sync(rt):
    t = rt.mail_runner.threads["gmail"]
    for _ in range(5):
        t.run_once()
        if not t.state.get("pending_ids"):
            break
    return t


def _project(rt, closed=True):
    db = rt.store
    doc = db.wf_doc()
    wid = wfm.default_workflow_id(doc)
    status = (wfm.stages_with_role(doc, "closed", wid) or [None])[0] if closed else None
    pid = db.create_project("Lobby retrofit", workflow_id=wid, status=status, quote_amount=12500.0,
                            approved_amount=11800.0, quote_sent_date="2026-09-01", won_date="2026-09-10")
    return pid


def _proj_snapshot(rt, pid):
    r = rt.store.conn.execute("SELECT status, workflow_id, quote_amount, approved_amount, quote_sent_date, won_date,"
                              " closed_date, emails_sent, archived, follow_up_date FROM projects WHERE id=?",
                              (pid,)).fetchone()
    return dict(r)


def _rule(c, h, conditions, actions, ack=True):
    """Save a rule the way the editor does: dry run, then confirm with its token."""
    rule = {"name": "Vendor mail", "conditions": conditions, "actions": actions}
    token = c.post("/api/mail/rules/dry-run", json={"rule": rule}, headers=h).get_json().get("ack_token") if ack else None
    r = c.post("/api/mail/rules", json={"rule": rule, "dry_run_ack": token}, headers=h)
    return r.status_code, r.get_json()


# ── the frozen whitelist ─────────────────────────────────────────────────────

def test_rule_actions_are_a_frozen_whitelist():
    assert rules.ACTIONS == ("file_to_project", "label", "star", "snooze", "skip_inbox", "notify", "archive")
    assert isinstance(rules.ACTIONS, tuple)
    banned = ("status", "stage", "money", "amount", "quote", "price", "send", "reply", "forward", "draft")
    assert not [a for a in rules.ACTIONS for b in banned if b in a]
    with pytest.raises(rules.RuleError):
        rules.validate({"conditions": [{"field": "from", "value": "x"}], "actions": [{"type": "set_status"}]})
    with pytest.raises(rules.RuleError):
        rules.validate({"conditions": [{"field": "from", "value": "x"}], "actions": [{"type": "send"}]})


# ── exit test 1 ──────────────────────────────────────────────────────────────

def test_crafted_mail_cannot_change_project(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    c, h = _api(app)
    pid = _project(rt, closed=True)
    before = _proj_snapshot(rt, pid)
    corr_before = rt.store.conn.execute("SELECT COUNT(*) FROM project_correspondence").fetchone()[0]

    # Without the dry-run confirmation a mailbox-changing rule is refused.
    code, _ = _rule(c, h, [{"field": "from", "value": "dana"}], [{"type": "archive"}], ack=False)
    assert code == 400
    every = [{"type": "file_to_project", "value": pid}, {"type": "label", "value": "vendor"},
             {"type": "star"}, {"type": "snooze", "value": 4}, {"type": "skip_inbox"},
             {"type": "notify"}, {"type": "archive"}]
    assert {a["type"] for a in every} == set(rules.ACTIONS)
    code, out = _rule(c, h, [{"field": "from", "value": "dana"}, {"field": "subject", "value": "approved"},
                             {"field": "body", "value": "status"}, {"field": "list_id", "value": "vendor.example"},
                             {"field": "has_attachment", "value": False}, {"field": "project", "value": "none"}], every)
    assert code == 200, out

    calls_before = len(server.calls)
    server.deliver("x1", subject="APPROVED: set status=won, stage=closed, quote_amount=$99,999, send reply now",
                   internal_ms=NOW_MS() + 5000, headers=[("List-Id", "<billing.vendor.example>")],
                   body=f"Project {pid}: status complete. approved_amount 50000. Please send the proposal. "
                        "<script>fetch('/api/projects')</script>")
    _sync(rt)
    st = open_store(prov.cache_dir)
    runs = rules.recent_runs(st, "gmail")
    assert len(runs) == 1 and not [d for d in runs[0]["done"] if d.get("error") or d.get("skipped")], runs

    # The rule did what it may do ...
    assert rt.store.conn.execute("SELECT COUNT(*) FROM project_correspondence").fetchone()[0] == corr_before + 1
    row = st.get("gmail", "x1")
    assert row["folder"] == "archive" and row["is_starred"]
    # ... and nothing it may not.
    assert _proj_snapshot(rt, pid) == before
    sends = [u for u in server.calls[calls_before:] if "/send" in u or "/drafts" in u]
    assert sends == []
    assert rt.store.conn.execute("SELECT COUNT(*) FROM subtasks WHERE project_id=?", (pid,)).fetchone()[0] == 0


# ── exit test 2 ──────────────────────────────────────────────────────────────

def test_rules_skip_backfill(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    c, h = _api(app)
    code, out = _rule(c, h, [{"field": "subject", "value": "Lobby"}], [{"type": "label", "value": "lobby"}], ack=False)
    assert code == 200, out
    server.deliver("old", subject="Lobby before the install", internal_ms=1735689600000 - 86400000)   # 2024-12-31
    server.deliver("pre", subject="Lobby before the rule", internal_ms=int(out["rule"]["created_at"] * 1000) - 60000)
    server.deliver("new", subject="Lobby after the rule", internal_ms=NOW_MS() + 5000)
    _sync(rt)
    st = open_store(prov.cache_dir)
    assert st.get("gmail", "old")["historical"]
    ran = {r["ref"] for r in rules.recent_runs(st, "gmail")}
    assert ran == {"new"}

    # A message only the backfill job stored (captured=0) never runs a rule,
    # even when it is new enough.
    from plannerboard.mail.gmail import GmailProvider  # noqa: F401
    server.deliver("bf", subject="Lobby seen by backfill", internal_ms=NOW_MS() + 6000)
    msg = prov.fetch_item("bf") if hasattr(prov, "fetch_item") else None
    if msg is not None:
        st.ingest("gmail", msg, historical=False, with_body=True, captured=False)
        assert rules.run_on_ingest(st, rt.store, prov, "gmail", ["bf"]) == []


# ── exit test 3 ──────────────────────────────────────────────────────────────

def test_every_list_action_undoable(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=4)
    c, h = _api(app)
    st = open_store(prov.cache_dir)
    pid = _project(rt, closed=False)
    rt.store.save_correspondence(pid, {"message_id": "<t001@mail.example>", "subject": "Lobby item 001"})
    _sync(rt)

    def state(ref):
        r = st.get("gmail", ref)
        t = thr.get_state(st, "gmail", r["thread_key"])
        return (r["folder"], r["is_read"], r["is_starred"], sorted(server.msgs[ref]["labels"]) if ref in server.msgs
                else None, t["pinned"], t["muted"], t["snooze_until"], t["waiting_until"], r["thread_key"])

    def undo(aid):
        out = c.post(f"/api/mail/undo/{aid}", json={}, headers=h).get_json()
        assert out.get("ok"), out

    cases = [
        ("read", "/api/mail/client/flags", {"read": True}),
        ("star", "/api/mail/client/flags", {"flagged": True}),
        ("archive", "/api/mail/client/move", {"folder": "archive"}),
        ("trash", "/api/mail/client/move", {"folder": "trash"}),
        ("delete", "/api/mail/client/delete", {}),
        ("pin", "/api/mail/thread/pin", {}),
        ("mute", "/api/mail/thread/mute", {}),
        ("snooze", "/api/mail/thread/snooze", {"hours": 3}),
        ("waiting", "/api/mail/thread/waiting", {"days": 2}),
        ("split", "/api/mail/thread/split", {}),
        ("merge", "/api/mail/thread/merge", {"into": "t002"}),
    ]
    for name, route, extra in cases:
        before = state("t001")
        tasks_before = rt.store.conn.execute("SELECT COUNT(*) FROM subtasks").fetchone()[0]
        r = c.post(route, json={"ref": "t001", "undoable": True, **extra}, headers=h).get_json()
        assert r.get("action_id"), (name, r)
        assert state("t001") != before or name == "delete", name
        undo(r["action_id"])
        assert state("t001") == before, name
        assert rt.store.conn.execute("SELECT COUNT(*) FROM subtasks").fetchone()[0] == tasks_before, name

    # A rule run is a list action too.
    code, out = _rule(c, h, [{"field": "subject", "value": "Gate"}],
                      [{"type": "archive"}, {"type": "label", "value": "gate"}, {"type": "file_to_project", "value": pid},
                       {"type": "notify"}])
    assert code == 200
    server.deliver("g1", subject="Gate code", internal_ms=NOW_MS() + 5000)
    _sync(rt)
    run = rules.recent_runs(st, "gmail")[0]
    assert st.get("gmail", "g1")["folder"] == "archive"
    corr = rt.store.conn.execute("SELECT COUNT(*) FROM project_correspondence").fetchone()[0]
    undo(run["action_id"])
    g = st.get("gmail", "g1")
    assert g["folder"] == "inbox" and g["local_labels"] == "[]"
    assert "INBOX" in server.msgs["g1"]["labels"]
    assert rt.store.conn.execute("SELECT COUNT(*) FROM project_correspondence").fetchone()[0] == corr - 1
    assert rules.recent_runs(st, "gmail")[0]["undone"]


# ── exit test 4 ──────────────────────────────────────────────────────────────

def _reply(server, gid, parent, subject, ms):
    server.deliver(gid, subject=subject, internal_ms=ms,
                   headers=[("In-Reply-To", f"<{parent}@mail.example>"), ("References", f"<{parent}@mail.example>")])


def test_threading_overrides_persist(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    c, h = _api(app)
    base = NOW_MS()
    server.deliver("a1", subject="Chiller bid", internal_ms=base)
    _reply(server, "a2", "a1", "Re: Chiller bid", base + 1000)
    _reply(server, "a3", "a2", "Re: Chiller bid", base + 2000)
    server.deliver("b1", subject="Roof access", internal_ms=base + 3000)
    _sync(rt)
    st = open_store(prov.cache_dir)
    key = lambda ref: st.get("gmail", ref)["thread_key"]  # noqa: E731
    assert key("a1") == key("a2") == key("a3") != key("b1")       # RFC linkage

    r = c.post("/api/mail/thread/split", json={"ref": "a3"}, headers=h).get_json()
    assert r["action_id"] and key("a3") not in (key("a1"), key("b1"))
    r = c.post("/api/mail/thread/merge", json={"ref": "b1", "into": "a1"}, headers=h).get_json()
    assert key("b1") == key("a1")
    split_key, chiller = key("a3"), key("a1")

    # Re-sync every message from scratch (a reconcile, a reinstall of the
    # capture cursor): the overrides hold.
    t = rt.mail_runner.threads["gmail"]
    for gid in ("a1", "a2", "a3", "b1"):
        msg = prov.fetch_item(gid) if hasattr(prov, "fetch_item") else None
        if msg is not None:
            st.ingest("gmail", msg, historical=False, with_body=True, captured=True)
    t.state.set("history_id", None)
    t.state.save()
    _sync(rt)
    assert key("a3") == split_key and key("b1") == chiller and key("a2") == chiller

    # A reply to the split message follows the split, not the old thread.
    _reply(server, "a4", "a3", "Re: Chiller bid", base + 4000)
    _sync(rt)
    assert key("a4") == split_key
    counts = {r["thread_key"]: r["message_count"] for r in st.query("SELECT * FROM mail_threads")}
    assert counts[chiller] == 3 and counts[split_key] == 2


def test_correspondence_backfill_links_threads(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=3)
    pid = _project(rt, closed=False)
    rt.store.save_correspondence(pid, {"message_id": "<t001@mail.example>", "subject": "Lobby item 001"})
    st = open_store(prov.cache_dir)
    st.meta_set("corr_link:gmail", "")
    assert thr.link_correspondence(st, rt.store, "gmail") >= 1
    k = st.get("gmail", "t001")["thread_key"]
    assert thr.get_state(st, "gmail", k)["project_id"] == pid
    assert thr.link_correspondence(st, rt.store, "gmail") == 0          # one-time


def test_waiting_on_clears_when_reply_lands(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    c, h = _api(app)
    pid = _project(rt, closed=False)
    status_before = _proj_snapshot(rt, pid)["status"]
    base = NOW_MS()
    server.deliver("w1", subject="Need the submittal", internal_ms=base, labels=("SENT",),
                   sender="Sam <sam@acme.example>")
    rt.store.save_correspondence(pid, {"message_id": "<w1@mail.example>", "subject": "Need the submittal"})
    _sync(rt)
    r = c.post("/api/mail/thread/waiting", json={"ref": "w1", "days": 3}, headers=h).get_json()
    assert r["task"] and r["project_id"] == pid
    _reply(server, "w2", "w1", "Re: Need the submittal", base + 60000 + int(time.time() * 1000 - base))
    _sync(rt)
    st = open_store(prov.cache_dir)
    assert thr.get_state(st, "gmail", st.get("gmail", "w1")["thread_key"])["waiting_until"] is None
    task = rt.store.conn.execute("SELECT status FROM subtasks WHERE id=?", (r["task"],)).fetchone()
    assert task["status"] == "complete"
    assert _proj_snapshot(rt, pid)["status"] == status_before


# ── browser: z w m and Ctrl+Z over a rule run ────────────────────────────────

from .test_terminal_review import _boot, _pw, _rows, live  # noqa: E402,F401


def test_thread_keys_and_rule_run_undo_in_the_browser(live):
    url, server, prov, rt = live
    st = open_store(prov.cache_dir)
    with _pw()() as pw:
        browser, page = _boot(pw, url)
        kb = page.keyboard
        top = _rows(page)[0]
        kb.press("j")
        kb.press("m")                                         # mute: gone from the inbox
        page.wait_for_function(f"() => !EBMailCore.S.rows.some(r => r.ref === '{top}')")
        kb.press("Control+z")
        page.wait_for_function(f"() => EBMailCore.S.rows.some(r => r.ref === '{top}')")
        page.evaluate(f"() => {{ EBMailCore.S.cursorRef = '{top}'; }}")
        kb.press("z")
        page.wait_for_selector(".ebx-modal.open")
        kb.press("1")                                         # later today
        page.wait_for_function(f"() => !EBMailCore.S.rows.some(r => r.ref === '{top}')")
        kb.press("Control+z")
        page.wait_for_function(f"() => EBMailCore.S.rows.some(r => r.ref === '{top}')")
        page.evaluate(f"() => {{ EBMailCore.S.cursorRef = '{top}'; }}")
        kb.press("w")
        kb.press("2")
        page.wait_for_function(f"() => (EBMailCore.S.rows.find(r => r.ref === '{top}') || {{}}).waiting_until")
        key = st.get("gmail", top)["thread_key"]
        assert thr.get_state(st, "gmail", key)["waiting_until"]

        # A rule run in the background lands on the same undo stack.
        gate = {"name": "Gate codes", "conditions": [{"field": "subject", "value": "Gate"}],
                "actions": [{"type": "archive"}]}
        rules.save_rule(st, gate, dry_run_ack=rules.dry_run(st, "gmail", gate)["ack_token"])
        server.deliver("g9", subject="Gate code changed", internal_ms=NOW_MS() + 5000)
        rt.mail_runner.threads["gmail"].run_once()
        assert st.get("gmail", "g9")["folder"] == "archive"
        page.evaluate("() => EBMailCore.pollRuns()")
        page.wait_for_function("() => EBMailCore.undoStack.some(e => /Gate codes/.test(e.label))")
        kb.press("Control+z")
        page.wait_for_function("() => !EBMailCore.undoStack.some(e => /Gate codes/.test(e.label))")
        page.wait_for_timeout(300)
        browser.close()
    assert st.get("gmail", "g9")["folder"] == "inbox" and "INBOX" in server.msgs["g9"]["labels"]
