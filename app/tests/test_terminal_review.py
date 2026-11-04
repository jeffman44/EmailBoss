"""Regressions from the Phase 2 adversarial review: undo puts a message back
where it really lived, the store stays in step with the server after a move,
the store answers only for folders it holds, and the list keeps up with the
store (periodic refresh, folder switch during a load)."""
import os
import threading
import time

import pytest

from tests.test_terminal import _terminal_app, _api, _mem  # noqa: F401
from plannerboard.mail import sync
from plannerboard.mail.store import open_store



def _refs(c, fid, kind):
    j = c.get(f"/api/mail/store/list?folder_id={fid}&folder_kind={kind}").get_json()
    return [x["ref"] for x in j.get("messages", [])], j


def _backfill_done(prov):
    st = open_store(prov.cache_dir)
    sync._save(st, "gmail", {**sync.state(st, "gmail"), "done": True})


# ── server side ──

def test_store_answers_only_for_folders_it_holds(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=2)
    server.deliver("T1", labels=("TRASH",), subject="old trash")
    rt.mail_runner.threads["gmail"].run_once()
    c, _h = _api(app)
    assert c.get("/api/mail/store/list?folder_id=INBOX&folder_kind=inbox").get_json()["covered"]
    assert not c.get("/api/mail/store/list?folder_id=TRASH&folder_kind=trash").get_json()["covered"]
    assert not c.get("/api/mail/store/list?folder_id=Label_7&folder_kind=other").get_json()["covered"]
    _backfill_done(prov)
    assert c.get("/api/mail/store/list?folder_id=TRASH&folder_kind=trash").get_json()["covered"]


def test_gmail_move_resyncs_labels(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    server.deliver("L1", labels=("INBOX", "UNREAD", "Label_7"), subject="labelled")
    rt.mail_runner.threads["gmail"].run_once()
    _backfill_done(prov)
    c, h = _api(app)
    assert "L1" in _refs(c, "Label_7", "other")[0]
    r = c.post("/api/mail/client/move", json={"ref": "L1", "folder": "archive", "undoable": True}, headers=h).get_json()
    assert r["ok"]
    st = open_store(prov.cache_dir)
    assert set(st.get("gmail", "L1")["labels"]) == server.msgs["L1"]["labels"]
    assert "L1" not in _refs(c, "INBOX", "inbox")[0]


def test_gmail_archive_undo_restores_user_labels(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    server.deliver("L2", labels=("INBOX", "UNREAD", "Label_7"), subject="labelled")
    rt.mail_runner.threads["gmail"].run_once()
    c, h = _api(app)
    before = set(server.msgs["L2"]["labels"])
    r = c.post("/api/mail/client/move", json={"ref": "L2", "folder": "archive", "undoable": True}, headers=h).get_json()
    u = c.post(f"/api/mail/undo/{r['action_id']}", json={}, headers=h).get_json()
    assert u["ok"]
    assert server.msgs["L2"]["labels"] == before
    assert "L2" in _refs(c, "INBOX", "inbox")[0]


def test_undo_returns_message_to_its_own_folder(tmp_path, monkeypatch):
    """A sent message trashed from the palette goes back to Sent, not the inbox
    on screen (the client no longer says where it came from)."""
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    server.deliver("S1", labels=("SENT",), subject="I sent this")
    rt.mail_runner.threads["gmail"].run_once()
    c, h = _api(app)
    r = c.post("/api/mail/client/move", json={"ref": "S1", "folder": "trash", "from": "INBOX",
                                              "undoable": True}, headers=h).get_json()
    c.post(f"/api/mail/undo/{r['action_id']}", json={}, headers=h)
    assert server.msgs["S1"]["labels"] == {"SENT"}


def test_undo_after_new_id_move_keeps_row(tmp_path, monkeypatch):
    """Graph and IMAP give a new id on move: the row follows it, and an undo
    finds it and puts it back in the list."""
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=3)
    orig = prov.move_message
    counter = {"n": 0}

    def move_new_id(ref, folder):
        orig(ref, folder)
        counter["n"] += 1
        new = f"{ref}x{counter['n']}"
        server.msgs[new] = server.msgs.pop(ref)
        return new
    monkeypatch.setattr(prov, "move_message", move_new_id)
    monkeypatch.setattr(prov, "labels_of", None, raising=False)
    monkeypatch.setattr(prov, "restore_labels", None, raising=False)
    c, h = _api(app)
    r = c.post("/api/mail/client/move", json={"ref": "t001", "folder": "archive", "undoable": True}, headers=h).get_json()
    st = open_store(prov.cache_dir)
    assert st.get("gmail", "t001x1")["folder"] == "archive"
    u = c.post(f"/api/mail/undo/{r['action_id']}", json={}, headers=h).get_json()
    assert u.get("ok"), u
    assert u["ref"] in _refs(c, "INBOX", "inbox")[0]


def test_imap_store_id_normalizes_client_refs():
    from plannerboard.mail.imap import ImapProvider
    p = ImapProvider.__new__(ImapProvider)
    p.settings = {"inbox_folder": "INBOX", "sent_folder": "Sent"}
    assert p.store_id("7:42:INBOX") == "inbox:7:42"
    assert p.store_id("7:43:Sent") == "sent:7:43"
    assert p.store_id("7:44:Projects/Lobby") == "x:Projects/Lobby:7:44"
    assert p.store_id("inbox:7:42") == "inbox:7:42"
    assert p.undo_folder({"provider_id": "x:Projects/Lobby:7:44", "folder": "other"}) == "Projects/Lobby"


# ── browser ──

@pytest.fixture
def live(tmp_path, monkeypatch):
    from werkzeug.serving import make_server
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=30)
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_port}", server, prov, rt
    srv.shutdown()


def _boot(pw, url):
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(url + "/", wait_until="networkidle")
    page.evaluate("() => setView('mail')")
    page.wait_for_selector(".mail-virt .mail-row", timeout=8000)
    page.wait_for_timeout(300)
    return browser, page


def _rows(page):
    return page.evaluate("() => EBMailCore.S.rows.map(r => r.ref)")


def _pw():
    return pytest.importorskip("playwright.sync_api").sync_playwright


def test_periodic_refresh_shows_new_mail(live):
    url, server, prov, rt = live
    with _pw()() as pw:
        browser, page = _boot(pw, url)
        assert _rows(page)[0] == "t029"
        server.deliver("new1", subject="Fresh one", internal_ms=1790870400000 + 99 * 60000)
        rt.mail_runner.threads["gmail"].run_once()
        page.evaluate("() => EBMailCore.loadList(false, true)")   # the timer's quiet load
        page.wait_for_timeout(600)
        r = _rows(page)
        browser.close()
    assert r[0] == "new1" and len(r) == len(set(r)) == 31


def test_folder_switch_during_page_load(live):
    url, server, prov, rt = live
    with _pw()() as pw:
        browser, page = _boot(pw, url)

        def slow(route):
            time.sleep(1.0)
            route.continue_()
        page.route(lambda u: "/api/mail/store/list" in u and "offset=10" in u, slow)
        page.evaluate("() => { EBMailCore.S.rows = EBMailCore.S.rows.slice(0, 10); EBMailCore.S.cursor = 'store'; EBMailCore.more(); }")
        page.wait_for_timeout(100)
        trash = page.evaluate("() => (EBMailCore.S.folders.find(f => f.kind === 'trash') || {}).id")
        page.evaluate("(id) => EBMailCore.selectFolder(id)", trash)
        page.wait_for_timeout(2000)
        st = page.evaluate("() => ({folder: EBMailCore.S.folder, n: EBMailCore.S.rows.length})")
        browser.close()
    assert st == {"folder": trash, "n": 0}


def test_reply_after_failed_open_does_not_target_previous(live):
    url, server, prov, rt = live
    st = open_store(prov.cache_dir)
    st.drop_body([st.get("gmail", "t028")["rowid"]])          # outside the body window
    with _pw()() as pw:
        browser, page = _boot(pw, url)
        reqs = []
        page.on("request", lambda r: reqs.append(r.url) if "/compose" in r.url else None)
        kb = page.keyboard
        kb.press("j"); kb.press("o")
        page.wait_for_selector(".mail-subject")
        server.msgs.pop("t028")                               # gone on the server: the open fails
        kb.press("j")
        page.wait_for_timeout(500)
        opened = page.evaluate("() => EBMailCore.S.open && EBMailCore.S.open.ref")
        kb.press("r")
        page.wait_for_timeout(600)
        composer = page.evaluate("() => EBMailCore.C.open")
        browser.close()
    assert opened is None and not composer and not reqs


def test_keys_ignored_behind_settings_overlay(live):
    url, server, prov, rt = live
    with _pw()() as pw:
        browser, page = _boot(pw, url)
        page.keyboard.press("j")                                # cursor on t029
        page.evaluate("() => openSettings()")
        page.wait_for_timeout(300)
        assert page.evaluate("() => document.getElementById('set-overlay').classList.contains('open')")
        page.evaluate("() => document.querySelector('#set-overlay .set-btn').focus()")
        page.keyboard.press("#")
        page.wait_for_timeout(600)
        browser.close()
    assert "TRASH" not in server.msgs["t029"]["labels"], "'#' trashed a message behind the Settings window"


def test_keys_act_on_search_rows(live):
    url, server, prov, rt = live
    with _pw()() as pw:
        browser, page = _boot(pw, url)
        page.keyboard.press("/")
        page.keyboard.type("Lobby item 007")
        page.wait_for_selector(".ms-row", timeout=5000)
        shown = page.evaluate("() => [...document.querySelectorAll('.ms-row')].map(r => r.dataset.ref)")
        page.evaluate("() => document.activeElement.blur()")
        page.keyboard.press("j")
        page.keyboard.press("#")
        page.wait_for_timeout(600)
        trashed = [g for g, m in server.msgs.items() if "TRASH" in m["labels"]]
        browser.close()
    assert set(trashed) <= set(shown), f"trashed {trashed} while the screen showed only {shown}"


def test_bulk_archive_partial_failure_keeps_failed_row(live):
    url, server, prov, rt = live
    with _pw()() as pw:
        browser, page = _boot(pw, url)
        kb = page.keyboard
        kb.press("j"); kb.press("x"); kb.press("j"); kb.press("x")
        sel = page.evaluate("() => [...EBMailCore.S.sel]")
        assert sel == ["t029", "t028"]
        gone = server.msgs.pop("t028")                       # server rejects this one (404)
        kb.press("e")
        page.wait_for_timeout(800)
        server.msgs["t028"] = gone
        r = _rows(page)
        browser.close()
    assert "INBOX" in server.msgs["t028"]["labels"]          # still in the inbox on the server
    assert "t028" in r, "failed archive removed the row from the list anyway"


def test_palette_open_trash_undo_returns_to_sent(live):
    url, server, prov, rt = live
    server.deliver("s001", labels=("SENT",), subject="Quote I sent", internal_ms=1790870400000)
    rt.mail_runner.threads["gmail"].run_once()
    with _pw()() as pw:
        browser, page = _boot(pw, url)
        kb = page.keyboard
        kb.press("Control+k")
        kb.type("Quote I sent")
        page.wait_for_timeout(800)
        labels = page.evaluate("() => [...document.querySelectorAll('.eb-pal-item')].map(x => x.innerText)")
        # pick the Messages row
        idx = next(i for i, t in enumerate(labels) if t.startswith("Quote I sent"))
        for _ in range(idx):
            kb.press("ArrowDown")
        kb.press("Enter")
        page.wait_for_selector(".mail-subject")
        assert page.inner_text(".mail-subject") == "Quote I sent"
        kb.press("#")
        page.wait_for_timeout(600)
        assert "TRASH" in server.msgs["s001"]["labels"]
        kb.press("Control+z")
        page.wait_for_timeout(1000)
        browser.close()
    assert "INBOX" not in server.msgs["s001"]["labels"], server.msgs["s001"]["labels"]
