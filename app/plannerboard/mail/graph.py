"""Microsoft Graph provider.

Generalized from v2/graph_auth.py: that script carried one hard-coded tenant
and client id, a device-code bootstrap (lines 36-79) and a refresh-token
``access_token`` (lines 82-95) that wrote the token to a plaintext file. Here
the client id and tenant (default ``common``) come from config, the primary
flow is auth-code + PKCE on a loopback redirect with device-code as the
fallback, and the token JSON lives in the keyring (``graph_token``).

Fetching uses delta queries on ``/me/mailFolders/inbox/messages/delta`` and
``/me/mailFolders/sentitems/messages/delta`` with the ``@odata.deltaLink``
kept as the watermark. Attachments come through ``$expand=attachments``
when the server honours it and a per-message ``/attachments`` fetch
otherwise, inline ones included. Drafts are made with ``createReply`` (or
``/me/messages`` for a fresh draft) followed by a PATCH of the body.
The capture path never sends; ``send_message`` (the mail client) does,
and only when the API has checked ``mail.send_enabled``.
"""
from __future__ import annotations

import base64
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Iterator

from .base import BaseProvider, ConnResult, ProviderState, folder_dict, guess_folder_kind, summary_dict
from .message import Address, Attachment, Message, parse_iso, safe_filename, safe_id
from .oauth import PENDING, TokenCache, http_request

GRAPH = "https://graph.microsoft.com/v1.0"
# Mail.ReadWrite covers reading, flags, moves, drafts and trash. Mail.Send is
# requested only when mail.send_enabled is on (least privilege: a token that
# cannot send is the stronger guarantee than an app rule).
SCOPES_BASE = "offline_access Mail.ReadWrite User.Read"
SCOPES = SCOPES_BASE
SCOPES_SEND = SCOPES_BASE + " Mail.Send"
LIST_SELECT = ("id,receivedDateTime,hasAttachments,internetMessageId,subject,bodyPreview,"
               "importance,conversationId,isRead,isDraft,flag,from,toRecipients")
WELL_KNOWN = {"inbox": "inbox", "sentitems": "sent", "drafts": "drafts", "deleteditems": "trash",
              "junkemail": "junk", "archive": "archive"}
SELECT = ("id,receivedDateTime,sentDateTime,hasAttachments,internetMessageId,subject,"
          "bodyPreview,importance,conversationId,isRead,isDraft,body,from,toRecipients,"
          "ccRecipients,replyTo,internetMessageHeaders")
FOLDERS = (("inbox", "inbox"), ("sent", "sentitems"))


def _login_base(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant or 'common'}/oauth2/v2.0"


def _recips(lst) -> list[Address]:
    out = []
    for r in lst or []:
        ea = (r or {}).get("emailAddress") or {}
        if ea.get("address"):
            out.append(Address(ea["address"], ea.get("name") or ""))
    return out


DEFAULT_INITIAL_DAYS = 30

class GraphProvider(BaseProvider):
    id = "graph"
    label = "Microsoft 365 / Outlook.com (Graph API)"
    required_fields = [
        {"key": "client_id", "label": "App (client) ID", "type": "text",
         "help": "An Azure app registration with a public client redirect on http://127.0.0.1."},
        {"key": "tenant", "label": "Tenant", "type": "text", "default": "common"},
        {"key": "initial_days", "label": "Days of history on first sync", "type": "number", "default": DEFAULT_INITIAL_DAYS},
    ]

    def __init__(self, cfg: dict, cache_dir=None, http=http_request, token_cache: TokenCache | None = None):
        super().__init__()
        mail_cfg = cfg.get("mail", cfg) if isinstance(cfg, dict) else {}
        g = dict(mail_cfg.get("graph") or {})
        self.client_id = (g.get("client_id") or "").strip()
        self.tenant = (g.get("tenant") or "common").strip() or "common"
        self.builtin = False
        if not self.client_id:
            # Built-in public client (PKCE, no secret), tenant "common".
            from .gmail import oauth_defaults
            did = oauth_defaults()["graph_default_client_id"]
            if did:
                self.client_id, self.builtin = did, True
        self.initial_days = int(g.get("initial_days") or DEFAULT_INITIAL_DAYS)
        self.max_per_cycle = int(g.get("max_per_cycle") or 100)
        self.cache_dir = cache_dir
        self.http = http
        self.tokens = token_cache or TokenCache("graph_token")
        self.user_email = ""
        self._device_pending: dict | None = None
        self.send_enabled = bool(mail_cfg.get("send_enabled", False))
        self.scopes = SCOPES_SEND if self.send_enabled else SCOPES_BASE

    # ── OAuth: auth code + PKCE ──
    def begin_auth(self, redirect_uri: str) -> dict:
        if not self.client_id:
            raise ValueError("graph.client_id is not configured")
        state, _verifier, challenge = PENDING.start(self.id, redirect_uri)
        params = {
            "client_id": self.client_id, "response_type": "code",
            "redirect_uri": redirect_uri, "response_mode": "query",
            "scope": self.scopes, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }
        return {"url": _login_base(self.tenant) + "/authorize?" + urllib.parse.urlencode(params),
                "state": state}

    def finish_auth(self, state: str, code: str) -> ConnResult:
        pend = PENDING.pop(state)
        if not pend or pend.get("provider") != self.id:
            return ConnResult(False, error="unknown or expired OAuth state")
        st, resp = self.http("POST", _login_base(self.tenant) + "/token", data={
            "client_id": self.client_id, "grant_type": "authorization_code",
            "code": code, "redirect_uri": pend["redirect_uri"],
            "code_verifier": pend["verifier"], "scope": self.scopes,
        }, form=True)
        if st != 200 or not resp.get("access_token"):
            return ConnResult(False, error=f"token exchange failed: {str(resp)[:200]}")
        self.tokens.store_response(resp)
        return ConnResult(True, detail={"scope": resp.get("scope")})

    # ── OAuth: device code fallback (graph_auth.py 36-79) ──
    def begin_device_code(self) -> dict:
        st, j = self.http("POST", _login_base(self.tenant) + "/devicecode",
                          data={"client_id": self.client_id, "scope": self.scopes}, form=True)
        if st != 200:
            raise RuntimeError(f"devicecode failed: {str(j)[:200]}")
        self._device_pending = j
        return {"user_code": j.get("user_code"), "verification_uri": j.get("verification_uri"),
                "expires_in": j.get("expires_in"), "message": j.get("message")}

    def poll_device_code(self, max_seconds: int = 150) -> ConnResult:
        j = self._device_pending
        if not j:
            return ConnResult(False, error="no device-code flow pending")
        interval = int(j.get("interval", 5))
        deadline = time.time() + max_seconds
        while time.time() < deadline:
            st, r = self.http("POST", _login_base(self.tenant) + "/token", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.client_id, "device_code": j["device_code"]}, form=True)
            if st == 200 and r.get("access_token"):
                self.tokens.store_response(r)
                self._device_pending = None
                return ConnResult(True, detail={"scope": r.get("scope")})
            err = r.get("error")
            if err in ("authorization_pending", "slow_down"):
                if err == "slow_down":
                    interval += 5
                time.sleep(interval)
                continue
            return ConnResult(False, error=f"{err}: {str(r.get('error_description', ''))[:200]}")
        return ConnResult(False, error="timeout: code not entered yet")

    def _refresh(self, doc: dict) -> dict | None:
        st, r = self.http("POST", _login_base(self.tenant) + "/token", data={
            "grant_type": "refresh_token", "client_id": self.client_id,
            "refresh_token": doc.get("refresh_token"), "scope": SCOPES}, form=True)
        if st != 200 or not r.get("access_token"):
            self.note_warning(f"graph token refresh failed: {str(r)[:160]}")
            return None
        return r

    def access_token(self) -> str | None:
        return self.tokens.access_token(self._refresh)

    def _call(self, method: str, url: str, data=None, raw=False):
        tok = self.access_token()
        if not tok:
            raise RuntimeError("Graph is not signed in (no token); run the OAuth flow")
        if not url.startswith("http"):
            url = GRAPH + url
        st, body = self.http(method, url, data=data, raw=raw,
                             headers={"Authorization": f"Bearer {tok}"})
        if st not in (200, 201, 202, 204):
            raise RuntimeError(f"Graph {method} {url[:80]} -> {st}: {str(body)[:200]}")
        return body

    # ── MailProvider ──
    def test_connection(self) -> ConnResult:
        try:
            me = self._call("GET", "/me?$select=mail,userPrincipalName,displayName")
            self.user_email = me.get("mail") or me.get("userPrincipalName") or ""
            return ConnResult(True, detail={"email": self.user_email, "name": me.get("displayName")})
        except Exception as e:
            return ConnResult(False, error=str(e))

    def fetch_new(self, state: ProviderState, limit: int = 200) -> Iterator[Message]:
        n = 0
        cap = max(1, min(limit or self.max_per_cycle, self.max_per_cycle))
        errors = []
        try:
            for kind, folder_id in FOLDERS:
                try:
                    for msg in self._folder_delta(kind, folder_id, state, cap):
                        n += 1
                        yield msg
                except Exception as fe:
                    errors.append(f"{kind}: {type(fe).__name__}: {fe}")
            state.save()
            self._record_fetch(n, "; ".join(errors) or None)
        except Exception as e:
            self._record_fetch(n, f"{type(e).__name__}: {e}")

    def _folder_delta(self, kind: str, folder_id: str, state: ProviderState, cap: int):
        fs = state.folder(kind)
        url = fs.get("delta_link")
        if not url:
            since = datetime.now(timezone.utc).timestamp() - self.initial_days * 86400
            since_iso = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            url = (f"{GRAPH}/me/mailFolders/{folder_id}/messages/delta?"
                   + urllib.parse.urlencode({"$select": SELECT,
                                             "$filter": f"receivedDateTime ge {since_iso}"}))
        count = 0
        while url and count < cap:
            page = self._call("GET", url)
            for item in page.get("value") or []:
                if "@removed" in item or item.get("isDraft"):
                    continue
                try:
                    msg = self._to_message(item, kind)
                except Exception as e:
                    self.note_warning(f"message {item.get('id', '')[:20]} failed ({type(e).__name__}); skipped")
                    continue
                count += 1
                yield msg
            if page.get("@odata.deltaLink"):
                fs["delta_link"] = page["@odata.deltaLink"]
                url = None
            else:
                url = page.get("@odata.nextLink")
                if url:
                    fs["next_link"] = url
        if url:
            # Capped mid-page: resume from the nextLink next cycle.
            fs["delta_link"] = url
            self.note_warning(f"{kind}: more than {cap} new messages, continuing next cycle")

    def _attachments(self, item: dict) -> list[Attachment]:
        atts = item.get("attachments")
        if atts is None:
            # Not expanded. Inline-only mail reports hasAttachments=false yet
            # still carries inline parts, so fetch regardless of that flag.
            atts = (self._call("GET", f"/me/messages/{item['id']}/attachments") or {}).get("value") or []
        out = []
        for a in atts or []:
            if a.get("@odata.type", "").endswith("fileAttachment") or a.get("contentBytes"):
                try:
                    content = base64.b64decode(a.get("contentBytes") or "")
                except Exception:
                    content = b""
                out.append(Attachment(
                    name=safe_filename(a.get("name") or "attachment.bin"),
                    content_type=a.get("contentType") or "application/octet-stream",
                    size=int(a.get("size") or len(content)), is_inline=bool(a.get("isInline")),
                    content_id=a.get("contentId"), content=content, id=str(a.get("id") or ""),
                    last_modified=str(a.get("lastModifiedDateTime") or "")))
        return out

    def _to_message(self, item: dict, folder: str) -> Message:
        sender = _recips([item.get("from")])
        body = item.get("body") or {}
        headers = [{"name": h.get("name", ""), "value": h.get("value", "")}
                   for h in (item.get("internetMessageHeaders") or [])][:200]
        imid = item.get("internetMessageId") or ""
        received = parse_iso(item.get("receivedDateTime") or item.get("sentDateTime")) \
            or datetime.now(timezone.utc)
        return Message(
            id=safe_id(imid or f"graph:{item.get('id', '')}", prefix="graph"),
            received_at=received,
            subject=item.get("subject") or "",
            body=body.get("content") or "",
            is_html=(body.get("contentType") or "").lower() == "html",
            sender=sender[0] if sender else Address(""),
            to=_recips(item.get("toRecipients")),
            cc=_recips(item.get("ccRecipients")),
            reply_to=_recips(item.get("replyTo")),
            attachments=self._attachments(item),
            internet_message_id=imid,
            conversation_id=item.get("conversationId") or "",
            importance=(item.get("importance") or "normal").lower(),
            is_read=bool(item.get("isRead")),
            headers=headers,
            folder=folder,
            body_preview=item.get("bodyPreview") or "",
            provider=self.id,
            provider_ref=item.get("id") or "",
        )

    # ── mail client ──
    CAPS = {"folders": True, "list": True, "read": True, "flags": True, "move": True,
            "delete": True, "search": True, "draft": True, "send": True, "attachments": True}

    def _folder_kind(self, f: dict, well_known: dict) -> str:
        wk = well_known.get(f.get("id"))
        if wk:
            return wk
        return guess_folder_kind(f.get("displayName") or "")

    def list_folders(self) -> list[dict]:
        # Resolve the well-known ids once so kinds do not depend on display language.
        well_known: dict[str, str] = {}
        for name, kind in WELL_KNOWN.items():
            try:
                f = self._call("GET", f"/me/mailFolders/{name}?$select=id")
                if f.get("id"):
                    well_known[f["id"]] = kind
            except Exception:
                continue
        out = []
        url = f"{GRAPH}/me/mailFolders?$top=100&$select=id,displayName,unreadItemCount,totalItemCount,childFolderCount"
        while url:
            page = self._call("GET", url)
            for f in page.get("value") or []:
                kind = self._folder_kind(f, well_known)
                out.append(folder_dict(f["id"], f.get("displayName") or "", kind,
                                       unread=f.get("unreadItemCount"), total=f.get("totalItemCount"),
                                       path=f.get("displayName") or ""))
                if f.get("childFolderCount"):
                    try:
                        kids = self._call("GET", f"/me/mailFolders/{f['id']}/childFolders?$top=100"
                                          "&$select=id,displayName,unreadItemCount,totalItemCount")
                        for k in kids.get("value") or []:
                            out.append(folder_dict(k["id"], k.get("displayName") or "", "other",
                                                   unread=k.get("unreadItemCount"), total=k.get("totalItemCount"),
                                                   path=f"{f.get('displayName') or ''}/{k.get('displayName') or ''}"))
                    except Exception:
                        pass
            url = page.get("@odata.nextLink")
        order = {"inbox": 0, "drafts": 1, "sent": 2, "archive": 3, "junk": 4, "trash": 5, "other": 6}
        out.sort(key=lambda f: (order.get(f["kind"], 9), f["path"].lower()))
        return out

    def list_messages(self, folder_id: str, limit: int = 50, cursor: str | None = None,
                      query: str | None = None) -> dict:
        limit = max(1, min(int(limit or 50), 100))
        fid = {"inbox": "inbox", "sent": "sentitems", "drafts": "drafts", "trash": "deleteditems",
               "junk": "junkemail", "archive": "archive"}.get(folder_id, folder_id) or "inbox"
        if cursor:
            url = cursor
        else:
            params = {"$top": limit, "$select": LIST_SELECT}
            if query:
                params["$search"] = f'"{query.replace(chr(34), "")}"'
            else:
                params["$orderby"] = "receivedDateTime desc"
            url = f"{GRAPH}/me/mailFolders/{fid}/messages?" + urllib.parse.urlencode(params)
        page = self._call("GET", url)
        rows = []
        for item in page.get("value") or []:
            if item.get("isDraft") and fid != "drafts":
                continue
            item = dict(item)
            item["attachments"] = []           # no attachment expansion in a list
            msg = self._to_message(item, "sent" if fid == "sentitems" else "inbox")
            d = summary_dict(msg, flagged=((item.get("flag") or {}).get("flagStatus") == "flagged"))
            d["has_attachments"] = bool(item.get("hasAttachments"))
            rows.append(d)
        return {"folder": folder_id, "messages": rows, "next_cursor": page.get("@odata.nextLink"),
                "total": None}

    def get_message(self, ref: str) -> Message | None:
        item = self._call("GET", f"/me/messages/{ref}?$select={SELECT},flag,parentFolderId")
        if not item:
            return None
        msg = self._to_message(item, "inbox")
        msg.flagged = (item.get("flag") or {}).get("flagStatus") == "flagged"
        return msg

    def set_flags(self, ref: str, read: bool | None = None, flagged: bool | None = None) -> bool:
        patch = {}
        if read is not None:
            patch["isRead"] = bool(read)
        if flagged is not None:
            patch["flag"] = {"flagStatus": "flagged" if flagged else "notFlagged"}
        if patch:
            self._call("PATCH", f"/me/messages/{ref}", data=patch)
        return True

    def move_message(self, ref: str, folder_id: str) -> str | None:
        fid = {"inbox": "inbox", "sent": "sentitems", "drafts": "drafts", "trash": "deleteditems",
               "junk": "junkemail", "archive": "archive"}.get(folder_id, folder_id)
        moved = self._call("POST", f"/me/messages/{ref}/move", data={"destinationId": fid})
        return (moved or {}).get("id")

    def delete_message(self, ref: str) -> bool:
        self.move_message(ref, "deleteditems")
        return True

    def send_message(self, to, cc, bcc, subject, body_html, attachments=(), reply_to=None) -> dict:
        from .compose import build_message
        to_l = [{"emailAddress": {"address": a.email, "name": a.name}} for a in Address.parse_many(to)]
        cc_l = [{"emailAddress": {"address": a.email, "name": a.name}} for a in Address.parse_many(cc)]
        bcc_l = [{"emailAddress": {"address": a.email, "name": a.name}} for a in Address.parse_many(bcc)]
        atts = []
        for att in attachments or ():
            atts.append({"@odata.type": "#microsoft.graph.fileAttachment",
                         "name": getattr(att, "name", None) or "attachment.bin",
                         "contentType": getattr(att, "content_type", None) or "application/octet-stream",
                         "isInline": bool(getattr(att, "is_inline", False)),
                         "contentId": getattr(att, "content_id", None),
                         "contentBytes": base64.b64encode(getattr(att, "content", b"") or b"").decode("ascii")})
        if reply_to is not None and reply_to.provider == self.id and reply_to.provider_ref:
            # createReply keeps Outlook's conversation threading; patch, then send.
            draft = self._call("POST", f"/me/messages/{reply_to.provider_ref}/createReply", data={})
            did = draft.get("id")
            patch = {"body": {"contentType": "HTML", "content": body_html or ""},
                     "toRecipients": to_l, "ccRecipients": cc_l, "bccRecipients": bcc_l}
            if subject:
                patch["subject"] = subject
            self._call("PATCH", f"/me/messages/{did}", data=patch)
            for a in atts:
                self._call("POST", f"/me/messages/{did}/attachments", data=a)
            self._call("POST", f"/me/messages/{did}/send", data=None)
            return {"ok": True, "id": did, "message_id": draft.get("internetMessageId", "")}
        em = build_message(Address(self.user_email or ""), to, cc, bcc, subject, body_html, (), reply_to)
        message = {"subject": subject or "", "toRecipients": to_l, "ccRecipients": cc_l,
                   "bccRecipients": bcc_l, "body": {"contentType": "HTML", "content": body_html or ""},
                   "attachments": atts}
        self._call("POST", "/me/sendMail", data={"message": message, "saveToSentItems": True})
        return {"ok": True, "id": None, "message_id": str(em["Message-ID"] or "")}

    def health(self) -> dict:
        h = self.base_health()
        h["signed_in"] = bool(self.tokens.load())
        h["email"] = self.user_email
        h["tenant"] = self.tenant
        return h

    # ── drafts (never send) ──
    def create_draft(self, reply_to: Message | None, to, cc, subject, body_html, attachments=()) -> str | None:
        to_l = [{"emailAddress": {"address": a.email, "name": a.name}} for a in Address.parse_many(to)]
        cc_l = [{"emailAddress": {"address": a.email, "name": a.name}} for a in Address.parse_many(cc)]
        if reply_to is not None and reply_to.provider == self.id and reply_to.provider_ref:
            draft = self._call("POST", f"/me/messages/{reply_to.provider_ref}/createReply", data={})
            did = draft.get("id")
            patch = {"body": {"contentType": "HTML", "content": body_html or ""}}
            if to_l:
                patch["toRecipients"] = to_l
            if cc_l:
                patch["ccRecipients"] = cc_l
            if subject:
                patch["subject"] = subject
            self._call("PATCH", f"/me/messages/{did}", data=patch)
        else:
            draft = self._call("POST", "/me/messages", data={
                "subject": subject or "", "toRecipients": to_l, "ccRecipients": cc_l,
                "body": {"contentType": "HTML", "content": body_html or ""}})
            did = draft.get("id")
        for att in attachments or ():
            data = getattr(att, "content", b"") or b""
            self._call("POST", f"/me/messages/{did}/attachments", data={
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": getattr(att, "name", None) or "attachment.bin",
                "contentType": getattr(att, "content_type", None) or "application/octet-stream",
                "contentBytes": base64.b64encode(data).decode("ascii")})
        return did
