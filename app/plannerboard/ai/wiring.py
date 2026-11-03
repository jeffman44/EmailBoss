"""Glue between the AI layer and the app's Runtime (`app.extensions["pb"]`).

The runtime may be the core `Runtime` object (attributes: config, store,
profession(), mail, ...) or, in tests and small harnesses, a plain dict with
keys {store, cfg, profession, llm, mail, secret_getter}. `resolve()` turns
either into one view the blueprints and the scheduler read.
"""
from __future__ import annotations

import threading
from typing import Any

from . import build_llm
from .assistant import AssistantScheduler

_DEFAULT_STATUS = {"running": False, "last_run": None, "last_summary": None, "next_run_at": None, "error": ""}


class ExtView(dict):
    """dict view over the runtime; writes go to the backing object too."""

    def __init__(self, backing: Any):
        super().__init__()
        self.backing = backing

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if isinstance(self.backing, dict):
            self.backing[key] = value
        else:
            try:
                setattr(self.backing, key, value)
            except Exception:
                pass


def _cfg_dict(config: Any) -> dict:
    if isinstance(config, dict):
        return config
    doc = getattr(config, "doc", None)
    return doc if isinstance(doc, dict) else {}


def resolve(ext: Any) -> ExtView:
    """Normalize the runtime into {store, cfg, profession, llm, mail,
    secret_getter, assistant, assistant_lock, assistant_status}."""
    view = ExtView(ext)
    if isinstance(ext, dict):
        view.update(ext)
        view.setdefault("cfg", {})
        view.setdefault("profession", {})
    else:
        view["store"] = getattr(ext, "store", None)
        view["cfg"] = _cfg_dict(getattr(ext, "config", None))
        prof = getattr(ext, "profession", None)
        view["profession"] = prof() if callable(prof) else (prof or {})
        for key in ("llm", "mail", "secret_getter", "assistant", "assistant_lock", "assistant_status"):
            val = getattr(ext, key, None)
            if val is not None:
                view[key] = val
        if "secret_getter" not in view:
            try:
                from ..config import get_secret
                view["secret_getter"] = get_secret
            except Exception:
                pass
    if view.get("assistant_lock") is None:
        view["assistant_lock"] = threading.Lock()
    if view.get("assistant_status") is None:
        view["assistant_status"] = dict(_DEFAULT_STATUS)
    return view


def license_creds_getter(rt: Any):
    """Lazy reader of the license client's material for the built-in provider.
    The license client is attached by the license middleware, possibly after
    the LLM is built, so look it up on every call."""
    def _get() -> dict:
        client = None
        if isinstance(rt, dict):
            client = rt.get("license")
        else:
            client = getattr(rt, "license", None) or getattr(rt, "license_client", None)
            if client is None:
                ext = getattr(rt, "ext", None)
                if isinstance(ext, dict):
                    client = ext.get("license")
        if client is None:
            return {}
        try:
            st = client.status() or {}
            act = client.stored_activation()
            lid = ""
            if act:
                try:
                    lid = str(client.parse_activation(act).get("lid") or "")
                except Exception:
                    lid = ""
            return {"lid": lid, "fingerprint": client.fingerprint, "activation": act,
                    "server_url": client.server_url, "status": st.get("status", ""),
                    "sign": getattr(client, "sign_request", None)}
        except Exception:
            return {}
    return _get


def install(rt: Any, secret_getter=None, start_scheduler: bool = True) -> Any:
    """Build the LLM from the runtime's config, attach it, wire the health
    hook and (optionally) start the hourly assistant pass. Returns the LLM."""
    view = resolve(rt)
    getter = secret_getter or view.get("secret_getter")
    llm = build_llm(view["cfg"], view["profession"], view["store"], getter,
                    license_creds=license_creds_getter(rt))
    view["llm"] = llm
    if not isinstance(rt, dict):
        try:
            rt.ai_status = llm.status
        except Exception:
            pass
    acfg = (view["cfg"].get("assistant") or {})
    sched = AssistantScheduler(view, int(acfg.get("schedule_minutes") or 60))
    view["assistant"] = sched
    if not isinstance(rt, dict):
        try:
            rt.run_assistant = sched.run_once
        except Exception:
            pass
    if start_scheduler:
        sched.start()
    return llm
