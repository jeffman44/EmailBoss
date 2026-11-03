"""
Workflow layer: the project lifecycle as DATA, with stage ROLES.

Ported from the reference v2/workflows.py (validate 343-461, load/save
474-559, accessors 566-633, ladder 656-739) and generalized:

  * every stage carries a `role`:
        todo | waiting | out | won | in_progress | closed | cold | ignored
    and every literal status check in the server is replaced by a role check.
    `out` = proposal / quote / offer sent, waiting on the client.
  * `won_transition {from, to}` replaces the sale-specific transition key.
  * the document lives in <DATA_ROOT>/workflows.json and is seeded from the
    profession template's `workflows` block on first read.

WHAT IS AUTO-ARMED (deliberately narrow): a stage's `owning_task` is armed
automatically only when the stage is on the ladder chain (the walk from
`won_transition.to` following `advance_on_task_complete`) or is the workflow's
`entry_stage`, and the stage does not carry `"auto_arm": false`.

The quote task (an owning_task with date_type 'quote') is never a rung: it has
its own cascade with its own guards (see api/projects.py).

Never imports db: db.py imports this at migration time.
"""

import json
import os
import re
import shutil
from copy import deepcopy
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

ROLES = ("todo", "waiting", "out", "won", "in_progress", "closed", "cold", "ignored")
CLOSED_ROLES = ("closed", "ignored")
REVIVE_ROLES = ("closed", "cold")          # fresh work revives these; ignored never

_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
BUILTIN_DATE_TYPES = {"follow_up", "deadline", "sla", "quote"}
_VALID_URGENCY = {"max", "sum", "deadline", "none"}
_VALID_SORT_MODES = {"subtask", "project"}
_SORT_MATCH_KEYS = {"date_type", "workflow", "source", "text_prefix"}
CATCH_ALL_RULE = {"id": "other", "label": "Everything else", "match": {}}

DEFAULT_SORT = {
    "mode": "subtask",
    "priority": [
        {"id": "deadline", "label": "Deadline", "match": {"date_type": "deadline"}},
        {"id": "sla", "label": "SLA", "match": {"date_type": "sla"}},
        {"id": "quote", "label": "Quote request", "match": {"date_type": "quote"}},
        {"id": "follow_up", "label": "Follow-up", "match": {"date_type": "follow_up"}},
        {"id": "other", "label": "Everything else", "match": {}},
    ],
}


def default_sort():
    return deepcopy(DEFAULT_SORT)


def minimal_document():
    """The fallback roster when no profession block is available. Mirrors the
    `general` template in DESIGN.md section 3."""
    return {
        "version": SCHEMA_VERSION,
        "workflows": [{
            "id": "general", "label": "General", "icon": "\U0001f4cb", "color": "#8899aa",
            "stages": [
                {"id": "action_required", "label": "Action Required", "role": "todo", "column": True,
                 "owning_task": {"text": "Respond", "date_type": "sla", "sla_hours": 4}},
                {"id": "waiting", "label": "Waiting", "role": "waiting", "column": True},
                {"id": "complete", "label": "Done", "role": "closed", "column": False, "closed": True},
                {"id": "ignored", "label": "Ignored", "role": "ignored", "column": False, "closed": True},
            ],
            "entry_stage": "action_required", "won_transition": None, "extra_tasks": [],
        }],
        "tabs": [
            {"id": "active", "label": "Active", "filter": {"closed": False}, "urgency": "sum"},
            {"id": "deadlines", "label": "Deadlines",
             "filter": {"subtask_date_type": "deadline", "pending": True}, "urgency": "deadline"},
            {"id": "closed", "label": "Closed", "filter": {"closed": True}, "urgency": "none"},
        ],
        "tab_rules": {"auto_stage_tabs": True},
        "sort": default_sort(),
        "hud": {"hp": "threads_closed_vs_goal", "mp": "tasks_done_vs_goal", "xp": "threads_closed"},
        "theme": "default",
    }


# ══════════════════════════════════════════════════════════════════════════
# NORMALIZE
# ══════════════════════════════════════════════════════════════════════════

def _infer_role(stage):
    if stage.get("role") in ROLES:
        return stage["role"]
    sid = (stage.get("id") or "").lower()
    if stage.get("closed"):
        return "ignored" if sid == "ignored" else "closed"
    if sid in ("cold",):
        return "cold"
    if sid in ("waiting", "on_hold"):
        return "waiting"
    if "sent" in sid or sid in ("quoted", "proposed", "offered", "submitted"):
        return "out"
    return "todo"


def normalize(doc):
    """Fill derived keys in place and return the doc: roles on every stage,
    `closed` flag kept in sync with the role, legacy `sold_transition`
    renamed to `won_transition`, `version` present, sort catch-all present."""
    if not isinstance(doc, dict):
        return doc
    doc.setdefault("version", SCHEMA_VERSION)
    for wf in doc.get("workflows") or []:
        if not isinstance(wf, dict):
            continue
        if "won_transition" not in wf and "sold_transition" in wf:
            wf["won_transition"] = wf.pop("sold_transition")
        wf.setdefault("extra_tasks", [])
        for st in wf.get("stages") or []:
            if not isinstance(st, dict):
                continue
            st["role"] = _infer_role(st)
            if st["role"] in CLOSED_ROLES:
                st["closed"] = True
            elif "closed" not in st:
                st["closed"] = False
    if isinstance(doc.get("sort"), dict):
        ensure_sort_catch_all(doc["sort"])
    return doc


def ensure_sort_catch_all(sort_block):
    """Port of workflows.py:151-169. Unmatched must be an ordinary, last-ranked
    case, never undefined ordering."""
    if not isinstance(sort_block, dict):
        return sort_block
    rules = sort_block.get("priority")
    if not isinstance(rules, list):
        sort_block["priority"] = rules = []
    if not any(isinstance(r, dict) and not (r.get("match") or {}) for r in rules):
        rules.append(deepcopy(CATCH_ALL_RULE))
    return sort_block


# ══════════════════════════════════════════════════════════════════════════
# VALIDATION
# ══════════════════════════════════════════════════════════════════════════

def _validate_sort(sort_block, date_types):
    errs = []
    if not isinstance(sort_block, dict):
        return ["'sort' must be an object"]
    mode = sort_block.get("mode", "subtask")
    if mode not in _VALID_SORT_MODES:
        errs.append(f"sort.mode '{mode}' must be one of {sorted(_VALID_SORT_MODES)}")
    rules = sort_block.get("priority")
    if rules is None:
        return errs
    if not isinstance(rules, list):
        return errs + ["sort.priority must be a list"]
    seen = set()
    for ri, r in enumerate(rules):
        where = f"sort.priority[{ri}]"
        if not isinstance(r, dict):
            errs.append(f"{where}: must be an object")
            continue
        rid = r.get("id") or ""
        if not _ID_RE.match(str(rid)):
            errs.append(f"{where}: id '{rid}' must be lowercase a-z0-9_ (max 40)")
        if rid in seen:
            errs.append(f"{where}: duplicate sort rule id '{rid}'")
        seen.add(rid)
        m = r.get("match")
        if not isinstance(m, dict):
            errs.append(f"{where} ({rid}): needs a 'match' object (use {{}} for the catch-all)")
            continue
        for k in m:
            if k not in _SORT_MATCH_KEYS:
                errs.append(f"{where} ({rid}): unknown match key '{k}'")
        dt = m.get("date_type")
        if dt is not None and dt not in date_types:
            errs.append(f"{where} ({rid}): match.date_type '{dt}' is not a known date type")
    return errs


def validate(doc, date_types=None):
    """Human-readable errors; empty list == valid. Strict on what would BREAK
    (ids become statuses, task text becomes a lookup key, roles drive the
    cascades), permissive on cosmetics."""
    date_types = set(BUILTIN_DATE_TYPES) | set(date_types or ())
    errs = []
    if not isinstance(doc, dict):
        return ["workflows document must be a JSON object"]
    wfs = doc.get("workflows")
    if not isinstance(wfs, list) or not wfs:
        return ["'workflows' must be a non-empty list"]

    seen_wf = set()
    for wi, wf in enumerate(wfs):
        where = f"workflows[{wi}]"
        if not isinstance(wf, dict):
            errs.append(f"{where}: must be an object")
            continue
        wid = wf.get("id") or ""
        if not _ID_RE.match(str(wid)):
            errs.append(f"{where}: id '{wid}' must be lowercase a-z0-9_ (max 40)")
        if wid in seen_wf:
            errs.append(f"{where}: duplicate workflow id '{wid}'")
        seen_wf.add(wid)
        if not wf.get("label"):
            errs.append(f"{where} ({wid}): label is required")

        stages = wf.get("stages")
        if not isinstance(stages, list) or not stages:
            errs.append(f"{where} ({wid}): 'stages' must be a non-empty list")
            continue
        stage_ids = []
        for si, st in enumerate(stages):
            sw = f"{where}.stages[{si}]"
            if not isinstance(st, dict):
                errs.append(f"{sw}: must be an object")
                continue
            sid = st.get("id") or ""
            if not _ID_RE.match(str(sid)):
                errs.append(f"{sw}: stage id '{sid}' must be lowercase a-z0-9_ "
                            f"(it becomes a project status value)")
            if sid in stage_ids:
                errs.append(f"{sw}: duplicate stage id '{sid}' in workflow '{wid}'")
            stage_ids.append(sid)
            if not st.get("label"):
                errs.append(f"{sw} ({sid}): label is required")
            role = st.get("role")
            if role is not None and role not in ROLES:
                errs.append(f"{sw} ({sid}): role '{role}' must be one of {list(ROLES)}")
            ot = st.get("owning_task")
            if ot is not None:
                if not isinstance(ot, dict) or not str(ot.get("text") or "").strip():
                    errs.append(f"{sw} ({sid}): owning_task needs a non-empty 'text'")
                else:
                    dt = ot.get("date_type") or "follow_up"
                    if dt not in date_types:
                        errs.append(f"{sw} ({sid}): owning_task.date_type '{dt}' is not a "
                                    f"known date type ({sorted(date_types)})")
                    if dt == "sla" and not ot.get("sla_hours"):
                        errs.append(f"{sw} ({sid}): an sla task needs sla_hours")
        for xt in (wf.get("extra_tasks") or []):
            if not isinstance(xt, dict) or not str(xt.get("text") or "").strip():
                errs.append(f"{where} ({wid}): every extra_task needs a 'text'")
                break

        for si, st in enumerate(stages):
            if not isinstance(st, dict):
                continue
            nxt = st.get("advance_on_task_complete")
            if nxt and nxt not in stage_ids:
                errs.append(f"{where}.stages[{si}] ({st.get('id')}): advance_on_task_complete "
                            f"'{nxt}' is not a stage of this workflow")
            if nxt and not st.get("owning_task"):
                errs.append(f"{where}.stages[{si}] ({st.get('id')}): advance_on_task_complete "
                            f"needs an owning_task to check")

        entry = wf.get("entry_stage")
        if not entry or entry not in stage_ids:
            errs.append(f"{where} ({wid}): entry_stage '{entry}' is not one of this "
                        f"workflow's stages")
        won = wf.get("won_transition", wf.get("sold_transition"))
        if won is not None:
            if not isinstance(won, dict):
                errs.append(f"{where} ({wid}): won_transition must be an object or null")
            else:
                for k in ("from", "to"):
                    v = won.get(k)
                    if v and v not in stage_ids:
                        errs.append(f"{where} ({wid}): won_transition.{k} '{v}' is not a "
                                    f"stage of this workflow")

    tabs = doc.get("tabs")
    if tabs is not None:
        if not isinstance(tabs, list):
            errs.append("'tabs' must be a list")
        else:
            seen_tab = set()
            for ti, tb in enumerate(tabs):
                if not isinstance(tb, dict):
                    errs.append(f"tabs[{ti}]: must be an object")
                    continue
                tid = tb.get("id") or ""
                if not _ID_RE.match(str(tid)):
                    errs.append(f"tabs[{ti}]: id '{tid}' must be lowercase a-z0-9_")
                if tid in seen_tab:
                    errs.append(f"tabs[{ti}]: duplicate tab id '{tid}'")
                seen_tab.add(tid)
                u = tb.get("urgency", "max")
                if u not in _VALID_URGENCY:
                    errs.append(f"tabs[{ti}] ({tid}): urgency '{u}' must be one of "
                                f"{sorted(_VALID_URGENCY)}")
    if doc.get("sort") is not None:
        errs += _validate_sort(doc.get("sort"), date_types)
    return errs


# ══════════════════════════════════════════════════════════════════════════
# ACCESSORS (pure, over a document)
# ══════════════════════════════════════════════════════════════════════════

def get_workflow(doc, wid):
    for wf in (doc or {}).get("workflows") or []:
        if wf.get("id") == wid:
            return wf
    return None


def workflow_ids(doc):
    return [wf.get("id") for wf in (doc or {}).get("workflows") or []]


def default_workflow_id(doc):
    ids = workflow_ids(doc)
    return ids[0] if ids else "general"


def get_stage(wf, sid):
    for st in (wf or {}).get("stages") or []:
        if st.get("id") == sid:
            return st
    return None


def stage_for_status(doc, wid, status):
    """The stage object a status names inside a workflow; falls back to any
    workflow carrying that stage id."""
    st = get_stage(get_workflow(doc, wid), status)
    if st:
        return st
    for wf in (doc or {}).get("workflows") or []:
        st = get_stage(wf, status)
        if st:
            return st
    return None


def role_of(doc, status, wid=None):
    st = stage_for_status(doc, wid, status)
    return (st or {}).get("role") or _infer_role(st or {"id": status or ""})


def all_stage_ids(doc):
    out = []
    for wf in (doc or {}).get("workflows") or []:
        for st in wf.get("stages") or []:
            sid = st.get("id")
            if sid and sid not in out:
                out.append(sid)
    return out


def stages_with_role(doc, role, wid=None):
    """Stage ids carrying `role`, optionally within one workflow, in file order."""
    out = []
    wfs = [get_workflow(doc, wid)] if wid else ((doc or {}).get("workflows") or [])
    for wf in wfs:
        for st in (wf or {}).get("stages") or []:
            if (st.get("role") or _infer_role(st)) == role and st.get("id") not in out:
                out.append(st.get("id"))
    return out


def closed_stage_ids(doc):
    return set(stages_with_role(doc, "closed")) | set(stages_with_role(doc, "ignored"))


def is_closed(doc, status, wid=None):
    return role_of(doc, status, wid) in CLOSED_ROLES


def entry_stage(doc, wid):
    wf = get_workflow(doc, wid)
    if wf and wf.get("entry_stage"):
        return wf["entry_stage"]
    todo = stages_with_role(doc, "todo", wid) if wf else []
    return todo[0] if todo else "action_required"


def resolve_workflow_id(doc, work_type, work_types=None):
    """work_type -> workflow id via the profession's work_types list, falling
    back to the first workflow in the document."""
    wt = (work_type or "").lower().strip()
    ids = workflow_ids(doc)
    for row in work_types or []:
        if (row.get("id") or "").lower() == wt and row.get("workflow_id") in ids:
            return row["workflow_id"]
    if wt in ids:
        return wt
    return default_workflow_id(doc)


def workflow_for_project(doc, proj, work_types=None):
    wid = (proj or {}).get("workflow_id") or ""
    wf = get_workflow(doc, wid) if wid else None
    if wf:
        return wf
    return get_workflow(doc, resolve_workflow_id(doc, (proj or {}).get("work_type"), work_types))


# ══════════════════════════════════════════════════════════════════════════
# THE LADDER (derived, never a constant)
# ══════════════════════════════════════════════════════════════════════════

def is_quote_owning_task(ot):
    return isinstance(ot, dict) and (ot.get("date_type") or "") == "quote"


def quote_task_for(doc, wid):
    """(stage_id, owning_task) of the workflow's quote task, or (None, None)."""
    wf = get_workflow(doc, wid)
    for st in (wf or {}).get("stages") or []:
        ot = st.get("owning_task")
        if is_quote_owning_task(ot):
            return st.get("id"), ot
    return None, None


def ladder_rows(doc, wid):
    """The advancing rungs of a workflow as 6-tuples:
        (rung_status, task text, next status, category, sla_hours, follow_up_days)
    A stage is a RUNG when it has an owning_task AND an advance_on_task_complete
    target AND the task is not the quote task. Port of workflows.py:656-691."""
    wf = get_workflow(doc, wid)
    rows = []
    for st in (wf or {}).get("stages") or []:
        ot = st.get("owning_task")
        nxt = st.get("advance_on_task_complete")
        if not ot or not nxt or is_quote_owning_task(ot):
            continue
        dt = ot.get("date_type") or "follow_up"
        rows.append((
            st.get("id") or "", ot.get("text") or "", nxt, ot.get("category") or "",
            (ot.get("sla_hours") if dt == "sla" else None),
            (ot.get("days") if dt in ("follow_up", "deadline") else None),
        ))
    return rows


def ladder_by_status(doc, wid):
    return {r[0]: r for r in ladder_rows(doc, wid)}


def ladder_stage_ids(doc, wid):
    return [r[0] for r in ladder_rows(doc, wid)]


def all_ladder_stage_ids(doc):
    out = []
    for w in (doc or {}).get("workflows") or []:
        for r in ladder_rows(doc, w.get("id")):
            if r[0] not in out:
                out.append(r[0])
    return out


def rung_for_text(doc, text, wid=None):
    """Which rung owns this subtask text? Exact, case-folded. Scoped to one
    workflow when given, else the union."""
    key = (text or "").strip().lower()
    if not key:
        return None
    if wid:
        rows = ladder_rows(doc, wid)
    else:
        rows = []
        for w in (doc or {}).get("workflows") or []:
            rows.extend(ladder_rows(doc, w.get("id")))
    for r in rows:
        if (r[1] or "").strip().lower() == key:
            return r[0]
    return None


def won_target(doc, wid):
    wf = get_workflow(doc, wid)
    return ((wf or {}).get("won_transition") or {}).get("to") or None


def won_revert_target(doc, wid):
    wf = get_workflow(doc, wid)
    return ((wf or {}).get("won_transition") or {}).get("from") or None


def auto_arm_stage_ids(doc, wf):
    wid = (wf or {}).get("id")
    ids = set(ladder_stage_ids(doc, wid))
    ent = (wf or {}).get("entry_stage")
    if ent:
        ids.add(ent)
    out = set()
    for sid in ids:
        st = get_stage(wf, sid)
        if st and st.get("owning_task") and st.get("auto_arm", True):
            out.add(sid)
    return out


def stage_task_texts(doc):
    out = set()
    for wf in (doc or {}).get("workflows") or []:
        for st in wf.get("stages") or []:
            ot = st.get("owning_task") or {}
            if ot.get("text"):
                out.add(ot["text"].strip().lower())
    return out


def sort_config(doc):
    s = (doc or {}).get("sort")
    if not isinstance(s, dict):
        return None
    return ensure_sort_catch_all(s)


# ══════════════════════════════════════════════════════════════════════════
# LOAD / SAVE (per data root)
# ══════════════════════════════════════════════════════════════════════════

class WorkflowStore:
    """The workflows.json file for one data root. `load()` never raises and
    never returns junk: an unparseable or invalid file falls back to the seed
    and records `last_error`. Port of workflows.py:468-559."""

    def __init__(self, path, backup_dir=None, seed=None, date_types=None):
        self.path = Path(path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.path.parent / "backups" / "workflows"
        self._seed = seed
        self.date_types = set(date_types or ())
        self._cache = None
        self._cache_mtime = None
        self.last_error = None

    def seed_document(self):
        doc = deepcopy(self._seed) if self._seed else minimal_document()
        return normalize(doc)

    def set_seed(self, seed):
        self._seed = seed

    def load(self, force=False):
        try:
            if not self.path.exists():
                doc = self.seed_document()
                self._write(doc)
                self._cache, self._cache_mtime = doc, self.path.stat().st_mtime
                self.last_error = None
                return self._cache
            mtime = self.path.stat().st_mtime
            if not force and self._cache is not None and mtime == self._cache_mtime:
                return self._cache
            doc = normalize(json.loads(self.path.read_text(encoding="utf-8")))
            errs = validate(doc, self.date_types)
            if errs:
                self.last_error = f"{self.path.name} invalid: {errs[0]} ({len(errs)} error(s))"
                self._cache, self._cache_mtime = self.seed_document(), mtime
                return self._cache
            self._cache, self._cache_mtime = doc, mtime
            self.last_error = None
            return self._cache
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self._cache = self._cache or self.seed_document()
            self._cache_mtime = None
            return self._cache

    def _write(self, doc):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(self.path))

    def backup(self):
        if not self.path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = self.backup_dir / f"workflows_{ts}.json"
        shutil.copy2(str(self.path), str(dest))
        return dest

    def save(self, doc):
        """Validate -> back up -> write. Returns (ok, errors, backup_path)."""
        doc = normalize(deepcopy(doc))
        errs = validate(doc, self.date_types)
        if errs:
            return False, errs, None
        bpath = None
        try:
            bpath = self.backup()
        except Exception:
            pass
        self._write(doc)
        self._cache = doc
        try:
            self._cache_mtime = self.path.stat().st_mtime
        except OSError:
            self._cache_mtime = None
        return True, [], (str(bpath) if bpath else None)
