"""Shared helpers for the API blueprints.

Two shapes of `app.extensions["pb"]` exist: the core `Runtime` object (the
real app) and a plain dict {store, cfg, profession, llm, mail, ...} that the
mail / ai / license layers use in their own test harnesses. `rt()` returns
whichever is there; `view()` returns a dict-like view that works for both.
"""

from flask import current_app, jsonify, request


def rt():
    """The Runtime behind the current app (or the dict a harness installed)."""
    return current_app.extensions["pb"]


def view():
    """Normalized {store, cfg, profession, llm, mail, secret_getter, ...}."""
    from ..ai.wiring import resolve
    return resolve(current_app.extensions["pb"])


def store():
    ext = rt()
    if isinstance(ext, dict):
        return ext.get("store")
    return ext.store


def cfg_dict():
    """The config DOCUMENT as a dict, whichever shape the runtime has."""
    ext = rt()
    if isinstance(ext, dict):
        return ext.get("cfg") or {}
    doc = getattr(getattr(ext, "config", None), "doc", None)
    return doc if isinstance(doc, dict) else {}


def cfg_get(dotted, default=None):
    cur = cfg_dict()
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def profession_dict():
    ext = rt()
    if isinstance(ext, dict):
        return ext.get("profession") or {}
    prof = getattr(ext, "profession", None)
    return prof() if callable(prof) else (prof or {})


def feature_on(name):
    """Unknown feature names are ON (a typo must never hide an endpoint)."""
    feats = cfg_dict().get("features") or {}
    return bool(feats.get(name, True))


def body(default=None):
    data = request.get_json(force=True, silent=True)
    if data is None:
        return {} if default is None else default
    return data


def error_response(status, message, **extra):
    out = {"ok": False, "error": message}
    out.update(extra)
    return jsonify(out), status


def int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
