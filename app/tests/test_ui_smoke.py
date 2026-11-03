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
    stub.QUESTIONS[:] = [dict(q) for q in stub.QUESTIONS_SEED]
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

    # Today is the default view: the five sections, rows open the drawer
    assert page.evaluate("() => currentView") == "today"
    today = page.inner_text("#today-view")
    for sec in ("Reply today", "Nudge today", "Decisions for you", "New mail to sort", "Overdue"):
        assert sec in today, f"{sec!r} missing from Today"
    assert "Birch Dental Insurance renewal" in today and "1 day late" in today
    assert page.inner_text("#brand") == "Email Boss" and "PLANNER" not in page.inner_text(".header").upper()
    assert page.inner_text("#decisions-badge") == "1"
    page.click("#today-view .today-row >> nth=0")
    page.wait_for_timeout(700)
    assert page.evaluate("() => document.getElementById('detail-panel').classList.contains('open')")
    page.keyboard.press("Escape")
    page.click(".view-btn:text-is(\"LIST\")")
    page.wait_for_timeout(500)

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
    for tab in ("display", "fields"):
        page.evaluate(f"() => openSettings('{tab}')")
        page.wait_for_timeout(400)
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
    assert "Reply today" in page.inner_text("#today-view")
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.evaluate("() => setView('list')")
    page.wait_for_timeout(500)
    names = page.evaluate("() => [...document.querySelectorAll('.list-row-name')].map(e => e.textContent)")
    assert any("Acme Corp" in n for n in names)
    # no horizontal scroll on the list either
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
    """Three screens (you / work / done): an app-password mailbox, a
    profession, your own AI key; the POST carries them in the setup shape."""
    errors = []
    page = browser.new_page(viewport={"width": 1200, "height": 900})
    _collect(page, errors)
    page.goto(server + "/setup", wait_until="networkidle")
    page.wait_for_timeout(600)
    page.fill("#name", "Sam Rivera")
    page.fill("#email", "sam@example.com")
    page.evaluate("() => pickProvider('yahoo')")
    page.fill("#imap_password", "app-pass")
    page.evaluate("() => step(1)")
    page.evaluate("() => pickProfession('legal')")
    page.evaluate("() => step(1)")
    page.evaluate("() => { document.getElementById('ai_own').checked = true; aiModeChanged(); "
                  "document.getElementById('ai_provider').value = 'anthropic'; aiProviderChanged(); }")
    page.evaluate("() => { document.getElementById('ai_api_key').value = 'sk-test'; }")   # lives under Advanced
    page.click("#save")
    page.wait_for_timeout(800)
    body = stub.app.last_setup_body
    assert body["user"]["name"] == "Sam Rivera" and body["user"]["email"] == "sam@example.com"
    assert body["profession"] == "legal"
    assert body["mail"]["provider"] == "imap" and body["mail"]["imap"]["host"] == "imap.mail.yahoo.com"
    assert body["mail"]["imap_password"] == "app-pass"
    assert body["ai"]["provider"] == "anthropic" and body["ai"]["api_key"] == "sk-test"
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
    _src = page.evaluate("() => document.querySelector('#mail-body iframe').srcdoc")
    assert "attachment?ref=7:42:INBOX&index=1" in _src or "data:image/png" in _src   # inline image resolved
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
    assert page.evaluate("() => [...document.querySelectorAll('#mail-compose .mc-foot .mail-btn')].map(b => b.textContent.trim())")[0] in ("Save draft", "Save as draft")
    assert "Sending is off" in page.inner_text("#mail-compose .mc-hint")
    page.evaluate("() => { document.getElementById('mc-body').focus(); document.execCommand('insertText', false, 'Thanks Priya'); }")
    page.click("#mc-save")
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
    page.once("dialog", lambda d: d.accept())   # Send confirms the recipients by name
    page.click("#mail-compose >> text=Send")
    page.wait_for_timeout(700)
    assert not page.evaluate("() => document.getElementById('mail-compose-overlay').classList.contains('open')")
    page.wait_for_timeout(10500)                  # the undo window, then it really sends

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


def test_board_13_today_decisions_ai_drafts(server, browser):
    """1.3.0 surfaces: Decisions modal (keys), Settings > AI cards, AI result
    panel with Save as draft, Drafts tab, triage relabels, urgency words,
    Add all counts, 402 license gate, Display toggles, keyboard."""
    stub.TRIAGE[:] = [dict(t) for t in stub.TRIAGE_SEED]
    stub.QUESTIONS[:] = [dict(q) for q in stub.QUESTIONS_SEED]
    stub.app.unknown.clear()
    errors = []
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    _collect(page, errors)
    page.goto(server + "/", wait_until="networkidle")
    page.wait_for_timeout(1200)

    # Decisions: open from the header, answer with key 1, draft ready -> open draft
    page.click("#decisions-btn")
    page.wait_for_timeout(500)
    assert page.evaluate("() => document.getElementById('dec-overlay').classList.contains('open')")
    modal = page.inner_text("#dec-modal")
    assert "Commit to it?" in modal and "1 of 1" in modal and "Something else..." in modal and "Skip" in modal
    page.keyboard.press("1")
    page.wait_for_timeout(700)
    assert "Draft ready" in page.inner_text("#dec-modal")
    assert stub.QUESTIONS[0]["answer"] == "Yes"
    page.click("#dec-modal >> text=Open draft")
    page.wait_for_timeout(800)
    assert page.evaluate("() => currentView") == "drafts"
    drafts = page.inner_text("#drafts-view")
    assert "Hi Priya, yes, two weeks works." in drafts and "written by the helper" in drafts
    assert "Copy" in drafts and "Open in my mail" in drafts and "Delete" in drafts
    assert page.inner_text("#decisions-badge") == "0" or not page.is_visible("#decisions-badge")
    # Esc closes an (empty) decisions modal too
    page.evaluate("() => openDecisions()")
    page.wait_for_timeout(400)
    assert "Nothing needs your decision" in page.inner_text("#dec-modal")
    page.keyboard.press("Escape")
    assert not page.evaluate("() => document.getElementById('dec-overlay').classList.contains('open')")

    # Settings > AI: three cards, built-in balance + buy, helper wording, log uses names
    page.evaluate("() => openSettings('ai')")
    page.wait_for_timeout(700)
    body = page.inner_text("#set-body")
    for t in ("Email Boss AI (built in, recommended)", "My own key (advanced)", "Off", "Check my inbox and board now",
              "never sends", "Birch Dental Insurance renewal"):
        assert t in body, f"{t!r} missing from Settings > AI"
    assert "proj_002" not in body and "[object Object]" not in body
    assert page.is_visible("#ai-own-key")             # own key provider: key field right there
    page.click("#set-body .eb-card >> nth=0")
    page.wait_for_timeout(300)
    assert "TURN ON" in page.inner_text("#set-body")
    page.click("#set-body >> text=TURN ON")
    page.wait_for_timeout(900)
    assert stub.CONFIG["ai"]["provider"] == "emailboss"
    body = page.inner_text("#set-body")
    assert "Credits left: 240" in body and "500 credits ($5.00)" in body and "Used this month: 31 requests" in body
    page.click("#set-ai-run")
    page.wait_for_timeout(700)
    res = page.inner_text("#set-ai-run-result")
    assert res.startswith("Done.") and "[object" not in res
    page.evaluate("() => closeSettings()")

    # AI off: the drawer buttons stay, a click lands on Settings > AI with the reason
    page.evaluate("() => setView('list')")
    page.wait_for_timeout(400)
    page.click(".list-row >> nth=0")
    page.wait_for_timeout(700)
    for t in ("Write a reply for me", "Write a follow-up", "What's the status?"):
        assert t in page.inner_text("#panel-body")
    page.click("#panel-body >> text=Write a reply for me")
    page.wait_for_timeout(700)
    assert "Reply draft" in page.inner_text("#ai-modal")
    assert "Two weeks works for us." in page.input_value("#ai-text")
    page.click("#ai-modal >> text=Save as draft")
    page.wait_for_timeout(500)
    assert stub.DRAFTS[0]["body"].startswith("Hi Priya") and stub.DRAFTS[0]["created_by"] == "user"
    page.evaluate("() => { HEALTH.ai.provider = 'none'; }")
    page.click("#panel-body >> text=Write a follow-up")
    page.wait_for_timeout(500)
    assert page.evaluate("() => _setTab") == "ai"
    assert "Turn on Email Boss AI" in page.inner_text("#set-body")
    page.evaluate("() => { closeSettings(); HEALTH.ai.provider = 'anthropic'; HEALTH.ai.credits_exhausted = true; }")
    page.click("#panel-body >> text=What's the status?")
    page.wait_for_timeout(500)
    assert "Out of AI credits" in page.inner_text("#set-body")
    page.evaluate("() => { closeSettings(); HEALTH.ai.credits_exhausted = false; closePanel(); }")

    # Ask mode + weekly digest render plain text
    page.click("#ask-toggle")
    page.fill("#search", "acme")
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    assert "Found 1 match." in page.inner_text("#ai-modal") and "Acme Corp Website refresh" in page.inner_text("#ai-modal")
    page.keyboard.press("Escape")
    page.click("#ask-toggle")
    page.fill("#search", "")
    page.click("#digest-btn")
    page.wait_for_timeout(600)
    assert "A steady week" in page.inner_text("#ai-modal") and "Overdue: 1" in page.inner_text("#ai-modal")
    page.keyboard.press("Escape")

    # Triage relabels, leave reason, one quote control, Reply by, AI reply on the card
    tri = page.inner_text("#triage-content")
    for t in ("Add to board", "Not now", "Not work, hide", "Write a reply", "No matching matter yet", "Needs a quote"):
        assert t.lower() in tri.lower(), f"{t!r} missing on triage cards"
    for old in ("APPROVE\n", "IGNORE\n", "$ QR"):
        assert old not in tri
    assert page.evaluate("() => document.querySelector('#triage-tri_002 .qr-check input').checked") is True
    assert "Reply by" in page.evaluate("() => [...document.querySelectorAll('#triage-datetype-tri_001 option')].map(o => o.textContent).join('|')")
    # urgency words on rows, number only in the tooltip
    words = page.evaluate("() => [...document.querySelectorAll('.list-row .urg-word')].map(e => e.textContent)")
    assert "Due today" in words and "1 day late" in words and not any(w.strip().isdigit() for w in words)
    assert page.evaluate("() => document.querySelector('.list-row .urg-word').title").startswith("Urgency score")

    # keyboard: j selects, ? opens the cheat sheet
    page.click("body", position={"x": 5, "y": 5})
    page.keyboard.press("j")
    assert page.evaluate("() => !!document.querySelector('.kb-sel')")
    page.keyboard.press("?")
    assert page.evaluate("() => document.getElementById('kbd-overlay').classList.contains('open')")
    page.keyboard.press("Escape")

    # Not work, hide on a card matched to a live project asks first; Cancel only sets the mail aside
    page.once("dialog", lambda d: (d.message.count("Birch Dental") >= 1) and d.dismiss())
    page.evaluate("() => ignoreTriage('tri_003')")
    page.wait_for_timeout(700)
    assert next(p for p in stub.PROJECTS if p["id"] == "proj_002")["status"] == "action_required"

    # Add all: server counts in the toast, unmatched card left
    page.evaluate("() => approveAllTriage()")
    page.wait_for_timeout(900)
    assert "Added 1, 1 left for you" in page.inner_text("#toast")
    assert [t["triage_id"] for t in stub.TRIAGE if t["status"] == "pending"] == ["tri_002"]

    # Display: large text + light theme persist through PUT /api/config
    page.evaluate("() => openSettings('display')")
    page.wait_for_timeout(400)
    page.evaluate("() => setDisplay('large_text', true)")
    page.wait_for_timeout(400)
    assert page.evaluate("() => document.body.classList.contains('large-text')")
    assert page.evaluate("() => parseFloat(getComputedStyle(document.querySelector('.set-row-sub')).fontSize)") >= 13
    page.evaluate("() => setDisplay('light_theme', true)")
    page.wait_for_timeout(400)
    assert page.evaluate("() => document.body.classList.contains('light')")
    assert stub.CONFIG["display"] == {"large_text": True, "light_theme": True}
    page.evaluate("() => setFeature('money', false)")
    page.wait_for_timeout(600)
    assert stub.app.last_overrides["features"]["money"] is False
    assert page.evaluate("() => showMoney()") is False
    page.evaluate("() => setFeature('money', true)")
    page.wait_for_timeout(500)

    # Fields: add a custom field through the profession overrides
    page.evaluate("() => openSettings('fields')")
    page.wait_for_timeout(300)
    page.fill("#cf-label", "Hearing date")
    page.select_option("#cf-type", "date")
    page.click("#set-body >> text=ADD FIELD")
    page.wait_for_timeout(600)
    added = [f for f in stub.app.last_overrides["fields"] if f["key"] == "hearing_date"]
    assert added and added[0]["type"] == "date" and "card" in added[0]["show_on"]
    assert len(stub.app.last_overrides["fields"]) == 6

    # General: timezone + 24x7 hours
    page.evaluate("() => openSettings('general')")
    page.wait_for_timeout(400)
    assert page.evaluate("() => document.querySelectorAll('#set-tz option').length") > 5
    page.check("#set-247")
    page.click("#set-body >> text=SAVE")
    page.wait_for_timeout(600)
    assert stub.app.last_overrides["business_hours"]["days"] == [0, 1, 2, 3, 4, 5, 6]
    assert stub.app.last_overrides["business_hours"]["end"] == 24

    # Stage editor speaks plain roles
    page.evaluate("() => openSettings('workflows')")
    page.wait_for_timeout(500)
    roles = page.evaluate("() => [...document.querySelectorAll('#set-wf-body option')].map(o => o.textContent)")
    assert "Waiting on someone" in roles and "Gone quiet" in roles
    page.evaluate("() => closeSettings()")

    # 402 license_required anywhere opens Settings > License with the banner + trial
    page.evaluate("""() => { const orig = window.fetch; window.fetch = (u, o) => (String(u).startsWith('/api/projects/') && o && o.method === 'PUT')
        ? orig('/api/_force402') : orig(u, o); }""")
    page.evaluate("() => apiPut('proj_001', {scope: 'x'}).catch(() => {})")
    page.wait_for_timeout(800)
    assert page.evaluate("() => _setTab") == "license"
    lic = page.inner_text("#set-body")
    assert "Your board is read-only until your trial starts or a license key is entered" in lic
    assert "Start free trial" in lic
    page.click("#set-body >> text=Start free trial")
    page.wait_for_timeout(600)
    assert stub.app.last_trial == {"email": "sam@example.com"}

    errors = [e for e in errors if "status of 402" not in e]   # the forced 402 itself
    assert errors == [], "\n".join(errors)
    assert stub.app.unknown == [], f"page hit endpoints the stub does not define: {stub.app.unknown}"
    page.close()
