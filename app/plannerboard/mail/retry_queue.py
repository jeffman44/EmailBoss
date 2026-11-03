"""Retry queue for failed message fetches (2.0 plan, Phase 0).

Before 1.3.x a fetch that raised was logged as a warning and the item was
never asked for again, because every provider advances its cursor past it.
Now a failure is enqueued here and the provider asks for it again at the
start of each later cycle, with exponential backoff, until it succeeds, the
server says it is gone, or ``max_attempts`` is reached. Given-up items stay
in the table and are surfaced in health so the loss is visible, not silent.

Rows live in ``mail_retry_queue`` in the sync database (``syncdb.py``).
"""
from __future__ import annotations

import time

from .syncdb import SyncDB

BASE_DELAY_S = 60
MAX_DELAY_S = 6 * 3600
MAX_ATTEMPTS = 8


def backoff_seconds(attempts: int, base: float = BASE_DELAY_S, cap: float = MAX_DELAY_S) -> float:
    """60 s, 120 s, 240 s ... capped at six hours."""
    return float(min(cap, base * (2 ** max(0, int(attempts) - 1))))


class RetryQueue:
    def __init__(self, sync: SyncDB, provider: str, max_attempts: int = MAX_ATTEMPTS,
                 clock=time.time):
        self.sync = sync
        self.provider = provider
        self.max_attempts = int(max_attempts)
        self.clock = clock

    def enqueue(self, item_id: str, folder: str = "", error: str = "") -> dict:
        """Record a failed fetch (first failure or a repeat). Returns the row."""
        now = self.clock()
        with self.sync._conn() as c:
            row = c.execute("SELECT attempts FROM mail_retry_queue WHERE provider=? AND item_id=?",
                            (self.provider, item_id)).fetchone()
            attempts = (int(row[0]) if row else 0) + 1
            gave_up = 1 if attempts >= self.max_attempts else 0
            next_at = now + backoff_seconds(attempts)
            c.execute(
                "INSERT INTO mail_retry_queue (provider, item_id, folder, attempts, next_at, last_error,"
                " created_at, gave_up) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(provider, item_id) DO UPDATE SET attempts=excluded.attempts,"
                " next_at=excluded.next_at, last_error=excluded.last_error, gave_up=excluded.gave_up,"
                " folder=CASE WHEN excluded.folder != '' THEN excluded.folder ELSE folder END",
                (self.provider, item_id, folder or "", attempts, next_at, (error or "")[:300], now, gave_up))
        return {"item_id": item_id, "attempts": attempts, "next_at": next_at, "gave_up": bool(gave_up)}

    def due(self, limit: int = 50) -> list[dict]:
        """Items whose backoff has elapsed, oldest first. Given-up rows excluded."""
        with self.sync._conn() as c:
            rows = c.execute("SELECT item_id, folder, attempts, last_error FROM mail_retry_queue"
                             " WHERE provider=? AND gave_up=0 AND next_at<=? ORDER BY next_at LIMIT ?",
                             (self.provider, self.clock(), int(limit))).fetchall()
        return [dict(r) for r in rows]

    def done(self, item_id: str) -> None:
        """Fetched, or the server says the item no longer exists."""
        with self.sync._conn() as c:
            c.execute("DELETE FROM mail_retry_queue WHERE provider=? AND item_id=?",
                      (self.provider, item_id))

    def pending_ids(self) -> set[str]:
        with self.sync._conn() as c:
            return {r[0] for r in c.execute(
                "SELECT item_id FROM mail_retry_queue WHERE provider=? AND gave_up=0",
                (self.provider,)).fetchall()}

    def stats(self) -> dict:
        with self.sync._conn() as c:
            row = c.execute("SELECT SUM(gave_up=0), SUM(gave_up=1), MIN(CASE WHEN gave_up=0 THEN next_at END)"
                            " FROM mail_retry_queue WHERE provider=?", (self.provider,)).fetchone()
            last = c.execute("SELECT item_id, last_error FROM mail_retry_queue WHERE provider=? AND gave_up=1"
                             " ORDER BY next_at DESC LIMIT 5", (self.provider,)).fetchall()
        return {"pending": int(row[0] or 0), "gave_up": int(row[1] or 0),
                "next_retry_at": row[2],
                "gave_up_items": [{"item_id": r[0], "error": r[1]} for r in last]}
