"""Output guards ported from the reference install's v2/ai.py.

These are checks, not requests. The prompts forbid fabrication; this module
verifies it. Ported ranges (v2/ai.py):
  _parse_json .................. 309-336
  _clean_body + markers ........ 64-137   (signature markers now built from profile vars)
  _strip_noise_lines / residual  194-272  (install-specific lines removed, generic kept)
  _GROUND_* / _ground_bullets .. 673-776  (allowlist now injected from profession vocab)
  _META_BULLET_RE .............. 692-696
  _GLOSS_RE / _strip_fake_glosses 700, 718-726
  _CONTACT_* filters ........... 708-715, 856-863
  bullet normalization ......... 825-839
  verbatim / thin-body guards .. 796-805
"""
from __future__ import annotations

import json
import re
from typing import Iterable

# ---------------------------------------------------------------------------
# JSON extraction (ai.py:309-336)
# ---------------------------------------------------------------------------

def parse_json(text):
    """Extract JSON from a model response, tolerating markdown fences and prose."""
    if not text:
        return None
    if not isinstance(text, str):
        return text
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE)
    text = text.strip()
    # Fast path: the whole thing is JSON (scalars included).
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find('{')
    if start == -1:
        start = text.find('[')
        if start == -1:
            return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in ('{', '['):
            depth += 1
        elif ch in ('}', ']'):
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


_parse_json = parse_json  # ported name


# ---------------------------------------------------------------------------
# Body preprocessing (ai.py:64-137)
# ---------------------------------------------------------------------------

_GENERIC_SIG_MARKERS = [
    r'(?m)^[\-_]{2,}\s*$',                              # "-- " or "___"
    r'(?mi)^(?:Cell|Mobile|Office|Direct|Tel|Phone)\s*:\s*[\d(+]',  # phone line
    r'(?mi)^Sent from my\s',                            # Mobile sigs
    r'(?mi)^Get Outlook for\s',                         # Outlook mobile
    r'(?mi)^Sent from (?:Outlook|Mail|Yahoo|Gmail)',    # Generic mobile sigs
]

_QUOTE_MARKERS = [
    r'(?m)^From:\s+.+?\s+Sent:\s',                      # Outlook "From: X Sent:"
    r'(?m)^On\s+.+?\s+wrote:\s*$',                      # Gmail "On [date] X wrote:"
    r'(?m)^-{3,}\s*Original Message\s*-{3,}',           # "--- Original Message ---"
    r'(?m)^_{3,}\s*$',                                  # "___" separator line
    r'(?m)^>+\s',                                       # > quoted lines
]
_quote_patterns = [re.compile(p) for p in _QUOTE_MARKERS]


def signature_markers(profile: dict | None) -> list[re.Pattern]:
    """Signature start markers: generic ones plus the user's own name, role
    and company from the profile vars (replaces the hard-coded install lines
    at ai.py:66-70)."""
    pats = list(_GENERIC_SIG_MARKERS)
    profile = profile or {}
    name = (profile.get("user_name") or "").strip()
    role = (profile.get("role") or "").strip()
    company = (profile.get("company") or "").strip()
    if name:
        pats.append(r'(?mi)^' + re.escape(name) + r'\s*$')
        if role:
            pats.append(r'(?mi)^' + re.escape(name) + r'[\s\-|,]*' + re.escape(role))
    if role:
        pats.append(r'(?mi)^[A-Za-z]+ [A-Za-z]+[\s\-|,]*' + re.escape(role) + r'\s*$')
    if company:
        pats.append(r'(?mi)^' + re.escape(company) + r'\s*$')
    return [re.compile(p) for p in pats]


def clean_body(text: str, max_chars: int = 500, profile: dict | None = None) -> str:
    """Strip signatures, quoted replies and boilerplate, then truncate.

    Order matters: signatures often appear BEFORE quoted replies, so sigs are
    cut first. A marker in the first 5 chars is ignored (probably not a sig).
    """
    if not text:
        return ""
    body = text
    earliest_sig = len(body)
    for pat in signature_markers(profile):
        m = pat.search(body)
        if m and m.start() < earliest_sig and m.start() >= 5:
            earliest_sig = m.start()
    if earliest_sig < len(body):
        body = body[:earliest_sig]

    earliest_quote = len(body)
    for pat in _quote_patterns:
        m = pat.search(body)
        if m and m.start() < earliest_quote and m.start() >= 5:
            earliest_quote = m.start()
    if earliest_quote < len(body):
        body = body[:earliest_quote]

    body = re.sub(r'\n{3,}', '\n\n', body)
    body = re.sub(r'[ \t]+', ' ', body)
    body = body.strip()
    return body[:max_chars]


_clean_body = clean_body


# ---------------------------------------------------------------------------
# Noise lines: signature / contact / boilerplate (ai.py:200-259)
# ---------------------------------------------------------------------------

_NOISE_LINE_PATS = [re.compile(p) for p in (
    r'(?im)^\s*(?:Director|Manager|President|Estimator|Project Manager|Service Manager|'
    r'Superintendent|Account Executive|Partner|Associate|Coordinator|Owner|Principal)\b.*$',
    r'(?im)^\s*(?:Cell|Office|Mobile|Direct|Tel|Telephone|Phone|Fax|Desk|Main)\s*:?\s*[\d(+].*$',
    r'(?im)^.*\(?\d{3}\)?[-.\s]\s*\d{3}[-.\s]\d{4}.*$',      # any line containing a phone #
    r'(?im)^\s*\S+@\S+\.\w{2,}\s*$',                           # bare email-address line
    r'(?im)^.*\b[A-Z]{2}\b[ ,]+\d{5}(?:-\d{4})?\s*$',         # "... ST 12345" city/state/zip
    r'(?im)^\s*\d{3,6}\s+[NSEW]?\.?\s*\w+.*\b(?:St|Ave|Way|Rd|Blvd|Dr|Ln|Ct|Suite|Ste)\b.*$',
    r'(?im)^\s*(?:Thanks|Thank you|Regards|Best(?: regards)?|Kind regards|Sincerely|Cheers|'
    r'Respectfully)[,!.]?\s*$',
    r'(?im)^\s*Sent from .*$',
    r'(?im)^\s*Get Outlook for .*$',
)]

# External-sender signature furniture (ai.py:223-237).
_EXT_SIG_PATS = [re.compile(p) for p in (
    r'(?im)^.*\|\s*(?:[\w. ]{0,20}\b)?(?:manager|director|president|estimator|'
    r'supervisor|coordinator|technician|engineer|owner|principal|executive|'
    r'foreman|superintendent|account|partner|associate|agent|broker)\b.*$',
    r'(?im)^.*\|.*(?:\(?\d{3}\)?[-.\s]?\d{3}[-.\s]\d{4}|\S+@\S+\.\w{2,}).*$',
    r'(?im)^\s*proudly serving\b.*$',
    r'(?im)^\s*(?:https?://)?www\.\S+\s*$',
    r'(?m)^\s*(?:[A-Z][\w&.\'-]*\s+){1,4}(?:Inc|LLC|L\.L\.C|Corp|Co|Ltd|PLLC|LLP|'
    r'Contracting|Contractors|Companies|Group|Partners)\.?,?\s*$',
)]

# The any-line-with-a-phone pattern is safe for MEASURING but too aggressive
# for STRIPPING ("call Kelly at 303-555-1212 to schedule" would vanish).
_PHONE_ANY_SRC = r'(?im)^.*\(?\d{3}\)?[-.\s]\s*\d{3}[-.\s]\d{4}.*$'
_STRIP_LINE_PATS = [p for p in _NOISE_LINE_PATS if p.pattern != _PHONE_ANY_SRC] + _EXT_SIG_PATS


def _profile_line_pats(profile: dict | None) -> list[re.Pattern]:
    pats = []
    for key in ("user_name", "company"):
        val = ((profile or {}).get(key) or "").strip()
        if val:
            pats.append(re.compile(r'(?im)^\s*' + re.escape(val) + r'\b.*$'))
    return pats


def strip_noise_lines(text: str, profile: dict | None = None) -> str:
    """Remove signature/contact/boilerplate LINES before text goes to the model."""
    if not text:
        return ""
    pats = _STRIP_LINE_PATS + _profile_line_pats(profile)
    kept = [ln for ln in text.split('\n') if not any(p.search(ln) for p in pats)]
    out = '\n'.join(kept)
    out = re.sub(r'\n{3,}', '\n\n', out)
    return out.strip()


def residual_content(text: str, profile: dict | None = None) -> str:
    """What is left after stripping noise: a proxy for 'has real content'."""
    if not text:
        return ""
    t = text
    for pat in _NOISE_LINE_PATS + _profile_line_pats(profile):
        t = pat.sub("", t)
    return re.sub(r'\s+', ' ', t).strip()


# Verbatim-mode thresholds (ai.py:796-805).
THIN_BODY_CHARS = 25
VERBATIM_MAX_CHARS = 250


def verbatim_mode(body: str) -> str | None:
    """Decide whether a (noise-stripped) body needs a model at all.

    Returns:
      ""      -> thin body: nothing to summarize, do NOT call the model
      "• ..." -> short body: the sender's own words beat any summary
      None    -> long enough to summarize; call the model
    """
    if len(residual_content(body)) < THIN_BODY_CHARS:
        return ""
    if len(body) < VERBATIM_MAX_CHARS:
        return '• ' + re.sub(r'\s+', ' ', body).strip()
    return None


# ---------------------------------------------------------------------------
# Grounding (ai.py:673-776)
# ---------------------------------------------------------------------------

_GROUND_NUM_RE = re.compile(r'\b(\d[\d,]{1,})\b')
_GROUND_TOKEN_RE = re.compile(r'\b([A-Z]{2,}[A-Za-z0-9]*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*)\b')
# Generic allowlist; the profession's vocab.acronyms is merged in per call.
_GROUND_ALLOW_BASE = {
    "OK", "ASAP", "EOD", "EOW", "COB", "TBD", "NA", "FYI", "PO", "RFP", "RFQ", "RFI",
    "AM", "PM", "US", "USA", "ID", "PDF", "CC", "RE", "FW", "FWD",
    "I", "A", "II", "III", "IV",
}


def ground_bullets(bullets: list[str], source_text: str,
                   acronyms: Iterable[str] | None = None) -> list[str]:
    """Drop any bullet asserting an identifier that is not in the source.

    Numbers compare as whole runs, so a truncated "INV1" fails against a
    source containing "INV10107046". Pure-alpha tokens get substring credit
    (case/possessive variants); anything with a digit must match whole.
    """
    if not bullets or not source_text:
        return bullets
    allow = set(_GROUND_ALLOW_BASE) | {str(a).upper() for a in (acronyms or [])}
    src = str(source_text)
    src_nums = set(_GROUND_NUM_RE.findall(src.replace(",", "")))
    src_tokens = {t.upper() for t in _GROUND_TOKEN_RE.findall(src)}
    src_upper = src.upper()

    kept = []
    for line in bullets:
        ok = True
        for num in _GROUND_NUM_RE.findall(line.replace(",", "")):
            if num not in src_nums:
                ok = False
                break
        if ok:
            for tok in _GROUND_TOKEN_RE.findall(line):
                up = tok.upper()
                if up in allow or up in src_tokens:
                    continue
                if not any(c.isdigit() for c in up) and up in src_upper:
                    continue
                ok = False
                break
        if ok:
            kept.append(line)
    return kept


_ground_bullets = ground_bullets


def ground_text(text: str, source_text: str, acronyms: Iterable[str] | None = None) -> str:
    """Sentence-level grounding for prose (drafts): any sentence carrying a
    number or identifier absent from the source is removed."""
    if not text or not source_text:
        return text or ""
    out_lines = []
    for line in text.strip().split("\n"):
        sentences = [s for s in re.split(r'(?<=[.!?])\s+', line.strip()) if s.strip()]
        kept = ground_bullets(sentences, source_text, acronyms)
        out_lines.append(" ".join(s.strip() for s in kept))
    # Keep line structure (closers and signatures are line-based), drop
    # lines that lost every sentence.
    return "\n".join(l for l in out_lines if l.strip()).strip()


# ---------------------------------------------------------------------------
# Meta / gloss / contact filters (ai.py:692-715, 718-726, 845-863)
# ---------------------------------------------------------------------------

_META_BULLET_RE = re.compile(
    r'(?i)\b(acknowledg\w+|positive tone|willingness to \w+|'
    r'readiness (?:or|and|to) \w+|intent(?:ion)?s? of the (?:sender|email)|'
    r'engage in further|further communication|'
    r'(?:collaborative|cooperative) (?:tone|spirit|intent))\b')

_GLOSS_RE = re.compile(r'\s*\(([A-Za-z][A-Za-z ]{2,40})\)')

_CONTACT_INFO_RE = re.compile(
    r'(?i)(?:\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}|\b\S+@\S+\.\w{2,})')
_CONTACT_PHRASE_RE = re.compile(
    r'(?i)\b(?:contact (?:is|info(?:rmation)?)|(?:office|cell|phone|fax) '
    r'number|can be reached at|email (?:address )?(?:is|provided)|'
    r'(?:number|phone|contact) (?:was )?provided)\b')
_CONTACT_LEAD_RE = re.compile(
    r'(?i)^•?\s*(?:the\s+)?contact (?:is|person is)\b')


def strip_fake_glosses(line: str, source_lower: str) -> str:
    """Remove parenthetical expansions whose words are not in the source."""
    def _repl(m):
        words = [w.lower() for w in re.findall(r'[A-Za-z]+', m.group(1))]
        if words and all(w in source_lower for w in words):
            return m.group(0)
        return ""
    return _GLOSS_RE.sub(_repl, line)


def is_meta_bullet(line: str) -> bool:
    return bool(_META_BULLET_RE.search(line)) or len(line.lstrip('• ').strip()) <= 3


def is_contact_bullet(line: str) -> bool:
    return bool((_CONTACT_INFO_RE.search(line) and _CONTACT_PHRASE_RE.search(line))
                or _CONTACT_LEAD_RE.match(line))


def normalize_bullets(text: str) -> list[str]:
    """Model text -> list of '• ...' lines (continuations folded in)."""
    lines: list[str] = []
    for line in (text or "").strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        if line.startswith(('-', '*', '•')):
            line = '• ' + line.lstrip('-*• ').strip()
        elif lines:
            lines[-1] += ' ' + line
            continue
        else:
            line = '• ' + line
        lines.append(line)
    return lines


def filter_bullets(bullets: list[str], source_text: str,
                   acronyms: Iterable[str] | None = None, limit: int = 4) -> list[str]:
    """The full anti-slop pass: grounding -> glosses -> meta -> contact-card."""
    lines = ground_bullets(list(bullets), source_text, acronyms)
    src_l = (source_text or "").lower()
    lines = [strip_fake_glosses(l, src_l) for l in lines]
    lines = [l for l in lines if not is_meta_bullet(l)]
    lines = [l for l in lines if not is_contact_bullet(l)]
    return lines[:limit]


# ---------------------------------------------------------------------------
# Draft hygiene (ai.py:934-941, 1005-1008)
# ---------------------------------------------------------------------------

_DRAFT_CLOSERS = ("best regards", "kind regards", "warm regards", "sincerely", "regards,",
                  "thanks,", "thank you,", "cheers,", "best,")


def strip_signature_from_draft(text: str, profile: dict | None = None) -> str:
    """Cut anything from a closing line onward, plus any line naming the
    user, their role or company (the mail client adds the real signature)."""
    if not text:
        return ""
    profile = profile or {}
    needles = [n for n in (profile.get("user_name"), profile.get("role"),
                           profile.get("company")) if n]
    out: list[str] = []
    for line in text.strip().split('\n'):
        low = line.strip().lower()
        if any(low.startswith(c) for c in _DRAFT_CLOSERS):
            break
        if any(n.lower() in low for n in needles) and len(low) < 80:
            break
        out.append(line)
    body = '\n'.join(out).strip()
    # Drop a leading salutation ("Hi Kelly," / "Dear ...") per the prompts.
    body = re.sub(r'^(?:hi|hello|hey|dear)\b[^\n]{0,60}[,:\-]\s*\n+', '', body, flags=re.I)
    return body.strip()
