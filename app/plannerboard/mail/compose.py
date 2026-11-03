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


def _body_html(msg: Message) -> str:
    if msg.is_html and msg.body:
        return msg.body
    return "<div style=\"white-space:pre-wrap\">" + _html.escape(msg.body or "") + "</div>"


def quote_reply(msg: Message) -> str:
    """The quoted original a reply starts with (below the user's text)."""
    return ("<br><br><div class=\"pb-quote\" style=\"border-left:2px solid #999;padding-left:10px;"
            "margin:8px 0;color:#555\">" + _header_block(msg) + "<br>" + _body_html(msg) + "</div>")


def quote_forward(msg: Message) -> str:
    return ("<br><br><div class=\"pb-forward\"><div>---------- Forwarded message ----------</div>"
            + _header_block(msg) + "<br>" + _body_html(msg) + "</div>")


def reply_recipients(msg: Message, me: str, reply_all: bool) -> tuple[list[Address], list[Address]]:
    """(to, cc) for a reply. Reply-To wins over From; reply-all adds every
    other To/Cc except the user's own address."""
    me_l = (me or "").strip().lower()
    to = list(msg.reply_to) if msg.reply_to else ([msg.sender] if msg.sender.email else [])
    cc: list[Address] = []
    if reply_all:
        for a in list(msg.to) + list(msg.cc):
            if a.email.lower() == me_l:
                continue
            if any(a.email.lower() == t.email.lower() for t in to):
                continue
            cc.append(a)
    to = [a for a in to if a.email.lower() != me_l] or to
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
