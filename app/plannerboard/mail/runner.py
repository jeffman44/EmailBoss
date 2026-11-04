"""Capture threads: one per configured provider.

Each cycle: ``provider.fetch_new(state)`` -> ``write_record`` (PA files into
``<mail_cache>``) -> ``triage.pipeline.run_cycle`` -> ``on_cycle(summary)``.
The loop pattern (stagger, belt-and-braces guard, never let an exception
kill the thread) is v2/imap_capture.py lines 1036-1071.

Config is a plain dict (DESIGN.md section 2). ``mail.provider`` names one
provider; ``mail.providers`` (a list) may name several.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from . import accounts as accounts_mod
from . import reconcile
from . import sync as backfill
from .base import BaseProvider, ProviderState
from .message import write_record
from .pa_folder import PAFolderProvider
from .imap import ImapProvider
from .gmail import GmailProvider
from .graph import GraphProvider

INGEST_BATCH = 200

PROVIDER_CLASSES = {
    "pa_folder": PAFolderProvider,
    "imap": ImapProvider,
    "gmail": GmailProvider,
    "graph": GraphProvider,
}


def mail_cache_dir(cfg: dict) -> Path:
    paths = (cfg or {}).get("paths") or {}
    mc = paths.get("mail_cache")
    if mc:
        return Path(mc)
    root = paths.get("data_root") or str(Path.home() / "EmailBoss")
    return Path(root) / "mail_cache"


def configured_accounts(cfg: dict) -> list[dict]:
    return accounts_mod.accounts(cfg)


def configured_provider_ids(cfg: dict) -> list[str]:
    mail = (cfg or {}).get("mail") or {}
    ids = list(mail.get("providers") or [])
    p = (mail.get("provider") or "").strip().lower()
    if p and p not in ids:
        ids.insert(0, p)
    return [i for i in ids if i in PROVIDER_CLASSES]


def build_provider(cfg: dict, provider_id: str, account: dict | None = None):
    """A provider instance. With ``account`` (from ``accounts.accounts``) the
    account's settings overlay ``mail.<provider>``, its OAuth token comes
    from its own keyring key and every store row is keyed by its id."""
    cls = PROVIDER_CLASSES.get(provider_id)
    if cls is None:
        raise KeyError(f"unknown mail provider {provider_id!r}")
    if account is None:
        return cls(cfg, mail_cache_dir(cfg))
    acfg = accounts_mod.account_cfg(cfg, account)
    if provider_id in ("gmail", "graph"):
        from .oauth import TokenCache
        prov = cls(acfg, mail_cache_dir(cfg), token_cache=TokenCache(accounts_mod.token_key(account)))
    else:
        prov = cls(acfg, mail_cache_dir(cfg))
    prov.account_id = account["id"]
    return prov


def provider_catalog() -> list[dict]:
    """What the setup wizard renders: id, label and the fields each needs."""
    out = []
    for pid, cls in PROVIDER_CLASSES.items():
        out.append({"id": pid, "label": cls.label,
                    "fields": list(getattr(cls, "required_fields", []))})
    return out


class ProviderThread:
    def __init__(self, provider, cfg, store, profession, on_cycle=None, llm=None,
                 poll_seconds=None, run_triage=True):
        self.provider = provider
        self.cfg = cfg
        self.store = store
        self.profession = profession
        self.on_cycle = on_cycle
        self.llm = llm
        self.run_triage = run_triage
        self.poll_seconds = max(30, int(poll_seconds or ((cfg.get("mail") or {}).get("poll_seconds") or 300)))
        self.cache_dir = mail_cache_dir(cfg)
        self.account_id = getattr(provider, "account_id", None) or provider.id
        self.state = ProviderState(self.cache_dir, self.account_id)
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.last_cycle: dict | None = None
        self.cycles = 0

    def run_once(self) -> dict:
        """Fetch -> write -> triage. Never raises."""
        started = time.time()
        summary = {"provider": self.provider.id, "account": self.account_id, "fetched": 0, "written": 0,
                   "historical": 0, "errors": [], "triage": None,
                   "at": datetime.now().isoformat(timespec="seconds")}
        sync = getattr(self.provider, "sync", None)
        cutoff = self.cutoff_ts()
        self._buf = []
        this_cycle: set = set()
        try:
            for msg in self.provider.fetch_new(self.state):
                sid = getattr(msg, "sync_id", "") or msg.provider_ref
                if sid and sid in this_cycle:
                    continue                  # reported twice in one cycle (buffered, not yet in the store)
                if sid:
                    this_cycle.add(sid)
                summary["fetched"] += 1
                rts = _msg_ts(msg)
                historical = cutoff is not None and rts is not None and rts < cutoff
                if historical:
                    # Before the install cutoff: store and search only, never triage.
                    summary["historical"] += 1
                    if getattr(msg, "pa_path", None) is not None:
                        _set_aside(self.cache_dir, msg.pa_path)
                elif getattr(msg, "skip_write", False):
                    pass                              # already in the cache (drop folder)
                else:
                    try:
                        write_record(self.cache_dir, msg)
                        summary["written"] += 1
                    except Exception as e:
                        summary["errors"].append(f"write {msg.id[:30]}: {type(e).__name__}: {e}")
                        self._requeue(msg, e)
                        continue
                self._ledger(sync, msg, historical)
                if len(self._buf) >= INGEST_BATCH:
                    self._flush(sync)
        except Exception as e:
            summary["errors"].append(f"fetch: {type(e).__name__}: {e}")
        self._flush(sync)
        summary["reconcile"] = self._maybe_reconcile(sync)
        if sync is not None and hasattr(sync, "ingest") and backfill.store_settings(self.cfg)["backfill"] \
                and backfill.supports_backfill(self.provider):
            summary["backfill"] = backfill.step(self.provider, sync, self.cfg, cutoff)
        if self.run_triage:
            try:
                from ..triage.pipeline import run_cycle
                summary["triage"] = run_cycle(self.store, self.cfg, self.profession,
                                              llm=self.llm, mail_cache=self.cache_dir)
            except Exception as e:
                summary["errors"].append(f"triage: {type(e).__name__}: {e}")
        summary["seconds"] = round(time.time() - started, 2)
        self.last_cycle = summary
        self.cycles += 1
        if self.on_cycle:
            try:
                self.on_cycle(summary)
            except Exception:
                pass
        return summary

    def cutoff_ts(self) -> float | None:
        """``app_meta.install_cutoff`` as a timestamp; None without a board
        store (unit tests, tools), which means no gate."""
        getter = getattr(self.store, "meta_get", None)
        if not callable(getter):
            return None
        try:
            raw = getter("install_cutoff")
        except Exception:
            return None
        return _iso_ts(raw)

    def _ledger(self, sync, msg, historical: bool = False) -> None:
        """Captured: store the message (identity, headers, body, indexes)."""
        if sync is None:
            return
        sid = getattr(msg, "sync_id", "") or msg.provider_ref
        if not sid:
            return
        try:
            if hasattr(sync, "ingest"):
                since = backfill.body_since(self.cfg)
                rts = _msg_ts(msg)
                with_body = since is None or rts is None or rts >= since
                texts = {}
                if with_body:
                    from .index import attachment_text
                    for i, a in enumerate(msg.attachments):     # outside the store lock
                        if not a.is_inline:
                            try:
                                texts[i] = (attachment_text(a) or "")[:200_000]
                            except Exception:
                                texts[i] = ""
                self._buf.append((msg, historical, with_body, texts))
                return
            else:
                sync.upsert(self.account_id, sid,
                            folder=getattr(msg, "sync_folder", "") or msg.folder,
                            labels=getattr(msg, "sync_labels", None) or getattr(msg, "labels", None),
                            flags=getattr(msg, "sync_flags", None),
                            message_id=msg.internet_message_id)
            retry = getattr(self.provider, "retry", None)
            if retry is not None:
                retry.done(sid)
        except Exception as e:  # noqa: BLE001
            note = getattr(self.provider, "note_warning", None)
            if callable(note):
                note(f"mail store write failed ({type(e).__name__}: {str(e)[:120]})")
            if historical:
                # Nothing else holds it (no triage record): queue a re-fetch.
                self._requeue(msg, e)

    def _flush(self, sync) -> None:
        """Ingest the buffered messages in one store transaction."""
        buf, self._buf = getattr(self, "_buf", []) or [], []
        if not buf or sync is None:
            return
        try:
            with sync.batch():
                if not getattr(self, "_account_known", False):
                    sync.ensure_account(self.account_id, self.provider.id,
                                        getattr(self.provider, "user_email", "") or "")
                for msg, historical, with_body, texts in buf:
                    sync.ingest(self.account_id, msg, historical=historical, with_body=with_body,
                                captured=True, att_texts=texts)
                retry = getattr(self.provider, "retry", None)
                if retry is not None:
                    pending = retry.pending_ids()
                    for msg, *_ in buf:
                        sid = getattr(msg, "sync_id", "") or msg.provider_ref
                        if sid in pending:
                            retry.done(sid)
            self._account_known = True
        except Exception as e:  # noqa: BLE001
            note = getattr(self.provider, "note_warning", None)
            if callable(note):
                note(f"mail store write failed ({type(e).__name__}: {str(e)[:120]})")
            for msg, historical, *_ in buf:
                if historical:
                    self._requeue(msg, e)          # nothing else holds it

    def _requeue(self, msg, err) -> None:
        """The provider handed us the message but it could not be written:
        the provider's cursor has moved, so queue a re-fetch."""
        sid = getattr(msg, "sync_id", "") or msg.provider_ref
        can_refetch = type(self.provider).fetch_item is not BaseProvider.fetch_item \
            if isinstance(self.provider, BaseProvider) else False
        if sid and can_refetch:
            self.provider.fetch_failed(sid, getattr(msg, "sync_folder", "") or msg.folder, err)

    def _maybe_reconcile(self, sync):
        if sync is None or not isinstance(self.provider, BaseProvider) or \
                type(self.provider).server_counts is BaseProvider.server_counts:
            return None
        interval = int(((self.cfg or {}).get("mail") or {}).get("reconcile_seconds")
                       or reconcile.DEFAULT_INTERVAL_S)
        try:
            if not reconcile.is_due(sync, self.account_id, interval):
                return None
            return reconcile.run(self.provider, sync)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    def _loop(self):
        time.sleep(5)
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception as e:                       # belt AND braces
                self.last_cycle = {"provider": self.provider.id,
                                   "errors": [f"loop guard: {type(e).__name__}: {e}"]}
            self.stop_event.wait(self.poll_seconds)

    def start(self):
        if self.thread and self.thread.is_alive():
            return self.thread
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True,
                                       name=f"mail-{self.account_id}")
        self.thread.start()
        return self.thread

    def stop(self):
        self.stop_event.set()

    def status(self) -> dict:
        h = {}
        try:
            h = self.provider.health()
        except Exception as e:
            h = {"provider": self.provider.id, "ok": False, "error": str(e)}
        h.update({
            "thread": "alive" if (self.thread and self.thread.is_alive()) else "stopped",
            "poll_seconds": self.poll_seconds,
            "cycles": self.cycles,
            "last_cycle": self.last_cycle,
            "state_warnings": list(self.state.warnings),
            "queued_ids": len(self.state.get("pending_ids") or []),
        })
        try:
            h["sync"] = self.provider.sync_status() if hasattr(self.provider, "sync_status") else {}
        except Exception as e:  # noqa: BLE001
            h["sync"] = {"error": str(e)}
        return h


class MailRunner:
    def __init__(self):
        self.threads: dict[str, ProviderThread] = {}

    def status(self) -> dict:
        return {pid: t.status() for pid, t in self.threads.items()}

    def health_summary(self) -> dict:
        """The ``mail{provider, ok, last_fetch, error}`` block for /api/health."""
        if not self.threads:
            return {"provider": "none", "ok": True, "last_fetch": None, "error": None}
        pid, t = next(iter(self.threads.items()))
        h = t.status()
        return {"provider": pid if len(self.threads) == 1 else ",".join(self.threads),
                "ok": all(x.status().get("ok", False) for x in self.threads.values()),
                "last_fetch": h.get("last_fetch"), "error": h.get("error"),
                "providers": self.status()}

    def stop_all(self):
        for t in self.threads.values():
            t.stop()


def start_all(cfg: dict, store, profession: dict, on_cycle=None, llm=None,
              run_triage: bool = True, autostart: bool = True) -> MailRunner:
    """Build a thread per configured provider and (by default) start them."""
    runner = MailRunner()
    for acct in configured_accounts(cfg):
        pid = acct["provider"]
        try:
            prov = build_provider(cfg, pid, acct)
        except Exception as e:
            print(f"[mail] account {acct['id']} ({pid}) not built: {e}")
            continue
        t = ProviderThread(prov, cfg, store, profession, on_cycle=on_cycle, llm=llm,
                           run_triage=run_triage)
        runner.threads[acct["id"]] = t
        if autostart:
            t.start()
    return runner


def wire_runtime(rt, llm=None, autostart: bool = True) -> MailRunner:
    """Attach the mail layer to the app's Runtime object (``app.extensions['pb']``):
    sets ``rt.mail_runner``, replaces the ``rt.mail_status`` hook with the live
    health block, installs ``rt.run_triage_cycle`` and starts the capture
    threads. Config is read from ``rt.config.doc``; the profession from
    ``rt.profession()``."""
    config = getattr(rt, "config", None)
    cfg = getattr(config, "doc", config) or {}
    prof = getattr(rt, "profession", None)
    profession = prof() if callable(prof) else (prof or {})
    store = getattr(rt, "store", None)
    llm = llm or getattr(rt, "llm", None)

    def _on_cycle(summary):
        hb = getattr(rt, "heartbeat", None)
        if callable(hb):
            try:
                hb("mail_capture", note=f"{summary.get('provider')}: {summary.get('fetched', 0)} fetched")
            except Exception:
                pass

    runner = start_all(cfg, store, profession, on_cycle=_on_cycle, llm=llm, autostart=autostart)
    rt.mail_runner = runner
    rt.mail_status = runner.health_summary

    def _run_triage():
        from ..triage.pipeline import run_cycle
        p = rt.profession() if callable(getattr(rt, "profession", None)) else profession
        c = getattr(getattr(rt, "config", None), "doc", cfg) or cfg
        return run_cycle(store, c, p, llm=llm, mail_cache=mail_cache_dir(c))

    rt.run_triage_cycle = _run_triage
    reg = getattr(rt, "register_watcher", None)
    if callable(reg) and runner.threads:
        try:
            poll_min = max(1, int(((cfg.get("mail") or {}).get("poll_seconds") or 300) // 60))
            reg("mail_capture", poll_min * 2, kind="loop", note="mail capture + triage")
        except Exception:
            pass
    return runner


def _msg_ts(msg) -> float | None:
    from .store import msg_time
    return msg_time(msg)


def _iso_ts(raw) -> float | None:
    if not raw:
        return None
    from .message import parse_iso
    dt = parse_iso(str(raw))
    if dt is None:
        return None
    from .store import _ts
    return _ts(dt)


def _set_aside(cache_dir, path) -> None:
    """A drop-folder record from before the install cutoff: move it where
    triage does not look (processed/<today>/historical/)."""
    try:
        dest = Path(cache_dir) / "processed" / datetime.now().strftime("%Y-%m-%d") / "historical"
        dest.mkdir(parents=True, exist_ok=True)
        Path(path).replace(dest / Path(path).name)
    except OSError:
        pass
