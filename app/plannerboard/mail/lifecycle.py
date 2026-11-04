"""Retention, purge and disk usage for local mail (2.0 plan section 5.1).

One function, ``purge``, applies the body retention window to everything
that holds mail text on this computer, together:

- ``mail_messages`` / ``mail_fts`` / ``mail_attachments_fts``: messages
  older than the window keep their headers (still searchable by sender,
  subject, date) and lose body text, snippet and attachment text
- ``mail_cache/processed/<date>/``: triaged raw records older than the window
- ``backups/``: board database snapshots older than the window (they carry
  correspondence text)

``purge_account`` removes one account's rows, cursor state and retry rows.
``usage`` reports bytes on disk per place, for Settings and
``/api/mail/store/usage``.
"""
from __future__ import annotations

import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import sync as backfill


def _dir_bytes(p: Path) -> int:
    total = 0
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def purge(store, cfg: dict, cache_dir, backups_dir=None, now: float | None = None,
          body_days: int | None = None) -> dict:
    """Apply the retention window. ``body_days`` 0 (or the config's 0) keeps everything."""
    now = now or time.time()
    days = backfill.store_settings(cfg)["body_days"] if body_days is None else max(0, int(body_days))
    out = {"body_days": days, "bodies_dropped": 0, "processed_dirs": 0, "backups": 0}
    if days <= 0:
        return out
    cutoff = now - days * 86400
    rids = [r["rowid"] for r in store.query(
        "SELECT rowid FROM mail_messages WHERE body_state='full' AND received_ts IS NOT NULL AND received_ts < ?",
        [cutoff])]
    out["bodies_dropped"] = store.drop_body(rids)

    day = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d")
    proc = Path(cache_dir) / "processed"
    if proc.is_dir():
        for d in proc.iterdir():
            if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", d.name) and d.name < day:
                shutil.rmtree(d, ignore_errors=True)
                out["processed_dirs"] += 1
    if backups_dir:
        bdir = Path(backups_dir)
        if bdir.is_dir():
            for f in bdir.glob("*.db"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        out["backups"] += 1
                except OSError:
                    pass
    if out["bodies_dropped"]:
        with store._conn() as c:
            c.execute("INSERT INTO mail_fts(mail_fts) VALUES ('optimize')")
    store.meta_set("last_purge", str(now))
    return out


def purge_account(store, cache_dir, account_id: str) -> dict:
    n = store.delete_account(account_id)
    removed = []
    for name in (f"state_{account_id}.json", f"state_{account_id}.json.tmp"):
        p = Path(cache_dir) / name
        if p.exists():
            p.unlink()
            removed.append(name)
    return {"account": account_id, "messages_removed": n, "files_removed": removed}


def usage(store, cache_dir, backups_dir=None) -> dict:
    cache = Path(cache_dir)
    sp = Path(store.path)
    store_bytes = sum(_dir_bytes(Path(str(sp) + s)) for s in ("", "-wal", "-shm"))
    rows = store.query("SELECT account_id, COUNT(*) AS n, SUM(body_state='full') AS bodies,"
                       " SUM(historical) AS historical FROM mail_messages WHERE removed=0 GROUP BY account_id")
    out = {
        "store_file": str(sp), "encrypted": bool(getattr(store, "encrypted", False)),
        "bytes": {
            "store": store_bytes,
            "mail_cache_records": sum(_dir_bytes(cache / d) for d in ("inbox", "sent")),
            "mail_cache_attachments": _dir_bytes(cache / "attachments"),
            "mail_cache_processed": _dir_bytes(cache / "processed"),
            "backups": _dir_bytes(Path(backups_dir)) if backups_dir else 0,
        },
        "accounts": [{"account": r["account_id"], "messages": r["n"], "with_body": int(r["bodies"] or 0),
                      "historical": int(r["historical"] or 0)} for r in rows],
    }
    out["bytes"]["total"] = sum(out["bytes"].values())
    return out
