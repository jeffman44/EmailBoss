"""Message / Attachment dataclasses and the Power Automate record boundary.

The interchange format between every mail provider and the triage layer is the
Power Automate (PA) record: one JSON file per message whose keys are exactly
``PA_REQUIRED_KEYS`` plus any of ``PA_OPTIONAL_KEYS`` the message has.

Ported from v2/imap_capture.py lines 76-98 (key sets, limits), 428-461
(safe_filename / safe_id), 722-773 (unique path + atomic write_record).

Two fixes applied at this boundary (DESIGN.md section 5):
  * ``cc`` is mapped (``ccRecipients``) instead of being dropped.
  * Display names are kept. The PA record stores bare addresses in ``from`` /
    ``toRecipients`` / ``ccRecipients`` (that is the live producer's shape), so
    the names ride in ``internetMessageHeaders`` as the From / To / Cc header
    values ("Name <addr>"). ``from_pa_record`` reads them back from there, and
    also accepts a legacy ``from_display_name`` key some producers wrote.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import getaddresses, formataddr
from pathlib import Path
from typing import Iterable

# ── The producer contract ───────────────────────────────────────────────────
PA_REQUIRED_KEYS = frozenset({
    "id", "receivedDateTime", "hasAttachments", "internetMessageId", "subject",
    "bodyPreview", "importance", "conversationId", "isRead",
    "internetMessageHeaders", "isHtml", "body", "from", "toRecipients",
    "attachments",
})
PA_OPTIONAL_KEYS = frozenset({"ccRecipients", "replyTo"})
# Legacy / producer-specific keys we READ but never write.
PA_TOLERATED_KEYS = frozenset({"from_display_name", "bccRecipients"})
PA_ATTACHMENT_KEYS = frozenset({
    "@odata.type", "id", "lastModifiedDateTime", "name", "contentType",
    "size", "isInline", "contentId", "contentBytes",
})

BODY_PREVIEW_MAX = 255
BODY_MAX = 400_000

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._@+\- ]+")


# ── Small helpers (shared by every provider) ────────────────────────────────

def text_from_html(html: str) -> str:
    """Cheap HTML to text for previews. Ported from imap_capture 385-392."""
    txt = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html or "")
    txt = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", txt)
    txt = _TAG_RE.sub(" ", txt)
    for ent, rep in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                     ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        txt = txt.replace(ent, rep)
    return txt


def make_preview(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()[:BODY_PREVIEW_MAX]


def safe_filename(name: str, fallback: str = "attachment.bin") -> str:
    """A filename safe on Windows AND stable across cycles (imap_capture 431)."""
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = _SAFE_NAME_RE.sub("_", name).strip(" .")
    if not name:
        name = fallback
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:110] + dot + ext[:9]) if dot else name[:120]
    return name


def safe_id(raw: str, prefix: str = "msg") -> str:
    """A message id that is ALSO a legal directory name (imap_capture 443-461).

    The attachment folder is ``attachments/<id>/`` verbatim, and a raw
    Message-ID carries ``<``, ``>`` and ``:``, all illegal in a Windows path.
    The published id IS the sanitised form; the raw header still ships as
    ``internetMessageId``.
    """
    raw = (raw or "").strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1]
    clean = _SAFE_NAME_RE.sub("_", raw).strip(" ._")
    if not clean:
        return f"{prefix}-{hashlib.sha1((raw or prefix).encode('utf-8', 'replace')).hexdigest()}"
    if len(clean) > 120:
        clean = clean[:104] + "-" + hashlib.sha1(
            raw.encode("utf-8", "replace")).hexdigest()[:15]
    return clean


def thread_key_from_ids(references: str, in_reply_to: str, message_id: str,
                        subject: str = "", participants: Iterable[str] = ()) -> str:
    """Stable conversation id (imap_capture 464-497).

    Root of the References chain wins, then In-Reply-To, then the message's own
    Message-ID (it IS a thread root). Only with none of those do we fall back to
    normalised subject + participants.
    """
    ids = re.findall(r"<[^<>@\s]+@[^<>\s]+>", references or "")
    root = ids[0] if ids else ""
    if not root:
        irt = re.findall(r"<[^<>@\s]+@[^<>\s]+>", in_reply_to or "")
        root = irt[0] if irt else ""
    if not root:
        own = re.findall(r"<[^<>@\s]+@[^<>\s]+>", message_id or "")
        root = own[0] if own else ""
    if not root:
        subj = re.sub(r"(?i)^\s*(re|fw|fwd|aw|sv)\s*:\s*", "", subject or "").strip().lower()
        subj = _WS_RE.sub(" ", subj)
        parts = sorted({a.lower() for a in participants if a})
        root = "subject:" + subj + "|" + ",".join(parts)
    digest = hashlib.sha256(root.encode("utf-8", "replace")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def iso_utc(dt: datetime | None) -> str:
    """``+00:00`` offset form, never ``Z`` (the live producer's shape)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass
class Address:
    email: str
    name: str = ""

    def display(self) -> str:
        """``Name <addr>`` when a name is known, else the bare address."""
        if self.name and self.name.lower() != self.email.lower():
            return formataddr((self.name, self.email))
        return self.email

    @staticmethod
    def parse_many(value) -> list["Address"]:
        """Accept ``;``-joined bare strings, RFC 5322 lists, or lists of
        strings / dicts / Address objects."""
        out: list[Address] = []
        if not value:
            return out
        if isinstance(value, Address):
            return [value]
        if isinstance(value, dict):
            em = (value.get("email") or value.get("address") or "").strip()
            if em:
                out.append(Address(em, (value.get("name") or "").strip()))
            return out
        if isinstance(value, (list, tuple)):
            for v in value:
                out.extend(Address.parse_many(v))
            return out
        text = str(value)
        # Both separators appear in the wild; getaddresses handles commas.
        chunks = [c for c in text.replace(";", ",").split(",") if c.strip()]
        for name, addr in getaddresses(chunks):
            addr = (addr or "").strip()
            if not addr:
                continue
            out.append(Address(addr, (name or "").strip()))
        # de-dup on address, keep first name seen
        seen, dedup = set(), []
        for a in out:
            k = a.email.lower()
            if k in seen:
                continue
            seen.add(k)
            dedup.append(a)
        return dedup


def _join_bare(addrs: list[Address]) -> str:
    return ";".join(a.email for a in addrs)


def _join_display(addrs: list[Address]) -> str:
    return ", ".join(a.display() for a in addrs)


@dataclass
class Attachment:
    name: str
    content_type: str = "application/octet-stream"
    size: int = 0
    is_inline: bool = False
    content_id: str | None = None
    content: bytes = b""
    id: str = ""
    last_modified: str = ""

    def to_pa(self, record_id: str, idx: int, stamp: str) -> dict:
        data = self.content or b""
        return {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": self.id or f"{record_id}-{idx}",
            "lastModifiedDateTime": self.last_modified or stamp,
            "name": self.name,
            "contentType": self.content_type or "application/octet-stream",
            "size": self.size or len(data),
            "isInline": bool(self.is_inline),
            "contentId": self.content_id,
            "contentBytes": base64.b64encode(data).decode("ascii"),
        }

    @staticmethod
    def from_pa(d: dict) -> "Attachment":
        try:
            content = base64.b64decode(d.get("contentBytes") or "")
        except Exception:
            content = b""
        return Attachment(
            name=d.get("name") or "attachment.bin",
            content_type=d.get("contentType") or "application/octet-stream",
            size=int(d.get("size") or len(content)),
            is_inline=bool(d.get("isInline")),
            content_id=d.get("contentId"),
            content=content,
            id=str(d.get("id") or ""),
            last_modified=str(d.get("lastModifiedDateTime") or ""),
        )


@dataclass
class Message:
    id: str
    received_at: datetime
    subject: str = ""
    body: str = ""
    is_html: bool = False
    sender: Address = field(default_factory=lambda: Address(""))
    to: list[Address] = field(default_factory=list)
    cc: list[Address] = field(default_factory=list)
    reply_to: list[Address] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    internet_message_id: str = ""
    conversation_id: str = ""
    importance: str = "normal"
    is_read: bool = False
    headers: list[dict] = field(default_factory=list)
    folder: str = "inbox"          # "inbox" | "sent"
    body_preview: str = ""
    provider: str = ""
    provider_ref: str = ""         # provider-native id (Gmail id, Graph id, IMAP uid)

    # ── convenience ──
    @property
    def direction(self) -> str:
        return "outbound" if self.folder == "sent" else "inbound"

    def preview(self) -> str:
        if self.body_preview:
            return make_preview(self.body_preview)
        src = text_from_html(self.body) if self.is_html else self.body
        return make_preview(src)

    def _headers_with_names(self) -> list[dict]:
        """Ensure From / To / Cc header entries carry display names."""
        out = [dict(h) for h in (self.headers or []) if isinstance(h, dict)
               and "name" in h and "value" in h]
        present = {str(h["name"]).lower() for h in out}
        wanted = (("From", [self.sender] if self.sender.email else []),
                  ("To", self.to), ("Cc", self.cc))
        for name, addrs in wanted:
            if addrs and name.lower() not in present:
                out.append({"name": name, "value": _join_display(addrs)[:2000]})
        return out[:200]

    def to_pa_record(self) -> dict:
        stamp = iso_utc(self.received_at)
        rid = self.id or safe_id(self.internet_message_id, prefix=self.folder)
        body = self.body or ""
        if len(body) > BODY_MAX:
            body = body[:BODY_MAX]
        atts = [a.to_pa(rid, i + 1, stamp) for i, a in enumerate(self.attachments)]
        record = {
            "id": rid,
            "receivedDateTime": stamp,
            # Graph semantics: inline-only mail reports FALSE.
            "hasAttachments": any(not a["isInline"] for a in atts),
            "internetMessageId": self.internet_message_id or "",
            "subject": self.subject or "",
            "bodyPreview": self.preview(),
            "importance": self.importance if self.importance in ("low", "normal", "high") else "normal",
            "conversationId": self.conversation_id or thread_key_from_ids(
                "", "", self.internet_message_id, self.subject,
                [self.sender.email] + [a.email for a in self.to]),
            "isRead": bool(self.is_read) or self.folder == "sent",
            "internetMessageHeaders": self._headers_with_names(),
            "isHtml": bool(self.is_html),
            "body": body,
            "from": self.sender.email or "",
            "toRecipients": _join_bare(self.to),
            "attachments": atts,
        }
        if self.cc:
            record["ccRecipients"] = _join_bare(self.cc)
        if self.reply_to:
            record["replyTo"] = _join_bare(self.reply_to)
        return record

    @staticmethod
    def from_pa_record(d: dict, folder: str = "inbox") -> "Message":
        headers = [h for h in (d.get("internetMessageHeaders") or [])
                   if isinstance(h, dict)]
        hmap: dict[str, str] = {}
        for h in headers:
            k = str(h.get("name") or "").lower()
            if k and k not in hmap:
                hmap[k] = str(h.get("value") or "")

        def _with_names(bare, header_key):
            bare_addrs = Address.parse_many(bare)
            named = {a.email.lower(): a for a in Address.parse_many(hmap.get(header_key, ""))}
            out = []
            for a in bare_addrs:
                n = named.get(a.email.lower())
                out.append(Address(a.email, (n.name if n and n.name else a.name)))
            # Headers may carry recipients the bare field lacks; keep the bare
            # field as the authority on WHO, headers only supply names.
            return out

        sender_list = _with_names(d.get("from") or "", "from")
        sender = sender_list[0] if sender_list else Address("")
        legacy_name = (d.get("from_display_name") or "").strip()
        if legacy_name and not sender.name:
            sender.name = legacy_name
        received = parse_iso(d.get("receivedDateTime")) or datetime.now(timezone.utc)
        return Message(
            id=str(d.get("id") or safe_id(d.get("internetMessageId") or "", prefix=folder)),
            received_at=received,
            subject=str(d.get("subject") or ""),
            body=str(d.get("body") or ""),
            is_html=bool(d.get("isHtml")),
            sender=sender,
            to=_with_names(d.get("toRecipients") or "", "to"),
            cc=_with_names(d.get("ccRecipients") or "", "cc"),
            reply_to=Address.parse_many(d.get("replyTo") or ""),
            attachments=[Attachment.from_pa(a) for a in (d.get("attachments") or [])
                         if isinstance(a, dict)],
            internet_message_id=str(d.get("internetMessageId") or ""),
            conversation_id=str(d.get("conversationId") or ""),
            importance=str(d.get("importance") or "normal"),
            is_read=bool(d.get("isRead")),
            headers=headers,
            folder=folder,
            body_preview=str(d.get("bodyPreview") or ""),
        )


# ── Validation (imap_capture 689-719) ───────────────────────────────────────

def validate_record(record: dict) -> list[str]:
    """Assert the producer contract. Returns a list of problems (empty = ok)."""
    problems = []
    keys = set(record)
    missing = PA_REQUIRED_KEYS - keys
    extra = keys - PA_REQUIRED_KEYS - PA_OPTIONAL_KEYS
    if missing:
        problems.append(f"missing keys: {sorted(missing)}")
    if extra:
        problems.append(f"unexpected keys: {sorted(extra)}")
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
                    str(record.get("receivedDateTime", ""))):
        problems.append("receivedDateTime is not ISO-8601 with a numeric offset")
    for f in ("from", "toRecipients", "subject", "body", "bodyPreview",
              "id", "internetMessageId", "conversationId", "importance"):
        if not isinstance(record.get(f), str):
            problems.append(f"{f} is not a string")
    for f in ("hasAttachments", "isRead", "isHtml"):
        if not isinstance(record.get(f), bool):
            problems.append(f"{f} is not a bool")
    if len(record.get("bodyPreview", "")) > BODY_PREVIEW_MAX:
        problems.append("bodyPreview exceeds 255 chars")
    for att in record.get("attachments") or []:
        if set(att) != PA_ATTACHMENT_KEYS:
            problems.append(f"attachment key set differs: {sorted(set(att))}")
            break
    for hdr in record.get("internetMessageHeaders") or []:
        if set(hdr) != {"name", "value"}:
            problems.append("internetMessageHeaders entry is not {name,value}")
            break
    return problems


# ── Writing the files (imap_capture 724-773) ────────────────────────────────

def unique_path(folder: Path, suffix: str) -> Path:
    """``<yyyyMMddHHmmssfff>-<suffix>.json``; suffix keeps inbox/ and sent/
    from ever producing the same name for the same millisecond."""
    folder.mkdir(parents=True, exist_ok=True)
    for _ in range(1000):
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
        p = folder / f"{stamp}-{suffix}.json"
        if not p.exists():
            return p
        time.sleep(0.002)
    return folder / f"{int(time.time() * 1000)}-{suffix}.json"


def write_pa_record(record: dict, cache_dir, folder_kind: str) -> tuple[Path, list[Path]]:
    """Attachment bytes FIRST, then the JSON, atomically.

    Order is load-bearing: the triage scanner reads the JSON and immediately
    looks for ``attachments/<id>/``. A JSON that lands before its files is a
    card with silently missing photos.
    """
    cache_dir = Path(cache_dir)
    att_dir = cache_dir / "attachments" / record["id"]
    written: list[Path] = []
    atts = record.get("attachments") or []
    if atts:
        att_dir.mkdir(parents=True, exist_ok=True)
        for att in atts:
            try:
                data = base64.b64decode(att.get("contentBytes") or "")
            except Exception:
                continue
            target = att_dir / safe_filename(att.get("name") or "attachment.bin")
            try:
                tmp = target.with_name(target.name + ".part")
                tmp.write_bytes(data)
                tmp.replace(target)
                written.append(target)
            except Exception:
                pass
    out = unique_path(cache_dir / folder_kind, folder_kind)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    return out, written


def write_record(cache_dir, msg: Message) -> tuple[Path, list[Path]]:
    """Message -> PA record -> files. The one entry point providers call."""
    rec = msg.to_pa_record()
    folder = "sent" if msg.folder == "sent" else "inbox"
    return write_pa_record(rec, cache_dir, folder)
