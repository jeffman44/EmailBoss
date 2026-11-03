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

from .base import ProviderState
from .message import write_record
from .pa_folder import PAFolderProvider
from .imap import ImapProvider
from .gmail import GmailProvider
from .graph import GraphProvider

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


def configured_provider_ids(cfg: dict) -> list[str]:
    mail = (cfg or {}).get("mail") or {}
    ids = list(mail.get("providers") or [])
    p = (mail.get("provider") or "").strip().lower()
    if p and p not in ids:
        ids.insert(0, p)
    return [i for i in ids if i in PROVIDER_CLASSES]


def build_provider(cfg: dict, provider_id: str):
    cls = PROVIDER_CLASSES.get(provider_id)
    if cls is None:
        raise KeyError(f"unknown mail provider {provider_id!r}")
    return cls(cfg, mail_cache_dir(cfg))


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
        self.state = ProviderState(self.cache_dir, provider.id)
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.last_cycle: dict | None = None
        self.cycles = 0

    def run_once(self) -> dict:
        """Fetch -> write -> triage. Never raises."""
        started = time.time()
        summary = {"provider": self.provider.id, "fetched": 0, "written": 0,
                   "errors": [], "triage": None,
                   "at": datetime.now().isoformat(timespec="seconds")}
        try:
            for msg in self.provider.fetch_new(self.state):
                summary["fetched"] += 1
                try:
                    write_record(self.cache_dir, msg)
                    summary["written"] += 1
                except Exception as e:
                    summary["errors"].append(f"write {msg.id[:30]}: {type(e).__name__}: {e}")
        except Exception as e:
            summary["errors"].append(f"fetch: {type(e).__name__}: {e}")
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
                                       name=f"mail-{self.provider.id}")
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
        })
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
    for pid in configured_provider_ids(cfg):
        try:
            prov = build_provider(cfg, pid)
        except Exception as e:
            print(f"[mail] provider {pid} not built: {e}")
            continue
        t = ProviderThread(prov, cfg, store, profession, on_cycle=on_cycle, llm=llm,
                           run_triage=run_triage)
        runner.threads[pid] = t
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
