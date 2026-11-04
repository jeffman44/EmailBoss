"""2.0 Phase 2 exit tests: the terminal UI.

- test_list_triage_without_mouse      Playwright, keyboard only, real app + store + fake Gmail
- test_csp_no_inline_handlers         every template and static file
- test_no_static_file_over_1500_lines 2.0 files capped; 1.x files may not grow
plus the store-backed list / reader / undo / context routes behind them.
(The bench thresholds are the fourth exit check: tests/bench, Windows CI.)
"""
import json
import os
import re
import threading
import time
from pathlib import Path

import pytest

from plannerboard.mail import secrets
from plannerboard.mail.store import forget_open, open_store

from .test_sync_truth import FakeGmail

STATIC = Path(__file__).resolve().parents[1] / "plannerboard" / "static"


@pytest.fixture(autouse=True)
def _mem():
    secrets.use_memory_backend(True)
    yield
    secrets.use_memory_backend(False)
    forget_open()


def _terminal_app(tmp_path, monkeypatch, n=40):
    """The real app, Gmail configured, a fake Gmail behind it, the store seeded
    through the real capture path."""
    from plannerboard import create_app
    from tests.conftest import make_config
    root = tmp_path / "data"
    monkeypatch.setenv("PB_DATA_ROOT", str(root))
    monkeypatch.setenv("PB_LICENSE_DEV", "1")
    monkeypatch.setenv("PB_TESTING", "1")
    cfg = make_config(root, mail={"provider": "gmail", "client_enabled": True, "send_enabled": False,
                                  "gmail": {"client_id": "cid"}, "store": {"backfill": False}})
    app = create_app(config=cfg, testing=True)
    rt = app.extensions["pb"]
    server = FakeGmail()
    for i in range(n):
        server.deliver(f"t{i:03d}", subject=f"Lobby item {i:03d}", internal_ms=1790870400000 + i * 60000)
    rt.store.meta_set("install_cutoff", "2026-01-01T00:00:00+00:00")
    thread = rt.mail_runner.threads["gmail"]
    prov = thread.provider
    prov.http = server
    prov.tokens.save({"access_token": "AT", "expires_at": 9e9})
    thread.run_triage = False
    for _ in range(5):
        thread.run_once()
        if not thread.state.get("pending_ids"):
            break
    return app, rt, server, prov


def _api(app):
    c = app.test_client()
    return c, {"X-Requested-With": "EmailBoss"}


# ── the routes ───────────────────────────────────────────────────────────────

def test_store_list_and_reader_come_from_the_store(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=12)
    c, h = _api(app)
    calls_before = len(server.calls)
    page = c.get("/api/mail/store/list?folder_id=INBOX&folder_kind=inbox&limit=5").get_json()
    assert page["covered"] and page["total"] == 12 and len(page["messages"]) == 5
    assert page["messages"][0]["subject"] == "Lobby item 011"          # newest first
    m = c.get("/api/mail/store/message?ref=t005").get_json()
    assert m["source"] == "store" and m["body"] == "body of t005" and m["subject"] == "Lobby item 005"
    assert len(server.calls) == calls_before                          # no server call for list or read


def test_moves_and_flags_update_the_store_and_undo(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=6)
    c, h = _api(app)
    r = c.post("/api/mail/client/move", json={"ref": "t002", "folder": "archive", "from": "INBOX",
                                              "undoable": True, "label": "Archived"}, headers=h).get_json()
    assert r["ok"] and r["action_id"]
    assert "INBOX" not in server.msgs["t002"]["labels"]
    inbox = c.get("/api/mail/store/list?folder_id=INBOX&folder_kind=inbox").get_json()
    assert "t002" not in [x["ref"] for x in inbox["messages"]] and inbox["total"] == 5
    u = c.post(f"/api/mail/undo/{r['action_id']}", json={}, headers=h).get_json()
    assert u["ok"] and "INBOX" in server.msgs["t002"]["labels"]
    inbox = c.get("/api/mail/store/list?folder_id=INBOX&folder_kind=inbox").get_json()
    assert "t002" in [x["ref"] for x in inbox["messages"]]
    again = c.post(f"/api/mail/undo/{r['action_id']}", json={}, headers=h).get_json()
    assert again.get("already")                                       # an undo runs once

    f = c.post("/api/mail/client/flags", json={"ref": "t003", "flagged": True, "undoable": True}, headers=h).get_json()
    assert "STARRED" in server.msgs["t003"]["labels"]
    row = open_store(prov.cache_dir).get("gmail", "t003")
    assert row["is_starred"] == 1
    c.post(f"/api/mail/undo/{f['action_id']}", json={}, headers=h)
    assert "STARRED" not in server.msgs["t003"]["labels"]
    assert open_store(prov.cache_dir).get("gmail", "t003")["is_starred"] == 0


def test_reader_falls_back_to_the_server_outside_the_body_window(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=2)
    st = open_store(prov.cache_dir)
    st.drop_body([st.get("gmail", "t000")["rowid"]])
    c, h = _api(app)
    m = c.get("/api/mail/store/message?ref=t000").get_json()
    assert m["source"] == "server" and m["subject"] == "Lobby item 000"


def test_context_pane_data(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=2)
    c, h = _api(app)
    ctx = c.get("/api/mail/store/context?ref=t001").get_json()
    assert ctx["ok"] and ctx["thread"] and ctx["thread"][0]["ref"] == "t001"
    assert any(p["email"] == "dana@northwind.example" for p in ctx["people"])
    assert ctx["project"] is None                                      # not filed yet


# ── the static invariants ────────────────────────────────────────────────────

def _static_files():
    return sorted(p for p in STATIC.iterdir() if p.suffix in (".js", ".html", ".css"))


def test_csp_no_inline_handlers():
    handler = re.compile(r"(?<![\w.\-])on[a-z]+\s*=\s*[\"']")
    js_url = re.compile(r"(?:href|src|action|formaction)\s*=\s*[\"']\s*javascript:", re.I)
    bad = []
    for p in _static_files():
        text = p.read_text(encoding="utf-8")
        for rx in (handler, js_url):
            bad += [f"{p.name}: {m.group(0)}" for m in rx.finditer(text)]
    assert not bad, bad[:10]
    page = (STATIC / "board.html").read_text(encoding="utf-8")
    csp = re.search(r'Content-Security-Policy" content="([^"]+)"', page).group(1)
    assert "unsafe-inline" not in re.search(r"script-src ([^;]+)", csp).group(1)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page), "inline <script> block"
    for name in ("eval(", "new Function("):
        for p in _static_files():
            if p.suffix == ".js":
                assert name not in p.read_text(encoding="utf-8"), f"{p.name} uses {name}"


# 1.x files that predate the rule. They may not grow (plan section 2, rule 9:
# no NEW file over 1,500 lines; board.js changes only through Phase 4 hooks).
LEGACY_CEILINGS = {"board.js": 10191, "board.html": 3700}


def test_no_static_file_over_1500_lines():
    over = []
    for p in _static_files():
        n = len(p.read_text(encoding="utf-8").splitlines())
        ceiling = LEGACY_CEILINGS.get(p.name, 1500)
        if n > ceiling:
            over.append(f"{p.name}: {n} lines (limit {ceiling})")
    assert not over, over


# ── keyboard-only triage in a real browser ──────────────────────────────────

@pytest.fixture
def live(tmp_path, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    from werkzeug.serving import make_server
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=30)
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_port}", server, prov
    srv.shutdown()


def test_list_triage_without_mouse(live):
    from playwright.sync_api import sync_playwright
    if os.path.isdir("/opt/pw-browsers"):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    url, server, prov = live
    errors = []
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # pragma: no cover
            pytest.skip(f"chromium not available: {e}")
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.add_init_script("window.__eb_mouse = 0; ['mousedown', 'click', 'pointerdown'].forEach(t =>"
                             " addEventListener(t, (e) => { if (e.isTrusted) window.__eb_mouse++; }, true));")
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url + "/", wait_until="networkidle")
        page.wait_for_timeout(600)
        kb = page.keyboard
        # Into the mail view with the palette: Ctrl+K, type, Enter.
        kb.press("Control+k")
        kb.type("Go to Inbox")
        page.wait_for_timeout(200)
        kb.press("Enter")
        page.wait_for_selector(".mail-virt .mail-row", timeout=8000)
        page.wait_for_timeout(400)
        rows = lambda: page.evaluate("() => [...document.querySelectorAll('.mail-virt .mail-row')].map(r => r.dataset.ref)")
        assert rows()[0] == "t029"                                  # store order, newest first
        # Virtualized: 30 messages, fewer rows in the DOM than a full render would need is fine;
        # every row has the fixed height of the density.
        h = page.evaluate("() => getComputedStyle(document.querySelector('.mail-virt .mail-row')).height")
        assert h in ("44px", "32px")

        kb.press("j")                                               # cursor on the first row
        kb.press("o")                                               # open it
        page.wait_for_selector(".mail-subject")
        assert page.inner_text(".mail-subject") == "Lobby item 029"
        kb.press("j")                                               # reading flow: the next opens
        page.wait_for_timeout(400)
        assert page.inner_text(".mail-subject") == "Lobby item 028"
        kb.press("e")                                               # archive the open one
        page.wait_for_timeout(600)
        assert "INBOX" not in server.msgs["t028"]["labels"]
        assert "t028" not in rows()

        kb.press("u")                                               # back to the list
        kb.press("x")                                               # select the cursor row
        kb.press("j")
        kb.press("x")                                               # and the next
        sel = page.evaluate("() => [...EBMailCore.S.sel]")
        assert len(sel) == 2
        kb.press("s")                                               # star both
        page.wait_for_timeout(600)
        assert all("STARRED" in server.msgs[r]["labels"] for r in sel)
        kb.press("#")                                               # trash both
        page.wait_for_timeout(800)
        assert all("TRASH" in server.msgs[r]["labels"] for r in sel)
        kb.press("Control+z")                                       # undo the trash
        page.wait_for_timeout(1200)
        assert all("TRASH" not in server.msgs[r]["labels"] for r in sel)
        assert all(r in rows() for r in sel)

        kb.press("Escape")                                          # clear the selection
        kb.press("o")
        page.wait_for_selector(".mail-subject")
        kb.press("]")                                               # context pane
        page.wait_for_selector("#mail-ctx .ctx-sec")
        assert "people" in page.inner_text("#mail-ctx").lower()
        kb.press("?")                                               # the cheat sheet lists every key
        assert "Ctrl+Z" in page.inner_text("#mail-keys")
        kb.press("Escape")

        kb.press("/")                                               # search from the keyboard
        assert page.evaluate("() => document.activeElement.id") == "mail-search"
        kb.type("Lobby item 007")
        page.wait_for_selector(".ms-row", timeout=5000)
        kb.press("Escape")

        timing = page.evaluate("() => window.__eb_timing")
        assert timing["first_list_ms"] and timing["open_message_ms"] and timing["search_ms"]
        mouse_events = page.evaluate("() => window.__eb_mouse || 0")
        assert mouse_events == 0
        browser.close()
    assert errors == [], errors
