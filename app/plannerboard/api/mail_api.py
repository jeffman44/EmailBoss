"""
/api/mail/*: providers (the setup catalog), test, status, run, oauth/start,
oauth/callback.

The provider classes live in plannerboard.mail; this blueprint only builds
one from the posted settings, asks it, and reports. Passwords posted to
/api/mail/test are used for that one connection attempt and never stored;
saving credentials is the setup / config endpoints' job (keyring only).
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..config import deep_merge
from ..mail import runner as mail_runner
from ..mail.imap import preset_list
from ._common import body, error_response, rt, view

bp = Blueprint("mail_api", __name__)

OAUTH_PROVIDERS = ("gmail", "graph")


def _settings_cfg(cfg: dict, provider: str, settings: dict | None) -> dict:
    """Overlay posted settings onto a copy of the config document."""
    settings = dict(settings or {})
    settings.pop("password", None)
    over = {"mail": {"provider": provider}}
    if provider == "pa_folder" and settings.get("mail_cache"):
        over["paths"] = {"mail_cache": settings["mail_cache"]}
    elif provider in ("imap", "gmail", "graph") and settings:
        over["mail"][provider] = settings
    return deep_merge(cfg or {}, over)


def _build(cfg: dict, provider: str, settings: dict | None):
    if provider not in mail_runner.PROVIDER_CLASSES:
        raise KeyError(provider)
    return mail_runner.build_provider(_settings_cfg(cfg, provider, settings), provider)


def _runner():
    ext = rt()
    return ext.get("mail_runner") if isinstance(ext, dict) else getattr(ext, "mail_runner", None)


def _persist_config(over: dict) -> None:
    """Merge `over` into the live config and write it to disk (real Runtime), or
    into the dict a test harness installed."""
    ext = rt()
    if isinstance(ext, dict):
        ext["cfg"] = deep_merge(ext.get("cfg") or {}, over)
        return
    conf = getattr(ext, "config", None)
    if conf is None:
        return
    conf.update(over)
    conf.write()


def _board_port(cfg: dict) -> int:
    try:
        return int(((cfg or {}).get("server") or {}).get("port") or 8080)
    except (TypeError, ValueError):
        return 8080


@bp.route("/api/mail/providers", methods=["GET"])
def api_mail_providers():
    v = view()
    cfg = v.get("cfg") or {}
    catalog = mail_runner.provider_catalog()
    for entry in catalog:
        entry["oauth"] = entry["id"] in OAUTH_PROVIDERS
    catalog.append({"id": "none", "label": "No mail (board only)", "fields": [], "oauth": False})
    return jsonify({"ok": True, "providers": catalog,
                    "current": (cfg.get("mail") or {}).get("provider") or "none",
                    "configured": mail_runner.configured_provider_ids(cfg),
                    "imap_presets": preset_list()})


@bp.route("/api/mail/test", methods=["POST"])
def api_mail_test():
    """Body: {provider, settings:{...}, password?}. Never echoes the password."""
    v = view()
    data = body()
    provider = (data.get("provider") or (v.get("cfg") or {}).get("mail", {}).get("provider") or "").strip().lower()
    if not provider or provider == "none":
        return error_response(400, "provider required")
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {
        k: val for k, val in data.items() if k not in ("provider", "password")}
    password = data.get("password") or (settings or {}).get("password")
    try:
        prov = _build(v.get("cfg") or {}, provider, settings)
    except KeyError:
        return error_response(400, f"unknown mail provider '{provider}'")
    except Exception as e:
        return error_response(400, f"{type(e).__name__}: {e}")
    if password and provider == "imap":
        prov._password = lambda: (password, "request")      # this attempt only; nothing stored
    try:
        res = prov.test_connection()
        out = res.to_dict()
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out["provider"] = provider
    out.pop("password", None)
    return jsonify(out)


@bp.route("/api/mail/status", methods=["GET"])
def api_mail_status():
    v = view()
    cfg = v.get("cfg") or {}
    runner = _runner()
    if runner is not None and getattr(runner, "threads", None):
        out = runner.health_summary()
        out["ok"] = bool(out.get("ok"))
        return jsonify({"ok": True, **out})
    provider = (cfg.get("mail") or {}).get("provider") or "none"
    if provider == "none":
        return jsonify({"ok": True, "provider": "none", "last_fetch": None, "error": None, "providers": {}})
    try:
        prov = _build(cfg, provider, None)
        h = prov.health()
    except Exception as e:
        h = {"provider": provider, "ok": False, "error": f"{type(e).__name__}: {e}"}
    return jsonify({"ok": True, **h, "provider": provider, "thread": "not started"})


@bp.route("/api/mail/run", methods=["POST"])
def api_mail_run():
    """One capture + triage cycle per configured provider, synchronously."""
    v = view()
    cfg = v.get("cfg") or {}
    runner = _runner()
    results = {}
    if runner is not None and getattr(runner, "threads", None):
        for pid, t in runner.threads.items():
            results[pid] = t.run_once()
    else:
        ids = mail_runner.configured_provider_ids(cfg)
        if not ids:
            # No provider: still run triage over whatever is in the drop folder.
            from ..triage.pipeline import run_cycle
            results["triage"] = run_cycle(v.get("store"), cfg, v.get("profession") or {},
                                          llm=v.get("llm"), mail_cache=mail_runner.mail_cache_dir(cfg))
        for pid in ids:
            try:
                prov = mail_runner.build_provider(cfg, pid)
                t = mail_runner.ProviderThread(prov, cfg, v.get("store"), v.get("profession") or {},
                                               llm=v.get("llm"))
                results[pid] = t.run_once()
            except Exception as e:
                results[pid] = {"provider": pid, "errors": [f"{type(e).__name__}: {e}"]}
        if ids:
            first = results.get(ids[0]) or {}
            results.setdefault("triage", first.get("triage"))
    ext = rt()
    hb = getattr(ext, "heartbeat", None)
    if callable(hb):
        hb("mail_capture", note="manual run")
    return jsonify({"ok": True, "results": results})


@bp.route("/api/mail/oauth/ready", methods=["GET"])
def api_mail_oauth_ready():
    """Is built-in sign-in available (shipped client ids present)?"""
    from ..mail.gmail import oauth_defaults
    d = oauth_defaults()
    return jsonify({"ok": True, "gmail": bool(d["gmail_default_client_id"] and d["gmail_default_client_secret"]),
                    "graph": bool(d["graph_default_client_id"])})


@bp.route("/api/mail/oauth/start", methods=["POST"])
def api_mail_oauth_start():
    """Body: {provider: gmail|graph, settings:{client_id?, tenant?}} (client_id/tenant may also be top-level; blank client_id uses the built-in one). Returns
    the URL to open in the browser; the loopback redirect lands on
    /api/mail/oauth/callback on the board port."""
    v = view()
    data = body()
    provider = (data.get("provider") or "").strip().lower()
    if provider not in OAUTH_PROVIDERS:
        return error_response(400, "provider must be gmail or graph")
    cfg = v.get("cfg") or {}
    settings = dict(data.get("settings")) if isinstance(data.get("settings"), dict) else {}
    # The wizard posts client_id / tenant at the top level; accept both shapes.
    for k in ("client_id", "tenant"):
        if not settings.get(k) and data.get(k):
            settings[k] = data[k]
    settings = {k: (val.strip() if isinstance(val, str) else val) for k, val in settings.items()
                if val not in (None, "")}
    port = int(data.get("port") or _board_port(cfg))
    redirect_uri = f"http://127.0.0.1:{port}/api/mail/oauth/callback"
    try:
        prov = _build(cfg, provider, settings)
        flow = prov.begin_auth(redirect_uri)
    except ValueError as e:
        return error_response(400, str(e))
    except Exception as e:
        return error_response(500, f"{type(e).__name__}: {e}")
    # Persist the pending choice BEFORE the user leaves for the consent page,
    # so the callback can rebuild the same provider from saved config.
    persist = {k: val for k, val in settings.items() if k in ("client_id", "tenant")}
    try:
        _persist_config({"mail": {"provider": provider, provider: persist}})
    except Exception as e:
        return error_response(500, f"could not save mail settings: {type(e).__name__}: {e}")
    return jsonify({"ok": True, "provider": provider, "url": flow["url"], "state": flow["state"],
                    "redirect_uri": redirect_uri})


@bp.route("/api/mail/oauth/callback", methods=["GET"])
def api_mail_oauth_callback():
    """Exchanges the code; the token goes to the keyring through the provider's
    TokenCache. Answers a small HTML page the user can close."""
    from ..mail.oauth import PENDING

    state = request.args.get("state") or ""
    code = request.args.get("code") or ""
    err = request.args.get("error")
    if err:
        return error_response(400, f"OAuth error: {err} {request.args.get('error_description') or ''}".strip())
    if not state or not code:
        return error_response(400, "state and code are required")
    pend = None
    with PENDING._lock:
        pend = dict(PENDING._flows.get(state) or {})
    if not pend:
        return error_response(400, "unknown or expired OAuth state")
    provider = pend.get("provider")
    v = view()
    cfg = v.get("cfg") or {}
    try:
        prov = _build(cfg, provider, None)
        res = prov.finish_auth(state, code)
    except Exception as e:
        return error_response(500, f"{type(e).__name__}: {e}")
    if not res.ok:
        return error_response(400, res.error or "token exchange failed")
    html = ("<!doctype html><html><head><meta charset='utf-8'><title>Email Boss</title></head>"
            "<body style='font-family:system-ui;background:#1b1e24;color:#e6e6e6;padding:40px'>"
            f"<h2>{provider} connected</h2><p>You can close this tab and return to Email Boss.</p>"
            "</body></html>")
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}
