"""Reconcile pass: server totals versus what this computer holds (2.0 plan, Phase 0).

Each provider reports its own server totals through ``server_counts()``:

- Gmail: ``messagesTotal`` of the INBOX and SENT labels
- Graph: ``totalItemCount`` of the inbox and sentitems folders
- IMAP:  ``STATUS <folder> (MESSAGES)`` of the configured inbox and sent folders

The local side is the live row count in ``mail_sync_ledger`` per folder.
``not_local = max(0, server - local)`` is the honest "not on this computer"
number the Phase 1 coverage footer shows. A provider that cannot count
records the error instead of a zero, so a broken count never reads as
"everything is here".
"""
from __future__ import annotations

import time

from .syncdb import SyncDB

DEFAULT_INTERVAL_S = 3600
FOLDERS = ("inbox", "sent")


def run(provider, sync: SyncDB, clock=time.time) -> dict:
    """Count, compare, store. Never raises."""
    pid = getattr(provider, "id", "unknown")
    now = clock()
    local = sync.local_counts(pid)
    out = {"provider": pid, "checked_at": now, "folders": {}, "error": None}
    try:
        server = provider.server_counts() or {}
    except Exception as e:
        server = {}
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    for folder in FOLDERS:
        total = server.get(folder)
        loc = int(local.get(folder, 0))
        not_local = max(0, int(total) - loc) if isinstance(total, int) else None
        err = "" if isinstance(total, int) else (out["error"] or "server did not report a count")
        out["folders"][folder] = {"server_total": total, "local_total": loc,
                                  "not_local": not_local, "error": err}
        with sync._conn() as c:
            c.execute("INSERT INTO mail_reconcile (provider, folder, server_total, local_total, not_local,"
                      " error, checked_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider, folder) DO UPDATE SET"
                      " server_total=excluded.server_total, local_total=excluded.local_total,"
                      " not_local=excluded.not_local, error=excluded.error, checked_at=excluded.checked_at",
                      (pid, folder, total if isinstance(total, int) else None, loc, not_local, err, now))
    return out


def latest(sync: SyncDB, provider_id: str) -> dict:
    """Last stored result per folder for one provider (the coverage footer's source)."""
    with sync._conn() as c:
        rows = c.execute("SELECT folder, server_total, local_total, not_local, error, checked_at"
                         " FROM mail_reconcile WHERE provider=?", (provider_id,)).fetchall()
    return {r["folder"]: dict(r) for r in rows}


def is_due(sync: SyncDB, provider_id: str, interval_s: float = DEFAULT_INTERVAL_S, clock=time.time) -> bool:
    last = latest(sync, provider_id)
    if not last:
        return True
    newest = max((v.get("checked_at") or 0) for v in last.values())
    return clock() - newest >= interval_s
