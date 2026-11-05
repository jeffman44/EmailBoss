"""Out of office: non-deadline work does not age while you are out.

Ranges (both ends inclusive, YYYY-MM-DD) are kept in the board's app_meta
(``ooo_ranges``). Their days count like weekends on every business clock
(business_hours.BusinessHours.ooo, and PROF.business_hours.ooo in the page):
response SLA, clock-driven tasks, the follow-up overdue curve and new
follow-up dates. Deadlines keep their dates and their own curve.

2.0 sets these by hand (Settings or the API); the calendar integration feeds
them from Out of Office events when it lands.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

META_KEY = "ooo_ranges"
MAX_DAYS = 366


def ranges(store) -> list[dict]:
    try:
        out = json.loads(store.meta_get(META_KEY) or "[]")
    except (TypeError, ValueError):
        return []
    return [r for r in out if isinstance(r, dict) and r.get("start") and r.get("end")]


def days(rs) -> list[str]:
    out = set()
    for r in rs:
        try:
            d, end = date.fromisoformat(str(r["start"])[:10]), date.fromisoformat(str(r["end"])[:10])
        except ValueError:
            continue
        n = 0
        while d <= end and n < MAX_DAYS:
            out.add(d.isoformat())
            d += timedelta(days=1)
            n += 1
    return sorted(out)


def apply(rt) -> list[str]:
    """Push the stored days into the live business clock."""
    ds = days(ranges(rt.store))
    rt.hours.set_ooo(ds)
    return ds


def set_range(store, start: str, end: str, note: str = "") -> list[dict]:
    s, e = date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
    if e < s:
        raise ValueError("The end date is before the start date.")
    if (e - s).days >= MAX_DAYS:
        raise ValueError("That is longer than a year.")
    rs = [r for r in ranges(store) if not (r["start"] == s.isoformat() and r["end"] == e.isoformat())]
    rs.append({"start": s.isoformat(), "end": e.isoformat(), "note": str(note or "")[:200]})
    rs.sort(key=lambda r: r["start"])
    store.meta_set(META_KEY, json.dumps(rs))
    return rs


def clear_range(store, start: str) -> list[dict]:
    rs = [r for r in ranges(store) if r["start"] != str(start)[:10]]
    store.meta_set(META_KEY, json.dumps(rs))
    return rs
