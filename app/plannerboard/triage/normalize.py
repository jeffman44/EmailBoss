"""PA record files -> ``email_obj`` dicts.

Ported from engine/email_processor.py ``scan_email_files`` (lines 2562-2697),
``parse_iso`` (1525-1549) and the processed/archive handling in ``main``
(3072-3208, simplified: a handled source file is MOVED to
``<mail_cache>/processed/<yyyy-mm-dd>/<folder>/`` instead of deleted, with a
90-day purge). The capture-health sidecar (``capture_status.json``, lines
3268-3360) records what the scan could not read so a dead or garbage-writing
producer is visible on /api/health.

``email_obj`` keys (the same set the old processor produced, plus ``cc`` and
the recipient display names):

  message_id, conversation_id, internet_message_id, from_name, from_email,
  to, cc, recipients[{name,email,kind}], subject, body_preview, body,
  body_raw, received_at, has_attachments, attachments[{name,contentType,
  size,isInline}], importance, is_read, source_file, source_folder,
  source_path, doc_links[], direction
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..mail.message import Address, parse_iso
from .text import (doc_domains_from_cfg, extract_links, strip_forward_headers,
                   strip_html)

BODY_TRIM = 500
BODY_RAW_MAX = 100_000
PROCESSED_KEEP_DAYS = 90


def _from_name(data: dict, from_email: str, hmap: dict) -> str:
    """Display name: legacy key, then the From header, then the local part."""
    name = (data.get("from_display_name") or data.get("from_name") or "").strip()
    if not name and hmap.get("from"):
        parsed = Address.parse_many(hmap["from"])
        for a in parsed:
            if a.email.lower() == (from_email or "").lower() and a.name:
                name = a.name
                break
    if not name and from_email:
        name = from_email.split("@")[0] if "@" in from_email else from_email
    return name


def _recipients(bare: str, hmap: dict, header: str, kind: str) -> list[dict]:
    named = {a.email.lower(): a.name for a in Address.parse_many(hmap.get(header, ""))}
    out = []
    for a in Address.parse_many(bare):
        out.append({"name": named.get(a.email.lower()) or a.name or "",
                    "email": a.email, "kind": kind})
    return out


def normalize_record(data: dict, source_path: str | os.PathLike, cfg: dict | None = None) -> dict:
    """One PA record dict -> email_obj. Pure; never touches disk."""
    source_path = Path(source_path)
    hmap: dict[str, str] = {}
    for h in data.get("internetMessageHeaders") or []:
        if isinstance(h, dict):
            k = str(h.get("name") or "").lower()
            if k and k not in hmap:
                hmap[k] = str(h.get("value") or "")

    from_email = (data.get("from") or data.get("from_email") or "").strip()
    from_name = _from_name(data, from_email, hmap)

    raw_html_body = data.get("body", "") or ""
    doc_links = extract_links(raw_html_body, doc_domains_from_cfg(cfg),
                              ((cfg or {}).get("user") or {}).get("internal_domains") or ())

    raw_body = strip_forward_headers(strip_html(raw_html_body))
    body_trimmed = raw_body[:BODY_TRIM] if len(raw_body) > BODY_TRIM else raw_body

    body_full = re.sub(r"<(style|script)\b[^>]*>.*?</\1>", " ", raw_html_body, flags=re.I | re.S)
    body_full = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+", "", body_full, flags=re.I)
    if len(body_full) > BODY_RAW_MAX:
        body_full = body_full[:BODY_RAW_MAX]

    attachments_lite = []
    for att in data.get("attachments") or []:
        if isinstance(att, dict):
            attachments_lite.append({
                "name": att.get("name", ""),
                "contentType": att.get("contentType", ""),
                "size": att.get("size", 0),
                "isInline": bool(att.get("isInline", False)),
            })
        else:
            attachments_lite.append({"name": str(att), "contentType": "", "size": 0, "isInline": False})

    to_bare = data.get("toRecipients") or data.get("to") or ""
    cc_bare = data.get("ccRecipients") or data.get("cc") or ""
    if isinstance(to_bare, list):
        to_bare = ";".join(a.email for a in Address.parse_many(to_bare))
    if isinstance(cc_bare, list):
        cc_bare = ";".join(a.email for a in Address.parse_many(cc_bare))
    recipients = _recipients(to_bare, hmap, "to", "to") + _recipients(cc_bare, hmap, "cc", "cc")

    folder = source_path.parent.name
    return {
        "message_id": data.get("id") or data.get("message_id", ""),
        "conversation_id": data.get("conversationId") or data.get("conversation_id", ""),
        "internet_message_id": data.get("internetMessageId") or "",
        "from_name": from_name,
        "from_email": from_email,
        "to": to_bare,
        "cc": cc_bare,
        "recipients": recipients,
        "subject": data.get("subject", "") or "",
        "body_preview": strip_forward_headers(data.get("bodyPreview") or data.get("body_preview", "") or ""),
        "body": body_trimmed,
        "body_raw": body_full,
        "received_at": data.get("receivedDateTime") or data.get("received_at") or data.get("sent_at", ""),
        "has_attachments": bool(data.get("hasAttachments", data.get("has_attachments", False))),
        "attachments": attachments_lite,
        "importance": data.get("importance", "normal") or "normal",
        "is_read": bool(data.get("isRead", data.get("is_read", True))),
        "source_file": source_path.name,
        "source_folder": folder,
        "source_path": str(source_path),
        "doc_links": doc_links,
        "direction": "outbound" if folder == "sent" else "inbound",
    }


class ScanWarnings:
    def __init__(self):
        self.unreadable: list[str] = []
        self.rescued_late: list[str] = []

    def to_dict(self) -> dict:
        return {"unreadable": list(self.unreadable), "rescued_late": list(self.rescued_late)}


def scan_email_files(folder, since_dt: datetime | None = None, cfg: dict | None = None,
                     warnings: ScanWarnings | None = None) -> list[dict]:
    """Read every record in ``folder``, newest-last by filename.

    A file that cannot be parsed is NOT skipped silently: it stays on disk for
    the next cycle and is listed in ``warnings.unreadable``. The since-cutoff is
    only honoured when the FILE is also old (a producer that writes late must
    not lose a message forever).
    """
    folder = Path(folder)
    warnings = warnings if warnings is not None else ScanWarnings()
    if not folder.is_dir():
        return []
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".json")
    if not files:
        files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.endswith(".tmp"))
    out = []
    for p in files:
        try:
            with p.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("record is not a JSON object")
        except Exception as e:
            warnings.unreadable.append(f"{folder.name}/{p.name} ({type(e).__name__})")
            continue
        received = parse_iso(data.get("receivedDateTime") or data.get("received_at") or data.get("sent_at"))
        if received and since_dt:
            s = since_dt if since_dt.tzinfo else since_dt.replace(tzinfo=timezone.utc)
            if received < s:
                try:
                    written = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
                except OSError:
                    written = None
                if written and written >= s:
                    warnings.rescued_late.append(f"{folder.name}/{p.name}")
                else:
                    continue
        try:
            out.append(normalize_record(data, p, cfg))
        except Exception as e:
            warnings.unreadable.append(f"{folder.name}/{p.name} (normalize {type(e).__name__})")
    return out


# ── processed / archive move ────────────────────────────────────────────────

def processed_dir(mail_cache, when: datetime | None = None) -> Path:
    when = when or datetime.now()
    return Path(mail_cache) / "processed" / when.strftime("%Y-%m-%d")


def move_to_processed(mail_cache, email_obj: dict, status: str = "handled") -> str | None:
    """Move a handled source file (and its attachment folder) out of the live
    inbox/ or sent/ folder. Returns the new path or None."""
    src = Path(email_obj.get("source_path") or "")
    if not src.is_file():
        return None
    dest_dir = processed_dir(mail_cache) / (email_obj.get("source_folder") or "misc")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.stem}-{status}-{int(datetime.now().timestamp())}{src.suffix}"
    shutil.move(str(src), str(dest))
    mid = email_obj.get("message_id") or ""
    att_src = Path(mail_cache) / "attachments" / mid
    if mid and att_src.is_dir():
        att_dest = processed_dir(mail_cache) / "attachments" / mid
        if not att_dest.exists():
            att_dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(att_src), str(att_dest))
            except Exception:
                pass
    return str(dest)


def purge_processed(mail_cache, keep_days: int = PROCESSED_KEEP_DAYS) -> int:
    root = Path(mail_cache) / "processed"
    if not root.is_dir():
        return 0
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    purged = 0
    for d in root.iterdir():
        if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", d.name) and d.name < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            purged += 1
    return purged


# ── capture-health sidecar ──────────────────────────────────────────────────

def write_capture_status(mail_cache, summary: dict, warnings: ScanWarnings) -> Path:
    path = Path(mail_cache) / "capture_status.json"
    doc = {
        "last_cycle": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        **warnings.to_dict(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass
    return path
