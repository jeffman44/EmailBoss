"""Deadline extraction from email text.

`extract_deadline(text, today)` finds the first "due / deadline / by <date> /
respond by / reply by / bids due / submit by" phrase that carries a real date
and returns it as YYYY-MM-DD, or "" when there is none. Handles ISO dates,
m/d[/yy[yy]], "March 14[, 2026]", "14 March", weekday names ("Friday", "next
Friday", "this Friday"), "today" and "tomorrow". A bare "by the way" never
matches because a date is required.

`parse_date_text(text, today)` is the same date grammar without the keyword,
used by the CSV import.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WD_RE = r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"

_DATE_RES = [
    ("iso", re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")),
    ("mdy", re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")),
    ("mon_d", re.compile(r"\b(" + _MONTH_RE + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", re.I)),
    ("d_mon", re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + _MONTH_RE + r")\.?(?:,?\s+(\d{4}))?\b", re.I)),
    ("rel", re.compile(r"\b(today|tomorrow|end of (?:the )?day|eod)\b", re.I)),
    ("wd", re.compile(r"\b(?:(next|this|coming)\s+)?(" + _WD_RE + r")\b", re.I)),
]

_KEYWORD_RE = re.compile(
    r"\b(?:bids?\s+(?:are\s+)?due|proposals?\s+(?:are\s+)?due|quotes?\s+(?:are\s+)?due|due(?:\s+(?:date|by|on))?|"
    r"deadline(?:\s+(?:is|for|of))?|respond\s+by|reply\s+by|response\s+(?:is\s+)?(?:needed|required)\s+by|"
    r"submit(?:ted)?\s+by|submission\s+(?:date|deadline)|no\s+later\s+than|not\s+later\s+than|needed\s+by|"
    r"must\s+be\s+(?:received|submitted|in)\s+by|by)\b[\s:,\-]*(?:the\s+|end\s+of\s+(?:the\s+)?(?:day\s+)?(?:on\s+)?)?", re.I)

_WINDOW = 48


def _mk(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _year_for(m, d, today):
    """No year given: this year, rolled to next when the date is >7 days past."""
    c = _mk(today.year, m, d)
    if c is None:
        return None
    if c < today - timedelta(days=7):
        c = _mk(today.year + 1, m, d)
    return c


def _norm_year(y):
    y = int(y)
    return 2000 + y if y < 100 else y


def _parse_at(kind, mt, today):
    g = mt.groups()
    if kind == "iso":
        return _mk(int(g[0]), int(g[1]), int(g[2]))
    if kind == "mdy":
        m, d = int(g[0]), int(g[1])
        if g[2]:
            return _mk(_norm_year(g[2]), m, d)
        return _year_for(m, d, today)
    if kind == "mon_d":
        m, d = _MONTHS[g[0][:3].lower()], int(g[1])
        return _mk(_norm_year(g[2]), m, d) if g[2] else _year_for(m, d, today)
    if kind == "d_mon":
        d, m = int(g[0]), _MONTHS[g[1][:3].lower()]
        return _mk(_norm_year(g[2]), m, d) if g[2] else _year_for(m, d, today)
    if kind == "rel":
        w = g[0].lower()
        return today + timedelta(days=1) if w == "tomorrow" else today
    if kind == "wd":
        qual, name = (g[0] or "").lower(), g[1].lower()
        idx = next(i for i, w in enumerate(_WEEKDAYS) if w.startswith(name[:3]))
        delta = (idx - today.weekday()) % 7
        if qual == "next":
            delta = delta + 7 if delta < 7 else delta
            if delta == 0:
                delta = 7
        elif delta == 0:
            delta = 0 if qual == "this" else 7
        return today + timedelta(days=delta)
    return None


def parse_date_text(text, today=None):
    """First date in `text` (any supported format) as YYYY-MM-DD, else ''."""
    today = today or date.today()
    best = None
    for kind, rx in _DATE_RES:
        mt = rx.search(text or "")
        if not mt:
            continue
        d = _parse_at(kind, mt, today)
        if d and (best is None or mt.start() < best[0]):
            best = (mt.start(), d)
    return best[1].isoformat() if best else ""


def extract_deadline(text, today=None):
    """YYYY-MM-DD of the first keyword-anchored date in `text`, else ''."""
    today = today or date.today()
    text = text or ""
    for km in _KEYWORD_RE.finditer(text):
        seg = text[km.end(): km.end() + _WINDOW]
        # the date has to start right after the keyword (allow a short filler)
        best = None
        for kind, rx in _DATE_RES:
            mt = rx.search(seg)
            if mt and mt.start() <= 12:
                d = _parse_at(kind, mt, today)
                if d and (best is None or mt.start() < best[0]):
                    best = (mt.start(), d)
        if best:
            return best[1].isoformat()
    return ""
