"""
/api/config, /api/setup, /api/setup/probe, /api/setup/presets,
/api/professions, /api/profession GET/PUT.

Ported from server.py 584-920 (config + setup contract) and extended with the
profession / ai / license fields from DESIGN.md section 2. The reference body
keys (name, email, aliases, data_root, port, preset, mail_source, imap,
imap_password, force) are still accepted; the new shape is
{user:{}, profession, paths:{}, server:{}, mail:{}, ai:{}, license:{}, features:{}}.

Secrets ride ONE request and go to the keyring; they never enter the document
and never come back from any endpoint.
"""

from pathlib import Path

from flask import Blueprint, jsonify, request

from .. import config as cfgmod
from .. import paths as _paths
from .. import profession as _profession
from .. import workflows as wfm
from ._common import body, error_response, rt

bp = Blueprint("setup", __name__)


@bp.route("/api/config", methods=["GET"])
def api_config():
    r = rt()
    out = r.config.public_dict()
    out["version"] = __import__("plannerboard").__version__
    out["business_hours"] = r.hours.as_dict()
    return jsonify(out)


@bp.route("/api/config", methods=["PUT"])
def api_put_config():
    """Deep-merge a partial document (no secrets) and write it. Returns the
    public subset. The running process re-reads the profession; a port or
    bind change needs a restart."""
    r = rt()
    data = body()
    if not isinstance(data, dict):
        return error_response(400, "body must be an object")
    for blk in ("mail", "ai"):
        if isinstance(data.get(blk), dict):
            for k in ("password", "imap_password", "api_key", "key"):
                data[blk].pop(k, None)
    if isinstance(data.get("mail"), dict) and isinstance(data["mail"].get("imap"), dict):
        data["mail"]["imap"].pop("password", None)
    r.config.update(data)
    r.config.write()
    r.reload(r.config)
    if any(k in data for k in ("mail", "ai", "assistant", "license", "user")):
        r.rewire()
    if "mail" in data:
        from .mail_client_api import reset_cache
        reset_cache()                       # the client provider is rebuilt from the new settings
    out = r.config.public_dict()
    out["restart_required"] = any(k in data for k in ("server", "paths"))
    return jsonify({"ok": True, "config": out})


@bp.route("/api/professions", methods=["GET"])
def api_professions():
    return jsonify({"ok": True, "professions": _profession.list_templates()})


@bp.route("/api/setup/presets", methods=["GET"])
def api_setup_presets():
    """Reference-shaped preset list for the wizard: one entry per profession
    template with its workflows summarized."""
    out = []
    for t in _profession.list_templates():
        if t.get("error"):
            out.append({"id": t["id"], "label": t["label"], "error": t["description"]})
            continue
        try:
            doc = _profession.load_template(t["id"])
        except _profession.ProfessionError as e:
            out.append({"id": t["id"], "label": t["label"], "error": str(e)})
            continue
        wfs = (doc.get("workflows") or {}).get("workflows") or []
        out.append({"id": t["id"], "label": t["label"], "description": t["description"], "icon": t["icon"],
                    "draft": False,
                    "workflows": [{"id": w.get("id"), "label": w.get("label"), "icon": w.get("icon", ""),
                                   "stages": [s.get("label") for s in (w.get("stages") or []) if s.get("column")]}
                                  for w in wfs],
                    "hud": (doc.get("workflows") or {}).get("hud") or {}})
    return jsonify({"presets": out})


@bp.route("/api/profession", methods=["GET"])
def api_profession():
    """The EFFECTIVE profession (template deep-merged with overrides) plus
    `user` and `features`."""
    return jsonify(rt().profession())


@bp.route("/api/profession", methods=["PUT"])
def api_put_profession():
    """Body: {overrides: {...}} or a bare overrides object; optional
    `template` switches templates. Writes config, reloads the profession."""
    r = rt()
    data = body()
    if not isinstance(data, dict):
        return error_response(400, "body must be an object")
    template = data.get("template")
    overrides = data.get("overrides") if "overrides" in data else {k: v for k, v in data.items() if k != "template"}
    if template:
        if template not in _profession.template_ids():
            return error_response(400, f"unknown profession template '{template}'")
        r.config.update({"profession": {"template": template}})
    if overrides is not None:
        if not isinstance(overrides, dict):
            return error_response(400, "overrides must be an object")
        r.config.set_overrides(overrides)
    if isinstance(overrides, dict) and isinstance(overrides.get("workflows"), dict):
        errs = wfm.validate(wfm.normalize(dict(overrides["workflows"])))
        if errs:
            return jsonify({"ok": False, "error": "invalid", "errors": errs}), 400
    r.config.write()
    r.reload(r.config)
    r.rewire()                      # prompts and the matcher read the new profession
    return jsonify({"ok": True, "profession": r.profession()})


@bp.route("/api/setup/probe", methods=["GET"])
def api_setup_probe():
    """Auto-detect helpers: does a candidate folder exist, is a local Ollama
    answering, is a keyring available, what mail / ai providers exist."""
    r = rt()
    path = request.args.get("path") or ""
    checked = None
    if path:
        try:
            p = Path(path).expanduser()
            checked = {"path": str(p), "exists": p.exists(), "is_dir": p.is_dir()}
        except Exception as e:
            checked = {"path": path, "error": str(e)}
    ollama = False
    try:
        from urllib.request import urlopen
        base = (r.config.get("ai.base_url") or "http://localhost:11434").rstrip("/")
        with urlopen(base + "/api/tags", timeout=1.0) as resp:
            ollama = resp.status == 200
    except Exception:
        ollama = False
    return jsonify({
        "ollama": ollama, "path": checked,
        "suggest": {"data_root": str(r.paths.data_root), "mail_cache": str(r.paths.mail_cache)},
        "keyring_available": cfgmod.keyring_available(),
        "mail": {"providers": list(cfgmod.MAIL_PROVIDERS), "current": r.config.mail_provider},
        "ai": {"providers": list(cfgmod.AI_PROVIDERS), "current": r.config.ai_provider,
               "key_present": bool(cfgmod.get_secret("ai_api_key")[0])},
        "professions": _profession.list_templates(),
        "configured": r.config.configured,
    })


@bp.route("/api/setup", methods=["POST"])
def api_setup():
    """First-run configuration. NEVER overwrites an existing config silently:
    a second POST must carry "force": true (server.py:725-741)."""
    r = rt()
    data = body()
    existing = cfgmod.Config.load(r.config.source_path) if r.config.source_path else cfgmod.Config.load()
    if existing.configured and not data.get("force"):
        return jsonify({"error": "already configured", "config_path": str(existing.source_path),
                        "hint": "POST again with force:true to overwrite"}), 409
    name = (data.get("name") or (data.get("user") or {}).get("name") or "").strip()
    email = (data.get("email") or (data.get("user") or {}).get("email") or "").strip().lower()
    if not name or "@" not in email:
        return jsonify({"error": "name and a valid email are required"}), 400

    prof_id = data.get("profession") if isinstance(data.get("profession"), str) else \
        (data.get("profession") or {}).get("template") if isinstance(data.get("profession"), dict) else data.get("preset")
    prof_id = (prof_id or "general").strip()
    if prof_id not in _profession.template_ids():
        return jsonify({"error": f"unknown profession '{prof_id}'"}), 400

    doc, problems = cfgmod.build_document(data, existing if existing.configured else None)
    doc["profession"]["template"] = prof_id
    new_cfg = cfgmod.Config(doc)
    created = new_cfg.paths.ensure()

    # Secrets -> keyring only.
    secret_notes = []
    pw = data.get("imap_password") or (data.get("mail") or {}).get("imap_password") \
        or ((data.get("mail") or {}).get("imap") or {}).get("password") or (data.get("imap") or {}).get("password")
    if pw:
        ok, where, err = cfgmod.set_secret("imap_password", pw)
        if not ok:
            problems.append(f"IMAP password not stored ({err}); set {cfgmod.SECRET_KEYS['imap_password']} "
                            f"in the environment before the board starts")
        else:
            secret_notes.append("imap_password")
    ai_key = (data.get("ai") or {}).get("api_key") or data.get("ai_api_key")
    if ai_key:
        ok, where, err = cfgmod.set_secret("ai_api_key", ai_key)
        if not ok:
            problems.append(f"AI key not stored ({err}); set {cfgmod.SECRET_KEYS['ai_api_key']} in the environment")
        else:
            secret_notes.append("ai_api_key")

    try:
        target = existing.source_path if existing.configured else _paths.config_path(new_cfg.paths.data_root)
        config_path = new_cfg.write(target)
    except Exception as e:
        return jsonify({"error": f"could not write config: {e}"}), 500

    # Reload the runtime on the new document; seed workflows.json from the
    # profession unless the user already has one.
    same_root = Path(new_cfg.paths.data_root).resolve() == Path(r.paths.data_root).resolve()
    old_port, old_bind, old_db = r.config.port, r.config.bind, r.paths.db_file
    r.reload(new_cfg)
    seeded = None
    if not r.workflows.path.exists() or data.get("reset_workflows"):
        ok, errs, _ = r.workflows.save(r.workflows.seed_document())
        seeded = r.workflows.path.name if ok else None
        if not ok:
            problems.append("workflows not seeded: " + "; ".join(errs[:3]))
    try:
        r.store._ensure_schema()
    except Exception as e:
        problems.append(f"schema migration: {e}")
    r.rewire()

    return jsonify({
        "status": "ok", "config_path": str(config_path), "profession": prof_id,
        "preset": prof_id, "features": new_cfg.features, "folders_created": created,
        "problems": problems, "secrets_stored": secret_notes,
        "mail_provider": new_cfg.mail_provider, "ai_provider": new_cfg.ai_provider,
        "workflows_seeded": seeded,
        "api_token": new_cfg.api_token if new_cfg.requires_token else None,
        # A data-root, port or bind change is read at start-up; say so rather
        # than pretending it took effect.
        "restart_required": (not same_root) or new_cfg.port != old_port or new_cfg.bind != old_bind
                            or new_cfg.paths.db_file != old_db,
    })
