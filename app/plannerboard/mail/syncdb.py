"""Sync truth database: one small SQLite file per mail cache.

Phase 0 of the 2.0 plan. Before the full ``mail_*`` store lands (Phase 1)
the app still needs one durable place that knows, per provider, which
server items it has captured, which the server has since removed or
relabelled, which fetches failed, and what the server says its totals
are. That is this file: ``<mail_cache>/sync.sqlite``.

Tables (all ``mail_*``, never in the board database):

- ``mail_sync_ledger``  one row per captured server item: folder, labels,
  flags, Message-ID header, removed flag. The local side of reconcile.
- ``mail_retry_queue``  failed fetches with exponential backoff (see
  ``retry_queue.py``).
- ``mail_reconcile``    last server-vs-local count per provider and folder
  (see ``reconcile.py``).

The ledger holds identity and state only, never bodies. Phase 1 folds it
into ``mail_messages``.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

DB_NAME = "sync.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_sync_ledger (
    provider      TEXT NOT NULL,
    item_id       TEXT NOT NULL,
    folder        TEXT NOT NULL DEFAULT 'inbox',
    labels        TEXT NOT NULL DEFAULT '[]',
    flags         TEXT NOT NULL DEFAULT '[]',
    message_id    TEXT NOT NULL DEFAULT '',
    removed       INTEGER NOT NULL DEFAULT 0,
    removed_reason TEXT NOT NULL DEFAULT '',
    first_seen    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (provider, item_id)
);
CREATE INDEX IF NOT EXISTS mail_sync_ledger_folder
    ON mail_sync_ledger (provider, folder, removed);
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
"""

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


class SyncDB:
    """Thread-safe wrapper: one connection per instance, every call serialized
    by a per-file lock, so the capture thread and the mail-client request
    thread can share it."""

    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)
        self.path = self.cache_dir / DB_NAME
        self._lock = _lock_for(self.path)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA secure_delete=ON")
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        return _Tx(self._db, self._lock)

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass

    # ── ledger ──
    def upsert(self, provider: str, item_id: str, folder: str = "inbox",
               labels: Iterable[str] | None = None, flags: Iterable[str] | None = None,
               message_id: str = "") -> None:
        if not item_id:
            return
        now = time.time()
        lab = json.dumps(sorted(set(labels or ())))
        fl = json.dumps(sorted(set(flags or ())))
        with self._conn() as c:
            c.execute(
                "INSERT INTO mail_sync_ledger (provider, item_id, folder, labels, flags, message_id,"
                " removed, removed_reason, first_seen, updated_at) VALUES (?,?,?,?,?,?,0,'',?,?)"
                " ON CONFLICT(provider, item_id) DO UPDATE SET folder=excluded.folder,"
                " labels=excluded.labels, flags=excluded.flags,"
                " message_id=CASE WHEN excluded.message_id != '' THEN excluded.message_id ELSE message_id END,"
                " removed=0, removed_reason='', updated_at=excluded.updated_at",
                (provider, item_id, folder or "inbox", lab, fl, message_id or "", now, now))

    def get(self, provider: str, item_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM mail_sync_ledger WHERE provider=? AND item_id=?",
                            (provider, item_id)).fetchone()
        return _ledger_row(row) if row else None

    def has(self, provider: str, item_id: str, live_only: bool = True) -> bool:
        row = self.get(provider, item_id)
        return bool(row) and (not live_only or not row["removed"])

    def mark_removed(self, provider: str, item_id: str, reason: str = "deleted") -> bool:
        """Mark one item gone from the server (or from the folder it was in).
        An id the ledger never saw is recorded too, so a later add of the same
        id is not mistaken for a resurrection. Returns True when a live row
        changed."""
        now = time.time()
        with self._conn() as c:
            cur = c.execute("UPDATE mail_sync_ledger SET removed=1, removed_reason=?, updated_at=?"
                            " WHERE provider=? AND item_id=? AND removed=0",
                            (reason, now, provider, item_id))
            changed = cur.rowcount > 0
            if not changed:
                c.execute("INSERT OR IGNORE INTO mail_sync_ledger (provider, item_id, folder, removed,"
                          " removed_reason, first_seen, updated_at) VALUES (?,?,'',1,?,?,?)",
                          (provider, item_id, reason, now, now))
        return changed

    def apply_labels(self, provider: str, item_id: str, added: Iterable[str] = (),
                     removed: Iterable[str] = (),
                     folder_fn: Callable[[set], str] | None = None) -> dict | None:
        """Merge a label delta into a ledger row. Returns the new row, or None
        when the item is not in the ledger (the caller decides whether to fetch)."""
        row = self.get(provider, item_id)
        if not row:
            return None
        labels = (set(row["labels"]) | set(added or ())) - set(removed or ())
        folder = folder_fn(labels) if folder_fn else row["folder"]
        with self._conn() as c:
            c.execute("UPDATE mail_sync_ledger SET labels=?, folder=?, updated_at=?"
                      " WHERE provider=? AND item_id=?",
                      (json.dumps(sorted(labels)), folder, time.time(), provider, item_id))
        row.update(labels=sorted(labels), folder=folder)
        return row

    def set_flags(self, provider: str, item_id: str, flags: Iterable[str]) -> bool:
        with self._conn() as c:
            cur = c.execute("UPDATE mail_sync_ledger SET flags=?, updated_at=? WHERE provider=? AND item_id=?",
                            (json.dumps(sorted(set(flags or ()))), time.time(), provider, item_id))
        return cur.rowcount > 0

    def live_ids(self, provider: str, folder: str | None = None, prefix: str = "") -> list[str]:
        sql = "SELECT item_id FROM mail_sync_ledger WHERE provider=? AND removed=0"
        args: list = [provider]
        if folder is not None:
            sql += " AND folder=?"
            args.append(folder)
        if prefix:
            sql += " AND item_id LIKE ?"
            args.append(prefix.replace("%", "\\%").replace("_", "\\_") + "%")
            sql += " ESCAPE '\\'"
        with self._conn() as c:
            return [r[0] for r in c.execute(sql, args).fetchall()]

    def local_counts(self, provider: str) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT folder, COUNT(*) FROM mail_sync_ledger WHERE provider=? AND removed=0"
                             " GROUP BY folder", (provider,)).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def ledger_rows(self, provider: str, include_removed: bool = True) -> list[dict]:
        sql = "SELECT * FROM mail_sync_ledger WHERE provider=?"
        if not include_removed:
            sql += " AND removed=0"
        with self._conn() as c:
            return [_ledger_row(r) for r in c.execute(sql, (provider,)).fetchall()]


class _Tx:
    """Context manager: lock, then commit or roll back on the way out."""

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


def _ledger_row(row) -> dict:
    d = dict(row)
    for k in ("labels", "flags"):
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except Exception:
            d[k] = []
    d["removed"] = bool(d.get("removed"))
    return d


_OPEN: dict[str, SyncDB] = {}


def open_sync(cache_dir) -> SyncDB | None:
    """Shared SyncDB per cache dir, or None when there is no cache dir."""
    if not cache_dir:
        return None
    key = str(Path(cache_dir).resolve())
    db = _OPEN.get(key)
    if db is None or not db.path.exists():
        db = _OPEN[key] = SyncDB(cache_dir)
    return db
