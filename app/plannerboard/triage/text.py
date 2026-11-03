"""Body text cleaning.

Ported from engine/email_processor.py:
  * strip_html             lines 1344-1367
  * strip_forward_headers  lines 1370-1423 (keeps the NEWEST message)
  * strip_signature_block  lines 317-349  (markers now come from cfg)
  * extract_links          lines 1426-1491 (doc domains + internal domains
                           now come from cfg instead of literals)
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

# ── strip_html ──────────────────────────────────────────────────────────────


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, d):
        self._parts.append(d)

    def get_text(self):
        return " ".join(self._parts)


def strip_html(html_str: str) -> str:
    """Strip HTML tags, collapse whitespace, return plain text."""
    if not html_str or "<" not in html_str:
        return html_str or ""
    txt = re.sub(r"(?is)<(style|script)\b[^>]*>.*?</\1>", " ", html_str)
    txt = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", txt)
    s = _HTMLStripper()
    try:
        s.feed(txt)
        txt = s.get_text()
    except Exception:
        txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"[ \t\r\f\v]+", " ", txt)
    txt = re.sub(r"\s*\n\s*", "\n", txt)
    return txt.strip()


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# ── strip_forward_headers ───────────────────────────────────────────────────

_HEADER_BLOCK_RE = re.compile(
    r"(?:_{5,}|[-]{5,})?\s*"
    r"From:\s*[^\n]+"
    r"\s+Sent:\s*[^\n]+"
    r"\s+To:\s*[^\n]+"
    r"(?:\s+(?:Cc|CC|cc):\s*[^\n]+)?"
    r"(?:\s+Subject:\s*[^\n]+)?"
    r"\s*",
    re.IGNORECASE,
)
# Gmail-style quote marker: "On <date>, <name> wrote:"
_ON_WROTE_RE = re.compile(r"\n\s*On .{0,120}?\bwrote:\s*\n", re.IGNORECASE | re.DOTALL)


def strip_forward_headers(text: str) -> str:
    """Return the NEWEST message from an email body, dropping quoted history.

    Replies and forwards share one shape: new text, then a quoted header
    block, then the old thread. The new content is everything BEFORE the first
    header block whenever that is substantive; only for a bare forward do we
    fall back to the content after the LAST block (the forwarded body).
    """
    if not text:
        return text or ""
    first = _HEADER_BLOCK_RE.search(text)
    ow = _ON_WROTE_RE.search(text)
    if ow and (not first or ow.start() < first.start()):
        top = text[:ow.start()].strip()
        if len(top) >= 15:
            return top
    if not first:
        return text
    top = text[:first.start()].strip()
    if len(top) >= 15:
        return top
    last_end = 0
    for m in _HEADER_BLOCK_RE.finditer(text):
        last_end = m.end()
    if last_end and last_end < len(text):
        bottom = text[last_end:].strip()
        if len(bottom) > 20:
            return bottom
    return top or text


# ── strip_signature_block ───────────────────────────────────────────────────

_BASE_SIG_PARTS = (
    r"\n\s*(?:--+|__+)\s*\n",
    r"\n\s*(?:thanks|thank you|many thanks|regards|best regards|kind regards|best|"
    r"sincerely|respectfully|cheers|v/r)\s*[,!.]?\s*\n",
    r"\bsent from my\b",
    r"\bconfidentiality notice\b",
    r"\bthis (?:e-?mail|message) (?:and any|is intended)\b",
    r"\n\s*from:\s",
    r"\n\s*-+\s*original message\s*-+",
    r"\n\s*on .{0,80}\bwrote:",
)


def signature_cut_re(markers=()) -> re.Pattern:
    """The boundary regex: generic signature/quote boundaries plus the
    user-specific markers from config (their name, company, aliases, the
    office address). Markers are escaped literals matched case-insensitively."""
    parts = list(_BASE_SIG_PARTS)
    for m in markers or ():
        m = (m or "").strip()
        if len(m) >= 3:
            parts.append(r"\b" + re.escape(m).replace(r"\ ", r"\s+") + r"\b")
    return re.compile(r"(?:" + "|".join(parts) + r")", re.IGNORECASE)


def signature_markers_from_cfg(cfg: dict | None) -> list[str]:
    user = ((cfg or {}).get("user") or {})
    out = []
    for v in (user.get("company"), user.get("name")):
        if v:
            out.append(str(v))
    for a in user.get("aliases") or []:
        if a and "@" not in str(a):
            out.append(str(a))
    for extra in (user.get("signature_markers") or []):
        if extra:
            out.append(str(extra))
    return out


def strip_signature_block(text: str, markers=None, cut_re: re.Pattern | None = None) -> str:
    """Cut a body at the first signature / quoted-reply boundary. Without this
    every email carries the user's own footer into keyword matching and the
    summarizer, and the card reports the sender's office as the job site."""
    if not text:
        return ""
    rx = cut_re or signature_cut_re(markers or ())
    m = rx.search(text)
    return (text[:m.start()] if m else text).strip()


# ── extract_links ───────────────────────────────────────────────────────────

DEFAULT_DOC_LINK_DOMAINS = {
    "dropbox.com": "Dropbox",
    "drive.google.com": "Google Drive",
    "docs.google.com": "Google Drive",
    "box.com": "Box",
    "1drv.ms": "Cloud share",
    "wetransfer.com": "WeTransfer",
    "sharefile.com": "ShareFile",
    "egnyte.com": "Egnyte",
}
_HREF_RE = re.compile(r'<a\s[^>]*href=["\']([^"\']+)["\']', re.IGNORECASE)
_BARE_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')


def extract_links(html_or_text: str, doc_domains: dict | None = None,
                  internal_domains=()) -> list[dict]:
    """Document-sharing links plus links on the user's own domains.

    Returns ``[{"url", "domain", "label"}]``. ``doc_domains`` maps domain
    fragment -> label; ``internal_domains`` are the user's company domains
    (links there get the label "Internal").
    """
    if not html_or_text:
        return []
    doc_domains = doc_domains or DEFAULT_DOC_LINK_DOMAINS
    internal = [d.lower().lstrip("@") for d in (internal_domains or ()) if d]
    seen: set[str] = set()
    links: list[dict] = []

    def _add(url: str):
        url_clean = url.rstrip(".,;:!?)")
        if url_clean in seen:
            return
        low = url_clean.lower()
        for dom, label in doc_domains.items():
            if dom in low:
                seen.add(url_clean)
                links.append({"url": url_clean, "domain": dom, "label": label})
                return
        host = re.sub(r"^https?://", "", low).split("/")[0]
        for dom in internal:
            if host == dom or host.endswith("." + dom) or dom in host:
                seen.add(url_clean)
                links.append({"url": url_clean, "domain": dom, "label": "Internal"})
                return

    for m in _HREF_RE.finditer(html_or_text):
        _add(m.group(1))
    for m in _BARE_URL_RE.finditer(html_or_text):
        _add(m.group(0))
    return links


def doc_domains_from_cfg(cfg: dict | None) -> dict:
    extra = ((cfg or {}).get("mail") or {}).get("doc_link_domains") or {}
    out = dict(DEFAULT_DOC_LINK_DOMAINS)
    if isinstance(extra, dict):
        out.update({str(k).lower(): str(v) for k, v in extra.items()})
    elif isinstance(extra, (list, tuple)):
        for d in extra:
            out.setdefault(str(d).lower(), str(d))
    return out
