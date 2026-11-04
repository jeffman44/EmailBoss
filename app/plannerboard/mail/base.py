"""MailProvider protocol, ProviderState and ConnResult (DESIGN.md section 5).

ProviderState ports the per-folder watermark file of v2/imap_capture.py
lines 314-345 (load_state / save_state, atomic replace, corrupt file =
loud warning not a silent re-download) and generalizes it to one JSON file
per provider: ``<mail_cache>/state_<provider>.json``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable

from .message import Message

STATE_VERSION = 1


def now_local_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class ConnResult:
    ok: bool
    detail: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "error": self.error, **self.detail}


class ProviderState:
    """A small JSON document the provider owns: watermarks, cursors, notes.

    ``data`` is a plain dict. ``get``/``set`` operate on it; ``save`` writes it
    atomically. Nothing secret ever goes here (tokens live in the keyring).
    """

    def __init__(self, cache_dir, provider_id: str):
        self.cache_dir = Path(cache_dir)
        self.provider_id = provider_id
        self.path = self.cache_dir / f"state_{provider_id}.json"
        self.data: dict[str, Any] = {"version": STATE_VERSION}
        self.warnings: list[str] = []
        self.load()

    def load(self) -> dict:
        try:
            if self.path.is_file():
                doc = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    doc.setdefault("version", STATE_VERSION)
                    self.data = doc
                    return doc
        except Exception as e:
            self.warnings.append(
                f"{self.path.name} unreadable ({type(e).__name__}); starting fresh")
        self.data = {"version": STATE_VERSION}
        return self.data

    def save(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.data["updated_at"] = now_local_iso()
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, indent=2, default=str), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except Exception as e:
            self.warnings.append(f"could not write state: {e}")
            return False

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value

    def folder(self, kind: str) -> dict:
        """Per-folder sub-document (inbox / sent)."""
        return self.data.setdefault("folders", {}).setdefault(kind, {})


FOLDER_KINDS = ("inbox", "sent", "drafts", "trash", "junk", "archive", "other")

# Every capability a provider can report. The UI hides what is false; the
# API answers 501 for it. ``send`` is reported true only when the provider
# CAN send; whether it MAY is ``mail.send_enabled`` (the API checks that).
CAPABILITIES = ("folders", "list", "read", "flags", "move", "delete", "search",
                "draft", "send", "attachments")


def folder_dict(id: str, name: str, kind: str = "other", unread: int | None = None,
                total: int | None = None, **extra) -> dict:
    kind = kind if kind in FOLDER_KINDS else "other"
    d = {"id": id, "name": name, "kind": kind, "unread": unread, "total": total}
    d.update(extra)
    return d


def guess_folder_kind(name: str, attrs: str = "") -> str:
    """Map a folder name / IMAP LIST attributes onto a kind (RFC 6154 first)."""
    a = (attrs or "").lower()
    for flag, kind in (("\\sent", "sent"), ("\\drafts", "drafts"), ("\\trash", "trash"),
                       ("\\junk", "junk"), ("\\archive", "archive"), ("\\all", "archive")):
        if flag in a:
            return kind
    n = (name or "").lower().split("/")[-1].split(".")[-1]
    if n == "inbox":
        return "inbox"
    for needle, kind in (("sent", "sent"), ("draft", "drafts"), ("trash", "trash"),
                         ("deleted", "trash"), ("bin", "trash"), ("junk", "junk"), ("spam", "junk"),
                         ("archive", "archive"), ("all mail", "archive")):
        if needle in n:
            return kind
    return "other"


def summary_dict(msg: Message, flagged: bool = False) -> dict:
    """The list-pane row for a message: no body, no attachment bytes."""
    return {
        "ref": msg.provider_ref or msg.id,
        "id": msg.id,
        "subject": msg.subject or "",
        "from": msg.sender.display() if msg.sender and msg.sender.email else "",
        "from_email": msg.sender.email if msg.sender else "",
        "to": ", ".join(a.display() for a in msg.to),
        "date": msg.received_at.isoformat(timespec="seconds") if isinstance(msg.received_at, datetime) else str(msg.received_at),
        "preview": msg.preview(),
        "is_read": bool(msg.is_read),
        "flagged": bool(flagged),
        "has_attachments": any(not a.is_inline for a in msg.attachments),
        "importance": msg.importance,
        "conversation_id": msg.conversation_id,
        "internet_message_id": msg.internet_message_id,
        "folder": msg.folder,
    }


def message_dict(msg: Message, flagged: bool = False) -> dict:
    """The reading-pane payload: summary + body + attachment metadata (bytes
    are fetched one at a time through the attachment endpoint)."""
    d = summary_dict(msg, flagged)
    d.update({
        "body": msg.body or "",
        "is_html": bool(msg.is_html),
        "cc": ", ".join(a.display() for a in msg.cc),
        "reply_to": ", ".join(a.display() for a in msg.reply_to),
        "to_list": [{"email": a.email, "name": a.name} for a in msg.to],
        "cc_list": [{"email": a.email, "name": a.name} for a in msg.cc],
        "attachments": [{"index": i, "name": a.name, "content_type": a.content_type,
                         "size": a.size or len(a.content or b""), "is_inline": bool(a.is_inline),
                         "content_id": a.content_id}
                        for i, a in enumerate(msg.attachments)],
    })
    return d


@runtime_checkable
class MailProvider(Protocol):
    id: str

    def test_connection(self) -> ConnResult: ...

    def fetch_new(self, state: ProviderState, limit: int = 200) -> Iterator[Message]: ...

    def health(self) -> dict: ...

    def create_draft(self, reply_to: Message | None, to, cc, subject, body_html,
                     attachments=()) -> str | None: ...

    # ── mail client (optional; see capabilities()) ──
    def capabilities(self) -> dict: ...

    def list_folders(self) -> list[dict]: ...

    def list_messages(self, folder_id: str, limit: int = 50, cursor: str | None = None,
                      query: str | None = None) -> dict: ...

    def get_message(self, ref: str) -> Message | None: ...

    def set_flags(self, ref: str, read: bool | None = None, flagged: bool | None = None) -> bool: ...

    def move_message(self, ref: str, folder_id: str) -> str | None: ...

    def delete_message(self, ref: str) -> bool: ...

    def send_message(self, to, cc, bcc, subject, body_html, attachments=(),
                     reply_to: Message | None = None) -> dict: ...


class SkipItem(Exception):
    """fetch_item: the item exists but is not something the capture path
    keeps (for example a Gmail message that left INBOX and SENT). The retry
    is dropped without marking the item removed."""


class BaseProvider:
    """Shared plumbing: a status dict every provider reports through health()."""
    id = "base"
    label = "Base"
    # Defaults: a capture-only provider. Subclasses override the ones they add.
    CAPS: dict = {k: False for k in CAPABILITIES}

    def __init__(self):
        self._last_fetch: str | None = None
        self._last_error: str | None = None
        self._fetched_total = 0
        self._warnings: list[str] = []
        self._sync = None
        self._retry = None
        self.clock = None          # tests inject a fake clock for the retry queue
        # 2.0: every store / retry / reconcile / state row is keyed by account.
        # A 1.x-shaped account's id is the provider id.
        self.account_id = self.id

    # ── sync truth (Phase 0): ledger + retry queue in <mail_cache>/sync.sqlite ──
    @property
    def sync(self):
        """The SyncDB for this provider's mail cache, or None without one."""
        if self._sync is None:
            cache = getattr(self, "cache_dir", None)
            if cache:
                from .syncdb import open_sync
                self._sync = open_sync(cache)
        return self._sync

    @sync.setter
    def sync(self, value):
        self._sync = value
        self._retry = None

    @property
    def retry(self):
        if self._retry is None and self.sync is not None:
            import time as _time
            from .retry_queue import RetryQueue
            self._retry = RetryQueue(self.sync, self.account_id, clock=self.clock or _time.time)
        return self._retry

    def fetch_failed(self, item_id: str, folder: str, err: Exception | str) -> None:
        """One fetch failed: queue it for retry (or warn when there is no queue)."""
        text = err if isinstance(err, str) else f"{type(err).__name__}: {err}"
        if self.retry is not None:
            row = self.retry.enqueue(item_id, folder, text)
            if row["gave_up"]:
                self.note_warning(f"{item_id}: gave up after {row['attempts']} attempts ({text[:80]})")
        else:
            self.note_warning(f"{item_id} failed ({text[:80]}); no retry queue, skipped")

    def fetch_item(self, item_id: str, folder: str = "") -> Message | None:
        """Fetch one server item by its sync id (the retry path). Return None
        when the server says it no longer exists; raise on any other failure."""
        raise NotImplementedError(f"{self.id} cannot fetch one item by id")

    def server_counts(self) -> dict:
        """{folder_kind: total} as the server reports it (reconcile.py)."""
        raise NotImplementedError(f"{self.id} cannot count server messages")

    def _drain_retries(self, limit: int = 50) -> Iterator[Message]:
        """Yield every due retry that now succeeds. Gone items are marked
        removed in the ledger; failures are re-enqueued with backoff."""
        if self.retry is None:
            return
        for row in self.retry.due(limit):
            iid, folder = row["item_id"], row.get("folder") or ""
            if self.sync.captured(self.account_id, iid, live_only=True):
                self.retry.done(iid)          # captured meanwhile by the normal path
                continue
            try:
                msg = self.fetch_item(iid, folder)
            except NotImplementedError:
                return
            except SkipItem:
                self.retry.done(iid)
                continue
            except Exception as e:
                self.fetch_failed(iid, folder, e)
                continue
            if msg is None:
                self.retry.done(iid)
                self.sync.mark_removed(self.account_id, iid, "gone on retry")
                continue
            # The row is cleared only after the consumer took the message (the
            # runner clears it on a successful write); a closed generator or a
            # crash here leaves it queued.
            yield msg
            self.retry.done(iid)

    def sync_status(self) -> dict:
        """Retry queue and last reconcile, for health."""
        if self.sync is None:
            return {}
        from . import reconcile as _rec
        try:
            return {"retry": self.retry.stats(), "reconcile": _rec.latest(self.sync, self.account_id)}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    # ── mail client defaults: not supported ──
    def capabilities(self) -> dict:
        return dict(self.CAPS)

    def list_folders(self) -> list[dict]:
        raise NotImplementedError(f"{self.id} cannot list folders")

    def list_messages(self, folder_id, limit=50, cursor=None, query=None) -> dict:
        raise NotImplementedError(f"{self.id} cannot list messages")

    def get_message(self, ref):
        raise NotImplementedError(f"{self.id} cannot read a message on demand")

    def set_flags(self, ref, read=None, flagged=None) -> bool:
        raise NotImplementedError(f"{self.id} cannot change flags")

    def move_message(self, ref, folder_id):
        raise NotImplementedError(f"{self.id} cannot move messages")

    def delete_message(self, ref) -> bool:
        raise NotImplementedError(f"{self.id} cannot delete messages")

    def send_message(self, to, cc, bcc, subject, body_html, attachments=(), reply_to=None) -> dict:
        raise NotImplementedError(f"{self.id} cannot send")

    def note_warning(self, text: str) -> None:
        if text not in self._warnings:
            self._warnings.append(text)

    def _record_fetch(self, n: int, error: str | None = None) -> None:
        self._last_fetch = now_local_iso()
        self._last_error = error
        self._fetched_total += int(n or 0)

    def base_health(self) -> dict:
        return {
            "provider": self.id,
            "account": self.account_id,
            "ok": self._last_error is None,
            "last_fetch": self._last_fetch,
            "error": self._last_error,
            "fetched_total": self._fetched_total,
            "warnings": list(self._warnings),
        }

    def create_draft(self, reply_to, to, cc, subject, body_html, attachments=()):
        return None
