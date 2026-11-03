"""Email -> project matching.

Ported from engine/email_processor.py:
  * scoring constants + recency decay    lines 292-311, 1671-1682
  * build_subject_keywords               lines 1552-1578
  * build_activity_index                 lines 1643-1668
  * extract_vendor_refs (ORD:/TRK: keys) lines 194-290
  * match_email_to_projects              lines 2362-2553

Generalized: the exact-reference short circuit uses the profession's
``id_patterns`` (each ``{key, label, regex, unique_match}``) against the
store's ``id_index()`` instead of a hard-coded 6-digit work-order rule; the
"internal" test uses ``cfg.user.internal_domains``; the location-word rarity
step reads each project's location field (``location`` / ``site`` /
``key_details.location``) instead of a separate site lookup.

Returns ``[{project_id, confidence, match_type}]``, strongest first.
"""
from __future__ import annotations

import re
from datetime import datetime, date

from .text import strip_signature_block, signature_cut_re

TOKEN_WEIGHT = 1.0
LOCATION_WEIGHT = 3.0
MATCH_FLOOR = 0.90
STRONG_MATCH = 2.00
MARGIN_RATIO = 1.50

RECENCY_FULL_DAYS = 30
RECENCY_HALF_LIFE = 60.0
RECENCY_MIN = 0.30
CONTACT_STALE_DAYS = 90

STOP_TOKENS = {"the", "and", "for", "with", "from", "this", "that", "are", "was",
               "has", "have", "been", "will", "not", "but", "all", "can", "our",
               "per", "new", "get", "set", "put", "re:", "fw:", "fwd:"}

# ── vendor order / shipment tracking references (namespaced keys) ──────────
ORDER_REF_RE = re.compile(
    r'(?:order|sales\s*order|invoice|inv|confirmation|ref(?:erence)?|quote|ticket|case)'
    r'\s*(?:number|no\.?|num|id)?'
    r'\s*(?:is|was)?'
    r'\s*[#:]+\s*'
    r'((?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{4,20})',
    re.IGNORECASE)
TRACKING_RE = re.compile(
    r'\b(1Z[0-9A-Z]{16}|96\d{20}|9[24]\d{20}|9\d{21})\b')
TRACKING_CTX_RE = re.compile(
    r'(?:tracking|track|shipment|airbill|air\s*bill|waybill|fedex|ups|usps|dhl)'
    r'[^\n\r]{0,24}?\b(\d{12}|\d{15})\b', re.IGNORECASE)


def sender_domain(addr: str) -> str:
    addr = (addr or "").lower().strip()
    return addr.split("@")[-1] if "@" in addr else ""


def extract_vendor_refs(text: str, sender_dom: str = "") -> list[str]:
    """``ORD:<domain>:<number>`` and ``TRK:<number>`` keys. Sorted, never raises."""
    refs = set()
    if not text:
        return []
    try:
        dom = (sender_dom or "").lower().strip()
        for num in ORDER_REF_RE.findall(text):
            num = num.upper().strip().rstrip("-")
            if len(num) < 5 or not re.search(r"\d", num):
                continue
            refs.add(f"ORD:{dom}:{num}" if dom else f"ORD:{num}")
        for num in TRACKING_RE.findall(text):
            refs.add("TRK:" + num.upper().strip())
        for num in TRACKING_CTX_RE.findall(text):
            refs.add("TRK:" + num.upper().strip())
    except (TypeError, ValueError):
        return []
    return sorted(refs)


# ── profession id patterns ─────────────────────────────────────────────────

def compile_id_patterns(profession: dict | None) -> list[dict]:
    out = []
    for p in (profession or {}).get("id_patterns") or []:
        rx = p.get("regex")
        if not rx:
            continue
        try:
            out.append({"key": p.get("key") or "ref", "label": p.get("label") or p.get("key") or "Ref",
                        "re": re.compile(rx, re.IGNORECASE),
                        "unique_match": bool(p.get("unique_match", True))})
        except re.error:
            continue
    return out


def normalize_id(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper().strip()


def extract_ids(text: str, patterns: list[dict]) -> list[tuple[str, str, bool]]:
    """[(pattern_key, normalized_value, unique_match)] in order of appearance."""
    out, seen = [], set()
    for p in patterns:
        for m in p["re"].finditer(text or ""):
            val = normalize_id(m.group(1) if m.groups() else m.group(0))
            if val and (p["key"], val) not in seen:
                seen.add((p["key"], val))
                out.append((p["key"], val, p["unique_match"]))
    return out


# ── indexes derived from the project list ───────────────────────────────────

def is_internal(addr: str, internal_domains) -> bool:
    a = (addr or "").lower().strip()
    if "@" not in a:
        return False
    dom = a.rsplit("@", 1)[-1]
    for d in internal_domains or ():
        d = str(d).lower().lstrip("@").strip()
        if d and (dom == d or dom.endswith("." + d)):
            return True
    return False


def _kd(p: dict) -> dict:
    kd = p.get("key_details") or {}
    if isinstance(kd, str):
        try:
            import json
            kd = json.loads(kd)
        except Exception:
            kd = {}
    return kd if isinstance(kd, dict) else {}


def project_location(p: dict) -> str:
    return str(p.get("location") or p.get("site") or _kd(p).get("location") or "").strip()


def build_keyword_index(projects: list[dict]) -> dict[str, list[str]]:
    """token -> [pid] over name + location (lightweight)."""
    kw: dict[str, set] = {}
    for p in projects:
        pid = p.get("id")
        if not pid:
            continue
        text = " ".join(filter(None, [p.get("name", ""), project_location(p)])).lower()
        for tok in text.split():
            tok = tok.strip(".,;:!?()[]{}\"'")
            if len(tok) >= 3 and tok not in STOP_TOKENS:
                kw.setdefault(tok, set()).add(pid)
    return {k: sorted(v) for k, v in kw.items()}


def build_location_index(projects: list[dict]) -> dict[str, list[str]]:
    idx: dict[str, list[str]] = {}
    for p in projects:
        loc = project_location(p)
        if loc and p.get("id"):
            idx.setdefault(loc, [])
            if p["id"] not in idx[loc]:
                idx[loc].append(p["id"])
    return idx


_ACTIVITY_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def build_activity_index(projects: list[dict], today: date | None = None) -> dict[str, int | None]:
    today = today or datetime.now().date()
    idx = {}
    for p in projects:
        pid = p.get("id")
        if not pid:
            continue
        m = _ACTIVITY_DATE_RE.match(str(p.get("last_activity") or p.get("updated_at") or ""))
        if not m:
            idx[pid] = None
            continue
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            idx[pid] = max((today - d).days, 0)
        except ValueError:
            idx[pid] = None
    return idx


def recency_factor(days: int | None) -> float:
    if days is None:
        return RECENCY_MIN
    if days <= RECENCY_FULL_DAYS:
        return 1.0
    return max(RECENCY_MIN, 0.5 ** ((days - RECENCY_FULL_DAYS) / RECENCY_HALF_LIFE))


def build_contact_index(projects: list[dict]) -> dict[str, list[str]]:
    """email_lower / name_lower -> [pid] from each project's contacts[]."""
    idx: dict[str, list[str]] = {}
    for p in projects:
        pid = p.get("id")
        for c in p.get("contacts") or []:
            if isinstance(c, str):
                keys = [c.lower().strip()]
            else:
                keys = [str(c.get("email") or "").lower().strip(), str(c.get("name") or "").lower().strip()]
            for k in keys:
                if k and pid:
                    idx.setdefault(k, [])
                    if pid not in idx[k]:
                        idx[k].append(pid)
    return idx


# ── the matcher ─────────────────────────────────────────────────────────────

class MatchContext:
    """Everything the matcher needs, built once per cycle."""

    def __init__(self, projects: list[dict], profession: dict | None, cfg: dict | None,
                 contact_index=None, id_index=None, keyword_index=None, today: date | None = None):
        self.projects = projects
        self.active_ids = {p.get("id") for p in projects if p.get("id")}
        self.id_patterns = compile_id_patterns(profession)
        self.internal_domains = list(((cfg or {}).get("user") or {}).get("internal_domains") or [])
        self.contact_index = {k.lower(): v for k, v in (contact_index or build_contact_index(projects)).items()}
        self.id_index = {normalize_id(k) if not str(k).startswith(("ORD:", "TRK:")) else str(k): v
                         for k, v in (id_index or {}).items()}
        self.keyword_index = keyword_index or build_keyword_index(projects)
        self.location_index = build_location_index(projects)
        self.activity_index = build_activity_index(projects, today)
        user = (cfg or {}).get("user") or {}
        self.user_email = (user.get("email") or "").lower().strip()
        from .text import signature_markers_from_cfg
        self.sig_re = signature_cut_re(signature_markers_from_cfg(cfg))

    def is_internal(self, addr: str) -> bool:
        return is_internal(addr, self.internal_domains)


def match_email_to_projects(email_obj: dict, ctx: MatchContext) -> list[dict]:
    """Rank candidate projects for one email. Returns list of dicts
    ``{project_id, confidence, match_type}``; empty when nothing clears the
    floor. Exact references (profession ids, vendor refs) short-circuit and
    are never decayed."""
    contact_matches: list[tuple[str, str, str]] = []
    keyword_scores: dict[str, float] = {}

    sender_email = (email_obj.get("from_email") or "").lower().strip()
    sender_name = (email_obj.get("from_name") or "").lower().strip()
    to_recipients = (email_obj.get("to") or "").lower().strip()
    cc_recipients = (email_obj.get("cc") or "").lower().strip()
    subject = (email_obj.get("subject") or "").lower()
    body_text = (email_obj.get("body") or "").lower()
    direction = email_obj.get("direction", "inbound")

    # ── Step 0: exact reference match (highest priority) ──
    exact: list[tuple[str, str, str]] = []
    if ctx.id_index:
        head = (email_obj.get("subject") or "") + " " + (email_obj.get("body") or "")[:300]
        found: list[tuple[str, str]] = []           # (key_for_index, match_type)
        for key, val, _uniq in extract_ids(head, ctx.id_patterns):
            found.append((val, f"id:{key}:{val}"))
        full = (email_obj.get("subject") or "") + " " + (email_obj.get("body") or "")
        for ref in extract_vendor_refs(full, sender_domain(sender_email)):
            found.append((ref, f"ref:{ref}"))
        for att in email_obj.get("attachment_data") or []:
            for num in att.get("id_refs") or []:
                found.append((normalize_id(num), f"id:attachment:{normalize_id(num)}"))
            for ref in att.get("vendor_refs") or []:
                found.append((ref, f"ref:{ref}"))
        for key, mtype in found:
            for pid in ctx.id_index.get(key, []):
                if pid in ctx.active_ids and not any(m[0] == pid for m in exact):
                    exact.append((pid, "high", mtype))
        if exact:
            return [{"project_id": p, "confidence": c, "match_type": t} for p, c, t in exact]

    # ── self-note detection ──
    if ctx.user_email and sender_email == ctx.user_email and ctx.user_email in to_recipients \
            and to_recipients.replace(ctx.user_email, "").strip("; ,") == "":
        email_obj["is_self_note"] = True

    # ── Step 1: contact match (internal senders contribute nothing) ──
    if direction == "inbound":
        if sender_email in ctx.contact_index and not ctx.is_internal(sender_email):
            for pid in ctx.contact_index[sender_email]:
                if pid in ctx.active_ids:
                    contact_matches.append((pid, "high", "from_email"))
        if sender_name and sender_name in ctx.contact_index and not ctx.is_internal(sender_email):
            for pid in ctx.contact_index[sender_name]:
                if pid in ctx.active_ids and not any(m[0] == pid for m in contact_matches):
                    contact_matches.append((pid, "low", "from_name"))
    else:
        for field in (to_recipients, cc_recipients):
            for recip in [e.strip() for e in field.replace(",", ";").split(";") if e.strip()]:
                if recip in ctx.contact_index and not ctx.is_internal(recip):
                    for pid in ctx.contact_index[recip]:
                        if pid in ctx.active_ids and not any(m[0] == pid for m in contact_matches):
                            contact_matches.append((pid, "high", "to_email"))

    # ── Step 2: subject + body keywords weighted by rarity (1/df) ──
    body_head = strip_signature_block(body_text, cut_re=ctx.sig_re)[:400]
    search_text = subject + " " + body_head
    for token in set(search_text.split()):
        token = token.strip(".,;:!?()[]{}\"'")
        pids = ctx.keyword_index.get(token)
        if pids:
            weight = TOKEN_WEIGHT / max(len(pids), 1)
            for pid in pids:
                if pid in ctx.active_ids:
                    keyword_scores[pid] = keyword_scores.get(pid, 0.0) + weight

    # ── Step 2b: location words weighted by how many locations share them ──
    if ctx.location_index:
        word_df: dict[str, int] = {}
        for loc in ctx.location_index:
            for w in set(loc.lower().replace("_", " ").split()):
                if len(w) >= 4:
                    word_df[w] = word_df.get(w, 0) + 1
        for loc, pids in ctx.location_index.items():
            words = [w for w in set(loc.lower().replace("_", " ").split()) if len(w) >= 4]
            strength = sum(1.0 / word_df.get(w, 1) for w in words if w in subject or w in body_head)
            if strength > 0:
                weight = LOCATION_WEIGHT * strength / max(len(pids), 1)
                for pid in pids:
                    if pid in ctx.active_ids:
                        keyword_scores[pid] = keyword_scores.get(pid, 0.0) + weight

    # ── Step 2c: recency decay (fuzzy only) + stale-contact demotion ──
    for pid in list(keyword_scores):
        keyword_scores[pid] *= recency_factor(ctx.activity_index.get(pid))
    if contact_matches:
        contact_matches.sort(key=lambda m: ctx.activity_index.get(m[0])
                             if ctx.activity_index.get(m[0]) is not None else 10 ** 6)
        demoted = []
        for pid, conf, mtype in contact_matches:
            days = ctx.activity_index.get(pid)
            if conf == "high" and (days is None or days > CONTACT_STALE_DAYS):
                conf = "medium"
                mtype = f"{mtype},stale{days if days is not None else '?'}d"
            demoted.append((pid, conf, mtype))
        contact_matches = demoted

    # ── Step 3: combine, rank, GATE ──
    matches = list(contact_matches)
    matched = {m[0] for m in matches}
    ranked = sorted(((pid, sc) for pid, sc in keyword_scores.items() if pid not in matched),
                    key=lambda x: -x[1])
    if ranked:
        top_pid, top_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        clears_floor = top_score >= MATCH_FLOOR
        clears_margin = (runner_up <= 0) or (top_score >= runner_up * MARGIN_RATIO)
        if clears_floor and clears_margin:
            conf = "high" if top_score >= STRONG_MATCH else "medium"
            matches.append((top_pid, conf, f"keywords({top_score:.2f})"))
        elif clears_floor:
            for pid, sc in ranked[:3]:
                matches.append((pid, "low", f"ambiguous({sc:.2f})"))

    return [{"project_id": p, "confidence": c, "match_type": t} for p, c, t in matches]


def unique_exact_match(matches: list[dict], ctx: MatchContext) -> str | None:
    """The project id when the email resolved to exactly ONE project through
    an exact reference whose pattern allows auto-commit; else None."""
    if len(matches) != 1:
        return None
    m = matches[0]
    mt = m.get("match_type") or ""
    if m.get("confidence") != "high":
        return None
    if mt.startswith("ref:"):
        return m["project_id"]
    if mt.startswith("id:"):
        key = mt.split(":", 2)[1]
        if key == "attachment":
            return m["project_id"]
        for p in ctx.id_patterns:
            if p["key"] == key:
                return m["project_id"] if p["unique_match"] else None
    return None
