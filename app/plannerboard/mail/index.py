"""Search over the local mail store (2.0 plan section 3.4, Phase 1).

Three parts:

1. Text preparation for indexing: body text with quoted replies and
   forwarded history stripped (``prepare_body``), sender text, and
   "codes" (part numbers, PO and invoice numbers, model numbers) for the
   trigram side index.
2. The query language (``parse``): free words, ``"quoted phrases"``,
   ``-negation``, ``OR``, and the operators in ``OPERATORS``. An unknown
   operator becomes plain text plus an inline hint, never a silent zero.
3. Execution (``search``): FTS5 ``bm25`` with column weights subject 3,
   sender 2, body 1, ``LIMIT 500``, then a Python rerank by recency
   (half-life 90 days) and reply affinity (how often the user has written
   to the sender). Results come back grouped (messages, people,
   attachments) with a coverage block saying what was and was not searched.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .message import text_from_html

CANDIDATE_LIMIT = 500
HALF_LIFE_DAYS = 90.0

OPERATORS = {
    "from": "sender name or address",
    "to": "recipient",
    "cc": "copied recipient",
    "subject": "words in the subject",
    "has": "has:attachment",
    "filename": "attachment file name",
    "project": "messages filed to a project",
    "status": "messages on projects at a status",
    "stage": "messages on projects at a stage",
    "before": "before a date (YYYY-MM-DD)",
    "after": "after a date (YYYY-MM-DD)",
    "newer_than": "newer than 3d, 2w, 6m or 1y",
    "older_than": "older than 3d, 2w, 6m or 1y",
    "is": "is:unread, is:read, is:starred, is:snoozed, is:waiting",
    "label": "a label or folder name",
    "in": "in:inbox, in:sent, in:drafts, in:trash, in:archive, in:junk",
    "account": "one connected account",
}
IN_FOLDERS = {"inbox", "sent", "drafts", "trash", "archive", "junk", "spam", "anywhere"}
IS_VALUES = {"unread", "read", "starred", "flagged", "snoozed", "waiting"}
LATER = {"snoozed": "Snooze", "waiting": "Waiting-on"}


# ── 1. text preparation ──────────────────────────────────────────────────────

_ON_WROTE = re.compile(r"^\s*On .{0,200}?wrote:\s*$", re.IGNORECASE | re.MULTILINE)
_ORIGINAL = re.compile(r"^\s*-{2,}\s*(Original Message|Forwarded message)\s*-{2,}\s*$",
                       re.IGNORECASE | re.MULTILINE)
_OUTLOOK_HDR = re.compile(r"^\s*From:\s.+\n\s*(Sent|Date):\s.+\n\s*To:\s", re.IGNORECASE | re.MULTILINE)
_UNDERSCORES = re.compile(r"^\s*_{8,}\s*$", re.MULTILINE)
_CODE_RE = re.compile(r"\b(?=[A-Za-z0-9./#-]*\d)[A-Za-z0-9][A-Za-z0-9./#-]{2,}[A-Za-z0-9]\b")


def strip_quoted(text: str) -> str:
    """Drop quoted reply history: '>' lines and everything after the first
    reply / forward header. What is left is what this message added."""
    if not text:
        return ""
    cut = len(text)
    for rx in (_ON_WROTE, _ORIGINAL, _OUTLOOK_HDR, _UNDERSCORES):
        m = rx.search(text)
        if m and m.start() < cut:
            cut = m.start()
    kept = [ln for ln in text[:cut].splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(kept).strip()


def prepare_body(msg) -> str:
    raw = msg.body or ""
    text = text_from_html(raw) if msg.is_html else raw
    if msg.is_html:
        # text_from_html collapses lines; recover the reply markers it keeps.
        text = re.sub(r"(?i)\s(On [^\n]{0,200}?wrote:)", r"\n\1\n", text)
        text = re.sub(r"(?i)\s(-{2,}\s*(Original Message|Forwarded message)\s*-{2,})", r"\n\1\n", text)
    return strip_quoted(text)[:400_000]


def sender_text(msg) -> str:
    s = msg.sender
    return f"{(s.name or '') if s else ''} {(s.email or '') if s else ''}".strip()


def codes_from(text: str, limit: int = 200) -> list[str]:
    """Part-number-like tokens: at least four characters with a digit."""
    seen, out = set(), []
    for m in _CODE_RE.finditer(text or ""):
        tok = m.group(0)
        k = tok.lower()
        if k not in seen and not re.fullmatch(r"\d{1,4}", tok):
            seen.add(k)
            out.append(tok)
            if len(out) >= limit:
                break
    return out


def attachment_codes(atts) -> list[str]:
    out = []
    for a in atts:
        out.extend(codes_from((a.name or "").rsplit(".", 1)[0], limit=10))
    return out


def attachment_text(att) -> str:
    """Searchable text of one attachment: plain text and CSV directly, PDF
    through pypdf when installed. Nothing else (no office conversion, no OCR)."""
    ctype = (att.content_type or "").lower()
    name = (att.name or "").lower()
    data = att.content or b""
    if not data:
        return ""
    if ctype.startswith("text/") or name.endswith((".txt", ".csv")):
        return data[:500_000].decode("utf-8", "replace")
    if ctype == "application/pdf" or name.endswith(".pdf"):
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages[:40])
        except Exception:
            return ""
    return ""


# ── 2. the query language ────────────────────────────────────────────────────

@dataclass
class Term:
    text: str
    negate: bool = False
    phrase: bool = False
    or_group: int = -1        # terms sharing a group id are OR'ed


@dataclass
class Query:
    raw: str
    terms: list[Term] = field(default_factory=list)
    filters: list[list] = field(default_factory=list)   # [op, value, negate, or_group]
    hints: list[str] = field(default_factory=list)

    @property
    def has_text(self) -> bool:
        return any(not t.negate for t in self.terms)


_TOKEN_RE = re.compile(r'(-(?=\S))?([A-Za-z_]+:(?=\S))?(?:"([^"]*)"?|(\S+))')


def _tokens(q: str) -> list[tuple[bool, str | None, str, bool]]:
    """(negated, operator or None, text, quoted) per token. ``-"two words"``,
    ``from:"Dana Smith"`` and ``-subject:lobby`` all keep their shape."""
    out = []
    for m in _TOKEN_RE.finditer(q or ""):
        neg = bool(m.group(1))
        op = m.group(2)[:-1].lower() if m.group(2) else None
        quoted = m.group(3) is not None
        text = m.group(3) if quoted else (m.group(4) or "")
        if not text and not quoted and not op:
            continue
        out.append((neg, op, text, quoted))
    return out


def parse(q: str) -> Query:
    query = Query(raw=q or "")
    group = 0
    pending_or = False
    last = None                        # the previous Term or filter list entry
    for neg, op, text, quoted in _tokens(q or ""):
        if text == "OR" and not quoted and not op and not neg:
            pending_or = True
            continue
        item = None
        if op is not None and text != "":
            if op in OPERATORS:
                problem = _check_value(op, text)
                if problem:
                    query.hints.append(problem)
                    pending_or = False
                    continue
                item = [op, text, neg, -1]
                query.filters.append(item)
            else:
                query.hints.append(f"\"{op}:\" is not a search operator, so \"{text}\" was searched as text. "
                                   f"Operators: {', '.join(o + ':' for o in OPERATORS)}.")
                op = None
        if item is None:
            if op is not None:          # "from:" with nothing after it
                text = f"{op}:"
            item = Term(text=text, negate=neg, phrase=quoted or " " in text)
            query.terms.append(item)
        if pending_or and last is not None and not neg and not _negated(last):
            if type(last) is not type(item):
                query.hints.append("OR joins two words or two operators, not a word and an operator; "
                                   "both were required.")
            else:
                g = _group_of(last)
                if g < 0:
                    g = group
                    group += 1
                    _set_group(last, g)
                _set_group(item, g)
        pending_or = False
        last = item
    return query


def _negated(item) -> bool:
    return item.negate if isinstance(item, Term) else bool(item[2])


def _group_of(item) -> int:
    return item.or_group if isinstance(item, Term) else item[3]


def _set_group(item, g: int) -> None:
    if isinstance(item, Term):
        item.or_group = g
    else:
        item[3] = g


def _check_value(op: str, val: str) -> str | None:
    v = val.lower()
    if op == "has" and v not in ("attachment", "attachments"):
        return f"has:{val} is not supported; use has:attachment."
    if op == "in" and v not in IN_FOLDERS:
        return f"in:{val} is not a folder; use in:inbox, in:sent, in:drafts, in:trash, in:archive or in:junk."
    if op == "is" and v not in IS_VALUES:
        return f"is:{val} is not supported; use is:unread, is:read, is:starred, is:snoozed or is:waiting."
    if op in ("before", "after") and _date_ts(val) is None:
        return f"{op}:{val} is not a date; use YYYY-MM-DD."
    if op in ("newer_than", "older_than") and _span_seconds(val) is None:
        return f"{op}:{val} is not a span; use a number and d, w, m or y (for example 2w)."
    return None


def _date_ts(val: str) -> float | None:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _span_seconds(val: str) -> float | None:
    m = re.fullmatch(r"(\d+)([dwmy])", (val or "").lower())
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2)
    return n * {"d": 86400, "w": 7 * 86400, "m": 30 * 86400, "y": 365 * 86400}[u]


def _fts_quote(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _fts_term(t: Term, prefix: bool) -> str:
    words = re.findall(r"\w+", t.text, flags=re.UNICODE)
    if not words:
        return ""
    if len(words) == 1 and not t.phrase:
        return _fts_quote(words[0]) + ("*" if prefix else "")
    return _fts_quote(" ".join(words))


def _is_code(text: str) -> bool:
    return len(text) >= 3 and bool(re.search(r"\d", text))


# ── 3. execution ─────────────────────────────────────────────────────────────

def search(store, q: str, limit: int = 50, now: float | None = None, account: str | None = None,
           project_resolver=None, coverage: dict | None = None, typing: bool | None = None) -> dict:
    """Run one query against the store. ``project_resolver(op, value)``
    returns a set of Message-ID headers for project:/status:/stage: (the
    board owns that data); without one those operators answer with a hint."""
    started = time.perf_counter()
    now = now or time.time()
    query = parse(q)

    # operators first: they narrow every query below
    filters: list[str] = []
    fargs: list = []
    empty_reason = None
    fgroups: dict[int, list] = {}
    for op, val, neg, grp in query.filters:
        cond, cargs, hint, empty = _filter_condition(op, val, now, project_resolver, store)
        if hint:
            query.hints.append(hint)
        if grp >= 0:
            fgroups.setdefault(grp, []).append((cond or "0", cargs, empty, hint))
            continue
        if empty and not neg:
            empty_reason = hint or "nothing matches"
        if cond:
            filters.append(("NOT " if neg else "") + cond)
            fargs.extend(cargs)
    for parts in fgroups.values():
        if all(p[2] for p in parts):
            empty_reason = parts[0][3] or "nothing matches"
        filters.append("(" + " OR ".join(p[0] for p in parts) + ")")
        for p in parts:
            fargs.extend(p[1])
    if account:
        filters.append("m.account_id = ?")
        fargs.append(account)

    # free text -> one FTS5 expression (AND of terms, OR groups, NOT)
    last_positive = max((i for i, t in enumerate(query.terms) if not t.negate), default=-1)
    if typing is None:
        typing = (q or "") == (q or "").rstrip()
    positives, negatives, groups = [], [], {}
    code_terms = []
    for i, t in enumerate(query.terms):
        e = _fts_term(t, prefix=(i == last_positive and typing and not t.phrase and len(t.text) >= 2))
        if not t.negate and t.or_group < 0 and _is_code(t.text):
            code_terms.append((t, e))
        if not e:
            continue
        if t.negate:
            negatives.append(e)
        elif t.or_group >= 0:
            groups.setdefault(t.or_group, []).append(e)
        else:
            positives.append(e)
    positives += ["(" + " OR ".join(g) + ")" for g in groups.values()]
    rank_expr = " AND ".join(positives)
    for n in negatives:
        if rank_expr:
            rank_expr = f"({rank_expr}) NOT {n}"
        else:
            filters.append("m.rowid NOT IN (SELECT rowid FROM mail_fts WHERE mail_fts MATCH ?)")
            fargs.append(n)

    if query.terms and not rank_expr and not negatives and not query.filters and empty_reason is None:
        # Only punctuation: nothing a word index can look up. Say so instead
        # of answering with the newest mail as if it matched.
        empty_reason = "There is nothing to search for in that; type a word, a name or an operator."
        query.hints.append(empty_reason)
    base = ["m.removed = 0"] + filters
    rows: list[dict] = []
    if empty_reason is None:
        if rank_expr:
            # Driven by the index: bm25 with column weights, best 500.
            rows = store.query(
                "SELECT m.*, bm25(mail_fts, 3.0, 2.0, 1.0) AS rank FROM mail_fts"
                " JOIN mail_messages m ON m.rowid = mail_fts.rowid"
                f" WHERE mail_fts MATCH ? AND {' AND '.join(base)}"
                f" ORDER BY rank LIMIT {CANDIDATE_LIMIT}", [rank_expr] + fargs)
            if code_terms:
                # Part numbers also match as substrings (trigram side index).
                seen = {r["rowid"] for r in rows}
                conds, cargs = [], []
                for t, e in code_terms:
                    alt = ["m.rowid IN (SELECT rowid FROM mail_tri WHERE mail_tri MATCH ?)"]
                    targs = ["{files codes} : " + _fts_quote(t.text)]
                    if e:
                        alt.append("m.rowid IN (SELECT rowid FROM mail_fts WHERE mail_fts MATCH ?)")
                        targs.append(e)
                    conds.append("(" + " OR ".join(alt) + ")")
                    cargs.extend(targs)
                others = [p for p in positives if p not in {e for _t, e in code_terms}]
                for p_ in others:
                    conds.append("m.rowid IN (SELECT rowid FROM mail_fts WHERE mail_fts MATCH ?)")
                    cargs.append(p_)
                for n in negatives:
                    conds.append("m.rowid NOT IN (SELECT rowid FROM mail_fts WHERE mail_fts MATCH ?)")
                    cargs.append(n)
                extra = store.query(
                    f"SELECT m.*, 0.0 AS rank FROM mail_messages m WHERE {' AND '.join(base + conds)}"
                    f" ORDER BY m.received_ts DESC LIMIT {CANDIDATE_LIMIT}", fargs + cargs)
                rows += [r for r in extra if r["rowid"] not in seen][:max(0, CANDIDATE_LIMIT - len(rows))]
        else:
            rows = store.query(
                f"SELECT m.*, 0.0 AS rank FROM mail_messages m WHERE {' AND '.join(base)}"
                f" ORDER BY m.received_ts DESC LIMIT {CANDIDATE_LIMIT}", fargs)
    ranked = _rerank(store, rows, now, has_text=bool(rank_expr))
    total = len(ranked)
    messages = [_result_row(r) for r in ranked[:limit]]
    acct = account or next((v.lower() for op, v, neg, _g in query.filters if op == "account" and not neg), None)
    people = _people(store, query) if query.has_text and not query.filters else []
    atts = _attachments(store, query, acct) if query.has_text and empty_reason is None else []
    if not messages and not query.hints:
        query.hints.append("Nothing on this computer matches. Search the server to look further.")
    return {
        "query": q, "messages": messages, "total": total, "truncated": total >= CANDIDATE_LIMIT,
        "people": people, "attachments": atts, "hints": query.hints,
        "operators": [{"op": k, "help": v} for k, v in OPERATORS.items()],
        "coverage": coverage if coverage is not None else coverage_for(store, now=now),
        "server_fallback": {"label": f"Search the server for \"{q.strip()}\"" if q.strip() else "Search the server"},
        "took_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _addr_condition(column_tri: str, column_like: str, val: str) -> tuple[str, list]:
    if len(val) >= 3:
        return (f"m.rowid IN (SELECT rowid FROM mail_tri WHERE mail_tri MATCH ?)",
                [f"{column_tri} : {_fts_quote(val)}"])
    return f"({column_like} LIKE ?)", [f"%{val}%"]


def _label_ids(store, name: str) -> list[str]:
    """Gmail label ids whose name matches (each provider files its id -> name
    map in mail_meta as ``labels:<account>``)."""
    import json as _json
    out = []
    if store is None:
        return out
    for r in store.query("SELECT value FROM mail_meta WHERE key LIKE 'labels:%'"):
        try:
            m = _json.loads(r["value"] or "{}")
        except Exception:
            continue
        for lid, lname in m.items():
            if str(lname).lower() == name.lower() or str(lname).lower().endswith("/" + name.lower()):
                out.append(lid)
    return out


def _filter_condition(op, val, now, project_resolver, store=None):
    """-> (sql, args, hint, empty)"""
    v = val.strip()
    lv = v.lower()
    if op == "from":
        c, a = _addr_condition("from_a", "m.from_email || ' ' || m.from_name", v)
        return c, a, None, False
    if op == "to":
        c, a = _addr_condition("to_a", "m.to_addrs", v)
        return c, a, None, False
    if op == "cc":
        c, a = _addr_condition("cc_a", "m.cc_addrs", v)
        return c, a, None, False
    if op == "subject":
        words = re.findall(r"\w+", v)
        if not words:
            return "", [], None, False
        return ("m.rowid IN (SELECT rowid FROM mail_fts WHERE mail_fts MATCH ?)",
                ["subject : " + _fts_quote(" ".join(words))], None, False)
    if op == "has":
        return "m.has_attachments = 1", [], None, False
    if op == "filename":
        if len(v) >= 3:
            return ("m.rowid IN (SELECT rowid FROM mail_tri WHERE mail_tri MATCH ?)",
                    ["files : " + _fts_quote(v)], None, False)
        return ("m.rowid IN (SELECT msg_rowid FROM mail_attachments WHERE name LIKE ?)", [f"%{v}%"], None, False)
    if op == "before":
        return "m.received_ts < ?", [_date_ts(v)], None, False
    if op == "after":
        return "m.received_ts >= ?", [_date_ts(v)], None, False
    if op == "newer_than":
        return "m.received_ts >= ?", [now - _span_seconds(v)], None, False
    if op == "older_than":
        return "m.received_ts < ?", [now - _span_seconds(v)], None, False
    if op == "is":
        if lv == "unread":
            return "m.is_read = 0", [], None, False
        if lv == "read":
            return "m.is_read = 1", [], None, False
        if lv in ("starred", "flagged"):
            return "m.is_starred = 1", [], None, False
        name = LATER.get(lv, lv)
        return "0", [], f"{name} is not in this version yet, so nothing is {lv}.", True
    if op == "in":
        if lv == "anywhere":
            return "", [], None, False
        folder = "junk" if lv == "spam" else lv
        return "m.folder = ?", [folder], None, False
    if op == "label":
        conds = ["m.labels LIKE ?", "m.folder = ?", "m.provider_id LIKE ? ESCAPE '\\'"]
        esc = v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args = [f'%"{v}"%', lv, f"x:{esc}:%"]
        for lid in _label_ids(store, v):
            conds.append("m.labels LIKE ?")
            args.append(f'%"{lid}"%')
        return "(" + " OR ".join(conds) + ")", args, None, False
    if op == "account":
        return "m.account_id = ?", [lv], None, False
    if op in ("project", "status", "stage"):
        if project_resolver is None:
            return "0", [], f"{op}: needs the board; it is not available here.", True
        ids = project_resolver(op, v)
        if ids is None:
            return "0", [], f"No project matches {op}:{v}.", True
        ids = [i for i in ids if i]
        if not ids:
            return "0", [], f"Nothing is filed under {op}:{v} yet.", True
        marks = ",".join("?" * len(ids))
        return f"(m.message_id IN ({marks}) OR m.thread_key IN ({marks}))", ids + ids, None, False
    return "", [], None, False


def _rerank(store, rows: list[dict], now: float, has_text: bool) -> list[dict]:
    if not rows:
        return []
    if not has_text:
        return rows
    senders = {r["from_email"] for r in rows if r.get("from_email")}
    affinity = {}
    if senders:
        marks = ",".join("?" * len(senders))
        for p in store.query(f"SELECT email, sent_count FROM mail_people WHERE email IN ({marks})", list(senders)):
            affinity[p["email"]] = p["sent_count"] or 0
    best = max((-(r.get("rank") or 0.0)) for r in rows) or 1.0
    top_aff = math.log1p(max(affinity.values(), default=0)) or 1.0

    def score(r):
        rel = max(0.0, -(r.get("rank") or 0.0)) / best
        age = max(0.0, (now - (r.get("received_ts") or 0)) / 86400.0)
        decay = math.pow(0.5, age / HALF_LIFE_DAYS)
        aff = math.log1p(affinity.get(r.get("from_email"), 0)) / top_aff
        return (0.65 * rel + 0.35 * decay) * (1.0 + 0.25 * aff)

    for r in rows:
        r["score"] = round(score(r), 6)
    return sorted(rows, key=lambda r: (-r["score"], -(r.get("received_ts") or 0)))


def _result_row(r: dict) -> dict:
    ts = r.get("received_ts")
    return {
        "account": r["account_id"], "ref": r["provider_id"], "message_id": r.get("message_id") or "",
        "subject": r.get("subject") or "", "from": r.get("from_name") or r.get("from_email") or "",
        "from_email": r.get("from_email") or "", "to": r.get("to_addrs") or "",
        "date": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None,
        "snippet": r.get("snippet") or "", "folder": r.get("folder") or "",
        "has_attachments": bool(r.get("has_attachments")), "is_read": bool(r.get("is_read")),
        "starred": bool(r.get("is_starred")), "body_searched": r.get("body_state") == "full",
        "historical": bool(r.get("historical")), "score": r.get("score"),
    }


def _people(store, query: Query, limit: int = 5) -> list[dict]:
    words = [t.text for t in query.terms if not t.negate]
    if not words:
        return []
    like = f"%{' '.join(words)}%"
    rows = store.query("SELECT email, name, sent_count, recv_count FROM mail_people WHERE email LIKE ? OR name LIKE ?"
                       " ORDER BY sent_count DESC, recv_count DESC LIMIT ?", [like, like, limit])
    return [{"email": r["email"], "name": r["name"], "sent": r["sent_count"], "received": r["recv_count"]}
            for r in rows]


def _attachments(store, query: Query, account: str | None = None, limit: int = 10) -> list[dict]:
    expr = " AND ".join(filter(None, (_fts_term(t, False) for t in query.terms if not t.negate)))
    if not expr:
        return []
    extra, args = "", [expr]
    if account:
        extra, args = " AND m.account_id = ?", [expr, account]
    rows = store.query(
        "SELECT a.name, a.content_type, a.size, a.idx, m.provider_id, m.account_id, m.subject, m.received_ts"
        " FROM mail_attachments_fts f JOIN mail_attachments a ON a.rowid = f.rowid"
        " JOIN mail_messages m ON m.rowid = a.msg_rowid"
        f" WHERE mail_attachments_fts MATCH ? AND m.removed = 0{extra} ORDER BY bm25(mail_attachments_fts) LIMIT ?",
        args + [limit])
    return [{"name": r["name"], "content_type": r["content_type"], "size": r["size"], "index": r["idx"],
             "ref": r["provider_id"], "account": r["account_id"], "subject": r["subject"]} for r in rows]


# ── coverage ─────────────────────────────────────────────────────────────────

def coverage_for(store, now: float | None = None, body_days: int | None = None) -> dict:
    """What the local search covered, as numbers and one sentence."""
    from . import reconcile
    now = now or time.time()
    cached = getattr(store, "_coverage_cache", None)
    ver = store.changes() if hasattr(store, "changes") else None
    if cached and ver is not None and cached[0] == ver and cached[1] > time.time() - 30:
        return cached[2]
    row = store.query("SELECT SUM(body_state!='none') AS n, SUM(body_state='full') AS full_n,"
                      " SUM(body_state='headers') AS hdr_n, SUM(body_state='none') AS pending_n,"
                      " MIN(CASE WHEN body_state='full' THEN received_ts END) AS body_since,"
                      " MIN(received_ts) AS oldest FROM mail_messages WHERE removed=0")[0]
    missed = 0
    import json as _json
    for r in store.query("SELECT value FROM mail_meta WHERE key LIKE 'backfill:%'"):
        try:
            missed += int((_json.loads(r["value"] or "{}")).get("missed_total") or 0)
        except Exception:
            continue
    not_local = 0
    unknown = []
    for acct in [a["account_id"] for a in store.accounts()] or _account_ids(store):
        latest = reconcile.latest(store, acct)
        if not latest:
            unknown.append(acct)
        for folder, rec in latest.items():
            if rec.get("not_local") is None:
                unknown.append(f"{acct} {folder}")
            else:
                not_local += int(rec["not_local"])
    since = row["body_since"]
    parts = []
    n = int(row["n"] or 0)
    # not_local: reconcile counts every row in the folder as local; rows that
    # are known but not indexed are not searchable, so they count as missing.
    if not n:
        sentence = "Nothing is on this computer yet."
    else:
        if since:
            when = datetime.fromtimestamp(since).strftime("%b %d, %Y").replace(" 0", " ")
            parts.append(f"Body text searched since {when}")
        if int(row["hdr_n"] or 0):
            parts.append(f"{int(row['hdr_n']):,} older messages matched on headers only")
        if not parts:
            parts.append("Headers searched")
        sentence = "; ".join(parts) + "."
    pending = int(row["pending_n"] or 0)
    if pending:
        sentence += f" {pending:,} known but not indexed yet."
    if not_local:
        sentence += f" {not_local:,} not on this computer."
    if missed:
        sentence += f" {missed:,} could not be read from the server."
    if unknown:
        sentence += " Server totals not checked yet for " + ", ".join(unknown) + "."
    out = {"sentence": sentence, "messages": n, "with_body": int(row["full_n"] or 0),
           "headers_only": int(row["hdr_n"] or 0), "not_indexed": pending, "unreadable": missed,
           "body_since": since, "oldest": row["oldest"], "not_local": not_local, "unchecked": unknown}
    try:
        store._coverage_cache = (ver, time.time(), out)
    except Exception:
        pass
    return out


def _account_ids(store) -> list[str]:
    return [r["account_id"] for r in store.query("SELECT DISTINCT account_id FROM mail_messages")]
