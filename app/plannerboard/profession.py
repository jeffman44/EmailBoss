"""
Profession templates: the domain vocabulary, stages, fields, id patterns and
prompts a board runs on. Shipped as professions/<id>.json, deep-merged with
the user's `profession.overrides`, served by /api/profession.

A template's shape is DESIGN.md section 3. `effective()` is the only thing the
rest of the app reads; nothing else opens the JSON files.
"""

import json
from copy import deepcopy
from pathlib import Path

from . import paths as _paths
from .config import deep_merge

REQUIRED_KEYS = ("id", "label", "entity", "work_types", "fields", "id_patterns", "date_types",
                 "business_hours", "triage", "vocab", "prompt_profile", "workflows")

BASE = {
    "id": "general", "label": "General", "description": "", "icon": "\U0001f4cb",
    "entity": {"project": "Project", "projects": "Projects", "client": "Client",
               "location": "Location", "contact": "Contact", "quote": "Proposal"},
    "work_types": [], "fields": [], "id_patterns": [],
    "date_types": [{"id": "follow_up", "label": "Follow-up"}, {"id": "deadline", "label": "Deadline"},
                   {"id": "sla", "label": "SLA"}],
    "teams": [],
    "business_hours": {"tz": "UTC", "start": 8, "end": 17, "days": [0, 1, 2, 3, 4]},
    "sla_hours": 4,
    "money": {"currency": "USD", "annual_goal": None},
    "triage": {"classifications": ["request", "client_reply", "approval", "vendor", "scheduling",
                                   "internal", "informational", "needs_reply", "no_reply", "junk"],
               "notification_senders": ["noreply@", "no-reply@", "notifications@"],
               "internal_admin_subjects": ["timesheet", "payroll", "benefits enrollment"]},
    "vocab": {"keywords": [], "acronyms": [], "scope_phrases": []},
    "prompt_profile": {"industry": "general business", "role_noun": "professional"},
    "customer_visible_fields": ["name", "location", "scope", "contacts"],
    "features": {"money": True, "game_layer": True, "analytics": True},
    "workflows": None,
}


class ProfessionError(Exception):
    pass


def _templates_dir():
    return _paths.PROFESSIONS_DIR


def list_templates():
    """[{id, label, description, icon}] for every shipped template."""
    out = []
    d = _templates_dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            out.append({"id": p.stem, "label": p.stem, "description": f"unreadable: {e}", "icon": "", "error": True})
            continue
        out.append({"id": doc.get("id") or p.stem, "label": doc.get("label") or p.stem,
                    "description": doc.get("description") or "", "icon": doc.get("icon") or ""})
    return out


def template_ids():
    return [t["id"] for t in list_templates() if not t.get("error")]


def load_template(template_id):
    """The raw shipped template merged over BASE. Raises ProfessionError."""
    tid = (template_id or "general").strip()
    if not tid.replace("_", "").isalnum():
        raise ProfessionError(f"bad template id '{tid}'")
    p = _templates_dir() / f"{tid}.json"
    if not p.is_file():
        raise ProfessionError(f"unknown profession template '{tid}'")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise ProfessionError(f"template '{tid}' unreadable: {e}")
    merged = deep_merge(BASE, doc)
    merged["id"] = merged.get("id") or tid
    return merged


def effective(template_id, overrides=None, user=None, features=None):
    """Template deep-merged with the user's overrides, plus `user` and
    `features` blocks for /api/profession. Falls back to `general` when the
    named template is missing so a typo in config never blanks the board."""
    try:
        base = load_template(template_id)
    except ProfessionError:
        try:
            base = load_template("general")
        except ProfessionError:
            base = deepcopy(BASE)
        base["template_error"] = f"template '{template_id}' not found; using general"
    prof = deep_merge(base, overrides or {})
    if not isinstance(prof.get("workflows"), dict):
        from . import workflows as wfm
        prof["workflows"] = wfm.minimal_document()
    prof["user"] = dict(user or {})
    feats = dict(prof.get("features") or {})
    for k, v in (features or {}).items():
        feats[k] = bool(v)
    prof["features"] = feats
    prof["customer_visible_fields"] = list(prof.get("customer_visible_fields") or [])
    return prof


def customer_visible_fields(prof):
    return list((prof or {}).get("customer_visible_fields") or [])


def date_type_ids(prof):
    return [d.get("id") for d in (prof or {}).get("date_types") or [] if d.get("id")]


def validate_template(doc):
    """Structural check for a shipped template. Returns a list of errors."""
    errs = []
    for k in REQUIRED_KEYS:
        if k not in doc:
            errs.append(f"missing '{k}'")
    for wt in doc.get("work_types") or []:
        if not wt.get("id") or not wt.get("workflow_id"):
            errs.append(f"work_type {wt} needs id and workflow_id")
    wf = doc.get("workflows") or {}
    wf_ids = {w.get("id") for w in (wf.get("workflows") or [])}
    for wt in doc.get("work_types") or []:
        if wt.get("workflow_id") and wt["workflow_id"] not in wf_ids:
            errs.append(f"work_type '{wt.get('id')}' points at unknown workflow '{wt.get('workflow_id')}'")
    for pat in doc.get("id_patterns") or []:
        if not pat.get("regex"):
            errs.append(f"id_pattern {pat.get('key')} needs a regex")
    return errs
