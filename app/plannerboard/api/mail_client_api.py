"""
/api/mail/client/*: the mail client. Folders, message lists, on-demand
reads, attachments, flags, move, delete, reply/forward scaffolding, provider
drafts, send, and the two bridges into the board (stage to triage, file to a
project).

Gates (checked here, every call):
  * ``mail.client_enabled`` (default true) turns the whole client off.
  * ``mail.send_enabled`` (default FALSE) gates /send. Off means 403 and the
    composer offers "Save draft" only. The capture path and the assistant
    never send regardless of this flag; only a person pressing Send here
    does.
  * A provider reports what it can do through ``capabilities()``; anything
    it cannot do answers 501 so the UI can hide the control.

The provider instance is shared with the capture thread when one is running
(IMAP keeps one client connection per provider), else built once and cached
on the runtime.
"""
from __future__ import annotations

import base64
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import quote, urljoin, urlsplit
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from ..mail import runner as mail_runner
from ..mail.base import message_dict
from ..mail.compose import (forward_subject, quote_forward, quote_reply, reply_recipients,
                            reply_subject, reply_to_differs)
from ..mail.imap import MailRefused
from ..mail.message import Address, Attachment, Message, write_record
from ._common import body, error_response, int_arg, rt, view

bp = Blueprint("mail_client_api", __name__)

MAX_ATTACH_BYTES = 25 * 1024 * 1024
# Request size limits. Attachments travel as base64 inside the JSON body (4/3 of
# the file) plus the message text; every other call carries a few hundred bytes.
MAX_UPLOAD_REQUEST_BYTES = MAX_ATTACH_BYTES * 4 // 3 + 2 * 1024 * 1024
MAX_SMALL_REQUEST_BYTES = 1024 * 1024
_UPLOAD_ENDPOINTS = frozenset(("mail_client_api.api_draft", "mail_client_api.api_send"))
_TOO_BIG = ("The attachments are too big. Email Boss can keep up to 25 MB at a time. "
            "Remove a file or send it another way.")
_lock = threading.Lock()
_cache: dict = {}            # {provider_id: provider}  (one per process)


# ── plumbing ────────────────────────────────────────────────────────────────

@bp.before_request
def _limit_request_size():
    """Cap the body before anything reads it (also covers chunked uploads that
    carry no Content-Length). Only draft/send may carry attachments."""
    limit = MAX_UPLOAD_REQUEST_BYTES if request.endpoint in _UPLOAD_ENDPOINTS else MAX_SMALL_REQUEST_BYTES
    request.max_content_length = limit
    if request.content_length is not None and request.content_length > limit:
        return error_response(413, _TOO_BIG if limit == MAX_UPLOAD_REQUEST_BYTES
                              else "That request is too large for Email Boss to handle.")
    return None


@bp.errorhandler(RequestEntityTooLarge)
def _too_large(_e):
    return error_response(413, _TOO_BIG if request.endpoint in _UPLOAD_ENDPOINTS
                          else "That request is too large for Email Boss to handle.")


def _cfg() -> dict:
    return view().get("cfg") or {}


def _mail_cfg() -> dict:
    return _cfg().get("mail") or {}


def _client_enabled() -> bool:
    return bool(_mail_cfg().get("client_enabled", True))


def _send_enabled() -> bool:
    return bool(_mail_cfg().get("send_enabled", False))


def _me() -> str:
    mc = _mail_cfg()
    user = (_cfg().get("user") or {})
    for cand in (((mc.get("imap") or {}).get("user")), user.get("email")):
        if cand:
            return str(cand)
    return ""


def _runner():
    ext = rt()
    return ext.get("mail_runner") if isinstance(ext, dict) else getattr(ext, "mail_runner", None)


def _provider():
    """The live provider: the capture thread's instance when running, else a
    cached one built from config. Raises LookupError with a user-facing
    message when there is none."""
    cfg = _cfg()
    pid = (cfg.get("mail") or {}).get("provider") or "none"
    if pid == "none":
        raise LookupError("No email account is connected yet. Open Settings > Mail to connect one.")
    if pid not in mail_runner.PROVIDER_CLASSES:
        raise LookupError("Email Boss does not recognise the email account type in your settings. "
                          "Open Settings > Mail and pick one again.")
    runner = _runner()
    if runner is not None and getattr(runner, "threads", None):
        t = runner.threads.get(pid)
        if t is not None and getattr(t, "provider", None) is not None:
            return t.provider
    ext = rt()
    injected = ext.get("mail_client_provider") if isinstance(ext, dict) else getattr(ext, "mail_client_provider", None)
    if injected is not None:
        return injected
    with _lock:
        prov = _cache.get(pid)
        if prov is None:
            prov = _cache[pid] = mail_runner.build_provider(cfg, pid)
        return prov


def reset_cache():
    """Called by the config writer when mail settings change."""
    with _lock:
        for p in _cache.values():
            close = getattr(p, "close_client", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        _cache.clear()


def _guard(cap: str | None = None):
    """(provider, None) or (None, error response)."""
    if not _client_enabled():
        return None, error_response(403, "The mail client is turned off. You can turn it back on in Settings > Mail.")
    try:
        prov = _provider()
    except LookupError as e:
        return None, error_response(409, str(e))
    except Exception as e:
        return None, error_response(500, "Email Boss could not open your mailbox. Try again in a moment.",
                                    detail=f"{type(e).__name__}: {e}")
    if cap and not (prov.capabilities() or {}).get(cap):
        return None, error_response(501, f"Your email account type ({prov.id}) does not support this in Email Boss.")
    return prov, None


def _mail_store_for(prov):
    """The local store this provider writes, or None (no cache dir, unreadable)."""
    try:
        from ..mail.store import open_store
        cache = getattr(prov, "cache_dir", None) or mail_runner.mail_cache_dir(_cfg())
        return open_store(cache)
    except Exception:
        return None


def _sid(prov, ref):
    """A client ref as the store's id (IMAP refs differ; the rest are the same)."""
    f = getattr(prov, "store_id", None)
    return f(ref) if callable(f) else ref


def _store_after(prov, fn, *args, **kw):
    """Apply a write to the store; a store problem never fails the write."""
    st = _mail_store_for(prov)
    if st is None:
        return None
    try:
        return fn(st, getattr(prov, "account_id", prov.id), *args, **kw)
    except Exception:
        return None


def _fail(e: Exception):
    """Plain-language error; the technical text rides along as ``detail``."""
    if isinstance(e, MailRefused):
        return error_response(409, str(e))
    msg = f"{type(e).__name__}: {e}"
    low = msg.lower()
    if "not signed in" in low or "login rejected" in low or "no imap password" in low \
            or "authentication" in low or "no smtp password" in low:
        return error_response(401, "Email Boss could not sign in to your mailbox. "
                                   "Check your password or sign in again in Settings > Mail.", detail=msg)
    if "unencrypted" in low:
        return error_response(400, str(e), detail=msg)
    if isinstance(e, (ConnectionError, TimeoutError, OSError)) or "timed out" in low:
        return error_response(502, "Email Boss could not reach your mail server. "
                                   "Check your internet connection and try again.", detail=msg)
    return error_response(502, "Your mail server did not accept that. Try again, and if it keeps happening "
                               "check Settings > Mail.", detail=msg)


def _aliases() -> list[str]:
    return [str(a) for a in ((_cfg().get("user") or {}).get("aliases") or []) if a]


_SAFE_INLINE = frozenset(("image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"))


def _safe_name(name: str, fallback: str = "attachment.bin") -> str:
    """A bare file name: no directories, no control characters."""
    n = str(name or "")
    n = PureWindowsPath(PurePosixPath(n.replace("\\", "/")).name).name
    n = re.sub(r"[\x00-\x1f\x7f]", "", n).strip().strip(".")
    return n[:200] or fallback


def _unique_names(names):
    seen: dict[str, int] = {}
    out = []
    for raw in names:
        n = _safe_name(raw)
        key = n.lower()
        if key in seen:
            seen[key] += 1
            stem, dot, ext = n.rpartition(".")
            n = f"{stem} ({seen[key]}).{ext}" if dot and stem else f"{n} ({seen[key]})"
            seen[n.lower()] = 0
        else:
            seen[key] = 0
        out.append(n)
    return out


def _attachments_from_body(items) -> list[Attachment]:
    out: list[Attachment] = []
    total = 0
    for it in items or []:
        if not isinstance(it, dict):
            continue
        raw = it.get("content_b64") or it.get("contentBytes") or ""
        if (len(raw) * 3 // 4) + total > MAX_ATTACH_BYTES:    # refuse before decoding a huge string
            raise ValueError(f"attachments exceed {MAX_ATTACH_BYTES // (1024 * 1024)} MB")
        try:
            content = base64.b64decode(raw)
        except Exception:
            content = b""
        total += len(content)
        if total > MAX_ATTACH_BYTES:
            raise ValueError(f"attachments exceed {MAX_ATTACH_BYTES // (1024 * 1024)} MB")
        out.append(Attachment(name=str(it.get("name") or "attachment.bin"),
                              content_type=str(it.get("content_type") or it.get("contentType")
                                               or "application/octet-stream"),
                              size=len(content), is_inline=bool(it.get("is_inline")),
                              content_id=it.get("content_id"), content=content))
    return out


def _file(store, pid, email_obj, paths):
    try:
        return store.file_correspondence(pid, email_obj, paths, source="manual")
    except TypeError:
        return store.file_correspondence(pid, email_obj, paths)


def _reply_target(prov, ref: str | None) -> Message | None:
    if not ref:
        return None
    try:
        return prov.get_message(ref)
    except Exception:
        return None


# ── capabilities / folders / list / read ────────────────────────────────────

@bp.route("/api/mail/client/capabilities", methods=["GET"])
def api_caps():
    cfg = _cfg()
    pid = (cfg.get("mail") or {}).get("provider") or "none"
    out = {"ok": True, "provider": pid, "client_enabled": _client_enabled(),
           "send_enabled": _send_enabled(), "me": _me(), "capabilities": {}}
    if pid == "none" or not _client_enabled():
        return jsonify(out)
    try:
        prov = _provider()
        out["capabilities"] = prov.capabilities()
        out["signed_in"] = bool((prov.health() or {}).get("signed_in", True))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return jsonify(out)


@bp.route("/api/mail/client/folders", methods=["GET"])
def api_folders():
    prov, err = _guard("folders")
    if err:
        return err
    try:
        return jsonify({"ok": True, "provider": prov.id, "folders": prov.list_folders()})
    except Exception as e:
        return _fail(e)


@bp.route("/api/mail/client/messages", methods=["GET"])
def api_messages():
    """?folder=&limit=&cursor=&q="""
    prov, err = _guard("list")
    if err:
        return err
    folder = request.args.get("folder") or "inbox"
    q = (request.args.get("q") or "").strip() or None
    if q and not prov.capabilities().get("search"):
        return error_response(501, f"The {prov.id} provider cannot search.")
    try:
        page = prov.list_messages(folder, limit=int_arg("limit", 50),
                                  cursor=request.args.get("cursor") or None, query=q)
        page["ok"] = True
        page["provider"] = prov.id
        return jsonify(page)
    except Exception as e:
        return _fail(e)


@bp.route("/api/mail/client/message", methods=["GET"])
def api_message():
    """?ref=  (refs carry ':' '|' and '/', so they ride a query param)"""
    prov, err = _guard("read")
    if err:
        return err
    ref = request.args.get("ref") or ""
    if not ref:
        return error_response(400, "Pick a message first.")
    try:
        msg = prov.get_message(ref)
    except Exception as e:
        return _fail(e)
    if msg is None:
        return error_response(404, "That message is no longer in the mailbox. Reload the list.")
    d = message_dict(msg, flagged=bool(getattr(msg, "flagged", False)))
    d["ok"] = True
    d["provider"] = prov.id
    # Inline images: the reading pane rewrites cid: URLs to this endpoint.
    for a in d["attachments"]:
        a["url"] = f"/api/mail/client/attachment?ref={quote(ref, safe='')}&index={a['index']}"
    if request.args.get("mark_read") in ("1", "true") and not msg.is_read and prov.capabilities().get("flags"):
        try:
            prov.set_flags(ref, read=True)
            d["is_read"] = True
        except Exception:
            pass
    return jsonify(d)


@bp.route("/api/mail/client/attachment", methods=["GET"])
def api_attachment():
    prov, err = _guard("attachments")
    if err:
        return err
    ref = request.args.get("ref") or ""
    idx = int_arg("index", -1)
    if not ref or idx < 0:
        return error_response(400, "That attachment link is incomplete.")
    try:
        msg = prov.get_message(ref)
    except Exception as e:
        return _fail(e)
    if msg is None or idx >= len(msg.attachments):
        return error_response(404, "That attachment could not be found.")
    att = msg.attachments[idx]
    ctype = (att.content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    safe = ctype in _SAFE_INLINE
    disp = "inline" if (att.is_inline and safe) else "attachment"
    fname = _safe_name(att.name)
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', "_", fname)
    return Response(att.content or b"", mimetype=ctype if safe else "application/octet-stream",
                    headers={"Content-Disposition": f"{disp}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(fname, safe='')}",
                             "Content-Security-Policy": "sandbox",
                             "X-Content-Type-Options": "nosniff"})


# ── flags / move / delete ───────────────────────────────────────────────────

@bp.route("/api/mail/client/flags", methods=["POST"])
def api_flags():
    """Body: {ref, read?, flagged?}"""
    prov, err = _guard("flags")
    if err:
        return err
    data = body()
    ref = data.get("ref") or ""
    if not ref:
        return error_response(400, "Pick a message first.")
    from ..mail import terminal
    try:
        prior = None
        st = _mail_store_for(prov)
        row = st.get(getattr(prov, "account_id", prov.id), _sid(prov, ref)) if st is not None else None
        if row is not None:
            prior = {"read": bool(row.get("is_read")), "flagged": bool(row.get("is_starred"))}
        ok = prov.set_flags(ref, read=data.get("read"), flagged=data.get("flagged"))
        _store_after(prov, terminal.apply_flags, _sid(prov, ref), read=data.get("read"), flagged=data.get("flagged"))
        out = {"ok": bool(ok), "ref": ref}
        if data.get("undoable") and prior is not None:
            inverse = {k: prior[k] for k in ("read", "flagged") if data.get(k) is not None}
            out["action_id"] = _store_after(prov, terminal.record, "flags", _sid(prov, ref), inverse,
                                            data.get("label") or "Change flags")
        return jsonify(out)
    except Exception as e:
        return _fail(e)


@bp.route("/api/mail/client/move", methods=["POST"])
def api_move():
    """Body: {ref, folder}  (folder = a folder id or a kind: inbox|archive|trash|junk)"""
    prov, err = _guard("move")
    if err:
        return err
    data = body()
    ref, folder = data.get("ref") or "", data.get("folder") or ""
    if not ref or not folder:
        return error_response(400, "Pick a message and a folder first.")
    from ..mail import terminal
    try:
        sref = _sid(prov, ref)
        undo_data = _store_after(prov, terminal.undo_data, sref, prov) if data.get("undoable") else None
        new_ref = prov.move_message(ref, folder)
        sid = _store_after(prov, terminal.apply_move, sref, folder, new_ref, prov)
        out = {"ok": True, "ref": ref, "new_ref": new_ref, "folder": folder}
        # A provider that hands the message a new id without saying which (IMAP
        # without COPYUID), or a folder the store only knows as "other" on
        # Graph, leaves nothing to undo against.
        lost = (new_ref is None and prov.id == "imap") or (
            prov.id == "graph" and (undo_data or {}).get("from_folder") == "other")
        if undo_data is not None and not lost:
            undo_data["new_ref"] = sid or new_ref or ref
            if not callable(getattr(prov, "restore_labels", None)):
                undo_data.pop("prior_labels", None)
            out["action_id"] = _store_after(prov, terminal.record, "move", sid or sref, undo_data,
                                            data.get("label") or f"Move to {folder}")
        return jsonify(out)
    except Exception as e:
        return _fail(e)


@bp.route("/api/mail/client/delete", methods=["POST"])
def api_delete():
    """Body: {ref}. Moves to Trash (never a permanent delete)."""
    prov, err = _guard("delete")
    if err:
        return err
    ref = body().get("ref") or ""
    if not ref:
        return error_response(400, "Pick a message first.")
    from ..mail import terminal
    try:
        ok = bool(prov.delete_message(ref))
        _store_after(prov, terminal.apply_move, _sid(prov, ref), "trash", None, prov)
        return jsonify({"ok": ok, "ref": ref})
    except Exception as e:
        return _fail(e)


# ── remote image proxy ("Load images") ──────────────────────────────────────
#
# The page CSP is img-src 'self' data:, so a remote image can only be shown by
# the server fetching it. https only, public addresses only (the connection is
# pinned to the address that was checked), redirects followed only to https and
# re-checked, 10 s and 5 MB overall, image/* only, sent back nosniff + sandboxed.

IMAGE_MAX_BYTES = 5 * 1024 * 1024
IMAGE_TIMEOUT_S = 10.0
IMAGE_MAX_REDIRECTS = 3


class _ImageRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise _ImageRefused(502, "Email Boss could not find that image's server.")
    return sorted({i[4][0] for i in infos})


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")


def _public_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip.version == 6 and (ip in _NAT64 or ip in _SIX_TO_FOUR or ip in _TEREDO):
        return False                         # tunnels that can wrap a private IPv4 address
    return ip.is_global and not ip.is_multicast


def _check_target(url: str) -> tuple[str, str, str]:
    """(host, ip, path) for an https URL that points at a public address."""
    if len(url) > 2000 or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise _ImageRefused(400, "That image address is not valid.")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise _ImageRefused(400, "Email Boss only loads images over a secure (https) connection.")
    host = (parts.hostname or "").strip(".").lower()
    try:
        port = parts.port
    except ValueError:
        port = -1
    if not host or parts.username is not None or parts.password is not None or port not in (None, 443):
        raise _ImageRefused(400, "That image address is not valid.")
    try:
        ipaddress.ip_address(host)
        addrs = [host]                       # a literal address: judged below like any other
    except ValueError:
        if re.fullmatch(r"(?:0x[0-9a-f]*|\d+)", host.rsplit(".", 1)[-1]):
            raise _ImageRefused(400, "That image address is not valid.")     # 2130706433, 0x7f.1: odd IP spellings
        addrs = _resolve(host)
    if not addrs or not all(_public_ip(a) for a in addrs):
        raise _ImageRefused(403, "Email Boss will not load images from that address.")
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    return host, addrs[0], path


class _PinnedHTTPS(http.client.HTTPSConnection):
    """TLS to ``host`` (certificate checked for that name) over a socket opened
    to an address that was already vetted, so DNS cannot change its mind."""

    def __init__(self, host: str, ip: str, timeout: float):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _http_get(host: str, ip: str, path: str, timeout: float):
    """(status, headers, response, connection). Split out so tests can stub the network."""
    conn = _PinnedHTTPS(host, ip, timeout)
    conn.request("GET", path, headers={"Host": host, "Accept": "image/*", "User-Agent": "Mozilla/5.0 (EmailBoss image proxy)",
                                       "Accept-Encoding": "identity", "Connection": "close"})
    resp = conn.getresponse()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp, conn


def fetch_remote_image(url: str) -> tuple[bytes, str]:
    deadline = time.monotonic() + IMAGE_TIMEOUT_S
    for _hop in range(IMAGE_MAX_REDIRECTS + 1):
        host, ip, path = _check_target(url)
        left = deadline - time.monotonic()
        if left <= 0:
            raise _ImageRefused(504, "That image took too long to load.")
        try:
            status, headers, resp, conn = _http_get(host, ip, path, left)
        except _ImageRefused:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise _ImageRefused(502, "Email Boss could not load that image.")
        try:
            if status in (301, 302, 303, 307, 308):
                loc = headers.get("location") or ""
                url = urljoin(f"https://{host}{path}", loc)
                continue                                    # re-validated at the top (https, public)
            if status != 200:
                raise _ImageRefused(502, "The image is no longer available.")
            ctype = (headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if not re.fullmatch(r"image/[a-z0-9.+-]+", ctype):
                raise _ImageRefused(415, "That link is not an image.")
            try:
                if int(headers.get("content-length") or 0) > IMAGE_MAX_BYTES:
                    raise _ImageRefused(413, "That image is too large to load.")
            except ValueError:
                pass
            chunks, size = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise _ImageRefused(504, "That image took too long to load.")
                try:
                    chunk = resp.read(65536)
                except (OSError, http.client.HTTPException, ssl.SSLError):
                    raise _ImageRefused(502, "Email Boss could not load that image.")
                if not chunk:
                    break
                size += len(chunk)
                if size > IMAGE_MAX_BYTES:
                    raise _ImageRefused(413, "That image is too large to load.")
                chunks.append(chunk)
            return b"".join(chunks), ctype
        finally:
            try:
                conn.close()
            except Exception:
                pass
    raise _ImageRefused(502, "That image link redirects too many times.")


@bp.route("/api/mail/client/image", methods=["GET"])
def api_image():
    """?url=<https image>. Bytes of the image, or a plain-language error."""
    if not _client_enabled():
        return error_response(403, "The mail client is turned off. You can turn it back on in Settings > Mail.")
    url = (request.args.get("url") or "").strip()
    if not url:
        return error_response(400, "That image address is not valid.")
    try:
        data, ctype = fetch_remote_image(url)
    except _ImageRefused as e:
        return error_response(e.status, e.message)
    resp = Response(data, mimetype=ctype)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    resp.headers["Cache-Control"] = "private, max-age=3600"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Disposition"] = "inline"
    return resp


# ── compose: reply / forward scaffolding, drafts, send ──────────────────────

@bp.route("/api/mail/client/compose", methods=["POST"])
def api_compose():
    """Body: {ref, mode: reply|reply_all|forward}. Returns the composer's
    starting state: to, cc, subject, body_html (quote), reply_ref, attachments
    (forward only)."""
    prov, err = _guard("read")
    if err:
        return err
    data = body()
    ref, mode = data.get("ref") or "", (data.get("mode") or "reply").lower()
    if not ref:
        return error_response(400, "Pick a message first.")
    try:
        msg = prov.get_message(ref)
    except Exception as e:
        return _fail(e)
    if msg is None:
        return error_response(404, "That message is no longer in the mailbox. Reload the list.")
    block = not bool(data.get("load_images"))
    if mode == "forward":
        atts = [{"name": a.name, "content_type": a.content_type, "size": a.size or len(a.content or b""),
                 "is_inline": a.is_inline, "content_id": a.content_id,
                 "content_b64": base64.b64encode(a.content or b"").decode("ascii")}
                for a in msg.attachments if not a.is_inline]
        q = quote_forward(msg, block_remote=block)
        return jsonify({"ok": True, "mode": mode, "to": "", "cc": "", "subject": forward_subject(msg.subject),
                        "body_html": q, "remote_blocked": block and "data-pb-src" in q,
                        "reply_ref": None, "attachments": atts})
    use_rt = bool(data.get("use_reply_to"))
    to, cc = reply_recipients(msg, _me(), reply_all=(mode == "reply_all"), aliases=_aliases(), use_reply_to=use_rt)
    alt = reply_to_differs(msg)
    q = quote_reply(msg, block_remote=block)
    return jsonify({"ok": True, "mode": mode,
                    "to": ", ".join(a.display() for a in to), "cc": ", ".join(a.display() for a in cc),
                    "recipient_count": len(to) + len(cc),
                    "from_display": msg.sender.display() if msg.sender and msg.sender.email else "",
                    "reply_to_alt": ", ".join(a.display() for a in alt), "use_reply_to": use_rt and bool(alt),
                    "subject": reply_subject(msg.subject), "body_html": q,
                    "remote_blocked": block and "data-pb-src" in q,
                    "reply_ref": ref, "attachments": []})


@bp.route("/api/mail/client/draft", methods=["POST"])
def api_draft():
    """Body: {to, cc, bcc, subject, body_html, reply_ref, attachments[], project_id}.
    Saves into the provider's Drafts when it can; otherwise into the board's
    own draft outbox (/api/drafts)."""
    prov, err = _guard()
    if err:
        return err
    data = body()
    try:
        atts = _attachments_from_body(data.get("attachments"))
    except ValueError as e:
        return error_response(413, _TOO_BIG)
    v = view()
    store = v.get("store")
    reply_to = _reply_target(prov, data.get("reply_ref"))
    where, draft_id = "provider", None
    if prov.capabilities().get("draft"):
        try:
            draft_id = prov.create_draft(reply_to, data.get("to") or "", data.get("cc") or "",
                                         data.get("subject") or "", data.get("body_html") or "", atts)
        except Exception as e:
            return _fail(e)
    else:
        where = "board"
    if where == "board" or data.get("also_board"):
        if store is not None:
            draft_id = store.add_draft(data.get("project_id") or "", data.get("to") or "", data.get("cc") or "",
                                       data.get("subject") or "", data.get("body_html") or "",
                                       created_by="user")
            where = "board"
    pid = data.get("project_id")
    if pid and store is not None and hasattr(store, "project_exists") and store.project_exists(pid):
        try:
            store.add_note(pid, f"Draft saved: {(data.get('subject') or '')[:60]}", source="user")
        except Exception:
            pass
    return jsonify({"ok": True, "where": where, "id": draft_id})


@bp.route("/api/mail/client/send", methods=["POST"])
def api_send():
    """Body: {to, cc, bcc, subject, body_html, reply_ref, attachments[], project_id}."""
    if not _send_enabled():
        return error_response(403, "Sending is turned off, so this can only be saved as a draft. "
                                   "Turn on sending in Settings > Mail to send from Email Boss.", send_enabled=False)
    prov, err = _guard("send")
    if err:
        return err
    data = body()
    to = data.get("to") or ""
    if not Address.parse_many(to) and not Address.parse_many(data.get("cc") or "") \
            and not Address.parse_many(data.get("bcc") or ""):
        return error_response(400, "Add at least one person to send this to.")
    if not (data.get("subject") or data.get("body_html")):
        return error_response(400, "Add a subject or a message before sending.")
    try:
        atts = _attachments_from_body(data.get("attachments"))
    except ValueError as e:
        return error_response(413, _TOO_BIG)
    reply_to = _reply_target(prov, data.get("reply_ref"))
    try:
        result = prov.send_message(to, data.get("cc") or "", data.get("bcc") or "",
                                   data.get("subject") or "", data.get("body_html") or "", atts, reply_to)
    except Exception as e:
        return _fail(e)
    # Bridge: a sent message filed onto its project is the outbound half of the thread.
    pid = data.get("project_id")
    store = view().get("store")
    if pid and store is not None and hasattr(store, "project_exists") and store.project_exists(pid):
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            email_obj = {
                "message_id": result.get("message_id") or f"sent:{now}",
                "internet_message_id": result.get("message_id") or "",
                "conversation_id": reply_to.conversation_id if reply_to else "",
                "from_name": "", "from_email": _me(), "to": to, "cc": data.get("cc") or "",
                "subject": data.get("subject") or "", "body_preview": "",
                "body": data.get("body_html") or "", "body_raw": data.get("body_html") or "",
                "received_at": now, "has_attachments": bool(atts),
                "attachments": [{"name": a.name, "contentType": a.content_type, "size": a.size, "isInline": False}
                                for a in atts],
                "direction": "outbound", "recipients": [],
            }
            _file(store, pid, email_obj, [])
            store.add_note(pid, f"Sent: {(data.get('subject') or '')[:80]}", source="user")
            if reply_to and reply_to.conversation_id:
                store.routing_set(reply_to.conversation_id, pid, source="send")
        except Exception as e:
            result["project_note"] = f"sent, but filing on the project failed: {e}"
    result["ok"] = True
    return jsonify(result)


# ── bridges into the board ──────────────────────────────────────────────────

@bp.route("/api/mail/client/stage", methods=["POST"])
def api_stage():
    """Body: {ref}. Writes the message into the mail cache and runs one triage
    cycle, so it lands on the triage panel exactly like captured mail would.
    A message the board has already processed answers {already: true}."""
    prov, err = _guard("read")
    if err:
        return err
    ref = body().get("ref") or ""
    if not ref:
        return error_response(400, "Pick a message first.")
    v = view()
    store, cfg = v.get("store"), v.get("cfg") or {}
    try:
        msg = prov.get_message(ref)
    except Exception as e:
        return _fail(e)
    if msg is None:
        return error_response(404, "That message is no longer in the mailbox. Reload the list.")
    if store is not None and store.message_seen(msg.id):
        return jsonify({"ok": True, "already": True, "message_id": msg.id})
    cache = mail_runner.mail_cache_dir(cfg)
    try:
        write_record(cache, msg)
    except Exception as e:
        return error_response(500, "Email Boss could not copy that message to the triage list.", detail=str(e))
    from ..triage.pipeline import run_cycle
    summary = run_cycle(store, cfg, v.get("profession") or {}, llm=v.get("llm"), mail_cache=cache)
    return jsonify({"ok": True, "already": False, "message_id": msg.id,
                    "staged": summary.get("staged"), "auto_committed": summary.get("auto_committed"),
                    "cards": summary.get("cards")})


@bp.route("/api/mail/client/file", methods=["POST"])
def api_file():
    """Body: {ref, project_id}. Files the message straight onto a project as
    correspondence (attachments included) and registers its thread so the
    rest of the conversation auto-files."""
    prov, err = _guard("read")
    if err:
        return err
    data = body()
    ref, pid = data.get("ref") or "", data.get("project_id") or ""
    if not ref or not pid:
        return error_response(400, "Pick a message and a project first.")
    v = view()
    store, cfg = v.get("store"), v.get("cfg") or {}
    if store is None or not store.project_exists(pid):
        return error_response(404, "That project no longer exists.")
    try:
        msg = prov.get_message(ref)
    except Exception as e:
        return _fail(e)
    if msg is None:
        return error_response(404, "That message is no longer in the mailbox. Reload the list.")
    from ..triage.normalize import normalize_record
    from ..triage.pipeline import register_conversation
    rec = msg.to_pa_record()
    email_obj = normalize_record(rec, f"{msg.folder}/{msg.id}.json", cfg)
    # Attachments: hand the store the bytes through temp files it reads.
    import tempfile
    from pathlib import Path
    paths = []
    tmpdir = None
    if msg.attachments:
        tmpdir = tempfile.mkdtemp(prefix="pbmail_")
        real = [a for a in msg.attachments if not a.is_inline]
        for a, nm in zip(real, _unique_names(a.name for a in real)):
            p = Path(tmpdir) / nm
            p.write_bytes(a.content or b"")
            paths.append(str(p))
    corr_id = _file(store, pid, email_obj, paths)
    register_conversation(store, email_obj, pid, source="manual")
    store.message_mark(msg.id, "filed", pid=pid)
    store.add_note(pid, f"Filed from mail: {email_obj.get('direction')} from "
                        f"{email_obj.get('from_name') or email_obj.get('from_email', '')}: "
                        f"{(msg.subject or '')[:100]}", source="user")
    if tmpdir:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    return jsonify({"ok": True, "project_id": pid, "correspondence_id": corr_id, "message_id": msg.id})
