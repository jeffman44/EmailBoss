"""Out of office: non-deadline work does not age while the user is out
(owner request, 2026-11-05). OOO days count like weekends on every business
clock, server and page; deadlines keep their own curve."""
from datetime import datetime

import pytest

from plannerboard.business_hours import BusinessHours

from .test_terminal import _api, _mem, _terminal_app  # noqa: F401


def test_ooo_days_count_like_weekends():
    bh = BusinessHours(tz="America/Denver", start=8, end=17, ooo={"2026-11-10", "2026-11-11"})
    mon9, thu9 = datetime(2026, 11, 9, 9), datetime(2026, 11, 12, 9)
    assert bh.elapsed_biz_hours(mon9, thu9) == 9.0                 # Mon 9-17 (8) + Thu 8-9 (1)
    assert bh.add_biz_days("2026-11-09", 2) == "2026-11-13"         # Thu, Fri
    assert BusinessHours(tz="America/Denver").elapsed_biz_hours(mon9, thu9) == 27.0


def test_ooo_api_feeds_the_clock_and_the_page(tmp_path, monkeypatch):
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    c, h = _api(app)
    assert c.post("/api/ooo", json={"start": "2026-11-17", "end": "2026-11-11"}, headers=h).status_code == 400
    r = c.post("/api/ooo", json={"start": "2026-11-11", "end": "2026-11-17", "note": "out"}, headers=h).get_json()
    assert r["days"][0] == "2026-11-11" and r["days"][-1] == "2026-11-17" and len(r["days"]) == 7
    assert "2026-11-12" in rt.hours.ooo
    prof = c.get("/api/profession").get_json()
    assert prof["business_hours"]["ooo"] == r["days"]
    # Re-saving the profession from the page never stores ooo as an override.
    c.put("/api/profession", json={"overrides": {"business_hours": prof["business_hours"]}}, headers=h)
    assert "ooo" not in (rt.config.profession_overrides.get("business_hours") or {})
    assert rt.hours.ooo                                              # still applied after the reload
    r = c.delete("/api/ooo/2026-11-11", headers=h).get_json()
    assert r["days"] == [] and not rt.hours.ooo


def test_page_clocks_skip_ooo_but_not_deadlines(tmp_path, monkeypatch):
    sync = pytest.importorskip("playwright.sync_api").sync_playwright
    import threading
    from werkzeug.serving import make_server
    app, rt, server, prov = _terminal_app(tmp_path, monkeypatch, n=0)
    c, h = _api(app)
    c.post("/api/ooo", json={"start": "2026-11-10", "end": "2026-11-11"}, headers=h)
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with sync() as pw:
            b = pw.chromium.launch()
            page = b.new_page()
            page.goto(f"http://127.0.0.1:{srv.server_port}/", wait_until="networkidle")
            out = page.evaluate("""() => {
              const mon = new Date(2026, 10, 9, 0, 0, 0), thu = new Date(2026, 10, 12, 0, 0, 0);
              return { tue: isBizDay(new Date(2026, 10, 10, 12)), fri: isBizDay(new Date(2026, 10, 13, 12)),
                       aging: bizDaysBetween(mon, thu, true), deadline: bizDaysBetween(mon, thu) };
            }""")
            b.close()
    finally:
        srv.shutdown()
    assert out == {"tue": False, "fri": True, "aging": 1, "deadline": 3}
