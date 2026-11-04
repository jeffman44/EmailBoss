"""Phase 0 name for the sync ledger, kept as an alias.

The ledger moved into the mail store in Phase 1 (``store.py``): the rows of
``mail_sync_ledger`` are now the identity columns of ``mail_messages``, and
an old ``sync.sqlite`` is folded in and removed on first open. Provider code
keeps calling ``self.sync.upsert / mark_removed / apply_labels / ...``.
"""
from __future__ import annotations

from .store import MailStore, open_store, store_path


def SyncDB(cache_dir) -> MailStore:  # noqa: N802 (kept for the Phase 0 call sites)
    return MailStore(store_path(cache_dir))


def open_sync(cache_dir):
    return open_store(cache_dir)


__all__ = ["SyncDB", "open_sync"]
