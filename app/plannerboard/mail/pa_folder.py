"""Drop-folder provider: an external flow (Power Automate or anything else)
writes PA records into ``<mail_cache>/inbox|sent`` itself.

``fetch_new`` is a no-op (the files are already where the triage scanner
reads them). ``health`` reports whether the folder is reachable, how old the
newest file is, and how many files are unreadable, so a flow that went quiet
or started writing garbage is visible on /api/health instead of silently
starving the board (the v2 ``email_capture`` sidecar did this job; see
engine/email_processor.py lines 2556-2697 and 3268-3360).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator

from .base import BaseProvider, ConnResult, ProviderState
from .message import Message


class PAFolderProvider(BaseProvider):
    id = "pa_folder"
    label = "Drop folder (Power Automate or any flow)"
    required_fields = [
        {"key": "mail_cache", "label": "Drop folder path", "type": "path",
         "help": "The folder your flow writes inbox/ and sent/ JSON records into."},
    ]

    def __init__(self, cfg: dict, cache_dir, stale_minutes: int = 240):
        super().__init__()
        self.cfg = cfg or {}
        self.cache_dir = Path(cache_dir)
        self.stale_minutes = int(stale_minutes or 240)

    # ── MailProvider ──
    def test_connection(self) -> ConnResult:
        h = self.health()
        return ConnResult(ok=bool(h.get("folder_exists")), detail=h,
                          error=None if h.get("folder_exists") else "folder not found")

    def fetch_new(self, state: ProviderState, limit: int = 2000) -> Iterator[Message]:
        """The records are already where triage reads them. This only feeds
        the local store (search) with records it has not seen, marked
        ``skip_write`` so the runner does not write them again; the runner
        moves a record received before the install cutoff out of triage's
        way (2.0: historical mail never reaches triage)."""
        n = 0
        sync = self.sync
        for kind in ("inbox", "sent"):
            d = self.cache_dir / kind
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.json")):
                if n >= limit:
                    break
                try:
                    with p.open("r", encoding="utf-8") as fh:
                        rec = json.load(fh)
                    msg = Message.from_pa_record(rec, kind)
                except Exception:
                    continue                      # health() counts unreadable files
                sid = str(rec.get("id") or p.stem)
                if sync is not None and sync.captured(self.account_id, sid):
                    continue
                msg.sync_id, msg.sync_folder = sid, kind
                msg.provider, msg.provider_ref = self.id, sid
                msg.skip_write = True
                msg.pa_path = p
                n += 1
                yield msg
        self._record_fetch(n)

    def health(self) -> dict:
        out = self.base_health()
        out.update({
            "folder": str(self.cache_dir),
            "folder_exists": self.cache_dir.is_dir(),
            "newest_file_age_min": None,
            "unreadable": 0,
            "pending": 0,
            "stale": False,
        })
        if not self.cache_dir.is_dir():
            out["ok"] = False
            out["error"] = "drop folder does not exist"
            return out
        newest = 0.0
        pending = 0
        unreadable = 0
        for kind in ("inbox", "sent"):
            d = self.cache_dir / kind
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if not p.is_file() or p.suffix.lower() != ".json":
                    continue
                pending += 1
                try:
                    newest = max(newest, p.stat().st_mtime)
                except OSError:
                    continue
                try:
                    with p.open("r", encoding="utf-8") as fh:
                        json.load(fh)
                except Exception:
                    unreadable += 1
        # processed/ files count toward recency: a flow that is alive but
        # whose output has already been consumed must not read as stale.
        proc = self.cache_dir / "processed"
        if proc.is_dir():
            for p in proc.rglob("*.json"):
                try:
                    newest = max(newest, p.stat().st_mtime)
                except OSError:
                    pass
        if newest:
            age_min = (time.time() - newest) / 60.0
            out["newest_file_age_min"] = round(age_min, 1)
            out["stale"] = age_min > self.stale_minutes
        out["pending"] = pending
        out["unreadable"] = unreadable
        if unreadable:
            out["ok"] = False
            out["error"] = f"{unreadable} unreadable record(s)"
        return out
