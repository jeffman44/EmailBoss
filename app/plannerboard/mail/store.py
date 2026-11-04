"""The local mail store (2.0 plan section 5.1, Phase 1).

One SQLite file per mail cache: ``<mail_cache>/mail_store.sqlite``. It holds
every message the app knows about, per account, with headers for the full
history and body text for a configurable window, plus the search indexes,
the retry queue and the reconcile counts. Nothing in the board database
(``plannerboard.db``) changes; the board keeps its own tables.

Canonical message identity is ``(account_id, provider_id)``; the RFC 5322
Message-ID header is stored beside it (``message_id``) so cross-account and
board joins can use it.

Tables (all ``mail_*``):

- ``mail_meta``          schema_version and per-account backfill cursors
- ``mail_accounts``      one row per account seen
- ``mail_messages``      one row per server item: identity, sync state
                         (folder, labels, flags, removed), headers, snippet
- ``mail_fts``           FTS5 unicode61 over subject / sender / body, prefix
                         index for type-ahead; rowid = mail_messages.rowid
- ``mail_tri``           FTS5 trigram over addresses, attachment filenames and
                         part-number-like codes (substring search)
- ``mail_threads``       per-account thread rollup (count, last date)
- ``mail_attachments``   attachment metadata per message
- ``mail_attachments_fts`` attachment name + extracted text
- ``mail_people``        addresses seen, with sent/received counts (reply affinity)
- ``mail_retry_queue``, ``mail_reconcile``  (Phase 0; ``provider`` column = account id)

The Phase 0 sync ledger (``sync.sqlite``) is folded in on first open.

Encryption: when ``PB_STORE_ENCRYPT=1`` and the ``sqlcipher3`` module is
importable, the file is opened through SQLCipher with a random key kept in
the OS keyring (``mail_store_key``). Otherwise plaintext. Either way
``secure_delete`` is on.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

STORE_NAME = "mail_store.sqlite"
LEGACY_SYNC_NAME = "sync.sqlite"
SCHEMA_VERSION = 1

# Forward-only, numbered. Each entry runs once, in order, inside the
# migration step that records its number in mail_meta.schema_version.
MIGRATIONS: list[tuple[int, str]] = [
    (1, """
CREATE TABLE IF NOT EXISTS mail_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS mail_accounts (
    account_id  TEXT PRIMARY KEY,
    provider    TEXT NOT NULL DEFAULT '',
    email       TEXT NOT NULL DEFAULT '',
    label       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_messages (
    rowid          INTEGER PRIMARY KEY,
    account_id     TEXT NOT NULL,
    provider_id    TEXT NOT NULL,
    message_id     TEXT NOT NULL DEFAULT '',
    thread_key     TEXT NOT NULL DEFAULT '',
    folder         TEXT NOT NULL DEFAULT 'inbox',
    labels         TEXT NOT NULL DEFAULT '[]',
    flags          TEXT NOT NULL DEFAULT '[]',
    subject        TEXT NOT NULL DEFAULT '',
    from_email     TEXT NOT NULL DEFAULT '',
    from_name      TEXT NOT NULL DEFAULT '',
    to_addrs       TEXT NOT NULL DEFAULT '',
    cc_addrs       TEXT NOT NULL DEFAULT '',
    received_ts    REAL,
    snippet        TEXT NOT NULL DEFAULT '',
    has_attachments INTEGER NOT NULL DEFAULT 0,
    is_read        INTEGER NOT NULL DEFAULT 0,
    is_starred     INTEGER NOT NULL DEFAULT 0,
    body_state     TEXT NOT NULL DEFAULT 'none',
    historical     INTEGER NOT NULL DEFAULT 0,
    captured       INTEGER NOT NULL DEFAULT 0,
    removed        INTEGER NOT NULL DEFAULT 0,
    removed_reason TEXT NOT NULL DEFAULT '',
    first_seen     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    UNIQUE (account_id, provider_id)
);
CREATE INDEX IF NOT EXISTS mail_messages_folder ON mail_messages (account_id, folder, removed);
CREATE INDEX IF NOT EXISTS mail_messages_date ON mail_messages (received_ts);
CREATE INDEX IF NOT EXISTS mail_messages_msgid ON mail_messages (message_id);
CREATE INDEX IF NOT EXISTS mail_messages_thread ON mail_messages (account_id, thread_key);
CREATE INDEX IF NOT EXISTS mail_messages_from ON mail_messages (from_email);
CREATE VIRTUAL TABLE IF NOT EXISTS mail_fts USING fts5(
    subject, sender, body, tokenize='unicode61 remove_diacritics 2', prefix='2 3');
CREATE VIRTUAL TABLE IF NOT EXISTS mail_tri USING fts5(
    from_a, to_a, cc_a, files, codes, tokenize='trigram');
CREATE TABLE IF NOT EXISTS mail_threads (
    account_id  TEXT NOT NULL,
    thread_key  TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0,
    last_ts     REAL,
    PRIMARY KEY (account_id, thread_key)
);
CREATE TABLE IF NOT EXISTS mail_attachments (
    rowid        INTEGER PRIMARY KEY,
    msg_rowid    INTEGER NOT NULL,
    idx          INTEGER NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    is_inline    INTEGER NOT NULL DEFAULT 0,
    content_id   TEXT NOT NULL DEFAULT '',
    UNIQUE (msg_rowid, idx)
);
CREATE VIRTUAL TABLE IF NOT EXISTS mail_attachments_fts USING fts5(
    name, text, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS mail_people (
    email       TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    sent_count  INTEGER NOT NULL DEFAULT 0,
    recv_count  INTEGER NOT NULL DEFAULT 0,
    last_ts     REAL
);
CREATE TABLE IF NOT EXISTS mail_retry_queue (
    provider      TEXT NOT NULL,
    item_id       TEXT NOT NULL,
    folder        TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_at       REAL NOT NULL,
    last_error    TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    gave_up       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, item_id)
);
CREATE TABLE IF NOT EXISTS mail_reconcile (
    provider      TEXT NOT NULL,
    folder        TEXT NOT NULL,
    server_total  INTEGER,
    local_total   INTEGER NOT NULL,
    not_local     INTEGER,
    error         TEXT NOT NULL DEFAULT '',
    checked_at    REAL NOT NULL,
    PRIMARY KEY (provider, folder)
);
"""),
]

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path), threading.RLock())


def _ts(dt) -> float | None:
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return float(dt)
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    try:
        from .message import parse_iso
        p = parse_iso(str(dt))
        return _ts(p) if p else None
    except Exception:
        return None


def encryption_wanted() -> bool:
    return os.environ.get("PB_STORE_ENCRYPT") == "1"


def _cipher_module():
    try:
        from sqlcipher3 import dbapi2 as sqlcipher  # optional dependency
        return sqlcipher
    except Exception:
        return None


def _store_key(create: bool = True) -> str | None:
    from . import secrets
    key, _src = secrets.get_secret("mail_store_key")
    if not key and not create:
        return None
    if not key:
        key = os.urandom(32).hex()
        ok, _where, err = secrets.set_secret("mail_store_key", key)
        if not ok:
            raise RuntimeError(f"cannot keep the mail store key in the OS keyring: {err}")
    return key


class MailStore:
    """Thread-safe: one connection, every call serialized by a per-file lock."""

    def __init__(self, path, encrypt: bool | None = None):
        self.path = Path(path)
        self.cache_dir = self.path.parent
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = _lock_for(self.path)
        want = encryption_wanted() if encrypt is None else encrypt
        db, mod = self._open(want)
        db.row_factory = (mod or sqlite3).Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA secure_delete=ON")
        self._db = db
        self._migrate()
        self._fold_legacy_ledger()

    # ── plumbing ──
    def _open(self, want: bool):
        """Open (or create) the file. An encrypted file always opens encrypted;
        a plaintext file is converted in place when encryption is wanted and
        SQLCipher is present. Turning encryption off never decrypts."""
        mod = _cipher_module()
        exists = self.path.exists() and self.path.stat().st_size > 0
        plain = exists and _is_plaintext(self.path)
        self.encryption_note = ""
        if exists and not plain:
            key = _store_key(create=False)
            if mod is None or not key:
                raise RuntimeError("The local mail store is encrypted, but SQLCipher or its key is missing "
                                   "on this computer. Search is off until it is restored.")
            db = mod.connect(str(self.path), timeout=30, check_same_thread=False)
            db.execute(f"PRAGMA key = \"x'{key}'\"")
            db.execute("SELECT count(*) FROM sqlite_master").fetchone()
            self.encrypted = True
            return db, mod
        if want and mod is None:
            self.encryption_note = "encryption was requested but SQLCipher is not available; the store is plaintext"
        if want and mod is not None:
            key = _store_key()
            if plain:
                self._encrypt_in_place(mod, key)
            db = mod.connect(str(self.path), timeout=30, check_same_thread=False)
            db.execute(f"PRAGMA key = \"x'{key}'\"")
            self.encrypted = True
            return db, mod
        self.encrypted = False
        return sqlite3.connect(str(self.path), timeout=30, check_same_thread=False), None

    def _encrypt_in_place(self, mod, key: str) -> None:
        tmp = self.path.with_suffix(".enc.tmp")
        if tmp.exists():
            tmp.unlink()
        src = mod.connect(str(self.path))
        try:
            src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            src.execute(f"ATTACH DATABASE ? AS enc KEY \"x'{key}'\"", (str(tmp),))
            src.execute("SELECT sqlcipher_export('enc')")
            src.execute("DETACH DATABASE enc")
        finally:
            src.close()
        for suffix in ("-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                p.unlink()
        os.replace(tmp, self.path)

    def _conn(self):
        return _Tx(self._db, self._lock)

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass

    def schema_version(self) -> int:
        try:
            with self._conn() as c:
                row = c.execute("SELECT value FROM mail_meta WHERE key='schema_version'").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _migrate(self) -> list[int]:
        done = []
        current = self.schema_version()
        for num, sql in MIGRATIONS:
            if num <= current:
                continue
            with self._conn() as c:
                c.executescript(sql)
                c.execute("INSERT INTO mail_meta (key, value) VALUES ('schema_version', ?)"
                          " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(num),))
            done.append(num)
        return done

    def _fold_legacy_ledger(self) -> int:
        """Phase 0 kept the ledger in ``sync.sqlite``; copy it in once, then remove it."""
        legacy = self.cache_dir / LEGACY_SYNC_NAME
        if not legacy.exists():
            return 0
        moved = 0
        try:
            src = sqlite3.connect(str(legacy))
            src.row_factory = sqlite3.Row
            try:
                tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                rows = src.execute("SELECT * FROM mail_sync_ledger").fetchall() if "mail_sync_ledger" in tables else []
                retry = src.execute("SELECT * FROM mail_retry_queue").fetchall() if "mail_retry_queue" in tables else []
                rec = src.execute("SELECT * FROM mail_reconcile").fetchall() if "mail_reconcile" in tables else []
            finally:
                src.close()
            with self._conn() as c:
                for r in rows:
                    c.execute("INSERT OR IGNORE INTO mail_messages (account_id, provider_id, folder, labels, flags,"
                              " message_id, removed, removed_reason, first_seen, updated_at, captured)"
                              " VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                              (r["provider"], r["item_id"], r["folder"], r["labels"], r["flags"], r["message_id"],
                               r["removed"], r["removed_reason"], r["first_seen"], r["updated_at"]))
                    moved += 1
                for r in retry:
                    c.execute("INSERT OR IGNORE INTO mail_retry_queue VALUES (?,?,?,?,?,?,?,?)", tuple(r))
                for r in rec:
                    c.execute("INSERT OR IGNORE INTO mail_reconcile VALUES (?,?,?,?,?,?,?)", tuple(r))
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(legacy) + suffix)
                if p.exists():
                    p.unlink()
        except Exception:
            return moved
        return moved

    # ── meta ──
    def meta_get(self, key: str, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM mail_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(self, key: str, value) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO mail_meta (key, value) VALUES (?, ?)"
                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def meta_delete(self, key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM mail_meta WHERE key=?", (key,))

    # ── accounts ──
    def ensure_account(self, account_id: str, provider: str = "", email: str = "", label: str = "") -> None:
        with self._conn() as c:
            c.execute("INSERT INTO mail_accounts (account_id, provider, email, label, created_at) VALUES (?,?,?,?,?)"
                      " ON CONFLICT(account_id) DO UPDATE SET"
                      " provider=CASE WHEN excluded.provider!='' THEN excluded.provider ELSE provider END,"
                      " email=CASE WHEN excluded.email!='' THEN excluded.email ELSE email END,"
                      " label=CASE WHEN excluded.label!='' THEN excluded.label ELSE label END",
                      (account_id, provider, email, label, time.time()))

    def accounts(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM mail_accounts ORDER BY account_id")]

    # ── ledger API (Phase 0 SyncDB contract; ``provider`` = account id) ──
    def upsert(self, provider: str, item_id: str, folder: str = "inbox",
               labels: Iterable[str] | None = None, flags: Iterable[str] | None = None,
               message_id: str = "") -> None:
        if not item_id:
            return
        now = time.time()
        fl = sorted(set(flags or ()))
        lab = sorted(set(labels or ()))
        with self._conn() as c:
            c.execute(
                "INSERT INTO mail_messages (account_id, provider_id, folder, labels, flags, message_id,"
                " is_read, is_starred, captured, first_seen, updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,?)"
                " ON CONFLICT(account_id, provider_id) DO UPDATE SET folder=excluded.folder, captured=1,"
                " labels=excluded.labels, flags=excluded.flags,"
                " is_read=excluded.is_read, is_starred=excluded.is_starred,"
                " message_id=CASE WHEN excluded.message_id != '' THEN excluded.message_id ELSE message_id END,"
                " removed=0, removed_reason='', updated_at=excluded.updated_at",
                (provider, item_id, folder or "inbox", json.dumps(lab), json.dumps(fl), message_id or "",
                 int(_is_read(fl, lab)), int(_is_starred(fl, lab)), now, now))

    def get(self, provider: str, item_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM mail_messages WHERE account_id=? AND provider_id=?",
                            (provider, item_id)).fetchone()
        return _row(row) if row else None

    def captured(self, provider: str, item_id: str, live_only: bool = False) -> bool:
        """True when the capture path (the one that feeds triage) already took
        this item, or (unless ``live_only``) when the server removed it. A row
        written only by the backfill job is NOT captured: new mail the backfill
        happened to see first must still reach triage."""
        with self._conn() as c:
            row = c.execute("SELECT captured, removed FROM mail_messages WHERE account_id=? AND provider_id=?",
                            (provider, item_id)).fetchone()
        if not row:
            return False
        if row[1]:
            return not live_only
        return bool(row[0])

    def has(self, provider: str, item_id: str, live_only: bool = True) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT removed FROM mail_messages WHERE account_id=? AND provider_id=?",
                            (provider, item_id)).fetchone()
        return bool(row) and (not live_only or not row[0])

    def mark_removed(self, provider: str, item_id: str, reason: str = "deleted") -> bool:
        now = time.time()
        with self._conn() as c:
            cur = c.execute("UPDATE mail_messages SET removed=1, removed_reason=?, updated_at=?"
                            " WHERE account_id=? AND provider_id=? AND removed=0",
                            (reason, now, provider, item_id))
            changed = cur.rowcount > 0
            if not changed:
                c.execute("INSERT OR IGNORE INTO mail_messages (account_id, provider_id, folder, removed,"
                          " removed_reason, first_seen, updated_at) VALUES (?,?,'',1,?,?,?)",
                          (provider, item_id, reason, now, now))
        return changed

    def apply_labels(self, provider: str, item_id: str, added: Iterable[str] = (),
                     removed: Iterable[str] = (),
                     folder_fn: Callable[[set], str] | None = None) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM mail_messages WHERE account_id=? AND provider_id=?",
                            (provider, item_id)).fetchone()
            if not row or row["removed"]:
                return None
            cur = _row(row)
            labels = (set(cur["labels"]) | set(added or ())) - set(removed or ())
            folder = folder_fn(labels) if folder_fn else cur["folder"]
            c.execute("UPDATE mail_messages SET labels=?, folder=?, is_read=?, is_starred=?, updated_at=?"
                      " WHERE account_id=? AND provider_id=?",
                      (json.dumps(sorted(labels)), folder, int(_is_read(cur["flags"], labels)),
                       int(_is_starred(cur["flags"], labels)), time.time(), provider, item_id))
        cur.update(labels=sorted(labels), folder=folder)
        return cur

    def set_flags(self, provider: str, item_id: str, flags: Iterable[str]) -> bool:
        fl = sorted(set(flags or ()))
        with self._conn() as c:
            row = c.execute("SELECT labels FROM mail_messages WHERE account_id=? AND provider_id=?",
                            (provider, item_id)).fetchone()
            if not row:
                return False
            labels = json.loads(row[0] or "[]")
            cur = c.execute("UPDATE mail_messages SET flags=?, is_read=?, is_starred=?, updated_at=?"
                            " WHERE account_id=? AND provider_id=?",
                            (json.dumps(fl), int(_is_read(fl, labels)), int(_is_starred(fl, labels)),
                             time.time(), provider, item_id))
        return cur.rowcount > 0

    def live_ids(self, provider: str, folder: str | None = None, prefix: str = "") -> list[str]:
        sql = "SELECT provider_id FROM mail_messages WHERE account_id=? AND removed=0"
        args: list = [provider]
        if folder is not None:
            sql += " AND folder=?"
            args.append(folder)
        if prefix:
            sql += " AND provider_id LIKE ? ESCAPE '\\'"
            args.append(prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        with self._conn() as c:
            return [r[0] for r in c.execute(sql, args).fetchall()]

    def local_counts(self, provider: str) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT folder, COUNT(*) FROM mail_messages WHERE account_id=? AND removed=0"
                             " GROUP BY folder", (provider,)).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def ledger_rows(self, provider: str, include_removed: bool = True) -> list[dict]:
        sql = "SELECT * FROM mail_messages WHERE account_id=?"
        if not include_removed:
            sql += " AND removed=0"
        with self._conn() as c:
            return [_row(r) for r in c.execute(sql, (provider,)).fetchall()]

    # ── ingest ──
    def ingest(self, account_id: str, msg, historical: bool = False, with_body: bool = True,
               attachment_text: Callable | None = None, captured: bool = False) -> int:
        """Store one captured message: identity, headers, snippet, body text
        for search (quoted text stripped), attachments, thread and people
        rollups. Idempotent: a second ingest of the same item updates it."""
        from .index import attachment_codes, codes_from, prepare_body, sender_text
        sid = getattr(msg, "sync_id", "") or msg.provider_ref or msg.id
        labels = list(getattr(msg, "sync_labels", None) or getattr(msg, "labels", None) or [])
        flags = list(getattr(msg, "sync_flags", None) or [])
        if not flags:
            flags = (["\\Seen"] if msg.is_read else []) + (["\\Flagged"] if getattr(msg, "flagged", False) else [])
        folder = getattr(msg, "sync_folder", "") or msg.folder or "inbox"
        to_s = ", ".join(a.display() for a in msg.to)
        cc_s = ", ".join(a.display() for a in msg.cc)
        body = prepare_body(msg) if with_body else ""
        snippet = msg.preview() if with_body else ""
        real_atts = [a for a in msg.attachments if not a.is_inline]
        files = " ".join(a.name for a in real_atts)
        codes = " ".join(codes_from(f"{msg.subject} {body}") + attachment_codes(real_atts))
        rts = _ts(msg.received_at)
        now = time.time()
        sender_email = (msg.sender.email or "").lower() if msg.sender else ""
        with self._conn() as c:
            prior = c.execute("SELECT rowid, subject FROM mail_messages WHERE account_id=? AND provider_id=?",
                              (account_id, sid)).fetchone()
            is_new_content = prior is None or not prior["subject"]
            c.execute(
                "INSERT INTO mail_messages (account_id, provider_id, message_id, thread_key, folder, labels, flags,"
                " subject, from_email, from_name, to_addrs, cc_addrs, received_ts, snippet, has_attachments,"
                " is_read, is_starred, body_state, historical, captured, first_seen, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(account_id, provider_id) DO UPDATE SET message_id=excluded.message_id,"
                " thread_key=excluded.thread_key, folder=excluded.folder, labels=excluded.labels,"
                " flags=excluded.flags, subject=excluded.subject, from_email=excluded.from_email,"
                " from_name=excluded.from_name, to_addrs=excluded.to_addrs, cc_addrs=excluded.cc_addrs,"
                " received_ts=excluded.received_ts, snippet=excluded.snippet,"
                " has_attachments=excluded.has_attachments, is_read=excluded.is_read,"
                " is_starred=excluded.is_starred, body_state=excluded.body_state,"
                " historical=MIN(historical, excluded.historical), captured=MAX(captured, excluded.captured),"
                " removed=0, removed_reason='',"
                " updated_at=excluded.updated_at",
                (account_id, sid, msg.internet_message_id or "", msg.conversation_id or "", folder,
                 json.dumps(sorted(set(labels))), json.dumps(sorted(set(flags))), msg.subject or "",
                 sender_email, (msg.sender.name if msg.sender else "") or "", to_s, cc_s, rts, snippet,
                 int(bool(real_atts) or bool(getattr(msg, "sync_has_attachments", False))), int(_is_read(flags, labels)), int(_is_starred(flags, labels)),
                 "full" if with_body else "headers", int(bool(historical)), int(bool(captured)), now, now))
            rowid = c.execute("SELECT rowid FROM mail_messages WHERE account_id=? AND provider_id=?",
                              (account_id, sid)).fetchone()[0]
            c.execute("DELETE FROM mail_fts WHERE rowid=?", (rowid,))
            c.execute("INSERT INTO mail_fts (rowid, subject, sender, body) VALUES (?,?,?,?)",
                      (rowid, msg.subject or "", sender_text(msg), body))
            c.execute("DELETE FROM mail_tri WHERE rowid=?", (rowid,))
            c.execute("INSERT INTO mail_tri (rowid, from_a, to_a, cc_a, files, codes) VALUES (?,?,?,?,?,?)",
                      (rowid, sender_text(msg), to_s, cc_s, files, codes))
            old_att = [r[0] for r in c.execute("SELECT rowid FROM mail_attachments WHERE msg_rowid=?", (rowid,))]
            for ar in old_att:
                c.execute("DELETE FROM mail_attachments_fts WHERE rowid=?", (ar,))
            c.execute("DELETE FROM mail_attachments WHERE msg_rowid=?", (rowid,))
            for i, a in enumerate(msg.attachments):
                cur = c.execute("INSERT INTO mail_attachments (msg_rowid, idx, name, content_type, size, is_inline,"
                                " content_id) VALUES (?,?,?,?,?,?,?)",
                                (rowid, i, a.name or "", a.content_type or "", int(a.size or len(a.content or b"")),
                                 int(bool(a.is_inline)), a.content_id or ""))
                if not a.is_inline:
                    text = ""
                    if with_body and attachment_text is not None:
                        try:
                            text = (attachment_text(a) or "")[:200_000]
                        except Exception:
                            text = ""
                    c.execute("INSERT INTO mail_attachments_fts (rowid, name, text) VALUES (?,?,?)",
                              (cur.lastrowid, a.name or "", text))
            if msg.conversation_id:
                c.execute("INSERT INTO mail_threads (account_id, thread_key, subject, message_count, last_ts)"
                          " VALUES (?,?,?,1,?) ON CONFLICT(account_id, thread_key) DO UPDATE SET"
                          " message_count=message_count + ?, last_ts=MAX(COALESCE(last_ts,0), COALESCE(excluded.last_ts,0)),"
                          " subject=CASE WHEN subject='' THEN excluded.subject ELSE subject END",
                          (account_id, msg.conversation_id, msg.subject or "", rts, 1 if is_new_content else 0))
            if is_new_content:
                if folder == "sent":
                    for a in list(msg.to) + list(msg.cc):
                        _bump_person(c, a.email, a.name, "sent_count", rts)
                elif sender_email:
                    _bump_person(c, sender_email, msg.sender.name if msg.sender else "", "recv_count", rts)
        return rowid

    def drop_body(self, rowids: Iterable[int]) -> int:
        """Retention: keep headers, forget body text and snippet."""
        n = 0
        with self._conn() as c:
            for rid in rowids:
                row = c.execute("SELECT subject, from_name, from_email FROM mail_messages WHERE rowid=?", (rid,)).fetchone()
                if not row:
                    continue
                c.execute("UPDATE mail_messages SET snippet='', body_state='headers', updated_at=? WHERE rowid=?",
                          (time.time(), rid))
                f = c.execute("SELECT subject, sender FROM mail_fts WHERE rowid=?", (rid,)).fetchone()
                c.execute("DELETE FROM mail_fts WHERE rowid=?", (rid,))
                c.execute("INSERT INTO mail_fts (rowid, subject, sender, body) VALUES (?,?,?,'')",
                          (rid, f[0] if f else row[0], f[1] if f else f"{row[1]} {row[2]}"))
                for (ar,) in c.execute("SELECT rowid FROM mail_attachments WHERE msg_rowid=?", (rid,)).fetchall():
                    c.execute("UPDATE mail_attachments_fts SET text='' WHERE rowid=?", (ar,))
                n += 1
        return n

    def delete_account(self, account_id: str) -> int:
        with self._conn() as c:
            rids = [r[0] for r in c.execute("SELECT rowid FROM mail_messages WHERE account_id=?", (account_id,))]
            for rid in rids:
                c.execute("DELETE FROM mail_fts WHERE rowid=?", (rid,))
                c.execute("DELETE FROM mail_tri WHERE rowid=?", (rid,))
                for (ar,) in c.execute("SELECT rowid FROM mail_attachments WHERE msg_rowid=?", (rid,)).fetchall():
                    c.execute("DELETE FROM mail_attachments_fts WHERE rowid=?", (ar,))
                c.execute("DELETE FROM mail_attachments WHERE msg_rowid=?", (rid,))
            c.execute("DELETE FROM mail_messages WHERE account_id=?", (account_id,))
            c.execute("DELETE FROM mail_threads WHERE account_id=?", (account_id,))
            c.execute("DELETE FROM mail_retry_queue WHERE provider=?", (account_id,))
            c.execute("DELETE FROM mail_reconcile WHERE provider=?", (account_id,))
            c.execute("DELETE FROM mail_accounts WHERE account_id=?", (account_id,))
            c.execute("DELETE FROM mail_meta WHERE key LIKE ?", (f"backfill:{account_id}%",))
        return len(rids)

    def reindex(self) -> int:
        """Rebuild the FTS structures in place (the footer's Re-index)."""
        with self._conn() as c:
            for t in ("mail_fts", "mail_tri", "mail_attachments_fts"):
                c.execute(f"INSERT INTO {t}({t}) VALUES ('rebuild')")
            c.execute("INSERT INTO mail_fts(mail_fts) VALUES ('optimize')")
            n = c.execute("SELECT COUNT(*) FROM mail_messages WHERE removed=0").fetchone()[0]
        return int(n)

    def count(self, where: str = "1=1", args: tuple = ()) -> int:
        with self._conn() as c:
            return int(c.execute(f"SELECT COUNT(*) FROM mail_messages WHERE {where}", args).fetchone()[0])

    def query(self, sql: str, args: tuple | list = ()) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, tuple(args)).fetchall()]


def _is_plaintext(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(16) == b"SQLite format 3\x00"


def _bump_person(c, email: str, name: str, column: str, ts) -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    c.execute(f"INSERT INTO mail_people (email, name, {column}, last_ts) VALUES (?,?,1,?)"
              f" ON CONFLICT(email) DO UPDATE SET {column}={column}+1,"
              " name=CASE WHEN excluded.name!='' THEN excluded.name ELSE name END,"
              " last_ts=MAX(COALESCE(last_ts,0), COALESCE(excluded.last_ts,0))",
              (email, name or "", ts))


def _is_read(flags, labels) -> bool:
    if labels and "UNREAD" in set(labels):
        return False
    if "\\Seen" in set(flags or ()):
        return True
    return bool(labels) and "UNREAD" not in set(labels)


def _is_starred(flags, labels) -> bool:
    return "\\Flagged" in set(flags or ()) or "STARRED" in set(labels or ())


class _Tx:
    def __init__(self, conn, lock):
        self.conn, self.lock = conn, lock

    def __enter__(self):
        self.lock.acquire()
        return self.conn

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.lock.release()
        return False


def _row(row) -> dict:
    d = dict(row)
    for k in ("labels", "flags"):
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except Exception:
            d[k] = []
    d["removed"] = bool(d.get("removed"))
    d["provider"] = d.get("account_id")
    d["item_id"] = d.get("provider_id")
    return d


_OPEN: dict[str, MailStore] = {}
_OPEN_GUARD = threading.Lock()
LAST_ERROR: dict[str, str] = {}


def store_path(cache_dir) -> Path:
    return Path(cache_dir) / STORE_NAME


def open_store(cache_dir) -> MailStore | None:
    """Shared MailStore per mail cache directory, or None without one."""
    if not cache_dir:
        return None
    p = store_path(cache_dir)
    key = str(p.resolve())
    with _OPEN_GUARD:
        st = _OPEN.get(key)
        if st is None or not st.path.exists():
            try:
                st = _OPEN[key] = MailStore(p)
            except Exception as e:  # noqa: BLE001  (an unreadable store must not stop capture)
                LAST_ERROR[key] = str(e)
                _OPEN.pop(key, None)
                return None
            LAST_ERROR.pop(key, None)
        return st


def open_error(cache_dir) -> str | None:
    return LAST_ERROR.get(str(store_path(cache_dir).resolve())) if cache_dir else None


def forget_open(cache_dir=None) -> None:
    """Close and drop cached handles (tests, account purge, data-root moves)."""
    with _OPEN_GUARD:
        keys = list(_OPEN) if cache_dir is None else [str(store_path(cache_dir).resolve())]
        for k in keys:
            st = _OPEN.pop(k, None)
            if st is not None:
                st.close()


def recency(ts: float | None, now: float | None = None, half_life_days: float = 90.0) -> float:
    if not ts:
        return 0.0
    age = max(0.0, ((now or time.time()) - ts) / 86400.0)
    return math.pow(0.5, age / half_life_days)
