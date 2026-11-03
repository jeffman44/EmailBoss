"""Outbound message assembly shared by every provider.

``build_message`` turns the composer payload (to / cc / bcc / subject / HTML
body / attachments / the message being replied to) into one RFC 5322
``EmailMessage``. IMAP uses it for APPEND-to-Drafts and SMTP, Gmail for its
``raw`` payload, and Graph only for the plain-text fallback. One builder means
one place where quoting, threading headers and the text alternative are
decided.

``quote_reply`` and ``quote_forward`` produce the HTML a reply / forward
starts from, in the shape every other mail client uses (a header block and a
left-bordered quote), so the reading pane and the composer agree.
"""
from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Iterable

from .message import Address, Attachment, Message, text_from_html

_WS = re.compile(r"[ \t]+")
_NL = re.compile(r"\n{3,}")


def html_to_text(body_html: str) -> str:
    """The text/plain alternative: readable, not a tag dump."""
    txt = text_from_html(body_html or "")
    txt = "\n".join(_WS.sub(" ", line).strip() for line in txt.splitlines())
    return _NL.sub("\n\n", txt).strip()


# ── HTML sanitizer (quoted original mail goes into a contenteditable) ───────
#
# Hand-written allowlist. Everything is rebuilt from parsed tokens: text is
# re-escaped, only listed tags and attributes are emitted, so a tag that the
# parser mangles (``<scr<script>ipt>``) can only ever come out as inert text.

ALLOWED_TAGS = frozenset("p br div span b i u em strong a ul ol li blockquote pre code "
                         "table thead tbody tfoot tr td th img".split())
_VOID = frozenset(("br", "img"))
_DROP_WITH_CONTENT = frozenset("script style iframe frame frameset object embed applet form base meta link "
                               "svg math noscript template title head textarea select option button "
                               "audio video source track canvas xml".split())
_PLACEHOLDER_SRC = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
_SAFE_DATA_IMG = re.compile(r"^data:image/(png|jpe?g|gif|webp);base64,[a-z0-9+/=]+$", re.I)
_URL_JUNK = re.compile(r"[\x00-\x20\x7f-\x9f\u200b-\u200f\u2028\u2029\ufeff]+")
_BAD_STYLE = re.compile(r"url\s*\(|image-set|expression|@import|behaviou?r|javascript|vbscript|-moz-binding|\\|/\*", re.I)
# Properties that let a quoted message escape its box, cover the page or hide
# content: dropped one by one (the rest of the style attribute survives).
_VENDOR = re.compile(r"^-(?:webkit|moz|ms|o)-")
_BAD_PROPS = frozenset((
    "position", "z-index", "top", "left", "right", "bottom", "inset", "inset-block", "inset-inline",
    "transform", "transform-origin", "transform-style", "translate", "rotate", "scale", "perspective",
    "perspective-origin", "clip", "clip-path", "mask", "mask-image", "filter", "backdrop-filter",
    "mix-blend-mode", "isolation", "pointer-events", "will-change", "contain", "content-visibility",
    "float", "appearance", "cursor", "zoom",
))
_NEG_NUM = re.compile(r"(^|[\s(,])-\s*(?:\d|\.\d)")
_NEG_PROPS = re.compile(r"^(?:margin(?:-[a-z]+)*|text-indent|letter-spacing|word-spacing|vertical-align)$")
_NUM = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(%?)\s*(?:!important)?\s*$", re.I)


def _clean_style(style: str) -> str:
    """Keep only declarations that cannot reposition, hide or overlay. Anything
    unparseable is dropped; the empty string means "emit no style attribute"."""
    keep = []
    for decl in style.split(";"):
        if ":" not in decl:
            continue
        prop, _, val = decl.partition(":")
        prop = _VENDOR.sub("", prop.strip().lower())
        val = val.strip()
        if not prop or not val or not re.fullmatch(r"[a-z-]+", prop):
            continue
        if prop in _BAD_PROPS or prop.startswith(("transform", "mask", "inset", "perspective", "animation", "transition")):
            continue
        if prop == "opacity":
            m = _NUM.match(val)
            if not m:
                continue
            n = float(m.group(1)) / (100.0 if m.group(2) else 1.0)
            if n < 0.5:
                continue
        if _NEG_PROPS.match(prop) and _NEG_NUM.search(val):
            continue
        keep.append(f"{prop}:{val}")
    return ";".join(keep)
_SIMPLE_ATTRS = {"a": ("title",), "img": ("alt", "title"), "td": ("colspan", "rowspan"), "th": ("colspan", "rowspan")}
_INT = re.compile(r"^\d{1,5}%?$")


def _clean_url(v: str) -> str:
    return _URL_JUNK.sub("", v or "")


def _is_remote(u: str) -> bool:
    u = _clean_url(u).lower()
    return u.startswith(("http:", "https:", "//", "ftp:"))


class _Sanitizer(HTMLParser):
    def __init__(self, block_remote: bool):
        super().__init__(convert_charrefs=True)
        self.block_remote = block_remote
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip: str | None = None
        self.skip_depth = 0
        self.remote_blocked = 0

    # tags
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip:
            if tag == self.skip:
                self.skip_depth += 1
            return
        if tag in _DROP_WITH_CONTENT:
            if tag not in _VOID:
                self.skip, self.skip_depth = tag, 1
            return
        if tag not in ALLOWED_TAGS:
            return                                   # unwrap: keep the text, lose the tag
        a = {k.lower(): (v or "") for k, v in attrs}
        parts = []
        if tag == "a":
            href = _clean_url(a.get("href", ""))
            if href.lower().startswith(("http://", "https://", "mailto:", "tel:")):
                parts.append(f'href="{_html.escape(href, quote=True)}"')
                parts.append('rel="noopener noreferrer"')
        elif tag == "img":
            src = _clean_url(a.get("src", ""))
            low = src.lower()
            if _is_remote(src):
                real = ("https:" + src) if src.startswith("//") else src
                if self.block_remote:
                    self.remote_blocked += 1
                    parts.append(f'src="{_PLACEHOLDER_SRC}"')
                    parts.append(f'data-pb-src="{_html.escape(real, quote=True)}"')
                else:
                    parts.append(f'src="{_html.escape(real, quote=True)}"')
            elif low.startswith("cid:") or _SAFE_DATA_IMG.match(src):
                parts.append(f'src="{_html.escape(src, quote=True)}"')
            for k in ("width", "height"):
                if _INT.match(a.get(k, "").strip()):
                    parts.append(f'{k}="{a[k].strip()}"')
        for k in _SIMPLE_ATTRS.get(tag, ()):
            if k in ("colspan", "rowspan"):
                if _INT.match(a.get(k, "").strip()):
                    parts.append(f'{k}="{a[k].strip()}"')
            elif a.get(k):
                parts.append(f'{k}="{_html.escape(a[k], quote=True)}"')
        style = a.get("style", "")
        if style and not _BAD_STYLE.search(style) and len(style) < 1000:
            style = _clean_style(style)
            if style:
                parts.append(f'style="{_html.escape(style, quote=True)}"')
        self.out.append("<" + " ".join([tag] + parts) + ">")
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID and not self.skip:
            self.handle_endtag(tag)
        elif self.skip and tag.lower() == self.skip:
            self.skip_depth -= 1
            if self.skip_depth == 0:
                self.skip = None

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip:
            if tag == self.skip:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip = None
            return
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(_html.escape(data, quote=False))

    # comments, doctype, processing instructions, CDATA: dropped
    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass


def sanitize_html(html_text: str, block_remote: bool = False) -> str:
    """Allowlist-sanitize untrusted mail HTML. With ``block_remote`` every
    remote ``<img src>`` becomes a placeholder carrying the real URL in
    ``data-pb-src`` (the UI offers "Load images"); CSS ``url()`` never survives."""
    return sanitize_html_info(html_text, block_remote)[0]


def sanitize_html_info(html_text: str, block_remote: bool = False) -> tuple[str, int]:
    """(clean_html, number_of_remote_images_blocked)."""
    p = _Sanitizer(block_remote)
    try:
        p.feed(html_text or "")
        p.close()
    except Exception:
        return _html.escape(html_text or "", quote=False), 0
    while p.stack:
        p.out.append(f"</{p.stack.pop()}>")
    return "".join(p.out), p.remote_blocked


def _subject_with_prefix(subject: str, prefix: str) -> str:
    s = (subject or "").strip()
    if re.match(rf"(?i)^{re.escape(prefix)}:", s):
        return s
    return f"{prefix}: {s}" if s else f"{prefix}:"


def reply_subject(subject: str) -> str:
    return _subject_with_prefix(re.sub(r"(?i)^(re|fw|fwd):\s*", "", subject or "", count=1), "Re")


def forward_subject(subject: str) -> str:
    return _subject_with_prefix(re.sub(r"(?i)^(fw|fwd):\s*", "", subject or "", count=1), "Fw")


def _header_block(msg: Message) -> str:
    def row(label, value):
        return (f"<div><b>{_html.escape(label)}:</b> {_html.escape(value)}</div>" if value else "")
    when = ""
    try:
        when = msg.received_at.strftime("%a, %b %d, %Y %I:%M %p") if isinstance(msg.received_at, datetime) else ""
    except Exception:
        when = ""
    return "".join([
        row("From", msg.sender.display() if msg.sender and msg.sender.email else ""),
        row("Sent", when),
        row("To", ", ".join(a.display() for a in msg.to)),
        row("Cc", ", ".join(a.display() for a in msg.cc)),
        row("Subject", msg.subject or ""),
    ])


def _body_html(msg: Message, block_remote: bool = False) -> str:
    if msg.is_html and msg.body:
        return sanitize_html(msg.body, block_remote=block_remote)
    return "<div style=\"white-space:pre-wrap\">" + _html.escape(msg.body or "") + "</div>"


def quote_reply(msg: Message, block_remote: bool = False) -> str:
    """The quoted original a reply starts with (below the user's text)."""
    return ("<br><br><div class=\"pb-quote\" style=\"border-left:2px solid #999;padding-left:10px;"
            "margin:8px 0;color:#555\">" + _header_block(msg) + "<br>" + _body_html(msg, block_remote) + "</div>")


def quote_forward(msg: Message, block_remote: bool = False) -> str:
    return ("<br><br><div class=\"pb-forward\"><div>---------- Forwarded message ----------</div>"
            + _header_block(msg) + "<br>" + _body_html(msg, block_remote) + "</div>")


def reply_to_differs(msg: Message) -> list[Address]:
    """The Reply-To addresses when they are not the sender's own; else []."""
    rt = [a for a in (msg.reply_to or []) if a.email]
    if not rt:
        return []
    frm = (msg.sender.email or "").strip().lower() if msg.sender else ""
    if all(a.email.strip().lower() == frm for a in rt):
        return []
    return rt


def reply_recipients(msg: Message, me: str, reply_all: bool, aliases: Iterable[str] = (),
                     use_reply_to: bool = False) -> tuple[list[Address], list[Address]]:
    """(to, cc) for a reply. The sender (From) is the default target; pass
    ``use_reply_to`` to answer the Reply-To address instead. Reply-all adds
    every other To/Cc except the user's own address and aliases."""
    mine = {(me or "").strip().lower()} | {str(a).strip().lower() for a in (aliases or ()) if a}
    mine.discard("")
    alt = reply_to_differs(msg)
    if use_reply_to and alt:
        to = list(alt)
    else:
        to = [msg.sender] if msg.sender and msg.sender.email else list(alt)
    cc: list[Address] = []
    if reply_all:
        for a in list(msg.to) + list(msg.cc):
            if a.email.lower() in mine:
                continue
            if any(a.email.lower() == t.email.lower() for t in to):
                continue
            if any(a.email.lower() == c.email.lower() for c in cc):
                continue
            cc.append(a)
    to = [a for a in to if a.email.lower() not in mine] or to
    return to, cc


def _threading_headers(reply_to: Message | None) -> dict:
    if reply_to is None or not reply_to.internet_message_id:
        return {}
    refs = ""
    for h in reply_to.headers or []:
        if str(h.get("name", "")).lower() == "references":
            refs = str(h.get("value") or "")
            break
    chain = (refs + " " + reply_to.internet_message_id).strip()
    return {"In-Reply-To": reply_to.internet_message_id, "References": chain}


def build_message(sender: Address | str, to, cc=(), bcc=(), subject: str = "",
                  body_html: str = "", attachments: Iterable[Attachment] = (),
                  reply_to: Message | None = None, message_id_domain: str | None = None) -> EmailMessage:
    """One multipart message: text alternative + HTML, attachments appended.
    Inline attachments (``is_inline`` with a ``content_id``) go into a
    multipart/related so ``cid:`` references in the HTML resolve."""
    em = EmailMessage()
    snd = sender if isinstance(sender, Address) else (Address.parse_many(sender) or [Address("")])[0]
    em["From"] = snd.display() if snd.email else ""
    to_l, cc_l, bcc_l = Address.parse_many(to), Address.parse_many(cc), Address.parse_many(bcc)
    if to_l:
        em["To"] = ", ".join(a.display() for a in to_l)
    if cc_l:
        em["Cc"] = ", ".join(a.display() for a in cc_l)
    if bcc_l:
        em["Bcc"] = ", ".join(a.display() for a in bcc_l)
    em["Subject"] = subject or ""
    em["Date"] = formatdate(localtime=True)
    try:
        em["Message-ID"] = make_msgid(domain=message_id_domain or (snd.email.split("@", 1)[1] if "@" in snd.email else None))
    except Exception:
        em["Message-ID"] = make_msgid()
    for k, v in _threading_headers(reply_to).items():
        em[k] = v
    em.set_content(html_to_text(body_html) or " ")
    em.add_alternative(body_html or "", subtype="html")
    html_part = em.get_payload()[-1]
    atts = list(attachments or ())
    for att in atts:
        if att.is_inline and att.content_id:
            main, _, sub = (att.content_type or "application/octet-stream").partition("/")
            html_part.add_related(att.content or b"", maintype=main or "application",
                                  subtype=sub or "octet-stream", cid=f"<{att.content_id}>",
                                  filename=att.name)
    for att in atts:
        if att.is_inline and att.content_id:
            continue
        main, _, sub = (att.content_type or "application/octet-stream").partition("/")
        em.add_attachment(att.content or b"", maintype=main or "application",
                          subtype=sub or "octet-stream", filename=att.name or "attachment.bin")
    return em


def envelope_recipients(em: EmailMessage) -> list[str]:
    """Every bare address in To / Cc / Bcc (the SMTP RCPT list)."""
    out: list[str] = []
    for h in ("To", "Cc", "Bcc"):
        for a in Address.parse_many(str(em.get(h) or "")):
            if a.email.lower() not in {o.lower() for o in out}:
                out.append(a.email)
    return out


def strip_bcc(em: EmailMessage) -> bytes:
    """Bytes to hand the transport: Bcc removed from the headers."""
    if "Bcc" in em:
        del em["Bcc"]
    return em.as_bytes()
