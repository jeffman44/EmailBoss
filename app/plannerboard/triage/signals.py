"""Deterministic signals over the cleaned body: completion phrases, the
approval tag, order / tracking references and the allowlisted alternate
thread key.

Ported from engine/email_processor.py lines 1886-2108 (completion table,
sentence-level approval exclusion), 2186-2241 (triage_signals, minus the
trade-specific "dispatch" tag), 2254-2312 (alt thread key). The completion
table is rewritten trade-neutral: documents, payment, scheduling,
delivery, approval, completion. Every pattern needs at least two tokens,
never a bare "approved" / "signed" / "done" floating in prose, because the
board pre-checks subtask boxes off these and a wrong pre-check gets
rubber-stamped.
"""
from __future__ import annotations

import hashlib
import json
import re

from .match import extract_vendor_refs, sender_domain, is_internal
from .text import strip_signature_block, signature_cut_re, signature_markers_from_cfg

_SIG_SOURCE_MAX = 4000
_SIG_MAX_SIGNALS = 8
_SIG_MAX_PHRASES = 6

_SCHED_WHEN = (
    r'(?:mon|tues?|wednes|wed|thurs?|thu|fri|satur|sat|sun)(?:day)?'
    r'|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?'
    r'|\d{1,2}\s*[/-]\s*\d{1,2}(?:\s*[/-]\s*\d{2,4})?'
    r'|tomorrow|next\s+week|this\s+(?:week|coming\s+week)'
)
_PO_TOKEN = r'(?:P\.?O\.?(?![a-z])|purchase\s+order)'

# (key, canonical phrases emitted, pattern)
COMPLETION_RULES = (
    ("po_issued", ("PO issued",), re.compile(
        r'\b(?:' + _PO_TOKEN + r'\s*(?:#|number|num|no\.?)?\s*[A-Z0-9-]{0,12}\s*'
        r'(?:has\s+been\s+|have\s+been\s+|was\s+|were\s+|is\s+|been\s+)?'
        r'(?:cut|issued|created|generated|approved|attached|in\s+hand)'
        r'|(?:cut|issued|created|generated|attached|attaching|sending|here\s+is)\s+'
        r'(?:you\s+|the\s+|a\s+|an\s+|our\s+|your\s+)?' + _PO_TOKEN + r')', re.IGNORECASE)),

    ("items_received", ("items received",), re.compile(
        r'\b(?:(?:parts?|materials?|equipment|items?|order|package|shipment|documents?)\s+'
        r'(?:(?:are|is|have|has|had|were|was|just)\s+)?(?:been\s+)?(?:arrived|delivered|received|here)'
        r'|(?:received|picked\s+up|got)\s+(?:the\s+|our\s+|your\s+|all\s+(?:the\s+)?)?'
        r'(?:parts?|materials?|equipment|items?|order|package|shipment|documents?))', re.IGNORECASE)),

    ("order_placed", ("order placed",), re.compile(
        r'\b(?:(?:parts?|materials?|equipment|items?)\s+(?:(?:are|is|have|has|been|were|was)\s+)*(?:on\s+)?order(?:ed)?\b'
        r'|(?:placed|submitted|put\s+in)\s+(?:the\s+|an?\s+|our\s+)?order\b'
        r'|order\s+(?:has\s+been\s+|was\s+|is\s+)?placed\b)', re.IGNORECASE)),

    ("scheduled", ("scheduled",), re.compile(
        r'\b(?:(?:we|it|they|the\s+(?:job|work|crew|meeting|call|visit|appointment|closing|hearing|interview))\s+'
        r'(?:is|are|has\s+been|have\s+been|will\s+be)\s+scheduled'
        r'|(?:job|work|meeting|call|visit|appointment|closing|hearing|interview)\s+(?:is|has\s+been)\s+scheduled'
        r'|(?:i|we)\s+(?:have\s+)?scheduled\s+(?:the|this|it|you|a|an))', re.IGNORECASE)),

    ("permit_approved", ("permit approved",), re.compile(
        r'\b(?:permits?\s+(?:(?:has|have|was|were|is|are)\s+)?(?:been\s+)?(?:approved|issued|granted)'
        r'|(?:approved|issued|granted)\s+(?:the\s+|a\s+|our\s+)?permits?)', re.IGNORECASE)),

    ("signed", ("signed",), re.compile(
        r'\b(?:signed\s+(?:and\s+)?(?:attached|copy|proposal|contract|agreement|quote|estimate|'
        r'documents?|paperwork|engagement\s+letter|retainer|offer|lease|' + _PO_TOKEN + r')'
        r'|attached\s+(?:is\s+|are\s+|please\s+find\s+)?(?:the\s+|a\s+|our\s+|your\s+)?signed'
        r'|(?:we|i|they|owner|ownership|customer|client|tenant|buyer|seller)\s+(?:have\s+|has\s+)?signed'
        r'|(?:fully\s+)?executed\s+(?:contract|agreement|copy|proposal|lease)'
        r'|signature[sd]?\s+(?:page\s+)?(?:attached|complete[d]?))', re.IGNORECASE)),

    ("approved", ("approved",), re.compile(
        r'\b(?:(?:is|are|has\s+been|have\s+been|was|were|we\s+are|you\s+are|it\s+is)\s+approved'
        r'|approved\s+(?:to\s+proceed|the\s+(?:quote|proposal|estimate|work|job|bid|budget|plan|request)'
        r'|for\s+(?:the\s+)?(?:work|quote|proposal|budget))'
        r'|(?:we|i|owner|ownership|board|client|customer|corporate|management|committee)\s+(?:have\s+|has\s+)?approved'
        r'|approval\s+(?:to\s+proceed|has\s+been\s+(?:given|granted)|granted)'
        r'|approved[,!.]?\s*(?:go\s+ahead|please\s+proceed|proceed))', re.IGNORECASE)),

    ("go_ahead", ("go ahead", "proceed"), re.compile(
        r'\b(?:you\s+(?:are|have)\s+(?:the\s+)?(?:go\s*-?\s*ahead|green\s*light)'
        r'|(?:we|i)\s+(?:are\s+)?giv(?:e|ing)\s+(?:you\s+)?(?:the\s+)?go\s*-?\s*ahead'
        r'|go\s+ahead\s+(?:and\s+)?(?:with|schedule|proceed|order|start|move|book|file|get\s+(?:it|this))'
        r'|please\s+proceed'
        r'|proceed\s+with\s+(?:the\s+)?(?:work|job|quote|proposal|scope|order|plan|filing|closing)'
        r'|(?:we|i)\s+would\s+like\s+to\s+(?:move\s+forward|proceed)'
        r'|move\s+forward\s+with\s+(?:the\s+)?(?:work|job|proposal|quote|plan|offer|purchase))', re.IGNORECASE)),

    ("payment_received", ("payment received",), re.compile(
        r'\b(?:payment\s+(?:has\s+been\s+|was\s+)?(?:received|processed|sent|made|issued)'
        r'|(?:check|cheque|wire|deposit|invoice)\s+(?:has\s+been\s+|was\s+|is\s+)?(?:received|sent|paid|processed|cleared)'
        r'|paid\s+in\s+full)', re.IGNORECASE)),

    ("work_complete", ("work complete",), re.compile(
        r'\b(?:(?:work|job|project|scope|task|report|review|inspection|install(?:ation)?)\s+'
        r'(?:is|was|are|were|has\s+been|have\s+been)\s+(?:been\s+)?(?:complete[d]?|finished|done|wrapped\s+up)'
        r'|(?:completed|finished|wrapped\s+up)\s+(?:the\s+|our\s+|all\s+(?:the\s+)?)?'
        r'(?:work|job|project|task|report|review|inspection|install(?:ation)?))', re.IGNORECASE)),

    ("documents_sent", ("documents sent",), re.compile(
        r'\b(?:(?:attached|attaching|sending|here\s+(?:is|are)|please\s+find)\s+(?:is\s+|are\s+)?'
        r'(?:the\s+|a\s+|our\s+|your\s+)?(?:revised\s+|updated\s+|final\s+)?'
        r'(?:proposal|quote|estimate|contract|agreement|invoice|report|drawings?|plans?|documents?))',
        re.IGNORECASE)),
)

_SCHED_DATED_RE = re.compile(
    r'\b(?:scheduled|schedule|set|booked|coming\s+out|on\s+site|meeting)\s+(?:for|on)\s+(' + _SCHED_WHEN + r')\b',
    re.IGNORECASE)

APPROVAL_SIGNAL_KEYS = frozenset({"approved", "go_ahead", "signed", "po_issued"})

_APPROVAL_EXCLUDE_RE = re.compile(
    r'\b(?:permit|licen[cs]e|inspection|w-?9|coi|certificate|insurance|'
    r'credit\s+app\w*|vendor\s+(?:packet|form|portal|application)|background\s+check)\b',
    re.IGNORECASE)


def _collapse(text, limit=80):
    return re.sub(r"\s+", " ", (text or "")).strip()[:limit]


def _sentence_around(text, start, end):
    left = max((text.rfind(c, 0, start) for c in ".!?\n"), default=-1)
    right = min((p for p in (text.find(c, end) for c in ".!?\n") if p != -1), default=len(text))
    return text[left + 1:right + 1]


def _approval_context_ok(text, m):
    sentence = _sentence_around(text, m.start(), m.end())
    if sentence.rstrip().endswith("?"):
        return False
    return not _APPROVAL_EXCLUDE_RE.search(sentence)


def signal_source_text(email_obj: dict, cfg: dict | None = None) -> str:
    """Subject + the SAME cleaned body the matcher uses, signature stripped."""
    if not isinstance(email_obj, dict):
        return ""
    body = email_obj.get("body") or email_obj.get("body_preview") or ""
    try:
        body = strip_signature_block(body, cut_re=signature_cut_re(signature_markers_from_cfg(cfg)))
    except Exception:
        body = body or ""
    return (str(email_obj.get("subject") or "") + "\n" + str(body))[:_SIG_SOURCE_MAX]


def extract_completion_signals(email_obj: dict, cfg: dict | None = None, id_patterns=None) -> list[str]:
    """Canonical completion phrases. Pre-check fuel ONLY; nothing here ever
    completes a subtask by itself."""
    text = signal_source_text(email_obj, cfg)
    if not text.strip():
        return []
    phrases: list[str] = []
    fired = set()
    for key, canon, pattern in COMPLETION_RULES:
        try:
            if not pattern.search(text):
                continue
        except Exception:
            continue
        fired.add(key)
        for c in canon:
            if c.lower() not in {p.lower() for p in phrases}:
                phrases.append(c)
    m = _SCHED_DATED_RE.search(text)
    if m:
        when = _collapse(m.group(1), 24)
        if when:
            dated = "scheduled for " + when
            if dated.lower() not in {p.lower() for p in phrases}:
                phrases.insert(0, dated)
            if "scheduled" not in {p.lower() for p in phrases}:
                phrases.append("scheduled")
    if "po_issued" in fired and id_patterns:
        from .match import extract_ids
        for key, val, _u in extract_ids(text, id_patterns)[:2]:
            tok = f"{key.upper()} {val}"
            if tok.lower() not in {p.lower() for p in phrases}:
                phrases.append(tok)
    return phrases[:_SIG_MAX_SIGNALS]


def extract_triage_signals(email_obj: dict, cfg: dict | None = None) -> dict:
    """``{approval: bool, phrases: [...], refs: [...]}``.

    approval: a go-ahead from an EXTERNAL sender on an INBOUND email, with the
    sentence-level exclusions (a question about approval, a permit the city
    approved, an insurance certificate) applied. refs: order / tracking keys.
    """
    out = {"approval": False, "phrases": [], "refs": []}
    if not isinstance(email_obj, dict):
        return out
    text = signal_source_text(email_obj, cfg)
    if not text.strip():
        return out
    internal = ((cfg or {}).get("user") or {}).get("internal_domains") or []
    sender = (email_obj.get("from_email") or "").lower().strip()
    inbound = (email_obj.get("direction") or "inbound") == "inbound"
    external = bool(sender) and not is_internal(sender, internal)
    phrases: list[str] = []

    def _add(span):
        span = _collapse(span)
        if span and span.lower() not in {p.lower() for p in phrases}:
            phrases.append(span)

    if inbound and external:
        for key, _canon, pattern in COMPLETION_RULES:
            if key not in APPROVAL_SIGNAL_KEYS:
                continue
            try:
                matches = list(pattern.finditer(text))
            except Exception:
                continue
            for m in matches:
                if not _approval_context_ok(text, m):
                    continue
                out["approval"] = True
                _add(m.group(0))
                break
    out["phrases"] = phrases[:_SIG_MAX_PHRASES]
    out["refs"] = extract_vendor_refs(
        (email_obj.get("subject") or "") + " " + (email_obj.get("body") or ""),
        sender_domain(sender))
    return out


# ── alternate thread key for allowlisted senders ────────────────────────────

_SUBJ_PREFIX_RE = re.compile(r'^\s*(?:re|fw|fwd|aw|tr|rv)\s*(?:\[\d+\])?\s*:\s*', re.IGNORECASE)
_SUBJ_TAG_RE = re.compile(r'\[[^\]]{0,40}\]')
_EMAIL_IN_TEXT_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def normalize_thread_subject(subject) -> str:
    s = str(subject or "")
    s = _SUBJ_TAG_RE.sub(" ", s)
    for _ in range(6):
        stripped = _SUBJ_PREFIX_RE.sub("", s)
        if stripped == s:
            break
        s = stripped
    return re.sub(r"\s+", " ", s).strip().lower()


def participant_emails(email_obj: dict) -> set[str]:
    found = set()
    for field in ("from_email", "to", "cc", "bcc"):
        val = email_obj.get(field)
        if not val:
            continue
        blob = val if isinstance(val, str) else json.dumps(val, default=str)
        for addr in _EMAIL_IN_TEXT_RE.findall(blob):
            found.add(addr.lower().strip(" .,;<>"))
    return found


def _in_domains(addr: str, domains) -> bool:
    if "@" not in addr:
        return False
    dom = addr.rsplit("@", 1)[-1]
    return any(dom == d or dom.endswith("." + d) for d in domains)


def alt_thread_domains(cfg: dict | None, profession: dict | None) -> list[str]:
    out = []
    for src in (((cfg or {}).get("mail") or {}).get("alt_thread_domains"),
                ((profession or {}).get("triage") or {}).get("alt_thread_domains")):
        for d in src or []:
            d = str(d).lower().lstrip("@").strip()
            if d and d not in out:
                out.append(d)
    return out


def compute_thread_key_alt(email_obj: dict, allow_domains, internal_domains=()) -> str | None:
    """sha1(normalized subject + '|' + sorted external participants), or None
    for every sender outside the allowlist. That None is the whole safety
    property: this is not a return of subject matching."""
    if not isinstance(email_obj, dict) or not allow_domains:
        return None
    addrs = participant_emails(email_obj)
    if not any(_in_domains(a, allow_domains) for a in addrs):
        return None
    subj = normalize_thread_subject(email_obj.get("subject", ""))
    external = sorted(a for a in addrs if a and not is_internal(a, internal_domains))
    raw = subj + "|" + ",".join(external)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def enrich_signals(email_obj: dict, cfg: dict | None, profession: dict | None, id_patterns=None) -> dict:
    """Stamp completion_signals / triage_signals / thread_key_alt on the
    email object. Per-field containment: one bad body never costs the cycle."""
    try:
        email_obj["completion_signals"] = extract_completion_signals(email_obj, cfg, id_patterns)
    except Exception:
        email_obj["completion_signals"] = []
    try:
        email_obj["triage_signals"] = extract_triage_signals(email_obj, cfg)
    except Exception:
        email_obj["triage_signals"] = {"approval": False, "phrases": [], "refs": []}
    try:
        email_obj["thread_key_alt"] = compute_thread_key_alt(
            email_obj, alt_thread_domains(cfg, profession),
            ((cfg or {}).get("user") or {}).get("internal_domains") or ())
    except Exception:
        email_obj["thread_key_alt"] = None
    return email_obj
