"""Gmail API provider (OAuth2 installed app, raw HTTPS via urllib).

Flow: ``begin_auth(redirect_uri)`` returns the consent URL (PKCE, loopback
redirect on the board port); ``finish_auth(state, code)`` exchanges the code
and stores the token JSON in the keyring (``gmail_token``). Fetching uses
``users.messages.list`` with a ``q`` window on the first run, then
``users.history.list`` from a ``historyId`` watermark. Labels INBOX / SENT
decide direction. Attachment bodies that Gmail does not inline in the
message payload are pulled with ``users.messages.attachments.get``.
``create_draft`` builds an RFC 822 message with ``email.message`` and posts
it to ``users.drafts.create`` (never ``send``).

The record shape produced is the same PA record every other provider
writes; see mail/message.py.
"""
from __future__ import annotations

import base64
import re
import urllib.parse
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Iterator

from .base import BaseProvider, ConnResult, ProviderState, folder_dict, guess_folder_kind, summary_dict
from .message import (Address, Attachment, Message, safe_filename, safe_id,
                      thread_key_from_ids)
from .oauth import PENDING, TokenCache, http_request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
# Capture-only posture: read + drafts. With the mail client on (the default)
# flags, moves and trash need gmail.modify, which also permits send; whether
# the app MAY send is mail.send_enabled, enforced by the API, not the scope.
SCOPES_CAPTURE = ("https://www.googleapis.com/auth/gmail.readonly "
                  "https://www.googleapis.com/auth/gmail.compose")
SCOPES_CLIENT = "https://www.googleapis.com/auth/gmail.modify"
SCOPES = SCOPES_CAPTURE
LABEL_KINDS = {"INBOX": "inbox", "SENT": "sent", "DRAFT": "drafts", "TRASH": "trash",
               "SPAM": "junk", "STARRED": "other", "IMPORTANT": "other"}
SYSTEM_HIDDEN = {"CHAT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
                 "CATEGORY_UPDATES", "CATEGORY_FORUMS", "UNREAD", "STARRED", "IMPORTANT"}
DEFAULT_INITIAL_DAYS = 30
PAGE_CAP = 500
# Gmail history events the sync asks for (1.3 asked for messageAdded only,
# so deletes and archive / label moves never reached the app).
HISTORY_TYPES = ("messageAdded", "messageDeleted", "labelAdded", "labelRemoved")
CAPTURE_LABELS = {"INBOX", "SENT"}


def folder_for_labels(labels) -> str:
    """Folder kind of a Gmail message from its full label set."""
    labels = set(labels or ())
    if "TRASH" in labels:
        return "trash"
    if "SPAM" in labels:
        return "junk"
    if "INBOX" in labels:
        return "inbox"
    if "SENT" in labels:
        return "sent"
    if "DRAFT" in labels:
        return "drafts"
    return "archive"


def _is_not_found(e: Exception) -> bool:
    return "-> 404" in str(e)


def _b64url_decode(s: str) -> bytes:
    s = (s or "").replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def oauth_defaults() -> dict:
    """Built-in OAuth client ids shipped with the app, read from the environment
    (set at build time). Never written to the user's config file.

    Google requires a client secret even for installed (desktop) apps, and
    documents that it is not confidential there: the flow is protected by PKCE
    and the loopback redirect, so shipping it in the build is expected.
    Microsoft's public-client PKCE flow needs no secret at all."""
    import os
    try:
        from .. import _oauth_ids as baked
    except Exception:  # pragma: no cover
        baked = None
    return {
        "gmail_default_client_id": (os.environ.get("PB_GMAIL_CLIENT_ID") or getattr(baked, "GMAIL_CLIENT_ID", "")).strip(),
        "gmail_default_client_secret": (os.environ.get("PB_GMAIL_CLIENT_SECRET") or getattr(baked, "GMAIL_CLIENT_SECRET", "")).strip(),
        "graph_default_client_id": (os.environ.get("PB_GRAPH_CLIENT_ID") or getattr(baked, "GRAPH_CLIENT_ID", "")).strip(),
    }


class GmailProvider(BaseProvider):
    id = "gmail"
    label = "Gmail API (OAuth)"
    required_fields = [
        {"key": "client_id", "label": "OAuth client ID", "type": "text",
         "help": "A Desktop-type OAuth client from Google Cloud Console."},
        {"key": "client_secret", "label": "OAuth client secret", "type": "secret",
         "secret_key": "gmail_client_secret"},
        {"key": "initial_days", "label": "Days of history on first sync", "type": "number", "default": DEFAULT_INITIAL_DAYS},
    ]

    def __init__(self, cfg: dict, cache_dir=None, http=http_request, token_cache: TokenCache | None = None):
        super().__init__()
        mail_cfg = cfg.get("mail", cfg) if isinstance(cfg, dict) else {}
        g = dict(mail_cfg.get("gmail") or {})
        self.client_id = (g.get("client_id") or "").strip()
        self.client_secret = (g.get("client_secret") or "").strip()
        self.builtin = False
        if not self.client_id:
            d = oauth_defaults()
            if d["gmail_default_client_id"]:
                self.client_id = d["gmail_default_client_id"]
                self.client_secret = d["gmail_default_client_secret"]
                self.builtin = True
        self.initial_days = int(g.get("initial_days") or DEFAULT_INITIAL_DAYS)
        self.max_per_cycle = int(g.get("max_per_cycle") or 100)
        self.cache_dir = cache_dir
        self.http = http
        self.tokens = token_cache or TokenCache("gmail_token")
        self.user_email = ""
        self.client_enabled = bool(mail_cfg.get("client_enabled", True))
        self.scopes = SCOPES_CLIENT if self.client_enabled else SCOPES_CAPTURE

    # ── OAuth ──
    def _secret(self) -> str:
        if self.client_secret:
            return self.client_secret
        from . import secrets as _s
        v, _ = _s.get_secret("gmail_client_secret")
        return v or ""

    def begin_auth(self, redirect_uri: str) -> dict:
        if not self.client_id:
            raise ValueError("gmail.client_id is not configured")
        state, _verifier, challenge = PENDING.start(self.id, redirect_uri)
        params = {
            "client_id": self.client_id, "redirect_uri": redirect_uri,
            "response_type": "code", "scope": self.scopes, "access_type": "offline",
            "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }
        return {"url": AUTH_URL + "?" + urllib.parse.urlencode(params), "state": state}

    def finish_auth(self, state: str, code: str) -> ConnResult:
        pend = PENDING.pop(state)
        if not pend or pend.get("provider") != self.id:
            return ConnResult(False, error="unknown or expired OAuth state")
        st, resp = self.http("POST", TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self._secret(),
            "code": code, "code_verifier": pend["verifier"],
            "grant_type": "authorization_code", "redirect_uri": pend["redirect_uri"],
        }, form=True)
        if st != 200 or not resp.get("access_token"):
            return ConnResult(False, error=f"token exchange failed: {str(resp)[:200]}")
        self.tokens.store_response(resp)
        return ConnResult(True, detail={"scope": resp.get("scope")})

    def _refresh(self, doc: dict) -> dict | None:
        st, resp = self.http("POST", TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self._secret(),
            "refresh_token": doc.get("refresh_token"), "grant_type": "refresh_token",
        }, form=True)
        if st != 200 or not resp.get("access_token"):
            self.note_warning(f"gmail token refresh failed: {str(resp)[:160]}")
            return None
        return resp

    def access_token(self) -> str | None:
        return self.tokens.access_token(self._refresh)

    def _get(self, path: str, params: dict | None = None, raw=False):
        tok = self.access_token()
        if not tok:
            raise RuntimeError("Gmail is not signed in (no token); run the OAuth flow")
        url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        st, body = self.http("GET", url, headers={"Authorization": f"Bearer {tok}"}, raw=raw)
        if st != 200:
            raise RuntimeError(f"Gmail GET {path} -> {st}: {str(body)[:200]}")
        return body

    def _post(self, path: str, data: dict):
        tok = self.access_token()
        if not tok:
            raise RuntimeError("Gmail is not signed in (no token); run the OAuth flow")
        st, body = self.http("POST", API + path, data=data,
                             headers={"Authorization": f"Bearer {tok}"})
        if st not in (200, 201):
            raise RuntimeError(f"Gmail POST {path} -> {st}: {str(body)[:200]}")
        return body

    # ── MailProvider ──
    def test_connection(self) -> ConnResult:
        try:
            prof = self._get("/profile")
            self.user_email = prof.get("emailAddress", "")
            return ConnResult(True, detail={"email": self.user_email,
                                            "messages_total": prof.get("messagesTotal"),
                                            "history_id": prof.get("historyId")})
        except Exception as e:
            return ConnResult(False, error=str(e))

    def fetch_new(self, state: ProviderState, limit: int = 200) -> Iterator[Message]:
        """One capture cycle.

        Sync truth (2.0 Phase 0): every id the server reports is written to
        ``state["pending_ids"]`` BEFORE ``history_id`` moves, so a burst larger
        than ``max_per_cycle`` drains over later cycles instead of being lost.
        History asks for adds, deletes and label changes; deletes and label
        changes go straight to the sync ledger. A fetch that fails goes to the
        retry queue instead of being skipped forever."""
        n = 0
        pending: list[str] = []
        loaded = False
        try:
            cap = max(1, min(limit or self.max_per_cycle, self.max_per_cycle))
            last_hist = state.get("history_id")
            prof = self._get("/profile")
            self.user_email = prof.get("emailAddress", "")
            newest_hist = prof.get("historyId")
            if last_hist:
                try:
                    ids, hist_after = self._history_scan(last_hist)
                except RuntimeError as e:
                    # 404 = historyId too old; fall back to the dated window.
                    self.note_warning(f"history window lost ({e}); resyncing by date")
                    ids, hist_after = self._list_ids(), newest_hist
            else:
                ids, hist_after = self._list_ids(), newest_hist
            pending = list(state.get("pending_ids") or [])
            loaded = True
            queued = set(pending)
            for gid in ids:
                if gid not in queued:
                    queued.add(gid)
                    pending.append(gid)
            # The ids are durable now; only then may the cursor move.
            state.set("pending_ids", pending)
            state.set("history_id", hist_after or newest_hist or last_hist)
            state.save()

            for msg in self._drain_retries():
                n += 1
                yield msg

            seen = set(state.get("recent_ids") or [])
            sync = self.sync
            done = 0
            while pending and done < cap:
                gid = pending[0]
                if (sync is not None and sync.has(self.id, gid, live_only=False)) or gid in seen:
                    pending.pop(0)
                    continue
                done += 1
                try:
                    msg = self._fetch_message(gid)
                except Exception as e:
                    pending.pop(0)
                    if _is_not_found(e):
                        if sync is not None:
                            sync.mark_removed(self.id, gid, "gone before fetch")
                    else:
                        self.fetch_failed(gid, "", e)
                    continue
                if msg is None:
                    pending.pop(0)
                    continue
                n += 1
                yield msg
                # Popped only after the consumer took it: a closed generator
                # leaves the id queued for the next cycle.
                pending.pop(0)
                seen.add(gid)
            if pending:
                self.note_warning("more new messages than the per cycle cap; the rest are queued "
                                  "and continue next cycle")
            state.set("recent_ids", list(seen)[-2000:])
            self._record_fetch(n)
        except Exception as e:
            self._record_fetch(n, f"{type(e).__name__}: {e}")
        finally:
            if loaded:
                state.set("pending_ids", pending)
                state.save()

    def _list_ids(self) -> list[str]:
        """Every id in the first-sync window (no cap: the cap applies to
        fetching, never to discovery)."""
        q = f"newer_than:{max(1, self.initial_days)}d (in:inbox OR in:sent)"
        out: list[str] = []
        token = None
        while True:
            params = {"q": q, "maxResults": PAGE_CAP}
            if token:
                params["pageToken"] = token
            body = self._get("/messages", params)
            out.extend(m["id"] for m in body.get("messages") or [])
            token = body.get("nextPageToken")
            if not token:
                break
        return list(reversed(out))   # oldest first

    def _history_scan(self, start: str) -> tuple[list[str], str | None]:
        """Walk every history page since ``start``. Returns (ids to fetch,
        the historyId the walk reached). Deletes and label changes are
        applied to the sync ledger as they are read (idempotent on replay)."""
        adds: dict[str, None] = {}
        sync = self.sync
        token = None
        reached = None
        while True:
            params = {"startHistoryId": start, "historyTypes": list(HISTORY_TYPES),
                      "maxResults": PAGE_CAP}
            if token:
                params["pageToken"] = token
            body = self._get("/history", params)
            reached = body.get("historyId") or reached
            for h in body.get("history") or []:
                for ma in h.get("messagesAdded") or []:
                    m = ma.get("message") or {}
                    labels = set(m.get("labelIds") or [])
                    if m.get("id") and (not labels or labels & CAPTURE_LABELS):
                        adds[m["id"]] = None
                for md in h.get("messagesDeleted") or []:
                    gid = (md.get("message") or {}).get("id")
                    if not gid:
                        continue
                    adds.pop(gid, None)
                    if sync is not None:
                        sync.mark_removed(self.id, gid, "deleted")
                for key, added in (("labelsAdded", True), ("labelsRemoved", False)):
                    for ev in h.get(key) or []:
                        m = ev.get("message") or {}
                        gid = m.get("id")
                        delta = set(ev.get("labelIds") or [])
                        if not gid or not delta:
                            continue
                        row = None
                        if sync is not None:
                            row = sync.apply_labels(self.id, gid,
                                                    added=delta if added else (),
                                                    removed=() if added else delta,
                                                    folder_fn=folder_for_labels)
                        if row is None and added and delta & CAPTURE_LABELS:
                            # Moved into the inbox (or sent) from somewhere we
                            # never captured: fetch it like a new message.
                            adds[gid] = None
            token = body.get("nextPageToken")
            if not token:
                break
        return list(adds), reached

    def fetch_item(self, item_id: str, folder: str = "") -> Message | None:
        try:
            return self._fetch_message(item_id)
        except Exception as e:
            if _is_not_found(e):
                return None
            raise

    def server_counts(self) -> dict:
        out = {}
        for label, kind in (("INBOX", "inbox"), ("SENT", "sent")):
            body = self._get(f"/labels/{label}")
            out[kind] = int(body.get("messagesTotal") or 0)
        return out

    # ── payload parsing ──
    def _fetch_message(self, gid: str, any_folder: bool = False, fmt: str = "full") -> Message | None:
        m = self._get(f"/messages/{gid}", {"format": fmt})
        labels = set(m.get("labelIds") or [])
        if "SENT" in labels and "INBOX" not in labels:
            folder = "sent"
        elif "INBOX" in labels:
            folder = "inbox"
        elif "SENT" in labels:
            folder = "sent"
        elif any_folder:
            folder = "inbox"
        else:
            return None   # drafts, spam, trash, chats: not correspondence
        payload = m.get("payload") or {}
        headers = [{"name": h.get("name", ""), "value": h.get("value", "")}
                   for h in (payload.get("headers") or [])][:200]
        hmap = {h["name"].lower(): h["value"] for h in headers}
        atts: list[Attachment] = []
        html, text = self._body_from_walk(payload, gid, atts)
        received = None
        try:
            if hmap.get("date"):
                received = parsedate_to_datetime(hmap["date"])
        except Exception:
            received = None
        if received is None and m.get("internalDate"):
            received = datetime.fromtimestamp(int(m["internalDate"]) / 1000, timezone.utc)
        received = received or datetime.now(timezone.utc)
        sender = Address.parse_many(hmap.get("from", ""))
        to = Address.parse_many(hmap.get("to", ""))
        imid = hmap.get("message-id", "")
        conv = thread_key_from_ids(hmap.get("references", ""), hmap.get("in-reply-to", ""),
                                   imid, hmap.get("subject", ""),
                                   [a.email for a in sender + to])
        importance = (hmap.get("importance") or "normal").lower()
        if importance not in ("low", "normal", "high"):
            importance = "normal"
        msg = Message(
            id=safe_id(imid or f"gmail:{gid}", prefix="gmail"),
            received_at=received,
            subject=hmap.get("subject", ""),
            body=html or text,
            is_html=bool(html),
            sender=sender[0] if sender else Address(""),
            to=to,
            cc=Address.parse_many(hmap.get("cc", "")),
            reply_to=Address.parse_many(hmap.get("reply-to", "")),
            attachments=atts,
            internet_message_id=imid,
            conversation_id=conv,
            importance=importance,
            is_read="UNREAD" not in labels,
            headers=headers,
            folder=folder,
            body_preview=m.get("snippet", ""),
            provider=self.id,
            provider_ref=f"{gid}|{m.get('threadId', '')}",
        )
        msg.flagged = "STARRED" in labels
        msg.labels = sorted(labels)
        msg.sync_id = gid
        msg.sync_folder = folder_for_labels(labels)
        msg.sync_labels = sorted(labels)
        return msg

    def _body_from_walk(self, payload: dict, gid: str, atts: list[Attachment]) -> tuple[str, str]:
        html, text = "", ""
        stack = [payload]
        while stack:
            part = stack.pop(0)
            mime = (part.get("mimeType") or "").lower()
            body = part.get("body") or {}
            filename = part.get("filename") or ""
            phead = {h.get("name", "").lower(): h.get("value", "") for h in (part.get("headers") or [])}
            if mime.startswith("multipart/"):
                stack = list(part.get("parts") or []) + stack
                continue
            is_body = (not filename) and mime in ("text/html", "text/plain") and \
                "attachment" not in (phead.get("content-disposition") or "").lower()
            if is_body:
                data = _b64url_decode(body.get("data") or "").decode("utf-8", "replace")
                if mime == "text/html" and not html:
                    html = data
                elif mime == "text/plain" and not text:
                    text = data
                continue
            # attachment (inline or real)
            content = b""
            if body.get("data"):
                content = _b64url_decode(body["data"])
            elif body.get("attachmentId"):
                try:
                    a = self._get(f"/messages/{gid}/attachments/{body['attachmentId']}")
                    content = _b64url_decode(a.get("data") or "")
                except Exception as e:
                    self.note_warning(f"attachment fetch failed on {gid}: {e}")
            cid = (phead.get("content-id") or "").strip()
            if cid.startswith("<") and cid.endswith(">"):
                cid = cid[1:-1]
            disp = (phead.get("content-disposition") or "").lower()
            name = safe_filename(filename or (cid.split("@")[0] if cid else f"part{len(atts) + 1}"))
            atts.append(Attachment(
                name=name, content_type=mime or "application/octet-stream",
                size=int(body.get("size") or len(content)), is_inline=bool(cid) or "inline" in disp,
                content_id=cid or None, content=content))
        return html, text

    # ── mail client ──
    CAPS = {"folders": True, "list": True, "read": True, "flags": True, "move": True,
            "delete": True, "search": True, "draft": True, "send": True, "attachments": True}

    @staticmethod
    def _gid(ref: str) -> str:
        return (ref or "").split("|", 1)[0]

    def list_folders(self) -> list[dict]:
        body = self._get("/labels")
        out = []
        for lb in body.get("labels") or []:
            lid = lb.get("id") or ""
            if lid in SYSTEM_HIDDEN or lb.get("labelListVisibility") == "labelHide":
                continue
            kind = LABEL_KINDS.get(lid) or guess_folder_kind(lb.get("name") or "")
            if lid.startswith("Label_") and kind in ("inbox", "sent", "drafts", "trash", "junk"):
                kind = "other"
            out.append(folder_dict(lid, (lb.get("name") or lid).split("/")[-1], kind,
                                   unread=lb.get("messagesUnread"), total=lb.get("messagesTotal"),
                                   path=lb.get("name") or lid))
        order = {"inbox": 0, "drafts": 1, "sent": 2, "archive": 3, "junk": 4, "trash": 5, "other": 6}
        out.sort(key=lambda f: (order.get(f["kind"], 9), f["path"].lower()))
        # Gmail has no "All Mail" label id in the list; expose it as the archive.
        out.insert(3, folder_dict("__all__", "All Mail", "archive", path="All Mail"))
        return out

    def list_messages(self, folder_id: str, limit: int = 50, cursor: str | None = None,
                      query: str | None = None) -> dict:
        limit = max(1, min(int(limit or 50), 100))
        params = {"maxResults": limit}
        if folder_id and folder_id != "__all__":
            params["labelIds"] = folder_id
        if query:
            params["q"] = query
        if cursor:
            params["pageToken"] = cursor
        body = self._get("/messages", params)
        rows = []
        for m in body.get("messages") or []:
            try:
                msg = self._fetch_message(m["id"], any_folder=True, fmt="metadata")
            except Exception as e:
                self.note_warning(f"list: message {m.get('id')} failed ({type(e).__name__})")
                continue
            if msg is None:
                continue
            d = summary_dict(msg, flagged=getattr(msg, "flagged", False))
            d["has_attachments"] = bool(msg.attachments)     # metadata format: unknown until opened
            rows.append(d)
        return {"folder": folder_id, "messages": rows, "next_cursor": body.get("nextPageToken"),
                "total": body.get("resultSizeEstimate")}

    def get_message(self, ref: str) -> Message | None:
        return self._fetch_message(self._gid(ref), any_folder=True)

    def set_flags(self, ref: str, read: bool | None = None, flagged: bool | None = None) -> bool:
        add, rem = [], []
        if read is True:
            rem.append("UNREAD")
        elif read is False:
            add.append("UNREAD")
        if flagged is True:
            add.append("STARRED")
        elif flagged is False:
            rem.append("STARRED")
        if not add and not rem:
            return True
        self._post(f"/messages/{self._gid(ref)}/modify", {"addLabelIds": add, "removeLabelIds": rem})
        return True

    def move_message(self, ref: str, folder_id: str) -> str | None:
        gid = self._gid(ref)
        kind_to_label = {"inbox": "INBOX", "trash": "TRASH", "junk": "SPAM", "archive": "__all__"}
        target = kind_to_label.get(folder_id, folder_id)
        if target == "TRASH":
            self._post(f"/messages/{gid}/trash", {})
            return ref
        current = self._get(f"/messages/{gid}", {"format": "minimal"})
        labels = set(current.get("labelIds") or [])
        removable = [l for l in labels if l in ("INBOX", "SPAM", "TRASH") or l.startswith("Label_")]
        add = [] if target == "__all__" else [target]
        if target == "__all__" and "TRASH" in labels:
            self._post(f"/messages/{gid}/untrash", {})
        self._post(f"/messages/{gid}/modify", {"addLabelIds": add,
                                               "removeLabelIds": [l for l in removable if l != target]})
        return ref

    def delete_message(self, ref: str) -> bool:
        self._post(f"/messages/{self._gid(ref)}/trash", {})
        return True

    def _profile_email(self) -> str:
        if not self.user_email:
            try:
                self.user_email = (self._get("/profile") or {}).get("emailAddress", "")
            except Exception:
                pass
        return self.user_email

    def send_message(self, to, cc, bcc, subject, body_html, attachments=(), reply_to=None) -> dict:
        from .compose import build_message
        em = build_message(Address(self._profile_email() or "me"), to, cc, bcc, subject, body_html,
                           attachments, reply_to)
        payload = {"raw": _b64url_encode(em.as_bytes())}
        if reply_to is not None and reply_to.provider_ref and "|" in reply_to.provider_ref:
            tid = reply_to.provider_ref.split("|", 1)[1]
            if tid:
                payload["threadId"] = tid
        resp = self._post("/messages/send", payload)
        return {"ok": True, "id": resp.get("id"), "thread_id": resp.get("threadId"),
                "message_id": str(em["Message-ID"] or "")}

    def health(self) -> dict:
        h = self.base_health()
        h["signed_in"] = bool(self.tokens.load())
        h["email"] = self.user_email
        return h

    # ── drafts (never send) ──
    def create_draft(self, reply_to: Message | None, to, cc, subject, body_html, attachments=()) -> str | None:
        em = EmailMessage()
        em["To"] = ", ".join(a.display() for a in Address.parse_many(to))
        cc_list = Address.parse_many(cc)
        if cc_list:
            em["Cc"] = ", ".join(a.display() for a in cc_list)
        em["Subject"] = subject or ""
        thread_id = None
        if reply_to is not None:
            if reply_to.internet_message_id:
                em["In-Reply-To"] = reply_to.internet_message_id
                em["References"] = reply_to.internet_message_id
            if reply_to.provider_ref and "|" in reply_to.provider_ref:
                thread_id = reply_to.provider_ref.split("|", 1)[1] or None
        plain = re.sub(r"<[^>]+>", " ", body_html or "")
        em.set_content(re.sub(r"\s+", " ", plain).strip())
        em.add_alternative(body_html or "", subtype="html")
        for att in attachments or ():
            name = getattr(att, "name", None) or "attachment.bin"
            data = getattr(att, "content", b"") or b""
            ctype = getattr(att, "content_type", "application/octet-stream") or "application/octet-stream"
            main, _, sub = ctype.partition("/")
            em.add_attachment(data, maintype=main or "application", subtype=sub or "octet-stream", filename=name)
        payload = {"message": {"raw": _b64url_encode(em.as_bytes())}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        resp = self._post("/drafts", payload)
        return resp.get("id")
