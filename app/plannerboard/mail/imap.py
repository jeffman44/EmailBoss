"""IMAP mail provider.

Ported nearly verbatim from v2/imap_capture.py:
  * presets                         lines 106-146
  * header / MIME helpers            lines 348-598
  * message_to_record                lines 601-686
  * IMAP conversation (connect, select, BODY.PEEK fetch, UID search,
    per-folder pass with UIDVALIDITY reset)  lines 818-961
  * run_once / test_connection / selftest   lines 964-1232

What changed: the module no longer imports config at import time (settings
arrive as a dict), the state file is a ``ProviderState``, the password comes
from ``mail.secrets`` (key ``imap_password``, account = the IMAP user), and an
XOAUTH2 hook was added (``auth_mode: password | xoauth2``). The capture path
never sends, never deletes, never marks a message read (BODY.PEEK) and never
writes the database. It reads two folders and yields Message objects. The
mail-client half (``_ImapClient``, bottom of this file) is what a person
drives from the MAIL view; it is the only place STORE / MOVE / APPEND and
SMTP happen.

Inline images: every MIME part that is not the body is captured, inline
included, so a message whose only pictures are inline still has its files on
disk (the drop-folder producer's ``hasAttachments`` branch lost those).
"""
from __future__ import annotations

import base64
import imaplib
import mimetypes
import re
import socket
import time
import ssl
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from typing import Iterator

from . import secrets
from .base import BaseProvider, ConnResult, ProviderState, now_local_iso
from .message import (Address, Message, PA_REQUIRED_KEYS, PA_OPTIONAL_KEYS,
                      BODY_MAX, iso_utc, make_preview, safe_filename, safe_id,
                      text_from_html, thread_key_from_ids, validate_record)

DEFAULT_MAX_PER_CYCLE = 50
DEFAULT_INITIAL_DAYS = 30

# ── Provider presets (imap_capture 106-141) ─────────────────────────────────
PRESETS = {
    "gmail": {
        "label": "Gmail / Google Workspace (IMAP)",
        "host": "imap.gmail.com", "port": 993,
        "inbox_folder": "INBOX", "sent_folder": "[Gmail]/Sent Mail",
        "app_password_hint": "Google account > Security > 2-Step Verification > "
                             "App passwords (a 16-character password, not your "
                             "normal one).",
    },
    "outlook": {
        "label": "Outlook.com / Hotmail / Microsoft 365 (IMAP)",
        "host": "outlook.office365.com", "port": 993,
        "inbox_folder": "INBOX", "sent_folder": "Sent",
        "app_password_hint": "account.microsoft.com > Security > Advanced "
                             "security options > App passwords (requires "
                             "two-step verification to be on).",
    },
    "icloud": {
        "label": "iCloud Mail",
        "host": "imap.mail.me.com", "port": 993,
        "inbox_folder": "INBOX", "sent_folder": "Sent Messages",
        "app_password_hint": "appleid.apple.com > Sign-In and Security > "
                             "App-Specific Passwords.",
    },
    "custom": {
        "label": "Other IMAP server",
        "host": "", "port": 993,
        "inbox_folder": "INBOX", "sent_folder": "Sent",
        "app_password_hint": "Whatever your provider calls an application or "
                             "device password. Many hosts accept the normal "
                             "mailbox password.",
    },
}


def preset_list():
    """Public metadata for the setup wizard. No secrets, no paths."""
    return [{"id": k, **v} for k, v in PRESETS.items()]


def settings_from_config(mail_cfg: dict | None) -> dict:
    """Resolve effective IMAP settings: preset overlaid by explicit values.
    Returns a plain dict, never a secret. (imap_capture 201-222)"""
    raw = dict((mail_cfg or {}).get("imap") or {})
    preset_id = (raw.get("preset") or raw.get("provider") or "custom").strip().lower()
    preset = PRESETS.get(preset_id, PRESETS["custom"])
    out = {
        "preset": preset_id if preset_id in PRESETS else "custom",
        "host": (raw.get("host") or preset["host"] or "").strip(),
        "port": int(raw.get("port") or preset["port"] or 993),
        "user": (raw.get("user") or "").strip(),
        "inbox_folder": (raw.get("inbox") or raw.get("inbox_folder") or preset["inbox_folder"]).strip(),
        "sent_folder": (raw.get("sent") or raw.get("sent_folder") or preset["sent_folder"]).strip(),
        "initial_days": int(raw.get("initial_days") or DEFAULT_INITIAL_DAYS),
        "max_per_cycle": int(raw.get("max_per_cycle") or DEFAULT_MAX_PER_CYCLE),
        "ssl": bool(raw.get("ssl", True)),
        "allow_insecure": raw.get("allow_insecure", False) is True,
        "auth_mode": (raw.get("auth_mode") or "password").strip().lower(),
    }
    return out


# ── Header / MIME helpers (imap_capture 348-598) ────────────────────────────

def _decode(value):
    """Decode an RFC 2047 header to str. Never raises."""
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        try:
            return str(value)
        except Exception:
            return ""


def _addresses(msg, header):
    """The ``;``-joined bare-address string the PA record carries."""
    try:
        raw = msg.get_all(header, [])
        if not raw:
            return ""
        addrs = [a for _n, a in getaddresses([str(r) for r in raw]) if a]
        seen, out = set(), []
        for a in addrs:
            k = a.lower()
            if k not in seen:
                seen.add(k)
                out.append(a)
        return ";".join(out)
    except Exception:
        return ""


def _importance(msg):
    try:
        imp = (str(msg.get("Importance", "")) or "").strip().lower()
        if imp in ("high", "low", "normal"):
            return imp
        prio = (str(msg.get("X-Priority", "")) or "").strip()
        if prio[:1] in ("1", "2"):
            return "high"
        if prio[:1] in ("4", "5"):
            return "low"
    except Exception:
        pass
    return "normal"


def _content_id(part):
    cid = part.get("Content-ID")
    if not cid:
        return None
    cid = str(cid).strip()
    if cid.startswith("<") and cid.endswith(">"):
        cid = cid[1:-1]
    return cid or None


def thread_key(msg, from_addr="", to_addrs=""):
    refs = ""
    try:
        refs = " ".join(str(r) for r in msg.get_all("References", []))
    except Exception:
        refs = ""
    return thread_key_from_ids(
        refs, str(msg.get("In-Reply-To") or ""), str(msg.get("Message-ID") or ""),
        _decode(msg.get("Subject")), (from_addr + ";" + to_addrs).split(";"))


def _headers_list(msg, limit=200):
    out = []
    try:
        for name, value in msg.items():
            out.append({"name": str(name), "value": _decode(value)[:2000]})
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def _payload_text(part):
    try:
        raw = part.get_payload(decode=True)
        if raw is None:
            return str(part.get_payload() or "")
        charset = part.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _body_parts(msg):
    """(html, text, body_part_ids): the parts that ARE the body."""
    html, text, used = "", "", set()
    try:
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp or part.get_filename():
                continue
            ctype = part.get_content_type()
            if ctype == "text/html" and not html:
                html = _payload_text(part)
                used.add(id(part))
            elif ctype == "text/plain" and not text:
                text = _payload_text(part)
                used.add(id(part))
    except Exception:
        pass
    return html, text, used


def _collect_attachments(msg, body_part_ids, record_id, stamp):
    """EVERY part that is not the body, inline images INCLUDED."""
    out, names, idx = [], {}, 0
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if id(part) in body_part_ids:
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if payload is None:
            continue
        ctype = part.get_content_type() or "application/octet-stream"
        disp = str(part.get("Content-Disposition") or "").lower()
        cid = _content_id(part)
        raw_name = _decode(part.get_filename())
        if not raw_name:
            ext = mimetypes.guess_extension(ctype) or ".bin"
            base = (cid or f"part{idx + 1}").split("@")[0]
            raw_name = f"{safe_filename(base, 'part')}{ext}"
        name = safe_filename(raw_name)
        if name in names:
            names[name] += 1
            stem, dot, ext = name.rpartition(".")
            name = (f"{stem}-{names[name]}{dot}{ext}" if dot else f"{name}-{names[name]}")
        else:
            names[name] = 1
        idx += 1
        out.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": f"{record_id}-{idx}",
            "lastModifiedDateTime": stamp,
            "name": name,
            "contentType": ctype,
            "size": len(payload),
            "isInline": bool(cid) or "inline" in disp,
            "contentId": cid,
            "contentBytes": base64.b64encode(payload).decode("ascii"),
        })
    return out


# ── The conversion (imap_capture 601-686) ───────────────────────────────────

def message_to_record(raw_bytes, folder_kind="inbox", uidvalidity=0, uid=0,
                      flags=(), internaldate=None):
    """One RFC-822 message -> one PA-shaped record. Never raises on a
    malformed message: a card with a bad subject beats an email nobody sees."""
    try:
        msg = message_from_bytes(raw_bytes, policy=policy.default)
    except Exception:
        msg = message_from_bytes(raw_bytes)

    received = None
    try:
        d = msg.get("Date")
        if d:
            received = parsedate_to_datetime(str(d))
    except Exception:
        received = None
    if received is None and internaldate is not None:
        try:
            received = (internaldate if isinstance(internaldate, datetime)
                        else datetime.fromtimestamp(internaldate, timezone.utc))
        except Exception:
            received = None
    if received is None:
        received = datetime.now(timezone.utc)
    stamp = iso_utc(received)

    from_addr = _addresses(msg, "From")
    to_addrs = _addresses(msg, "To")
    cc_addrs = _addresses(msg, "Cc")
    reply_to = _addresses(msg, "Reply-To")

    internet_id = str(msg.get("Message-ID") or "").strip()
    if internet_id:
        record_id = safe_id(internet_id)
    else:
        record_id = safe_id(f"{folder_kind}:{uidvalidity}:{uid}", prefix=folder_kind)

    html, text, body_ids = _body_parts(msg)
    is_html = bool(html)
    body = html if is_html else text
    if len(body) > BODY_MAX:
        body = body[:BODY_MAX]
    preview_src = text or text_from_html(html)

    attachments = _collect_attachments(msg, body_ids, record_id, stamp)
    flag_blob = " ".join(str(f) for f in (flags or []))

    record = {
        "id": record_id,
        "receivedDateTime": stamp,
        "hasAttachments": any(not a["isInline"] for a in attachments),
        "internetMessageId": internet_id,
        "subject": _decode(msg.get("Subject")),
        "bodyPreview": make_preview(preview_src),
        "importance": _importance(msg),
        "conversationId": thread_key(msg, from_addr, to_addrs),
        "isRead": ("\\Seen" in flag_blob) or folder_kind == "sent",
        "internetMessageHeaders": _headers_list(msg),
        "isHtml": is_html,
        "body": body,
        "from": from_addr,
        "toRecipients": to_addrs,
        "attachments": attachments,
    }
    if cc_addrs:
        record["ccRecipients"] = cc_addrs
    if reply_to:
        record["replyTo"] = reply_to
    return record


# ── IMAP conversation (imap_capture 818-961) ────────────────────────────────

def _xoauth2_string(user: str, token: str) -> bytes:
    return f"user={user}\x01auth=Bearer {token}\x01\x01".encode("utf-8")


def connect(settings: dict, password: str | None, timeout=45, oauth_token: str | None = None):
    """Log in. Raises on failure; every caller wraps it."""
    host, port = settings["host"], int(settings["port"])
    if not host:
        raise ValueError("no IMAP host configured")
    if settings.get("ssl", True):
        try:
            conn = imaplib.IMAP4_SSL(host, port, timeout=timeout)
        except TypeError:
            socket.setdefaulttimeout(timeout)
            conn = imaplib.IMAP4_SSL(host, port)
    else:
        conn = imaplib.IMAP4(host, port, timeout=timeout)
        # Never LOGIN in the clear: upgrade with STARTTLS, or refuse unless the
        # config explicitly says allow_insecure: true.
        caps = {(c.decode() if isinstance(c, bytes) else str(c)).upper() for c in (getattr(conn, "capabilities", None) or ())}
        if "STARTTLS" in caps:
            conn.starttls(ssl_context=ssl.create_default_context())
        elif settings.get("allow_insecure") is not True:
            _close(conn)
            raise ValueError("Email Boss will not send your password over an unencrypted connection. "
                             "Turn on SSL for the incoming server (or set allow_insecure: true).")
    if (settings.get("auth_mode") or "password") == "xoauth2":
        if not oauth_token:
            raise ValueError("auth_mode is xoauth2 but no access token is available")
        conn.authenticate("XOAUTH2", lambda _c: _xoauth2_string(settings["user"], oauth_token))
    else:
        conn.login(settings["user"], password)
    return conn


def _quote_folder(folder: str) -> str:
    return f'"{folder}"' if (" " in folder or "[" in folder) else folder


def _select(conn, folder):
    typ, data = conn.select(_quote_folder(folder), readonly=True)
    if typ != "OK":
        raise RuntimeError(f"cannot open folder {folder!r}: "
                           f"{(data[0] if data else b'').decode('utf-8', 'replace')}")
    typ, uv = conn.response("UIDVALIDITY")
    try:
        return int(uv[0])
    except Exception:
        return 0


_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
FLAG_WINDOW = 2000


def _fetch_one(conn, uid):
    """(raw_bytes, flags) for one UID. BODY.PEEK never marks a message read."""
    typ, data = conn.uid("FETCH", str(uid), "(FLAGS BODY.PEEK[])")
    if typ != "OK":
        # A NO / BAD is a failed fetch (retried), never "no such message".
        raise RuntimeError(f"UID FETCH {uid} -> {typ}: "
                           f"{(data[0] if data else b'')!r}"[:200])
    if not data:
        return None, []
    raw, flags = None, []
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2:
            head, body = item[0], item[1]
            if isinstance(body, (bytes, bytearray)) and len(body) > 0:
                raw = bytes(body)
            m = _FLAGS_RE.search(head if isinstance(head, bytes) else b"")
            if m:
                flags = m.group(1).decode("ascii", "replace").split()
        elif isinstance(item, (bytes, bytearray)):
            m = _FLAGS_RE.search(bytes(item))
            if m:
                flags = m.group(1).decode("ascii", "replace").split()
    return raw, flags


_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')


def _internaldate_ts(head: bytes) -> float | None:
    m = _INTERNALDATE_RE.search(head or b"")
    if not m:
        return None
    try:
        import imaplib as _il
        tt = _il.Internaldate2tuple(b'INTERNALDATE "' + m.group(1) + b'"')
        return time.mktime(tt) if tt else None
    except Exception:
        return None


def _fetch_one_dated(conn, uid, part="BODY.PEEK[]"):
    """(raw, flags, server_receive_ts) for one UID; raises on NO / BAD."""
    typ, data = conn.uid("FETCH", str(uid), f"(FLAGS INTERNALDATE {part})")
    if typ != "OK":
        raise RuntimeError(f"UID FETCH {uid} -> {typ}: {(data[0] if data else b'')!r}"[:200])
    raw, flags, ts = None, [], None
    for item in data or []:
        head = item[0] if isinstance(item, tuple) else item
        head = bytes(head) if isinstance(head, (bytes, bytearray)) else b""
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)) and item[1]:
            raw = bytes(item[1])
        m = _FLAGS_RE.search(head)
        if m:
            flags = m.group(1).decode("ascii", "replace").split()
        ts = ts or _internaldate_ts(head)
    return raw, flags, ts


def _search_uids(conn, last_uid, initial_days):
    """UIDs to fetch, ascending. ``UID n:*`` always returns at least one
    message, so the result is filtered on the way out."""
    if last_uid:
        typ, data = conn.uid("SEARCH", None, f"UID {int(last_uid) + 1}:*")
    else:
        since = (datetime.now(timezone.utc) -
                 timedelta(days=max(0, int(initial_days)))).strftime("%d-%b-%Y")
        typ, data = conn.uid("SEARCH", None, f"(SINCE {since})")
    if typ != "OK" or not data:
        return []
    uids = []
    for tok in (data[0] or b"").split():
        try:
            n = int(tok)
        except ValueError:
            continue
        if n > int(last_uid or 0):
            uids.append(n)
    return sorted(uids)


class _ImapCapture(BaseProvider):
    """The ported capture path (read-only). ``ImapProvider`` below adds the
    mail-client methods on top."""
    id = "imap"
    label = "IMAP"
    required_fields = [
        {"key": "preset", "label": "Mail service", "type": "select",
         "options": [{"id": k, "label": v["label"]} for k, v in PRESETS.items()]},
        {"key": "host", "label": "IMAP host", "type": "text", "preset": True},
        {"key": "port", "label": "Port", "type": "number", "default": 993},
        {"key": "user", "label": "Mailbox address", "type": "email"},
        {"key": "password", "label": "App password", "type": "secret",
         "secret_key": "imap_password"},
        {"key": "inbox", "label": "Inbox folder", "type": "text", "default": "INBOX"},
        {"key": "sent", "label": "Sent folder", "type": "text", "preset": True},
        {"key": "auth_mode", "label": "Auth", "type": "select",
         "options": [{"id": "password", "label": "App password"},
                     {"id": "xoauth2", "label": "OAuth2 (XOAUTH2)"}], "default": "password"},
    ]

    def __init__(self, cfg: dict, cache_dir=None, token_provider=None):
        """``cfg`` is the whole config dict (or just its ``mail`` block).
        ``token_provider`` is a zero-arg callable returning an OAuth access
        token for XOAUTH2 (the Gmail / Graph providers can supply one)."""
        super().__init__()
        mail_cfg = cfg.get("mail", cfg) if isinstance(cfg, dict) else {}
        self.settings = settings_from_config(mail_cfg)
        self.cache_dir = cache_dir
        self.token_provider = token_provider
        self.last_summary: dict | None = None

    # ── credentials ──
    def _password(self):
        if self.settings.get("auth_mode") == "xoauth2":
            return None, None
        pw, src = secrets.get_secret("imap_password", account=self.settings.get("user"))
        if not pw:
            pw, src = secrets.get_secret("imap_password")
        return pw, src

    def _oauth_token(self):
        if self.settings.get("auth_mode") != "xoauth2":
            return None
        if self.token_provider:
            try:
                return self.token_provider()
            except Exception as e:
                self.note_warning(f"xoauth2 token provider failed: {e}")
        tok, _src = secrets.get_secret("imap_oauth_token", account=self.settings.get("user"))
        return tok

    def _connect(self, timeout=45):
        pw, src = self._password()
        tok = self._oauth_token()
        if self.settings.get("auth_mode") != "xoauth2" and not pw:
            raise ValueError(
                "no IMAP password available; nothing in the OS credential store for "
                f"{self.settings.get('user')} and {secrets.env_name('imap_password')} is not set")
        if src == "env":
            self.note_warning("IMAP password taken from the environment, not the credential store")
        return connect(self.settings, pw, timeout=timeout, oauth_token=tok)

    # ── MailProvider ──
    def test_connection(self) -> ConnResult:
        """Log in, open both folders, count messages. Writes nothing."""
        s = self.settings
        detail = {"host": s.get("host"), "port": s.get("port"), "user": s.get("user"), "folders": {}}
        conn = None
        try:
            conn = self._connect(timeout=25)
            for kind, folder in (("inbox", s.get("inbox_folder")), ("sent", s.get("sent_folder"))):
                if not folder:
                    continue
                entry = {"folder": folder}
                try:
                    typ, data = conn.select(_quote_folder(folder), readonly=True)
                    if typ != "OK":
                        raise RuntimeError((data[0] if data else b"").decode("utf-8", "replace") or "SELECT failed")
                    entry["messages"] = int((data[0] or b"0").decode() or 0)
                    entry["ok"] = True
                except Exception as fe:
                    entry["ok"] = False
                    entry["error"] = f"{type(fe).__name__}: {fe}"
                    try:
                        typ, boxes = conn.list()
                        if typ == "OK":
                            entry["available"] = [
                                b.decode("utf-8", "replace").split(' "/" ')[-1].strip('"')
                                for b in (boxes or [])[:60]]
                    except Exception:
                        pass
                detail["folders"][kind] = entry
            ok = bool(detail["folders"]) and all(f.get("ok") for f in detail["folders"].values())
            return ConnResult(ok=ok, detail=detail,
                              error=None if ok else "one or more folders could not be opened")
        except imaplib.IMAP4.error as e:
            return ConnResult(ok=False, detail=detail, error=f"login rejected: {e}")
        except Exception as e:
            return ConnResult(ok=False, detail=detail, error=f"{type(e).__name__}: {e}")
        finally:
            _close(conn)

    def _folder_pass(self, conn, folder_kind, folder_name, state: ProviderState, limit):
        """Fetch one folder, then reconcile it. Yields Message."""
        prior = state.folder(folder_kind)
        uidvalidity = _select(conn, folder_name)
        last_uid = int(prior.get("last_uid") or 0)
        if prior.get("uidvalidity") and int(prior["uidvalidity"]) != uidvalidity:
            self.note_warning(f"UIDVALIDITY changed {prior['uidvalidity']} -> {uidvalidity} "
                              f"on {folder_name}; watermark reset, folder re-indexed")
            last_uid = 0
            self._retire_epoch(folder_kind, uidvalidity)
        if prior.get("folder") and prior["folder"] != folder_name:
            self.note_warning(f"folder renamed {prior['folder']!r} -> {folder_name!r}; watermark reset")
            last_uid = 0
            # Sync ids carry no mailbox name: the new mailbox's UIDs must not
            # match the old one's rows, even when UIDVALIDITY is the same.
            self._retire_epoch(folder_kind, None)
        uids = _search_uids(conn, last_uid, self.settings["initial_days"])
        cap = max(1, min(int(limit or 0) or self.settings["max_per_cycle"], self.settings["max_per_cycle"]))
        more = len(uids) > cap
        uids = uids[:cap]
        highest = last_uid
        sync = self.sync
        for uid in uids:
            sid = f"{folder_kind}:{uidvalidity}:{uid}"
            highest = max(highest, uid)
            if sync is not None and sync.captured(self.account_id, sid, live_only=True):
                continue
            try:
                msg = self._uid_to_message(conn, folder_kind, uidvalidity, uid)
            except Exception as e:
                # ONE bad message never strands the folder behind it, and it is
                # retried later instead of being skipped forever.
                self.fetch_failed(sid, folder_kind, e)
                continue
            if msg is not None:
                yield msg
        state.data.setdefault("folders", {})[folder_kind] = {
            "folder": folder_name, "uidvalidity": uidvalidity,
            "last_uid": highest, "updated_at": now_local_iso(),
        }
        if more:
            self.note_warning(f"{folder_name}: more than {cap} new messages, continuing next cycle")
        try:
            self._reconcile_folder(conn, folder_kind, uidvalidity)
        except Exception as e:
            self.note_warning(f"{folder_name}: change check failed ({type(e).__name__})")

    def _uid_to_message(self, conn, folder_kind, uidvalidity, uid):
        raw, flags, server_ts = _fetch_one_dated(conn, uid)
        if not raw:
            return None
        record = message_to_record(raw, folder_kind=folder_kind,
                                   uidvalidity=uidvalidity, uid=uid, flags=flags)
        problems = validate_record(record)
        if problems:
            self.note_warning(f"record shape warning uid={uid}: {problems[:2]}")
        msg = Message.from_pa_record(record, folder_kind)
        msg.provider, msg.provider_ref = self.id, f"{folder_kind}:{uidvalidity}:{uid}"
        msg.sync_id, msg.sync_folder, msg.sync_flags = msg.provider_ref, folder_kind, list(flags or [])
        if server_ts:
            msg.sync_received_ts = server_ts
        return msg

    def _retire_epoch(self, folder_kind: str, uidvalidity: int | None) -> int:
        """UIDVALIDITY changed: every UID of the old epoch is meaningless now.
        Mark those ledger rows removed and drop their queued retries, so the
        re-index that follows is the only copy the store counts."""
        sync = self.sync
        if sync is None:
            return 0
        keep = f"{folder_kind}:{int(uidvalidity)}:" if uidvalidity is not None else None
        n = 0
        for sid in sync.live_ids(self.account_id, prefix=f"{folder_kind}:"):
            if keep is None or not sid.startswith(keep):
                sync.mark_removed(self.account_id, sid, "uidvalidity reset" if keep else "folder changed")
                n += 1
        if self.retry is not None:
            for sid in self.retry.pending_ids():
                if sid.startswith(f"{folder_kind}:") and (keep is None or not sid.startswith(keep)):
                    self.retry.done(sid)
        return n

    def _reconcile_folder(self, conn, folder_kind: str, uidvalidity: int) -> dict:
        """Deletion and flag propagation without CONDSTORE: compare the
        ledger's live UIDs for this epoch with ``UID SEARCH ALL`` (expunged on
        the server = removed here) and refresh FLAGS for the most recent
        ``FLAG_WINDOW`` of them."""
        sync = self.sync
        out = {"removed": 0, "flags_changed": 0}
        if sync is None:
            return out
        prefix = f"{folder_kind}:{int(uidvalidity)}:"
        live = {}
        for sid in sync.live_ids(self.account_id, prefix=prefix):
            try:
                live[int(sid.rsplit(":", 1)[1])] = sid
            except ValueError:
                continue
        if not live:
            return out
        typ, data = conn.uid("SEARCH", None, "ALL")
        if typ != "OK":
            raise RuntimeError("UID SEARCH ALL failed")
        on_server = set()
        for tok in (data[0] or b"").split() if data else []:
            try:
                on_server.add(int(tok))
            except ValueError:
                continue
        for uid, sid in live.items():
            if uid not in on_server:
                sync.mark_removed(self.account_id, sid, "expunged on server")
                out["removed"] += 1
        window = sorted(u for u in live if u in on_server)[-FLAG_WINDOW:]
        for i in range(0, len(window), 500):
            chunk = window[i:i + 500]
            typ, data = conn.uid("FETCH", ",".join(str(u) for u in chunk), "(FLAGS)")
            if typ != "OK":
                continue
            for item in data or []:
                head = item[0] if isinstance(item, tuple) else item
                if not isinstance(head, (bytes, bytearray)):
                    continue
                m_uid = _UID_RE.search(bytes(head))
                m_fl = _FLAGS_RE.search(bytes(head))
                if not m_uid or not m_fl:
                    continue
                uid = int(m_uid.group(1))
                sid = live.get(uid)
                if not sid:
                    continue
                flags = sorted(m_fl.group(1).decode("ascii", "replace").split())
                row = sync.get(self.account_id, sid)
                if row is not None and sorted(row["flags"]) != flags:
                    sync.set_flags(self.account_id, sid, flags)
                    out["flags_changed"] += 1
        return out

    def fetch_new(self, state: ProviderState, limit: int = 200) -> Iterator[Message]:
        """One capture cycle. Never raises; errors land in health()."""
        s = self.settings
        n = 0
        errors = []
        conn = None
        try:
            if not s.get("user") or not s.get("host"):
                raise ValueError("IMAP is enabled but no host/user is configured")
            conn = self._connect()
            self._cycle_conn = conn
            for msg in self._drain_retries():
                n += 1
                yield msg
            for kind, folder in (("inbox", s["inbox_folder"]), ("sent", s["sent_folder"])):
                if not folder:
                    continue
                try:
                    for msg in self._folder_pass(conn, kind, folder, state, limit):
                        n += 1
                        yield msg
                except Exception as fe:
                    # A missing/renamed SENT folder must not cost us the INBOX.
                    errors.append(f"{kind} ({folder}): {type(fe).__name__}: {fe}")
            state.save()
            self._record_fetch(n, "; ".join(errors) or None)
        except Exception as e:
            self._record_fetch(n, f"{type(e).__name__}: {e}")
        finally:
            self._cycle_conn = None
            _close(conn)
        self.last_summary = {"fetched": n, "errors": errors}

    def _folder_for_kind(self, kind: str) -> str | None:
        return {"inbox": self.settings.get("inbox_folder"),
                "sent": self.settings.get("sent_folder")}.get(kind)

    def fetch_item(self, item_id: str, folder: str = "") -> Message | None:
        """Retry one ``kind:uidvalidity:uid``. A different epoch means the UID
        no longer names that message: report it gone."""
        try:
            kind, uv, uid = item_id.split(":", 2)
            uv, uid = int(uv), int(uid)
        except ValueError:
            return None
        name = self._folder_for_kind(kind)
        if not name:
            return None
        conn = getattr(self, "_cycle_conn", None)
        own = conn is None
        if own:
            conn = self._connect()
        try:
            if _select(conn, name) != uv:
                return None
            return self._uid_to_message(conn, kind, uv, uid)
        finally:
            if own:
                _close(conn)

    def backfill_page(self, cursor, limit: int = 200, body_since_ts: float | None = None):
        """One page of the full mailbox: every selectable folder, newest UIDs
        first, headers only (BODY.PEEK, never sets \\Seen) except inside the
        body window. Inbox and sent use the capture ids (``kind:uv:uid``) so
        the two paths dedupe; other folders use ``x:<folder>:uv:uid``."""
        import json as _json
        st = _json.loads(cursor) if cursor else None
        if st is None:
            folders = []
            for f in self.list_folders():
                if f.get("noselect"):
                    continue
                folders.append({"id": f["id"], "kind": f.get("kind") or "other"})
            st = {"folders": folders, "fi": 0, "below": None}
        out = []
        while st["fi"] < len(st["folders"]) and not out:
            f = st["folders"][st["fi"]]
            name, kind = f["id"], f["kind"]
            capture_kind = {self.settings.get("inbox_folder"): "inbox",
                            self.settings.get("sent_folder"): "sent"}.get(name)
            with self._client() as conn:
                uv = self._open(conn, name, readonly=True)
                cache_key = (name, uv)
                cached = getattr(self, "_bf_uids", None)
                if not cached or cached[0] != cache_key:
                    # One UID SEARCH ALL per folder walk, not per page.
                    typ, data = conn.uid("SEARCH", None, "ALL")
                    allu = sorted((int(t) for t in ((data or [b""])[0] or b"").split() if t.isdigit()), reverse=True)
                    self._bf_uids = (cache_key, allu)
                uids = list(self._bf_uids[1])
                if st["below"] is not None:
                    uids = [u for u in uids if u < st["below"]]
                batch = uids[:max(1, int(limit or 100))]
                for uid in batch:
                    sid = f"{capture_kind}:{uv}:{uid}" if capture_kind else f"x:{name}:{uv}:{uid}"
                    if self.sync is not None and self.sync.indexed(self.account_id, sid):
                        continue
                    try:
                        msg = self._backfill_one(conn, uid, uv, sid, capture_kind or kind, body_since_ts)
                    except Exception as e:  # noqa: BLE001
                        self.backfill_missed.append(sid)
                        self.note_warning(f"backfill: some messages could not be read ({type(e).__name__})")
                        continue
                    if msg is not None:
                        out.append(msg)
                if len(uids) > len(batch):
                    st["below"] = batch[-1]
                else:
                    st["fi"] += 1
                    st["below"] = None
        done = st["fi"] >= len(st["folders"])
        return out, (None if done else _json.dumps(st)), done

    def _backfill_one(self, conn, uid, uv, sid, kind, body_since_ts):
        raw, flags, server_ts = _fetch_one_dated(conn, uid, "BODY.PEEK[HEADER]")
        if not raw:
            return None
        fk = kind if kind in ("inbox", "sent") else "inbox"
        rec = message_to_record(raw, folder_kind=fk, uidvalidity=uv, uid=uid, flags=flags)
        msg = Message.from_pa_record(rec, fk)
        ts = server_ts or (msg.received_at.timestamp() if msg.received_at else 0)
        header_only = True
        if body_since_ts is None or ts >= body_since_ts:
            full, flags2, ts2 = _fetch_one_dated(conn, uid)
            if full:
                rec = message_to_record(full, folder_kind=fk, uidvalidity=uv, uid=uid, flags=flags2 or flags)
                msg = Message.from_pa_record(rec, fk)
                header_only = False
                server_ts = server_ts or ts2
        if header_only:
            head = raw.split(b"\r\n\r\n", 1)[0]
            msg.sync_has_attachments = bool(re.search(rb"(?im)^content-type:\s*multipart/mixed", head))
        msg.provider, msg.provider_ref = self.id, sid
        msg.sync_id, msg.sync_folder, msg.sync_flags = sid, kind, list(flags or [])
        if server_ts:
            msg.sync_received_ts = server_ts
        return msg

    def server_counts(self) -> dict:
        out = {}
        conn = self._connect(timeout=25)
        try:
            for kind in ("inbox", "sent"):
                name = self._folder_for_kind(kind)
                if not name:
                    continue
                typ, data = conn.status(_quote_folder(name), "(MESSAGES)")
                m = re.search(rb"MESSAGES (\d+)", (data[0] if data else b"") or b"")
                if typ == "OK" and m:
                    out[kind] = int(m.group(1))
        finally:
            _close(conn)
        return out

    def health(self) -> dict:
        h = self.base_health()
        h.update({"host": self.settings.get("host"), "user": self.settings.get("user"),
                  "auth_mode": self.settings.get("auth_mode"), "last_summary": self.last_summary})
        return h


def _close(conn):
    if conn is None:
        return
    for meth in ("close", "logout"):
        try:
            getattr(conn, meth)()
        except Exception:
            pass


# ── Selftest, no network (imap_capture 1133-1232) ───────────────────────────

def _synthetic_message():
    """multipart/related+mixed: HTML body, one INLINE image, one PDF."""
    from email.message import EmailMessage
    m = EmailMessage()
    m["Message-ID"] = "<abc123$def@mail.example.com>"
    m["Date"] = "Tue, 02 Sep 2026 15:57:06 -0600"
    m["Subject"] = "Unit 3 photos attached"
    m["From"] = "Sender Name <sender@example.com>"
    m["To"] = "user@example.com, second@example.com"
    m["Cc"] = "cc@example.com"
    m["References"] = "<root@example.com> <second@example.com>"
    m["In-Reply-To"] = "<second@example.com>"
    m["Importance"] = "high"
    m.set_content("Plain text version of the note.")
    m.add_alternative("<html><body><p>Look at this "
                      '<img src="cid:photo1" size="150273"></p></body></html>',
                      subtype="html")
    m.get_payload()[1].add_related(b"\x89PNG\r\n\x1a\nFAKEIMAGEBYTES",
                                   maintype="image", subtype="png",
                                   cid="<photo1>", filename="roof.png")
    m.add_attachment(b"%PDF-1.4 fake pdf bytes", maintype="application",
                     subtype="pdf", filename="Vendor Quote #12/34.pdf")
    return m.as_bytes()


def selftest(strict=True):
    fails = []
    rec = message_to_record(_synthetic_message(), folder_kind="inbox",
                            uidvalidity=7, uid=42, flags=["\\Seen"])
    fails.extend(validate_record(rec))
    if set(rec) - PA_OPTIONAL_KEYS != PA_REQUIRED_KEYS:
        fails.append(f"key set differs: {sorted(set(rec) - PA_OPTIONAL_KEYS)}")
    if rec["receivedDateTime"] != "2026-09-02T21:57:06+00:00":
        fails.append(f"receivedDateTime wrong: {rec['receivedDateTime']}")
    if rec["id"] != "abc123_def@mail.example.com":
        fails.append(f"id not sanitised for a Windows path: {rec['id']!r}")
    if rec["internetMessageId"] != "<abc123$def@mail.example.com>":
        fails.append("internetMessageId must be the raw header")
    if rec["from"] != "sender@example.com":
        fails.append(f"from must be a bare address string: {rec['from']!r}")
    if rec["toRecipients"] != "user@example.com;second@example.com":
        fails.append(f"toRecipients must be ';'-joined: {rec['toRecipients']!r}")
    if rec.get("ccRecipients") != "cc@example.com":
        fails.append("ccRecipients missing")
    if not rec["isHtml"] or "<html>" not in rec["body"]:
        fails.append("HTML body not selected")
    if "Plain text version" not in rec["bodyPreview"]:
        fails.append(f"bodyPreview should come from the text part: {rec['bodyPreview']!r}")
    if rec["importance"] != "high":
        fails.append("importance not read from the Importance header")
    if not rec["isRead"]:
        fails.append("\\Seen not honoured")
    if len(rec["attachments"]) != 2:
        fails.append(f"expected 2 attachments, got {len(rec['attachments'])}")
    else:
        inline = [a for a in rec["attachments"] if a["isInline"]]
        files = [a for a in rec["attachments"] if not a["isInline"]]
        if len(inline) != 1 or inline[0]["contentId"] != "photo1":
            fails.append("inline image not captured with its bare contentId")
        if len(files) != 1 or not files[0]["name"].endswith(".pdf"):
            fails.append("pdf attachment not captured")
        if files and "/" in files[0]["name"]:
            fails.append("attachment filename not sanitised for a path")
        if files and base64.b64decode(files[0]["contentBytes"]) != b"%PDF-1.4 fake pdf bytes":
            fails.append("contentBytes round-trip failed")
    if rec["hasAttachments"] is not True:
        fails.append("hasAttachments should be True with a real attachment")
    rec2 = message_to_record(_synthetic_message(), folder_kind="sent", uidvalidity=7, uid=43)
    if rec2["conversationId"] != rec["conversationId"]:
        fails.append("conversationId is not stable across the same thread")
    if not rec2["isRead"]:
        fails.append("sent mail should read as isRead")
    bare = b"Subject: no id\r\nFrom: a@b.com\r\nTo: c@d.com\r\n\r\nhi\r\n"
    r3 = message_to_record(bare, folder_kind="sent", uidvalidity=9, uid=5)
    if any(c in r3["id"] for c in '<>:"/\\|?*'):
        fails.append(f"fallback id is not path-safe: {r3['id']!r}")
    if validate_record(r3):
        fails.append(f"minimal message failed validation: {validate_record(r3)}")
    # Display names survive the round trip through the headers.
    m = Message.from_pa_record(rec, "inbox")
    if m.sender.name != "Sender Name":
        fails.append(f"sender display name lost: {m.sender.name!r}")
    if [a.email for a in m.cc] != ["cc@example.com"]:
        fails.append("cc not mapped")
    if fails and strict:
        raise AssertionError("imap selftest FAILED:\n  " + "\n  ".join(fails))
    return fails


# ═══════════════════════════════════════════════════════════════════════════
# Mail client: folders, on-demand reads, flags, move, delete, drafts, send
# ═══════════════════════════════════════════════════════════════════════════
#
# Everything above this line is the capture path and stays read-only
# (BODY.PEEK, readonly SELECT). The client path below opens folders
# read-write only for STORE / MOVE / EXPUNGE, holds one connection per
# provider behind a lock and reconnects when the server drops it.

import threading as _threading
import time as _time
from contextlib import contextmanager as _contextmanager

from . import compose as _compose
from . import smtp as _smtp
from .base import folder_dict, guess_folder_kind

_LIST_RE = re.compile(rb'\((?P<attrs>[^)]*)\)\s+(?:"(?P<delim>[^"]*)"|(?P<delim2>NIL))\s+(?P<name>.+)$')
_UID_RE = re.compile(rb"UID (\d+)")
_SIZE_RE = re.compile(rb"RFC822.SIZE (\d+)")
_COPYUID_RE = re.compile(rb"(?:COPYUID )?(\d+) ([\d:,]+) ([\d:,]+)")
_HEADER_FIELDS = "(FROM TO CC REPLY-TO SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES CONTENT-TYPE IMPORTANCE X-PRIORITY)"


def _decode_folder_name(raw: bytes) -> str:
    """IMAP LIST names: quoted, or modified UTF-7 for non-ASCII."""
    s = raw.strip()
    if s.startswith(b'"') and s.endswith(b'"'):
        s = s[1:-1]
    try:
        return s.decode("imap4-utf-7")
    except Exception:
        return s.decode("utf-8", "replace")


def parse_list_line(line: bytes) -> dict | None:
    m = _LIST_RE.match(line.strip())
    if not m:
        return None
    attrs = m.group("attrs").decode("ascii", "replace")
    name = _decode_folder_name(m.group("name"))
    delim = (m.group("delim") or b"/").decode("ascii", "replace")
    return {"name": name, "attrs": attrs, "delim": delim,
            "noselect": "\\noselect" in attrs.lower()}


class MailRefused(RuntimeError):
    """The server cannot do this safely (or the ref is stale). The API answers
    409 with this message as-is, so keep it a plain sentence."""


NO_TRASH_MSG = "This mail server has no Trash folder, so Email Boss will not delete"


def _ref(folder: str, uidvalidity: int, uid: int) -> str:
    return f"{int(uidvalidity)}:{int(uid)}:{folder}"


class _ImapClient:
    """The mail-client half of the provider. A separate class so the ported
    capture path above reads exactly as it was ported."""

    CAPS = {"folders": True, "list": True, "read": True, "flags": True, "move": True,
            "delete": True, "search": True, "draft": True, "send": True, "attachments": True}

    # ── connection reuse ──
    def _client_lock(self):
        lk = getattr(self, "_clk", None)
        if lk is None:
            lk = self._clk = _threading.RLock()
        return lk

    @_contextmanager
    def _client(self):
        with self._client_lock():
            conn = getattr(self, "_cconn", None)
            if conn is not None:
                try:
                    if conn.noop()[0] != "OK":
                        raise RuntimeError("noop")
                except Exception:
                    _close(conn)
                    conn = None
            if conn is None:
                conn = self._cconn = self._connect(timeout=40)
                self._cfolder = None
            try:
                yield conn
            except (imaplib.IMAP4.abort, OSError, socket.error):
                _close(conn)
                self._cconn = None
                self._cfolder = None
                raise

    def close_client(self):
        with self._client_lock():
            conn = getattr(self, "_cconn", None)
            if conn is not None:
                _close(conn)
            self._cconn = None
            self._cfolder = None

    def _open(self, conn, folder: str, readonly: bool) -> int:
        """SELECT (or EXAMINE) a folder once; returns UIDVALIDITY."""
        cur = getattr(self, "_cfolder", None)
        if cur and cur[0] == folder and cur[1] == readonly:
            return cur[2]
        typ, data = conn.select(_quote_folder(folder), readonly=readonly)
        if typ != "OK":
            self._cfolder = None
            raise RuntimeError(f"cannot open folder {folder!r}: "
                               f"{(data[0] if data else b'').decode('utf-8', 'replace')}")
        try:
            uv = int(conn.response("UIDVALIDITY")[1][0])
        except Exception:
            uv = 0
        self._cfolder = (folder, readonly, uv)
        return uv

    def _open_checked(self, conn, folder: str, uv_expected: int) -> int:
        """Writable SELECT that always asks the server for the live
        UIDVALIDITY and refuses (409) when the ref is from an older epoch."""
        self._cfolder = None
        uv = self._open(conn, folder, readonly=False)
        if uv_expected and uv and uv != uv_expected:
            raise MailRefused("This folder was rebuilt on the mail server. Reload the list and try again.")
        return uv

    @staticmethod
    def _caps(conn) -> set:
        return {(c.decode() if isinstance(c, bytes) else str(c)).upper() for c in (conn.capabilities or ())}

    def _resolve_ref(self, ref: str) -> tuple[str, int, int]:
        """(folder, uidvalidity, uid) from a client ref or a capture-path
        provider_ref (``inbox:uv:uid`` / ``sent:uv:uid``)."""
        parts = (ref or "").split(":", 2)
        if len(parts) != 3:
            raise ValueError(f"bad message ref {ref!r}")
        a, b, c = parts
        if a in ("inbox", "sent"):
            folder = self.settings["inbox_folder"] if a == "inbox" else self.settings["sent_folder"]
            return folder, int(b), int(c)
        return c, int(a), int(b)

    # ── folders ──
    def list_folders(self) -> list[dict]:
        s = self.settings
        with self._client() as conn:
            typ, lines = conn.list()
            if typ != "OK":
                raise RuntimeError("LIST failed")
            out, seen = [], set()
            for raw in lines or []:
                if isinstance(raw, tuple):          # literal-form name
                    raw = raw[0] + b" " + raw[1]
                f = parse_list_line(raw if isinstance(raw, bytes) else bytes(raw))
                if not f or f["name"] in seen:
                    continue
                seen.add(f["name"])
                kind = guess_folder_kind(f["name"], f["attrs"])
                if f["name"] == s.get("inbox_folder"):
                    kind = "inbox"
                elif f["name"] == s.get("sent_folder"):
                    kind = "sent"
                out.append(folder_dict(f["name"], f["name"].split(f["delim"])[-1] if f["delim"] else f["name"],
                                       kind, noselect=f["noselect"], path=f["name"]))
            # Unread counts for the folders a person actually looks at.
            budget = 12
            for f in out:
                if f["noselect"]:
                    continue
                if f["kind"] not in ("inbox", "drafts", "junk", "other") or budget <= 0:
                    continue
                if f["kind"] == "other" and budget < 6:
                    continue
                budget -= 1
                try:
                    typ, data = conn.status(_quote_folder(f["id"]), "(MESSAGES UNSEEN)")
                    if typ == "OK" and data:
                        blob = data[0] if isinstance(data[0], bytes) else bytes(data[0])
                        m1 = re.search(rb"MESSAGES (\d+)", blob)
                        m2 = re.search(rb"UNSEEN (\d+)", blob)
                        f["total"] = int(m1.group(1)) if m1 else None
                        f["unread"] = int(m2.group(1)) if m2 else None
                except Exception:
                    pass
            order = {"inbox": 0, "drafts": 1, "sent": 2, "archive": 3, "junk": 4, "trash": 5, "other": 6}
            out.sort(key=lambda f: (order.get(f["kind"], 9), f["id"].lower()))
            return out

    def _folder_of_kind(self, kind: str) -> str | None:
        s = self.settings
        if kind == "inbox":
            return s["inbox_folder"]
        if kind == "sent":
            return s["sent_folder"]
        cfg_name = s.get(f"{kind}_folder")
        if cfg_name:
            return cfg_name
        cache = getattr(self, "_kind_cache", None)
        if cache is None:
            cache = self._kind_cache = {}
            try:
                for f in self.list_folders():
                    cache.setdefault(f["kind"], f["id"])
            except Exception:
                pass
        return cache.get(kind)

    # ── list ──
    def list_messages(self, folder_id: str, limit: int = 50, cursor: str | None = None,
                      query: str | None = None) -> dict:
        folder = folder_id or self.settings["inbox_folder"]
        limit = max(1, min(int(limit or 50), 200))
        with self._client() as conn:
            uv = self._open(conn, folder, readonly=True)
            if query:
                q = query.replace('"', "").strip()
                typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", "TEXT", f'"{q}"')
                if typ != "OK":
                    typ, data = conn.uid("SEARCH", None, "TEXT", f'"{q.encode("ascii", "ignore").decode()}"')
            else:
                typ, data = conn.uid("SEARCH", None, "ALL")
            if typ != "OK":
                raise RuntimeError("SEARCH failed")
            uids = sorted({int(t) for t in (data[0] or b"").split() if t.isdigit()})
            if cursor:
                try:
                    uids = [u for u in uids if u < int(cursor)]
                except ValueError:
                    pass
            page = uids[-limit:]
            next_cursor = str(page[0]) if page and len(uids) > len(page) else None
            rows = []
            if page:
                uidset = ",".join(str(u) for u in page)
                typ, data = conn.uid("FETCH", uidset,
                                     f"(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS {_HEADER_FIELDS}])")
                if typ != "OK":
                    raise RuntimeError("FETCH failed")
                for item in data or []:
                    if not (isinstance(item, tuple) and len(item) >= 2):
                        continue
                    head = item[0] if isinstance(item[0], bytes) else b""
                    mu = _UID_RE.search(head)
                    if not mu:
                        continue
                    uid = int(mu.group(1))
                    flags = []
                    mf = _FLAGS_RE.search(head)
                    if mf:
                        flags = mf.group(1).decode("ascii", "replace").split()
                    ms = _SIZE_RE.search(head)
                    rec = message_to_record(bytes(item[1]) + b"\r\n", folder_kind="inbox",
                                            uidvalidity=uv, uid=uid, flags=flags)
                    msg = Message.from_pa_record(rec, "sent" if folder == self.settings["sent_folder"] else "inbox")
                    msg.provider, msg.provider_ref = self.id, _ref(folder, uv, uid)
                    ctype = ""
                    for h in msg.headers:
                        if str(h.get("name", "")).lower() == "content-type":
                            ctype = str(h.get("value") or "").lower()
                    d = summary_dict_imap(msg, flags, ctype, int(ms.group(1)) if ms else 0)
                    rows.append(d)
            rows.sort(key=lambda r: r["uid"], reverse=True)
            return {"folder": folder, "messages": rows, "next_cursor": next_cursor, "total": len(uids)}

    # ── read ──
    def get_message(self, ref: str) -> Message | None:
        folder, uv_expected, uid = self._resolve_ref(ref)
        with self._client() as conn:
            uv = self._open(conn, folder, readonly=True)
            if uv_expected and uv != uv_expected:
                raise MailRefused("This folder was rebuilt on the mail server. Reload the list and try again.")
            raw, flags = _fetch_one(conn, uid)
            if not raw:
                return None
            rec = message_to_record(raw, folder_kind="inbox", uidvalidity=uv, uid=uid, flags=flags)
            kind = "sent" if folder == self.settings["sent_folder"] else "inbox"
            msg = Message.from_pa_record(rec, kind)
            msg.provider, msg.provider_ref = self.id, _ref(folder, uv, uid)
            msg.flagged = "\\Flagged" in " ".join(flags)        # ad-hoc attribute the API reads
            return msg

    # ── flags / move / delete ──
    def set_flags(self, ref: str, read: bool | None = None, flagged: bool | None = None) -> bool:
        folder, uv_exp, uid = self._resolve_ref(ref)
        with self._client() as conn:
            self._open_checked(conn, folder, uv_exp)
            ok = True
            for value, flag in ((read, "\\Seen"), (flagged, "\\Flagged")):
                if value is None:
                    continue
                typ, _ = conn.uid("STORE", str(uid), "+FLAGS" if value else "-FLAGS", f"({flag})")
                ok = ok and typ == "OK"
            return ok

    def move_message(self, ref: str, folder_id: str) -> str | None:
        folder, uv_exp, uid = self._resolve_ref(ref)
        dest = folder_id
        if dest in FOLDER_KINDS_SET:
            resolved = self._folder_of_kind(dest)
            if not resolved:
                if dest == "trash":
                    raise MailRefused(NO_TRASH_MSG)
                raise RuntimeError(f"this mailbox has no {dest} folder; pick a folder by name")
            dest = resolved
        with self._client() as conn:
            self._open_checked(conn, folder, uv_exp)
            caps = self._caps(conn)
            if "MOVE" in caps:
                typ, data = conn.uid("MOVE", str(uid), _quote_folder(dest))
                if typ != "OK":
                    raise RuntimeError(f"MOVE failed: {(data[0] if data else b'').decode('utf-8', 'replace')}")
            else:
                # COPY + \\Deleted + EXPUNGE is only safe as UID EXPUNGE of this one
                # message; a bare EXPUNGE would also remove anything else flagged.
                if "UIDPLUS" not in caps:
                    raise MailRefused("This mail server cannot move messages safely, so Email Boss will not move this one.")
                typ, data = conn.uid("COPY", str(uid), _quote_folder(dest))
                if typ != "OK":
                    raise RuntimeError(f"COPY failed: {(data[0] if data else b'').decode('utf-8', 'replace')}")
                conn.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
                conn.uid("EXPUNGE", str(uid))
            new_ref = None
            try:
                blob = b" ".join(x for x in (conn.response("COPYUID")[1] or []) if isinstance(x, bytes))
                m = _COPYUID_RE.search(blob)
                if m and b":" not in m.group(3) and b"," not in m.group(3):
                    new_ref = _ref(dest, int(m.group(1)), int(m.group(3)))
            except Exception:
                new_ref = None
            return new_ref

    def delete_message(self, ref: str) -> bool:
        """Move to Trash. Never a permanent delete: no Trash folder (or already
        in Trash) means a 409, not an EXPUNGE."""
        folder, _uv, _uid = self._resolve_ref(ref)
        trash = self._folder_of_kind("trash")
        if not trash:
            raise MailRefused(NO_TRASH_MSG)
        if trash == folder:
            raise MailRefused("This message is already in Trash. Email Boss will not delete it for good.")
        self.move_message(ref, trash)
        return True

    # ── drafts / send ──
    def _sender(self):
        return Address(self.settings.get("user") or "", getattr(self, "display_name", "") or "")

    def _append(self, folder: str, raw: bytes, flags: str) -> str | None:
        with self._client() as conn:
            typ, data = conn.append(_quote_folder(folder), flags, imaplib.Time2Internaldate(_time.time()), raw)
            if typ != "OK":
                raise RuntimeError(f"APPEND to {folder!r} failed: {(data[0] if data else b'').decode('utf-8', 'replace')}")
            try:
                blob = b" ".join(x for x in (conn.response("APPENDUID")[1] or []) if isinstance(x, bytes))
                m = re.search(rb"(?:APPENDUID )?(\d+) (\d+)", blob)
                if m:
                    return _ref(folder, int(m.group(1)), int(m.group(2)))
            except Exception:
                pass
            return None

    def create_draft(self, reply_to, to, cc, subject, body_html, attachments=()) -> str | None:
        drafts = self._folder_of_kind("drafts")
        if not drafts:
            raise RuntimeError("no Drafts folder found on the server; set mail.imap.drafts")
        em = _compose.build_message(self._sender(), to, cc, (), subject, body_html, attachments, reply_to)
        return self._append(drafts, em.as_bytes(), "(\\Draft \\Seen)")

    def send_message(self, to, cc, bcc, subject, body_html, attachments=(), reply_to=None) -> dict:
        em = _compose.build_message(self._sender(), to, cc, bcc, subject, body_html, attachments, reply_to)
        rcpts = _compose.envelope_recipients(em)
        raw = _compose.strip_bcc(em)
        st = _smtp.smtp_settings(getattr(self, "mail_cfg", {}) or {}, self.settings)
        pw, _src = self._password()
        tok = self._oauth_token()
        result = _smtp.send(st, self._sender().email, rcpts, raw, password=pw, oauth_token=tok,
                            smtp_factory=getattr(self, "smtp_factory", None))
        result["message_id"] = str(em["Message-ID"] or "")
        # Servers that do not copy submitted mail into Sent get it appended.
        if st["preset"] not in ("gmail", "outlook") and self.settings.get("sent_folder"):
            try:
                result["sent_ref"] = self._append(self.settings["sent_folder"], raw, "(\\Seen)")
            except Exception as e:
                self.note_warning(f"sent but could not copy to {self.settings['sent_folder']!r}: {e}")
        return result


FOLDER_KINDS_SET = frozenset(("inbox", "sent", "drafts", "trash", "junk", "archive"))


def summary_dict_imap(msg: Message, flags, ctype: str, size: int) -> dict:
    from .base import summary_dict
    d = summary_dict(msg, flagged="\\Flagged" in " ".join(flags))
    d["uid"] = int(msg.provider_ref.rsplit(":", 1)[0].split(":")[1]) if msg.provider_ref else 0
    d["has_attachments"] = "multipart/mixed" in ctype
    d["size"] = size
    d["preview"] = ""           # header-only fetch; the reading pane has the body
    return d


class ImapProvider(_ImapClient, _ImapCapture):
    """Capture (read-only polling) + mail client on one IMAP mailbox."""
    id = "imap"
    label = "IMAP"
    required_fields = _ImapCapture.required_fields + [
        {"key": "drafts", "label": "Drafts folder", "type": "text", "default": ""},
        {"key": "trash", "label": "Trash folder", "type": "text", "default": ""},
        {"key": "display_name", "label": "Your display name", "type": "text", "default": ""},
    ]

    def __init__(self, cfg: dict, cache_dir=None, token_provider=None):
        super().__init__(cfg, cache_dir, token_provider)
        mail_cfg = cfg.get("mail", cfg) if isinstance(cfg, dict) else {}
        self.mail_cfg = mail_cfg
        imap_cfg = mail_cfg.get("imap") or {}
        self.display_name = str(imap_cfg.get("display_name") or "")
        self.smtp_factory = None            # tests inject a fake SMTP here
        for kind in ("drafts", "trash", "junk", "archive"):
            v = imap_cfg.get(kind)
            if v:
                self.settings[f"{kind}_folder"] = str(v).strip()
