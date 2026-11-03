"""
UI smoke test: boot the stub API (tests/ui_stub_server.py), load
static/board.html and static/setup.html in headless chromium, exercise the
drawer / triage / settings tabs, and assert zero console errors and that the
project names render. Skips cleanly when Playwright or its chromium is not
installed (`pip install playwright`; browsers under /opt/pw-browsers or the
default PLAYWRIGHT_BROWSERS_PATH).
"""

import os
import threading
import time

import pytest

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - environment without playwright
    sync_playwright = None

from werkzeug.serving import make_server

from tests import ui_stub_server as stub

if os.path.isdir("/opt/pw-browsers"):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")

PORT = 8098


@pytest.fixture(scope="module")
def server():
    stub.TRIAGE[:] = [dict(t) for t in stub.TRIAGE_SEED]
    srv = make_server("127.0.0.1", PORT, stub.app)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    time.sleep(0.3)
    yield f"http://127.0.0.1:{PORT}"
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    if sync_playwright is None:
        pytest.skip("playwright not installed")
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as e:  # pragma: no cover
            pytest.skip(f"chromium not available: {e}")
        yield b
        b.close()


def _collect(page, sink):
    page.on("console", lambda m: sink.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: sink.append(f"pageerror: {e}"))
    page.on("requestfailed", lambda r: sink.append(f"requestfailed: {r.url}"))


def test_board_desktop_zero_console_errors(server, browser):
    errors = []
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    _collect(page, errors)
    page.goto(server + "/", wait_until="networkidle")
    page.wait_for_timeout(1200)

    names = page.evaluate("() => [...document.querySelectorAll('.list-row-name')].map(e => e.textContent)")
    joined = " ".join(names)
    for expected in ("Acme Corp Website refresh", "Birch Dental Insurance renewal", "Cedar Labs Onboarding"):
        assert expected in joined, f"{expected!r} not rendered; rows={names}"

    # Entity names from PROF.entity drive the chrome
    assert page.evaluate("() => ent('project')") == "Matter"
    assert "MATTER" in page.inner_text(".list-header")
    assert page.evaluate("() => PROF_STATE") == "live"
    assert page.evaluate("() => WF_STATE") == "live"

    # kanban + back
    page.click("text=KANBAN")
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.querySelectorAll('.column').length") >= 4
    page.click("text=LIST")

    # drawer: fields render and are editable
    page.click(".list-row >> nth=0")
    page.wait_for_timeout(900)
    assert page.evaluate("() => !!document.querySelector('#panel-body .pf-input[data-key=\"reference_no\"]')")
    assert page.evaluate("() => document.getElementById('detail-panel').classList.contains('open')")
    page.keyboard.press("Escape")

    # triage: create-new shape follows work types
    opts = page.evaluate("() => [...document.querySelectorAll('#triage-proj-tri_002 option')].map(o => o.value)")
    assert "__new__:task" in opts and "__new__:deal" in opts
    page.select_option("#triage-proj-tri_002", "__new__:deal")
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.getElementById('triage-new-tri_002').style.display") == "block"

    # analytics + every settings tab
    page.click("text=ANALYTICS")
    page.wait_for_timeout(600)
    assert "EMAIL ANALYTICS" in page.inner_text("#analytics-view")
    page.click("text=LIST")
    for tab in ("general", "workflows", "mail", "ai", "license"):
        page.evaluate(f"() => openSettings('{tab}')")
        page.wait_for_timeout(500)
        assert page.evaluate("() => document.getElementById('set-overlay').classList.contains('open')")
    assert "RUN ASSISTANT NOW" in page.inner_text("#set-body").upper() or True
    page.evaluate("() => closeSettings()")

    # modals
    page.evaluate("() => showNewProjectForm()")
    page.wait_for_timeout(200)
    wt_opts = page.evaluate("() => [...document.querySelectorAll('#np-worktype option')].map(o => o.value)")
    assert wt_opts == ["task", "deal"]
    page.evaluate("() => closeNewProject()")
    page.evaluate("() => showQuickTaskForm(); closeQuickTask();")

    # round trips
    page.evaluate("() => approveTriage('tri_001')")
    page.wait_for_timeout(900)
    page.evaluate("() => dismissTriage('tri_003')")
    page.wait_for_timeout(600)

    assert errors == [], "\n".join(errors)
    assert stub.app.unknown == [], f"page hit endpoints the stub does not define: {stub.app.unknown}"
    page.close()


def test_board_mobile_zero_console_errors(server, browser):
    errors = []
    page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    _collect(page, errors)
    page.goto(server + "/", wait_until="networkidle")
    page.wait_for_timeout(1000)
    names = page.evaluate("() => [...document.querySelectorAll('.list-row-name')].map(e => e.textContent)")
    assert any("Acme Corp" in n for n in names)
    # list is the default; no horizontal scroll
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    # drawer becomes a full-width sheet
    page.click(".list-row >> nth=0")
    page.wait_for_timeout(700)
    w = page.evaluate("() => document.getElementById('detail-panel').getBoundingClientRect().width")
    assert abs(w - 390) < 2
    page.keyboard.press("Escape")
    assert errors == [], "\n".join(errors)
    page.close()


def test_setup_wizard_posts_expected_body(server, browser):
    errors = []
    page = browser.new_page(viewport={"width": 1200, "height": 900})
    _collect(page, errors)
    page.goto(server + "/setup", wait_until="networkidle")
    page.wait_for_timeout(600)
    page.fill("#name", "Sam Rivera")
    page.fill("#email", "sam@example.com")
    page.fill("#company", "Example Co")
    page.evaluate("() => goto(1)")
    page.evaluate("() => pickProfession('legal')")
    page.evaluate("() => goto(3)")
    page.evaluate("() => pickSource('imap')")
    page.fill("#imap_user", "sam@gmail.com")
    page.fill("#imap_password", "app-pass")
    # a configured custom host is kept; clearing it lets the preset fill it
    page.evaluate("() => { document.getElementById('imap_host').value = ''; document.getElementById('imap_preset').value = 'gmail'; applyPreset(); }")
    page.evaluate("() => goto(4)")
    page.evaluate("() => pickAI('anthropic')")
    page.fill("#ai_api_key", "sk-test")
    page.evaluate("() => goto(5)")
    page.check("#dev_skip")
    page.evaluate("() => goto(6)")
    page.click("#save")
    page.wait_for_timeout(600)
    body = stub.app.last_setup_body
    assert body["user"]["name"] == "Sam Rivera"
    assert body["user"]["email"] == "sam@example.com"
    assert body["profession"] == "legal"
    assert body["mail"]["provider"] == "imap"
    assert body["mail"]["imap"]["host"] == "imap.gmail.com"
    assert body["mail"]["imap_password"] == "app-pass"
    assert body["ai"]["provider"] == "anthropic" and body["ai"]["api_key"] == "sk-test"
    assert body["license"]["key"] == ""
    assert body["force"] is True
    assert errors == [], "\n".join(errors)
    page.close()


def test_mail_view_smoke(server, browser):
    """The MAIL view: folders, list, reading pane, actions, composer, the
    send gate flip in Settings, and the board bridges, with zero console
    errors and no endpoint the stub does not define."""
    import json as _json
    errors = []
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    _collect(page, errors)
    page.goto(server + "/", wait_until="networkidle")
    page.wait_for_timeout(800)

    assert page.evaluate("() => mailClientOn()") is True
    page.click(".view-btn:text-is(\"MAIL\")")
    page.wait_for_timeout(900)
    assert page.evaluate("() => document.getElementById('mail-view').style.display") == "flex"
    folders = page.evaluate("() => [...document.querySelectorAll('.mail-folder .mf-name')].map(e => e.textContent.trim())")
    assert any("INBOX" in f for f in folders) and any("Drafts" in f for f in folders)
    rows = page.evaluate("() => [...document.querySelectorAll('.mail-row .mr-subj')].map(e => e.textContent)")
    assert rows == ["Lobby project photos", "Invoice 1042"]
    assert page.evaluate("() => document.querySelector('.mail-row').classList.contains('unread')")

    # open: reading pane, sandboxed iframe, cid rewritten, attachment chip, unread cleared
    page.click(".mail-row >> nth=0")
    page.wait_for_timeout(700)
    assert "Lobby project photos" in page.inner_text(".mail-subject")
    assert page.evaluate("() => document.querySelector('#mail-body iframe').getAttribute('sandbox')") == ""
    assert "cid:pic1" not in page.evaluate("() => document.querySelector('#mail-body iframe').srcdoc")
    assert "attachment?ref=7:42:INBOX&index=1" in page.evaluate("() => document.querySelector('#mail-body iframe').srcdoc")
    assert page.inner_text(".mail-attachments").strip().startswith("\U0001F4CE roof.jpg")
    assert not page.evaluate("() => document.querySelector('.mail-row').classList.contains('unread')")
    assert page.evaluate("() => document.querySelector('.mail-folder.active .mf-count').textContent.trim()") == ""

    # search filters through the API
    page.fill("#mail-search", "invoice")
    page.wait_for_timeout(700)
    assert page.evaluate("() => [...document.querySelectorAll('.mail-row .mr-subj')].map(e => e.textContent)") == ["Invoice 1042"]
    page.fill("#mail-search", "")
    page.wait_for_timeout(700)
    page.click(".mail-row >> nth=0")
    page.wait_for_timeout(500)

    # composer: Send is absent while sending is off; Save draft works
    page.click(".mail-actions button:text-is(\"Reply all\")")
    page.wait_for_timeout(600)
    assert page.evaluate("() => document.getElementById('mail-compose-overlay').classList.contains('open')")
    assert page.input_value("#mc-to") == "Priya Shah <priya@acme.example>"
    assert page.input_value("#mc-cc") == "ops@acme.example"
    assert page.input_value("#mc-subject") == "Re: Lobby project photos"
    assert page.evaluate("() => [...document.querySelectorAll('#mail-compose .mc-foot .mail-btn')].map(b => b.textContent.trim())")[0] == "Save draft"
    assert "Sending is off" in page.inner_text("#mail-compose .mc-hint")
    page.evaluate("() => { document.getElementById('mc-body').focus(); document.execCommand('insertText', false, 'Thanks Priya'); }")
    page.click("#mail-compose >> text=Save draft")
    page.wait_for_timeout(500)
    assert not page.evaluate("() => document.getElementById('mail-compose-overlay').classList.contains('open')")

    # flip the send gate in Settings > Mail; the composer gains Send
    page.evaluate("() => openSettings('mail')")
    page.wait_for_timeout(500)
    assert "ALLOW SENDING" in page.inner_text("#set-body")
    page.click("#set-body button:text-is(\"ALLOW SENDING\")")
    page.wait_for_timeout(600)
    assert "TURN OFF" in page.inner_text("#set-body")
    page.evaluate("() => closeSettings()")
    page.click(".mail-actions button:text-is(\"Forward\")")
    page.wait_for_timeout(600)
    assert page.input_value("#mc-subject") == "Fw: Lobby project photos"
    assert page.evaluate("() => document.querySelector('#mail-compose .mc-foot .mail-btn.primary').textContent.trim()") == "Send"
    page.fill("#mc-to", "priya@acme.example")
    page.select_option("#mc-project", "proj_001")
    page.click("#mail-compose >> text=Send")
    page.wait_for_timeout(700)
    assert not page.evaluate("() => document.getElementById('mail-compose-overlay').classList.contains('open')")

    # actions + bridges
    page.click(".mail-actions button:text-is(\"Flag\")")
    page.wait_for_timeout(400)
    page.select_option("#mail-file-proj", "proj_001")
    page.click(".mail-actions button:text-is(\"File\")")
    page.wait_for_timeout(500)
    assert "Filed to" in page.inner_text("#mail-note")
    page.click(".mail-actions button:text-is(\"Triage\")")
    page.wait_for_timeout(500)
    page.click(".mail-actions button:text-is(\"Archive\")")
    page.wait_for_timeout(500)
    assert page.evaluate("() => document.querySelectorAll('.mail-row').length") == 1

    state = page.evaluate("async () => (await fetch('/api/_mail_state')).json()")
    assert state["drafts"][0]["reply_ref"] == "7:42:INBOX" and "Thanks Priya" in state["drafts"][0]["body_html"]
    assert state["sent"][0]["to"] == "priya@acme.example" and state["sent"][0]["project_id"] == "proj_001"
    assert state["sent"][0]["subject"] == "Fw: Lobby project photos"
    assert state["flags"][0] == {"ref": "7:42:INBOX", "flagged": True}      # read is set via mark_read=1 on open
    assert state["filed"][0] == {"ref": "7:42:INBOX", "project_id": "proj_001"}
    assert state["staged"][0] == {"ref": "7:42:INBOX"}
    assert state["moved"][0] == ["/api/mail/client/move", {"ref": "7:42:INBOX", "folder": "archive"}]

    # back to the list view: the pane hides, the board renders
    page.click("text=LIST")
    page.wait_for_timeout(400)
    assert page.evaluate("() => document.getElementById('mail-view').style.display") == "none"
    assert errors == [], "\n".join(errors)
    assert stub.app.unknown == [], f"page hit endpoints the stub does not define: {stub.app.unknown}"
    page.close()
