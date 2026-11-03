"""
Email Boss database layer: one SQLite file, WAL, per-thread connections.

Ported from the reference v2/db.py and generalized:
  - connection management / retry-on-locked / stranded-transaction release
    (db.py 47-60, 272-409)
  - schema bootstrap + additive column migration (410-430, 1040-1049)
  - projects / contacts / subtasks / notes (1101-1811)
  - conversation routing + alt thread keys (1849-1918)
  - alerts (1921-2031), contact/id indexes (2037-2095)
  - triage cards, processed messages, events, hp history (2101-2480)
  - auto-commit + proposal detection (2486-2707)
  - intake, board payload, correspondence, merge (2820-3295)

Everything install-specific is gone. Timezone and business hours come from a
BusinessHours instance; status validation comes from the workflow roster; id
patterns come from the profession. The `Store` class implements DESIGN.md
section 9 in full and is the only writer of the database.
"""

import functools
import json
import os
import re
import socket
import sqlite3
import threading
import time
import uuid
import weakref
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from . import paths as _paths
from . import workflows as wfm
from .business_hours import BusinessHours, DEFAULT_HOURS, parse_dt

SCHEMA_PATH = _paths.SCHEMA_PATH


def _retry_on_locked(func, max_retries=3, delay=0.5):
    """Retry on sqlite3.OperationalError 'database is locked'. db.py:47-60."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(max_retries + 1):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries:
                    time.sleep(delay * (attempt + 1))
                    continue
                raise
    return wrapper


# ── Note dedup (db.py:65-87) ─────────────────────────────────────────────────
NOTE_SIMILARITY_THRESHOLD = 0.70
_FACT_RE = re.compile(r'\b(?:PO\s*#?\d+|\$[\d,.]+|\d{4,}|[A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\b')


def _extract_facts(text):
    return set(m.group().lower() for m in _FACT_RE.finditer(text or ""))


def _notes_are_similar(existing, new_text):
    ratio = SequenceMatcher(None, existing.lower(), new_text.lower()).ratio()
    if ratio >= NOTE_SIMILARITY_THRESHOLD:
        return True
    a, b = _extract_facts(existing), _extract_facts(new_text)
    if not a or not b:
        return False
    smaller = min(len(a), len(b))
    return smaller > 0 and len(a & b) / smaller >= 0.6 and ratio >= 0.45


def slugify(name):
    if not name:
        return "_no_location"
    slug = re.sub(r'[^a-z0-9]+', '_', name.lower().strip()).strip('_')
    return slug or "_no_location"


def _tokens(text):
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) >= 3]


# Live connections by thread name, for the db-lock diagnostic (db.py:272-306).
CONN_REGISTRY = {}
_STORE_INSTANCES = weakref.WeakSet()


def _prune_dead_conns():
    alive = {t.name for t in threading.enumerate()}
    for name in list(CONN_REGISTRY):
        if name in alive:
            continue
        conn = CONN_REGISTRY.pop(name, None)
        if conn is None:
            continue
        try:
            conn.close()
        except Exception:
            pass


class StoreContext:
    """What the Store needs from the rest of the app, all optional:
    workflows (object with .load()), profession (callable -> effective dict),
    hours (BusinessHours), user (UserIdentity-like with .is_user())."""

    def __init__(self, workflows=None, profession=None, hours=None, user=None):
        self.workflows = workflows
        self.profession = profession or (lambda: {})
        self.hours = hours or DEFAULT_HOURS
        self.user = user


# ══════════════════════════════════════════════════════════════════════════
# STORE
# ══════════════════════════════════════════════════════════════════════════

class Store:
    """Single entry point for all data operations. One instance per process."""

    # (text-prefix, display) send-type guard: an imperative "send ... proposal"
    # task may only be completed by a trusted actor (db.py:1495-1514).
    SEND_TASK_RE = re.compile(
        r"\b(?:send|resend)\b.{0,60}\b(?:proposal|quote|offer|bid)\b"
        r"|\b(?:proposal|quote|offer|bid)\b.{0,30}\b(?:send|resend)\b",
        re.IGNORECASE)
    TRUSTED_SEND_ACTORS = {"manual", "sent_reply", "triage_approval", "user_directed", "board_sync"}

    ALT_ROUTE_PREFIX = "alt:"
    ALT_ROUTE_WINDOW_DAYS = 14
    ALERT_REOPEN_AFTER_ACK_HOURS = 12
    _ESCALATION_ACTIONS = {"approval_signal", "dispatch_detected"}

    def __init__(self, db_path, ctx=None):
        self.db_path = Path(db_path)
        self.ctx = ctx or StoreContext()
        self._local = threading.local()
        try:
            _STORE_INSTANCES.add(self)
        except Exception:
            pass
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ── context shortcuts ──────────────────────────────────────────────────

    @property
    def hours(self):
        return self.ctx.hours or DEFAULT_HOURS

    def wf_doc(self):
        try:
            if self.ctx.workflows is not None:
                return self.ctx.workflows.load()
        except Exception:
            pass
        return wfm.minimal_document()

    def profession(self):
        try:
            return self.ctx.profession() or {}
        except Exception:
            return {}

    def work_types(self):
        return self.profession().get("work_types") or []

    def money_enabled(self):
        return bool((self.profession().get("features") or {}).get("money", True))

    def is_user(self, from_email="", from_name=""):
        u = self.ctx.user
        return bool(u and u.is_user(from_email, from_name))

    # ── Connection management (db.py:327-409) ───────────────────────────────

    def _make_conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            _prune_dead_conns()
            CONN_REGISTRY[threading.current_thread().name] = conn
        except Exception:
            pass
        return conn

    @property
    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._make_conn()
            self._local.conn = conn
        return conn

    @property
    def conn(self):
        return self._conn

    def release_stranded(self, tag=""):
        """Roll back THIS thread's connection if a transaction is still open.
        A connection never outlives its unit of work with a transaction open."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return False
        try:
            if conn.in_transaction:
                conn.rollback()
                return True
        except Exception:
            pass
        return False

    @classmethod
    def release_all_stranded(cls, tag=""):
        n = 0
        for inst in list(_STORE_INSTANCES):
            try:
                if inst.release_stranded(tag):
                    n += 1
            except Exception:
                pass
        return n

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def write_probe(self, patience=1.0):
        """Fresh connection, BEGIN IMMEDIATE, rollback: can anyone write right
        now? Port of server.py:11879-11905."""
        try:
            c = sqlite3.connect(str(self.db_path), timeout=patience)
            try:
                c.execute(f"PRAGMA busy_timeout = {int(patience * 1000)}")
                t0 = time.time()
                c.execute("BEGIN IMMEDIATE")
                c.rollback()
                return {"ok": True, "waited_s": round(time.time() - t0, 2), "error": None}
            finally:
                c.close()
        except Exception as e:
            return {"ok": False, "waited_s": None, "error": f"{type(e).__name__}: {e}"[:200]}

    # ── Schema (db.py:410-429, additive migrations) ────────────────────────

    def _ensure_schema(self):
        has_tables = self._conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='projects'"
        ).fetchone()[0] > 0
        if has_tables:
            self._migrate_add_columns()
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        if not has_tables:
            self._migrate_add_columns()
        self._conn.commit()

    _ADDITIVE = {
        "projects": [("workflow_id", "TEXT NOT NULL DEFAULT ''"), ("source", "TEXT DEFAULT ''"),
                     ("won_date", "TEXT"), ("location", "TEXT NOT NULL DEFAULT ''"),
                     ("location_slug", "TEXT NOT NULL DEFAULT ''")],
        "subtasks": [("sla_type", "TEXT"), ("sla_created", "TEXT"), ("sla_hours", "REAL"),
                     ("category", "TEXT DEFAULT ''"), ("priority", "INTEGER DEFAULT 0"),
                     ("metadata", "TEXT DEFAULT '{}'")],
        "notes": [("source", "TEXT DEFAULT ''")],
        "conversation_routing": [("source", "TEXT DEFAULT ''"), ("created_at", "TEXT")],
        "triage_cards": [("cc", "TEXT"), ("thread_key_alt", "TEXT"), ("source_file", "TEXT"),
                         ("summary", "TEXT DEFAULT ''"), ("signals", "TEXT DEFAULT '{}'"),
                         ("suggestion", "TEXT"), ("workflow_id", "TEXT DEFAULT ''"),
                         ("new_project_location", "TEXT"), ("new_project_name", "TEXT"),
                         ("resolved_at", "TEXT"), ("resolved_by", "TEXT"), ("dismiss_reason", "TEXT"),
                         ("missing_inline_images", "INTEGER DEFAULT 0"), ("inline_image_refs", "TEXT"),
                         ("auto_matched_project", "TEXT DEFAULT ''"),
                         ("auto_match_reason", "TEXT DEFAULT ''"),
                         ("needs_reply", "INTEGER DEFAULT 0"), ("source_folder", "TEXT DEFAULT ''"),
                         ("internet_message_id", "TEXT DEFAULT ''"), ("auto_reason", "TEXT DEFAULT ''"),
                         ("doc_links", "TEXT DEFAULT '[]'")],
        "processed_messages": [("project_id", "TEXT")],
        "project_correspondence": [("cc_list", "TEXT DEFAULT ''")],
        "hp_history": [("damage_events", "TEXT DEFAULT '[]'")],
    }

    def _migrate_add_columns(self):
        """Add columns that older databases lack. Additive only."""
        tables = {r[0] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table, cols in self._ADDITIVE.items():
            if table not in tables:
                continue
            existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in existing:
                    self._conn.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {decl}')
        self._conn.commit()

    # ── Generic helpers ─────────────────────────────────────────────────────

    def _now(self):
        return self.hours.now_iso()

    def _today(self):
        return self.hours.today()

    @staticmethod
    def _row(row):
        return dict(row) if row is not None else None

    @staticmethod
    def _rows(rows):
        return [dict(r) for r in rows]

    @staticmethod
    def _json_col(val):
        if val is None:
            return None
        if isinstance(val, str):
            return val
        return json.dumps(val, ensure_ascii=False, default=str)

    @staticmethod
    def _parse_json(text, default=None):
        if text is None:
            return default
        if not isinstance(text, (str, bytes)):
            return text
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return default

    def _note_time(self, tag="triage"):
        now = self.hours.now_local_naive()
        return f"{now.month}/{now.day}, {now.strftime('%I:%M %p').lstrip('0')}, {tag}"

    def machine(self):
        return (os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME")
                or socket.gethostname() or "unknown")

    # ══════════════════════════════════════════════════════════════════════
    # APP META
    # ══════════════════════════════════════════════════════════════════════

    def meta_get(self, key, default=None):
        row = self._conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key, value):
        self._conn.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
        self._conn.commit()

    get_meta = meta_get
    set_meta = meta_set

    def next_project_id(self):
        n = int(self.meta_get("next_id", 1))
        self.meta_set("next_id", str(n + 1))
        return f"proj_{n:03d}"

    def license_get(self, key, default=None):
        row = self._conn.execute("SELECT value FROM license_state WHERE key = ?", (key,)).fetchone()
        return self._parse_json(row["value"], default) if row else default

    def license_set(self, key, value):
        self._conn.execute(
            "INSERT INTO license_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, self._json_col(value), self._now()))
        self._conn.commit()

    # ══════════════════════════════════════════════════════════════════════
    # PROJECTS (db.py:1112-1366)
    # ══════════════════════════════════════════════════════════════════════

    def get_project(self, pid):
        row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        if not row:
            return None
        proj = dict(row)
        proj["key_details"] = self._parse_json(proj.get("key_details"), {}) or {}
        proj["tags"] = self._parse_json(proj.get("tags"), []) or []
        proj["contacts"] = self._rows(self._conn.execute(
            "SELECT name, role, email, phone, direct, mobile, cell, company "
            "FROM contacts WHERE project_id = ? ORDER BY id", (pid,)).fetchall())
        proj["subtasks"] = self._subtask_rows(pid)
        proj["notes"] = self._rows(self._conn.execute(
            "SELECT text, time FROM notes WHERE project_id = ? ORDER BY id", (pid,)).fetchall())
        # UI compat: the board reads `site`; the matcher reads `client`.
        proj["site"] = proj.get("location") or ""
        kd = proj["key_details"]
        client = kd.get("client") or kd.get("client_name") or kd.get("customer") or ""
        if not client:
            for c in proj["contacts"]:
                if c.get("company"):
                    client = c["company"]
                    break
        proj["client"] = client or ""
        return proj

    def _subtask_rows(self, pid, status=None):
        sql = ("SELECT id, text, status, source, created, follow_up_date, date_type, "
               "completed_date, completed_by, blocked_by, sla_type, sla_created, sla_hours, "
               "category, priority, metadata FROM subtasks WHERE project_id = ?")
        params = [pid]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY priority DESC, rowid"
        out = []
        for r in self._conn.execute(sql, params).fetchall():
            d = dict(r)
            d["metadata"] = self._parse_json(d.get("metadata"), {}) or {}
            out.append(d)
        return out

    def get_project_location(self, pid):
        row = self._conn.execute("SELECT archived FROM projects WHERE id = ?", (pid,)).fetchone()
        if not row:
            return None
        return "archive" if row["archived"] else "active"

    def project_exists(self, pid):
        return self._conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone() is not None

    def all_projects(self, include_archived=False):
        sql = "SELECT id FROM projects" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY id"
        return [p for p in (self.get_project(r["id"]) for r in self._conn.execute(sql).fetchall()) if p]

    def active_projects(self):
        """Not archived, not closed-role (closed / ignored)."""
        doc = self.wf_doc()
        return [p for p in self.all_projects()
                if not wfm.is_closed(doc, p.get("status"), p.get("workflow_id"))]

    def get_projects_for_board(self):
        """{meta, projects (full), archived (summaries)}. db.py:2929-2965."""
        projects = self.all_projects()
        archived = []
        for row in self._conn.execute("SELECT * FROM projects WHERE archived = 1 ORDER BY id").fetchall():
            d = dict(row)
            d["key_details"] = self._parse_json(d.get("key_details"), {}) or {}
            d["tags"] = self._parse_json(d.get("tags"), []) or []
            archived.append(d)
        meta = {"next_id": self.meta_get("next_id", "1"),
                "last_triage": self.meta_get("last_triage", ""),
                "last_updated": self.meta_get("last_updated", "")}
        return {"meta": meta, "projects": projects, "archived": archived}

    def _validate_status(self, status, workflow_id=None, current=None):
        if not status:
            return status
        doc = self.wf_doc()
        if status in wfm.all_stage_ids(doc) or status == current:
            return status
        raise ValueError(f"unknown status '{status}' (not a stage of any workflow)")

    def create_project(self, name, workflow_id=None, status=None, work_type="", contacts=None,
                       key_details=None, scope="", tags=None, source="", subtasks=None,
                       notes=None, proj_id=None, **fields):
        """DESIGN section 9 signature plus free project columns in **fields.
        Resolves workflow_id from work_type when absent, status from the
        workflow's entry stage when absent. Returns the project id."""
        doc = self.wf_doc()
        location = fields.pop("location", fields.pop("site", "")) or ""
        wid = (workflow_id or "").strip() or wfm.resolve_workflow_id(doc, work_type, self.work_types())
        if not wfm.get_workflow(doc, wid):
            wid = wfm.default_workflow_id(doc)
        st = (status or "").strip() or wfm.entry_stage(doc, wid)
        self._validate_status(st)
        if proj_id is None:
            proj_id = self.next_project_id()
        row = {
            "name": name or "", "location": location, "location_slug": slugify(location),
            "workflow_id": wid, "status": st, "work_type": work_type or "",
            "scope": scope or "", "key_details": self._json_col(key_details or {}),
            "tags": self._json_col(tags or []), "source": source or "",
            "created": fields.pop("created", None) or self._today(),
            "updated_at": self._now(),
        }
        for k in ("site_slug",):
            fields.pop(k, None)
        for k, v in fields.items():
            if isinstance(v, (dict, list)):
                v = self._json_col(v)
            row[k] = v
        cols = ["id"] + list(row.keys())
        sql = (f"INSERT INTO projects ({', '.join(chr(34) + c + chr(34) for c in cols)}) "
               f"VALUES ({', '.join('?' * len(cols))})")
        try:
            self._conn.execute(sql, [proj_id] + list(row.values()))
            for c in contacts or []:
                if c.get("name") or c.get("email"):
                    self._insert_contact(proj_id, c)
            for s in subtasks or []:
                self._insert_subtask(proj_id, s)
            for n in notes or []:
                if isinstance(n, dict) and n.get("text"):
                    self._insert_note(proj_id, n["text"], n.get("time"))
                elif isinstance(n, str) and n.strip():
                    self._insert_note(proj_id, n.strip(), None)
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise
        self.meta_set("last_updated", self._now())
        return proj_id

    def update_project(self, pid, **fields):
        """Spread-merge key_details, validate status, write. Returns the
        project dict (None when the id is unknown)."""
        if not fields:
            return self.get_project(pid)
        before = self.get_project(pid)
        if before is None:
            return None
        if "site" in fields:
            fields["location"] = fields.pop("site")
        fields.pop("site_slug", None)
        if "location" in fields:
            fields["location_slug"] = slugify(fields.get("location") or "")
        if "key_details" in fields:
            incoming = fields["key_details"]
            if isinstance(incoming, str):
                incoming = self._parse_json(incoming, {}) or {}
            merged = dict(before.get("key_details") or {})
            merged.update(incoming or {})
            fields["key_details"] = self._json_col(merged)
        if "tags" in fields and not isinstance(fields["tags"], str):
            fields["tags"] = self._json_col(fields["tags"])
        if "status" in fields:
            self._validate_status(fields["status"], fields.get("workflow_id") or before.get("workflow_id"),
                                  current=before.get("status"))
        for k in ("contacts", "subtasks", "notes", "id"):
            fields.pop(k, None)
        for k, v in list(fields.items()):
            if isinstance(v, (dict, list)):
                fields[k] = self._json_col(v)
        fields["updated_at"] = self._now()
        set_clause = ", ".join(f'"{k}" = ?' for k in fields)
        self._conn.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", list(fields.values()) + [pid])
        self._conn.commit()
        return self.get_project(pid)

    def archive_project(self, pid):
        doc = self.wf_doc()
        proj = self.get_project(pid) or {}
        closed = wfm.stages_with_role(doc, "closed", proj.get("workflow_id")) or \
            wfm.stages_with_role(doc, "closed")
        upd = {"archived": 1}
        if closed:
            upd["status"] = closed[0]
        self.update_project(pid, **upd)
        self._conn.execute("DELETE FROM conversation_routing WHERE project_id = ?", (pid,))
        self._conn.commit()
        self.append_event("project_archived", {"project_id": pid})

    def delete_project(self, pid):
        for table in ("contacts", "subtasks", "notes", "conversation_routing",
                      "correspondence_files", "project_correspondence", "triage_cards",
                      "project_intake", "processed_messages", "questions", "drafts"):
            try:
                self._conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (pid,))
            except Exception:
                pass
        self._conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
        self._conn.commit()
        self.append_event("project_deleted", {"project_id": pid})

    # ── Contacts ────────────────────────────────────────────────────────────

    def _insert_contact(self, pid, c):
        self._conn.execute(
            "INSERT INTO contacts (project_id, name, role, email, phone, direct, mobile, cell, company) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, c.get("name", "") or "", c.get("role", "") or "", (c.get("email", "") or "").strip(),
             c.get("phone", "") or "", c.get("direct", "") or "", c.get("mobile", "") or "",
             c.get("cell", "") or "", c.get("company", "") or ""))

    def add_contact(self, pid, contact):
        self._insert_contact(pid, contact)
        self._conn.commit()

    def replace_contacts(self, pid, contacts):
        """Full replace (the board sends the whole array). server.py:1111-1117."""
        self._conn.execute("DELETE FROM contacts WHERE project_id = ?", (pid,))
        for c in contacts or []:
            if c.get("name") or c.get("email"):
                self._insert_contact(pid, c)
        self._conn.commit()

    # ── Subtasks (db.py:1394-1783) ──────────────────────────────────────────

    def _insert_subtask(self, pid, st):
        st_id = st.get("id") or ("st_" + uuid.uuid4().hex[:8])
        self._conn.execute(
            "INSERT OR IGNORE INTO subtasks (id, project_id, text, status, source, created, "
            "follow_up_date, date_type, completed_date, completed_by, blocked_by, sla_type, "
            "sla_created, sla_hours, category, priority, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (st_id, pid, st.get("text", ""), st.get("status", "pending") or "pending",
             st.get("source", "manual") or "manual", st.get("created") or self._today(),
             st.get("follow_up_date"), st.get("date_type"),
             st.get("completed_date") or st.get("completed"), st.get("completed_by"),
             st.get("blocked_by"), st.get("sla_type"), st.get("sla_created"), st.get("sla_hours"),
             st.get("category", "") or "", st.get("priority", 0) or 0,
             self._json_col(st.get("metadata") or {})))
        return st_id

    def add_subtask(self, pid, text, date_type="follow_up", follow_up_date=None, sla_hours=None,
                    priority=None, source="system", category=None, **kwargs):
        """Add a subtask unless one with the same text exists (pending-only when
        allow_if_completed). Returns the id, or None when deduped. An SLA task is
        `sla_hours` set with `date_type='sla'` (clock = sla_created, now unless
        given). Reactivates a closed/cold project. db.py:1412-1461."""
        text = (text or "").strip()
        if not text:
            return None
        if kwargs.get("allow_if_completed"):
            existing = self._conn.execute(
                "SELECT id FROM subtasks WHERE project_id = ? AND text = ? AND status = 'pending'",
                (pid, text)).fetchone()
        else:
            existing = self._conn.execute(
                "SELECT id FROM subtasks WHERE project_id = ? AND text = ?", (pid, text)).fetchone()
        if existing:
            return None
        if date_type is None:
            date_type = "follow_up"
        sla_type = kwargs.get("sla_type")
        sla_created = kwargs.get("sla_created")
        if sla_hours and date_type == "sla":
            sla_type = sla_type or "response"
            sla_created = sla_created or self.hours.now_local_naive().isoformat(timespec="seconds")
        st_id = kwargs.get("id") or ("st_" + uuid.uuid4().hex[:8])
        self._conn.execute(
            "INSERT INTO subtasks (id, project_id, text, status, source, created, follow_up_date, "
            "date_type, sla_type, sla_created, sla_hours, category, priority, metadata) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (st_id, pid, text, source or "system", kwargs.get("created") or self._today(),
             follow_up_date, date_type, sla_type, sla_created, sla_hours, category or "",
             int(priority or 0), self._json_col(kwargs.get("metadata") or {})))
        self._conn.commit()
        self._reactivate_if_closed(pid, trigger=text)
        return st_id

    def _reactivate_if_closed(self, pid, trigger=""):
        """closed/cold role + fresh pending work -> back to the workflow's entry
        stage. 'ignored' is an explicit dismissal and never revives.
        db.py:1463-1493 with roles."""
        row = self._conn.execute(
            "SELECT status, workflow_id, follow_up_date FROM projects WHERE id = ?", (pid,)).fetchone()
        if not row:
            return False
        doc = self.wf_doc()
        role = wfm.role_of(doc, row["status"], row["workflow_id"])
        if role not in wfm.REVIVE_ROLES:
            return False
        target = wfm.entry_stage(doc, row["workflow_id"] or wfm.default_workflow_id(doc))
        prior = row["status"]
        self._conn.execute(
            "UPDATE projects SET status = ?, closed_date = NULL, follow_up_date = ?, updated_at = ? "
            "WHERE id = ?", (target, row["follow_up_date"] or self._today(), self._now(), pid))
        self._conn.commit()
        snippet = (trigger or "").strip()[:60]
        self.append_note(pid, f"Reactivated ({prior} -> {target}): new work added"
                         + (f" - '{snippet}'" if snippet else ""), f"{self._today()}, auto")
        return True

    def open_subtasks(self, pid):
        return self._subtask_rows(pid, status="pending")

    def get_subtasks(self, pid, status=None):
        return self._subtask_rows(pid, status=status)

    def find_subtask_project(self, subtask_id):
        row = self._conn.execute("SELECT project_id FROM subtasks WHERE id = ?", (subtask_id,)).fetchone()
        return row["project_id"] if row else None

    def complete_subtask(self, pid, subtask_id, completed_by=None):
        """Idempotent; send-type tasks need a trusted actor. db.py:1516-1556."""
        row = self._conn.execute(
            "SELECT text, status FROM subtasks WHERE id = ? AND project_id = ?", (subtask_id, pid)).fetchone()
        if row is None:
            return False
        if row["status"] == "complete":
            return True
        actor = (completed_by or "").strip()
        if self.SEND_TASK_RE.search(row["text"] or "") and actor not in self.TRUSTED_SEND_ACTORS:
            self.append_note(pid, f"Blocked auto-completion of send-type task '{(row['text'] or '')[:60]}' "
                                  f"by '{actor or 'unknown'}': built is not sent.",
                             f"{self._today()}, guard")
            return False
        self._conn.execute(
            "UPDATE subtasks SET status = 'complete', completed_date = ?, completed_by = ? "
            "WHERE id = ? AND project_id = ?", (self._today(), actor or "auto_sync", subtask_id, pid))
        self._conn.commit()
        return True

    def reopen_subtask(self, pid, subtask_id):
        self._conn.execute(
            "UPDATE subtasks SET status = 'pending', completed_date = NULL, completed_by = NULL "
            "WHERE id = ? AND project_id = ?", (subtask_id, pid))
        self._conn.commit()

    def delete_subtask(self, pid, subtask_id):
        self._conn.execute("DELETE FROM subtasks WHERE id = ? AND project_id = ?", (subtask_id, pid))
        self._conn.commit()

    def update_subtask_fields(self, pid, subtask_id, **fields):
        allowed = {"text", "follow_up_date", "date_type", "sla_type", "sla_created", "sla_hours",
                   "category", "priority", "metadata", "blocked_by", "status", "completed_date",
                   "completed_by"}
        fields = {k: (self._json_col(v) if isinstance(v, (dict, list)) else v)
                  for k, v in fields.items() if k in allowed}
        if not fields:
            return
        set_clause = ", ".join(f'"{k}" = ?' for k in fields)
        self._conn.execute(f"UPDATE subtasks SET {set_clause} WHERE id = ? AND project_id = ?",
                           list(fields.values()) + [subtask_id, pid])
        self._conn.commit()

    def complete_subtasks_matching(self, pid, text_prefix, completed_by="system"):
        n = 0
        prefix = (text_prefix or "").lower()
        for st in self.open_subtasks(pid):
            if (st.get("text") or "").lower().startswith(prefix):
                if self.complete_subtask(pid, st["id"], completed_by=completed_by):
                    n += 1
        return n

    def _local_naive(self, raw):
        dt = parse_dt(raw)
        if dt is None:
            return None
        return self.hours.to_local_naive(dt)

    def auto_complete_response_subtasks(self, pid, email_obj):
        """The user's outbound reply clears the 'Respond to X' it answers.
        Temporal guard: only tasks older than the outbound. db.py:1574-1665."""
        pending = self.open_subtasks(pid)
        if not pending:
            return []
        outbound_dt = None
        for f in ("received_at", "sent_at"):
            if email_obj.get(f):
                outbound_dt = self._local_naive(email_obj[f])
                if outbound_dt:
                    break
        to_field = ((email_obj.get("to") or "") + ";" + (email_obj.get("cc") or "")).lower()
        subject = (email_obj.get("subject") or "").lower().strip()
        clean_subject = re.sub(r'^(re|fw|fwd)\s*:\s*', '', subject, flags=re.IGNORECASE).strip()
        completed = []
        for st in pending:
            text = st.get("text", "")
            if outbound_dt and st.get("sla_created"):
                sdt = parse_dt(st["sla_created"])
                if sdt is not None and sdt.replace(tzinfo=None) > outbound_dt:
                    continue
            if text.startswith("Respond to "):
                colon = text.find(":", 11)
                if colon > 11:
                    name = text[11:colon].strip().lower()
                    parts = [p for p in re.split(r'[.\s_-]+', name) if len(p) > 1]
                    if to_field and parts and all(p in to_field for p in parts):
                        self.complete_subtask(pid, st["id"], completed_by="sent_reply")
                        completed.append((st["id"], text))
                        continue
            if text.startswith("Email: "):
                st_subject = text[7:].lower().rstrip(".").rstrip()
                if clean_subject and st_subject and (clean_subject.startswith(st_subject)
                                                     or st_subject.startswith(clean_subject)):
                    self.complete_subtask(pid, st["id"], completed_by="sent_reply")
                    completed.append((st["id"], text))
        if completed:
            self.append_note(pid, "Auto-completed on sent reply: " + ", ".join(c[0] for c in completed),
                             f"{self._today()}, auto")
        return completed

    def auto_complete_followup_subtasks(self, pid, email_obj):
        """The other side writing back clears 'Follow up with X: ...'.
        db.py:1667-1774."""
        pending = self.open_subtasks(pid)
        if not pending:
            return []
        inbound_dt = None
        for f in ("received_at", "sent_at"):
            if email_obj.get(f):
                inbound_dt = self._local_naive(email_obj[f])
                if inbound_dt:
                    break
        inbound_day = inbound_dt.date() if inbound_dt else None
        from_email = (email_obj.get("from_email") or "").lower().strip()
        from_name = (email_obj.get("from_name") or "").lower().strip()
        sender = f"{from_email} {from_name}".strip()
        if not sender:
            return []
        sender_local = from_email.split("@")[0] if "@" in from_email else ""
        completed = []
        for st in pending:
            text = st.get("text", "")
            if not text.startswith("Follow up with "):
                continue
            colon = text.find(":", 15)
            if colon <= 15:
                continue
            recip = text[15:colon].strip().lower()
            if not recip or recip == "recipient" or self.SEND_TASK_RE.search(text):
                continue
            if inbound_day and st.get("created"):
                try:
                    if date.fromisoformat(str(st["created"])[:10]) > inbound_day:
                        continue
                except ValueError:
                    pass
            if "@" in recip:
                r_local = recip.split("@")[0]
                matched = recip in sender or (len(r_local) > 2 and sender_local and r_local == sender_local)
            else:
                parts = [p for p in re.split(r'[.\s_-]+', recip) if len(p) > 1]
                matched = bool(parts) and all(p in sender for p in parts)
            if matched:
                self.complete_subtask(pid, st["id"], completed_by="reply_received")
                completed.append((st["id"], text))
        if completed:
            self.append_note(pid, "Auto-completed on reply received: " + ", ".join(c[0] for c in completed),
                             f"{self._today()}, auto")
        return completed

    # ── Notes (db.py:1787-1811) ─────────────────────────────────────────────

    def _insert_note(self, pid, text, time_str=None, source=""):
        self._conn.execute(
            "INSERT INTO notes (project_id, text, time, source, text_hash) VALUES (?, ?, ?, ?, ?)",
            (pid, text, time_str, source or "", str(hash(text.lower().strip()[:200]))))

    def append_note(self, pid, text, time_str=None, source=""):
        """Append with similarity dedup over the last 20 notes. True if added."""
        text = (text or "").strip()
        if not text:
            return False
        for row in self._conn.execute(
                "SELECT text FROM notes WHERE project_id = ? ORDER BY id DESC LIMIT 20", (pid,)).fetchall():
            if _notes_are_similar(row["text"], text):
                return False
        self._insert_note(pid, text, time_str or f"{self._today()}, {source or 'triage'}", source)
        self._conn.commit()
        return True

    def add_note(self, pid, text, source="system"):
        self.append_note(pid, text, time_str=self._note_time(source), source=source)

    def increment_emails_sent(self, pid, count=1):
        self._conn.execute("UPDATE projects SET emails_sent = COALESCE(emails_sent, 0) + ? WHERE id = ?",
                           (count, pid))
        self._conn.commit()

    # ══════════════════════════════════════════════════════════════════════
    # ROUTING + DEDUP (db.py:1849-1918, 2352-2366)
    # ══════════════════════════════════════════════════════════════════════

    def routing_lookup(self, thread_key):
        if not thread_key:
            return None
        row = self._conn.execute("SELECT project_id FROM conversation_routing WHERE conversation_id = ?",
                                 (thread_key,)).fetchone()
        return row["project_id"] if row else None

    def routing_set(self, thread_key, pid, source=""):
        if not thread_key or not pid:
            return
        self._conn.execute(
            "INSERT INTO conversation_routing (conversation_id, project_id, source, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET "
            "project_id = excluded.project_id, source = excluded.source, created_at = excluded.created_at",
            (thread_key, pid, source or "", self._now()))
        self._conn.commit()

    get_conversation_project = routing_lookup

    def set_conversation_routing(self, conversation_id, pid, source=""):
        self.routing_set(conversation_id, pid, source)

    def set_alt_thread_routing(self, alt_key, pid):
        if alt_key:
            self.routing_set(f"{self.ALT_ROUTE_PREFIX}{alt_key}", pid, "alt")

    def get_alt_thread_project(self, alt_key, max_age_days=None):
        """Windowed alt-key lookup: (project_id, age_days) or (None, None)."""
        if not alt_key:
            return None, None
        window = self.ALT_ROUTE_WINDOW_DAYS if max_age_days is None else max_age_days
        row = self._conn.execute("SELECT project_id, created_at FROM conversation_routing "
                                 "WHERE conversation_id = ?", (f"{self.ALT_ROUTE_PREFIX}{alt_key}",)).fetchone()
        if not row:
            return None, None
        if not row["created_at"]:
            return row["project_id"], None
        dt = self._local_naive(row["created_at"])
        if dt is None:
            return row["project_id"], None
        age = (self.hours.now_local_naive() - dt).total_seconds() / 86400.0
        if age > window:
            return None, None
        return row["project_id"], round(age, 2)

    def thread_route_open(self, pid):
        """A thread route only auto-commits into an OPEN project (server.py:2982-3008)."""
        if not pid or self.get_project_location(pid) != "active":
            return False
        proj = self.get_project(pid) or {}
        return not wfm.is_closed(self.wf_doc(), proj.get("status"), proj.get("workflow_id"))

    def message_seen(self, message_id):
        if not message_id:
            return False
        return self._conn.execute("SELECT 1 FROM processed_messages WHERE message_id = ?",
                                  (message_id,)).fetchone() is not None

    @_retry_on_locked
    def message_mark(self, message_id, status="processed", pid=None, triage_id=None, subject=None,
                     received_at=None):
        if not message_id:
            return
        self._conn.execute(
            "INSERT INTO processed_messages (message_id, triage_id, project_id, subject, received_at, "
            "status, processed_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(message_id) DO UPDATE SET "
            "status = excluded.status, project_id = COALESCE(excluded.project_id, project_id), "
            "triage_id = COALESCE(excluded.triage_id, triage_id)",
            (message_id, triage_id, pid, subject, received_at, status or "processed", self._now()))
        self._conn.commit()

    is_processed = message_seen

    def mark_processed(self, message_id, triage_id=None, subject=None, received_at=None, pid=None):
        self.message_mark(message_id, "processed", pid=pid, triage_id=triage_id, subject=subject,
                          received_at=received_at)

    # ══════════════════════════════════════════════════════════════════════
    # INDEXES FOR MATCHING (db.py:2037-2095, generalized)
    # ══════════════════════════════════════════════════════════════════════

    def contact_index(self):
        """email_lower -> [pid] over active projects."""
        index = {}
        for r in self._conn.execute(
                "SELECT LOWER(c.email) AS k, c.project_id FROM contacts c JOIN projects p ON p.id = c.project_id "
                "WHERE c.email != '' AND p.archived = 0").fetchall():
            index.setdefault(r["k"], [])
            if r["project_id"] not in index[r["k"]]:
                index[r["k"]].append(r["project_id"])
        return index

    def _id_patterns(self):
        out = []
        for p in self.profession().get("id_patterns") or []:
            try:
                out.append((p.get("key") or "ref", re.compile(p.get("regex") or "", re.IGNORECASE)))
            except re.error:
                continue
        return out

    def extract_ids(self, text):
        """{pattern_key: [values]} found in free text with the profession's id patterns."""
        found = {}
        for key, rx in self._id_patterns():
            vals = sorted({re.sub(r"\s+", "", (m.group(1) if m.groups() else m.group(0))).upper()
                           for m in rx.finditer(text or "")})
            if vals:
                found[key] = vals
        return found

    def id_index(self):
        """id-pattern value (upper) -> [pid]: scans key_details, name and notes of
        active projects."""
        patterns = self._id_patterns()
        index = {}
        if not patterns:
            return index
        rows = self._conn.execute("SELECT id, name, key_details FROM projects WHERE archived = 0").fetchall()
        notes = {}
        for n in self._conn.execute(
                "SELECT n.project_id, n.text FROM notes n JOIN projects p ON p.id = n.project_id "
                "WHERE p.archived = 0").fetchall():
            notes.setdefault(n["project_id"], []).append(n["text"] or "")
        for r in rows:
            blob = " ".join([r["name"] or "", r["key_details"] or ""] + notes.get(r["id"], []))
            kd = self._parse_json(r["key_details"], {}) or {}
            vals = set()
            for key, rx in patterns:
                for m in rx.finditer(blob):
                    vals.add(re.sub(r"\s+", "", (m.group(1) if m.groups() else m.group(0))).upper())
                v = kd.get(key)
                if isinstance(v, (str, int)) and str(v).strip():
                    vals.add(re.sub(r"\s+", "", str(v)).upper())
            for v in (kd.get("references") or []):
                if isinstance(v, str) and v.strip():
                    vals.add(v.strip().upper())
            # Vendor refs (ORD:<domain>:<n> / TRK:<n>) are stored verbatim by the
            # triage layer under key_details.refs and matched verbatim.
            for v in (kd.get("refs") or []):
                if isinstance(v, str) and v.strip():
                    vals.add(v.strip())
            for v in vals:
                index.setdefault(v, [])
                if r["id"] not in index[v]:
                    index[v].append(r["id"])
        return index

    def keyword_index(self):
        """token -> [pid] over name, location and scope of active projects."""
        index = {}
        for r in self._conn.execute("SELECT id, name, location, scope FROM projects WHERE archived = 0").fetchall():
            for tok in set(_tokens(f"{r['name']} {r['location']} {r['scope']}")):
                index.setdefault(tok, [])
                if r["id"] not in index[tok]:
                    index[tok].append(r["id"])
        return index

    def store_ids_from_email(self, email_obj, pid):
        """Register every id-pattern hit from an email on the project's
        key_details.references (and fill the pattern key if empty). Returns the
        list of new values."""
        blob = " ".join(str(email_obj.get(k) or "") for k in ("subject", "body_preview", "body_raw", "body"))
        found = self.extract_ids(blob)
        if not found:
            return []
        proj = self.get_project(pid)
        if not proj:
            return []
        kd = dict(proj.get("key_details") or {})
        refs = set(v for v in (kd.get("references") or []) if isinstance(v, str))
        new = []
        for key, vals in found.items():
            for v in vals:
                if v not in refs:
                    refs.add(v)
                    new.append(v)
            if not kd.get(key):
                kd[key] = vals[0]
        if new:
            kd["references"] = sorted(refs)
            self.update_project(pid, key_details=kd)
        return new

    # ══════════════════════════════════════════════════════════════════════
    # ALERTS (db.py:1930-2031)
    # ══════════════════════════════════════════════════════════════════════

    def raise_alert(self, key, title, detail="", severity="warn", source="server"):
        if not key or not title:
            return None
        if severity not in ("hard", "warn", "info"):
            severity = "warn"
        now = self._now()
        row = self._conn.execute("SELECT id, status, acked_at FROM alerts WHERE key = ? AND status != 'resolved' "
                                 "ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        if row:
            reopen = False
            if row["status"] == "acked" and row["acked_at"]:
                a = self._local_naive(row["acked_at"])
                reopen = a is None or (self.hours.now_local_naive() - a).total_seconds() / 3600.0 \
                    > self.ALERT_REOPEN_AFTER_ACK_HOURS
            self._conn.execute(
                "UPDATE alerts SET last_seen_at = ?, seen_count = seen_count + 1, title = ?, detail = ?, "
                "severity = ?, status = CASE WHEN ? THEN 'open' ELSE status END, "
                "acked_at = CASE WHEN ? THEN NULL ELSE acked_at END WHERE id = ?",
                (now, title, detail, severity, 1 if reopen else 0, 1 if reopen else 0, row["id"]))
            self._conn.commit()
            return row["id"]
        cur = self._conn.execute(
            "INSERT INTO alerts (key, severity, title, detail, source, created_at, last_seen_at, seen_count, "
            "status) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'open')", (key, severity, title, detail, source, now, now))
        self._conn.commit()
        return cur.lastrowid

    def add_alert(self, kind, text, severity="warn", detail="", source="server"):
        return self.raise_alert(kind, text, detail=detail, severity=severity, source=source)

    def list_alerts(self, status="open", limit=100):
        if status == "all":
            rows = self._conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        elif status == "active":
            rows = self._conn.execute("SELECT * FROM alerts WHERE status IN ('open','acked') "
                                      "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM alerts WHERE status = ? ORDER BY id DESC LIMIT ?",
                                      (status, limit)).fetchall()
        return self._rows(rows)

    def open_alerts(self, limit=100):
        return self.list_alerts("open", limit)

    def ack_alert(self, alert_id, by="user"):
        self._conn.execute("UPDATE alerts SET status = 'acked', acked_at = ?, acked_by = ? "
                           "WHERE id = ? AND status = 'open'", (self._now(), by, alert_id))
        self._conn.commit()
        return self._row(self._conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone())

    def resolve_alerts(self, keys=None, source=None, except_keys=None):
        """COMMIT UNCONDITIONALLY: an UPDATE matching zero rows still opened a
        write transaction (the reference 2026-09-04 outage)."""
        now, n = self._now(), 0
        if keys:
            for k in keys:
                n += self._conn.execute("UPDATE alerts SET status = 'resolved', resolved_at = ? "
                                        "WHERE key = ? AND status != 'resolved'", (now, k)).rowcount
        elif source is not None:
            keep = set(except_keys or [])
            for r in self._conn.execute("SELECT id, key FROM alerts WHERE source = ? AND status != 'resolved'",
                                        (source,)).fetchall():
                if r["key"] in keep:
                    continue
                n += self._conn.execute("UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE id = ?",
                                        (now, r["id"])).rowcount
        self._conn.commit()
        return n

    # ══════════════════════════════════════════════════════════════════════
    # TRIAGE CARDS (db.py:2101-2217)
    # ══════════════════════════════════════════════════════════════════════

    _CARD_JSON_LISTS = ("project_matches", "attachments", "attachment_data", "triage_actions",
                        "completion_signals", "subtask_completions", "inline_image_refs", "doc_links")
    _CARD_JSON_OBJS = ("signals", "suggestion")

    def _card_out(self, row):
        d = dict(row)
        for col in self._CARD_JSON_LISTS:
            d[col] = self._parse_json(d.get(col), []) or []
        for col in self._CARD_JSON_OBJS:
            d[col] = self._parse_json(d.get(col))
        if d.get("signals") is None:
            d["signals"] = {}
        if d.get("suggestion") is None:
            d["suggestion"] = {}
        d["needs_reply"] = bool(d.get("needs_reply"))
        d["id"] = d.get("triage_id")          # the triage layer and the UI both read `id`
        return d

    def _card_columns(self):
        return {r[1] for r in self._conn.execute("PRAGMA table_info(triage_cards)").fetchall()}

    def list_triage(self, status="pending"):
        rows = self._conn.execute("SELECT * FROM triage_cards WHERE status = ? ORDER BY staged_at DESC",
                                  (status,)).fetchall()
        return [self._card_out(r) for r in rows]

    list_triage_cards = list_triage

    def get_triage_card(self, triage_id):
        row = self._conn.execute("SELECT * FROM triage_cards WHERE triage_id = ?", (triage_id,)).fetchone()
        return self._card_out(row) if row else None

    @_retry_on_locked
    def add_triage_card(self, card):
        """Insert a card (unknown keys are dropped, non-primitives JSON-encoded)."""
        card = dict(card or {})
        triage_id = card.get("triage_id") or ("msg_" + uuid.uuid4().hex[:12])
        card["triage_id"] = triage_id
        card.setdefault("card_type", "email")
        if card.get("status") not in ("pending", "approved", "dismissed", "auto_committed"):
            card["status"] = "pending"
        card.setdefault("staged_at", self._now())
        for flag in ("needs_reply", "is_self_note", "is_quote_request", "has_attachments"):
            if isinstance(card.get(flag), bool):
                card[flag] = 1 if card[flag] else 0
        if "site" in card and "new_project_location" not in card:
            card["new_project_location"] = card.pop("site")
        if "new_project_site" in card:
            card["new_project_location"] = card.pop("new_project_site")
        if "body" in card and not card.get("body_raw"):
            card["body_raw"] = card.pop("body")
        else:
            card.pop("body", None)
        if card.get("attachments") and not card.get("has_attachments"):
            card["has_attachments"] = 1
        cols = self._card_columns()
        clean = {}
        for k, v in card.items():
            if k not in cols:
                continue
            if v is not None and not isinstance(v, (str, int, float, bytes)):
                v = self._json_col(v)
            clean[k] = v
        names = list(clean.keys())
        self._conn.execute(
            f"INSERT OR REPLACE INTO triage_cards ({', '.join(chr(34) + c + chr(34) for c in names)}) "
            f"VALUES ({', '.join('?' * len(names))})", [clean[c] for c in names])
        self._conn.commit()
        return triage_id

    @_retry_on_locked
    def update_triage_card(self, triage_id, **fields):
        cols = self._card_columns()
        clean = {}
        for k, v in fields.items():
            if k not in cols:
                continue
            if v is not None and not isinstance(v, (str, int, float, bytes)):
                v = self._json_col(v)
            clean[k] = v
        if not clean:
            return
        set_clause = ", ".join(f'"{k}" = ?' for k in clean)
        self._conn.execute(f"UPDATE triage_cards SET {set_clause} WHERE triage_id = ?",
                           list(clean.values()) + [triage_id])
        self._conn.commit()

    def count_pending_triage(self):
        return self._conn.execute("SELECT COUNT(*) FROM triage_cards WHERE status = 'pending'").fetchone()[0]

    def dismiss_triage(self, triage_id, reason="", actor="system"):
        self._conn.execute("UPDATE triage_cards SET status = 'dismissed', resolved_at = ?, resolved_by = ?, "
                           "dismiss_reason = ? WHERE triage_id = ?", (self._now(), actor, reason or "", triage_id))
        self._conn.commit()

    def revive_triage(self, triage_id):
        self.update_triage_card(triage_id, status="pending", staged_at=self._now(), resolved_at=None,
                                resolved_by=None)

    def should_auto_commit(self, email_obj):
        """(project_id, reason) or (None, None): thread id or windowed alt key
        into an OPEN project only. db.py:2488-2520 + server.py:2982-3008."""
        for action in email_obj.get("triage_actions") or []:
            if isinstance(action, dict) and action.get("type") in self._ESCALATION_ACTIONS:
                return None, None
        conv = email_obj.get("conversation_id") or ""
        if conv:
            pid = self.routing_lookup(conv)
            if pid and self.thread_route_open(pid):
                return pid, "thread_match"
        alt = email_obj.get("thread_key_alt")
        if alt:
            pid, _age = self.get_alt_thread_project(alt)
            if pid and self.thread_route_open(pid):
                return pid, "alt_thread_match"
        return None, None

    # money signals (db.py:2524-2620), used only when the profession has money
    _PROPOSAL_SUBJECT_RE = re.compile(r"(?i)\b(proposal|quote|pricing|estimate|bid|offer)\b")
    _PROPOSAL_BODY_RE = re.compile(
        r"(?i)(our price|total price|lump sum price|we (?:are |)propos|pleased to (?:submit|provide|offer)|"
        r"attached (?:is |)(?:our |a |the )(?:proposal|quote|estimate)|scope of work)")
    _BODY_DOLLAR_RE = re.compile(r'\$\s*([\d,]+(?:\.\d{2})?)\b')

    def is_proposal_email(self, email_obj):
        subj = (email_obj.get("subject") or "").lower()
        if self._PROPOSAL_SUBJECT_RE.search(subj):
            return True
        if (email_obj.get("classification") or "").lower() in ("proposal_sent", "quote_sent"):
            return True
        for att in email_obj.get("attachment_data") or []:
            if isinstance(att, dict) and (att.get("file_type") in ("pdf_proposal", "pdf_contract")
                                          or att.get("labeled_amounts")):
                return True
        if email_obj.get("direction") == "outbound":
            body = email_obj.get("body_preview") or email_obj.get("body_raw") or email_obj.get("body") or ""
            if self._PROPOSAL_BODY_RE.search(body) and self._BODY_DOLLAR_RE.search(body):
                return True
        return False

    def extract_quote_amount(self, email_obj, cap=500_000):
        labeled_best = raw_best = 0
        for att in email_obj.get("attachment_data") or []:
            if not isinstance(att, dict):
                continue
            for la in att.get("labeled_amounts") or []:
                amt = (la or {}).get("amount", 0) if isinstance(la, dict) else 0
                if amt and amt > labeled_best:
                    labeled_best = amt
            for da in att.get("dollar_amounts") or []:
                if da and da > raw_best:
                    raw_best = da
        if labeled_best:
            return labeled_best
        if raw_best and raw_best <= cap:
            return raw_best
        if email_obj.get("dollar_amount"):
            return email_obj["dollar_amount"]
        body = email_obj.get("body_preview") or email_obj.get("body_raw") or email_obj.get("body") or ""
        best = 0
        for m in self._BODY_DOLLAR_RE.finditer(body):
            try:
                v = float(m.group(1).replace(",", ""))
                if best < v <= cap:
                    best = v
            except ValueError:
                pass
        return best or None

    def auto_commit_email(self, email_obj, pid, reason="thread_match"):
        """File an email straight onto a project, bypassing triage. Returns a
        proposal-detected dict (money installs, outbound proposal) or True."""
        proj = self.get_project(pid)
        if not proj:
            return False
        direction = email_obj.get("direction", "")
        from_info = email_obj.get("from_name") or email_obj.get("from_email") or "unknown"
        subject = email_obj.get("subject") or ""
        received_at = email_obj.get("received_at") or ""
        self.append_note(pid, f"Auto-filed ({reason}): {direction} from {from_info}: {subject[:60]}",
                         received_at or f"{self._today()}, auto")
        updates = {"last_activity": received_at or self._now(),
                   "last_communication": received_at[:10] if received_at else self._today()}
        for action in email_obj.get("triage_actions") or []:
            if isinstance(action, dict) and action.get("type") == "follow_up_sent" and action.get("new_follow_up"):
                updates["follow_up_date"] = action["new_follow_up"]
        self.update_project(pid, **updates)
        result = True
        if direction != "outbound" and not email_obj.get("is_self_note"):
            self.auto_complete_followup_subtasks(pid, email_obj)
        if direction == "outbound" and not email_obj.get("is_self_note"):
            self.increment_emails_sent(pid)
            self.auto_complete_response_subtasks(pid, email_obj)
            if self.money_enabled() and self.is_proposal_email(email_obj):
                result = {"project_id": pid, "quote_amount": self.extract_quote_amount(email_obj),
                          "detected_date": received_at[:10] if received_at else self._today(),
                          "project_status": proj.get("status")}
        if email_obj.get("conversation_id"):
            self.routing_set(email_obj["conversation_id"], pid, "auto_commit")
        self.set_alt_thread_routing(email_obj.get("thread_key_alt"), pid)
        self.append_event("auto_commit_email", {
            "project_id": pid, "reason": reason, "subject": subject, "direction": direction,
            "from_email": email_obj.get("from_email", ""), "received_at": received_at,
            "conversation_id": email_obj.get("conversation_id", ""), "message_id": email_obj.get("message_id", "")})
        return result

    def approve_triage(self, triage_id, pid_or_new, *, workflow_id=None, status=None, work_type=None,
                       new_name=None, new_location=None, actor="system", subtask_completions=None,
                       quote_request=None, dollar_amount=None):
        """Approve an email card onto an existing project or '__new__'.
        Generalized port of server.py:_approve_email (4000-4480): tracking
        updates, id registration, routing, response/follow-up SLA task,
        correspondence filing, sibling-card commit. Returns the approve
        response dict; raises ValueError / LookupError on bad input."""
        from . import ladder  # lazy: ladder takes a store, never imports db

        item = self.get_triage_card(triage_id)
        if not item:
            raise LookupError(f"triage card {triage_id} not found")
        if (item.get("status") or "pending") != "pending":
            prior = item.get("matched_project") or ""
            return {"ok": True, "already_approved": True, "approved": triage_id,
                    "prior_status": item.get("status"), "project": self.get_project(prior) if prior else None,
                    "remaining": self.count_pending_triage(), "thread_siblings_committed": 0}
        pid = (pid_or_new or item.get("matched_project") or "").strip()
        if pid in ("undefined", "null", "None"):
            raise ValueError(f"invalid project id '{pid}'")
        if new_name is not None:
            item["new_project_name"] = new_name
        if new_location is not None:
            item["new_project_location"] = new_location
        if dollar_amount is not None:
            item["dollar_amount"] = dollar_amount
        if not pid and (item.get("new_project_name") or item.get("new_project_location")):
            pid = "__new__"
        if not pid:
            raise ValueError("no project selected: assign to an existing project or '__new__'")

        doc = self.wf_doc()
        email_dt = self.hours.to_local_naive(parse_dt(item.get("received_at") or item.get("sent_at"))
                                             or self.hours.now())
        email_day = email_dt.strftime("%Y-%m-%d")
        follow_up = item.get("auto_follow_up")
        date_type = item.get("auto_date_type") or "follow_up"
        direction = item.get("direction") or "inbound"
        money = self.money_enabled()
        is_self = bool(item.get("is_self_note"))
        from_is_user = self.is_user(item.get("from_email"), item.get("from_name"))

        if pid != "__new__":
            proj = self.get_project(pid)
            if not proj:
                raise LookupError(f"project {pid} not found")
            self.store_ids_from_email(item, pid)
            updates = {"last_activity": self._now(), "last_communication": email_day}
            if follow_up:
                updates["follow_up_date"] = follow_up
            if direction == "outbound":
                self.increment_emails_sent(pid)
                if not is_self:
                    self.auto_complete_response_subtasks(pid, item)
            elif not is_self:
                self.auto_complete_followup_subtasks(pid, item)
            if money and item.get("dollar_amount") and not proj.get("quote_amount"):
                updates["quote_amount"] = item["dollar_amount"]
            if money and direction == "outbound" and self.is_proposal_email(item):
                if not proj.get("quote_sent_date"):
                    updates["quote_sent_date"] = email_day
                if not proj.get("quote_amount") and not updates.get("quote_amount"):
                    amt = self.extract_quote_amount(item)
                    if amt:
                        updates["quote_amount"] = amt
            self.update_project(pid, **updates)
            from_info = item.get("from_name") or item.get("from_email") or "unknown"
            self.append_note(pid, f"Triage: {direction} from {from_info}: {(item.get('subject') or '')[:60]}",
                             self._note_time("triage"))
            done = []
            for st_id in subtask_completions or []:
                if self.complete_subtask(pid, st_id, completed_by="triage_approval"):
                    done.append(st_id)
            if done:
                self.append_note(pid, "Subtasks completed via triage: " + ", ".join(done), self._note_time("triage"))
            if item.get("conversation_id"):
                self.routing_set(item["conversation_id"], pid, "approve")
            self.set_alt_thread_routing(item.get("thread_key_alt"), pid)
            if not is_self:
                ladder.spawn_response_subtask(self, item, pid, from_is_user)
            result_project = self.get_project(pid)
            final_pid = pid
        else:
            location = (item.get("new_project_location") or "").strip()
            job = (item.get("new_project_name") or "").strip()
            if location and job and job.lower().startswith(location.lower()):
                title = job
            elif location and job:
                title = f"{location} {job}"
            else:
                title = job or location or (item.get("subject") or "New project")
            contacts = []
            if direction == "inbound" and item.get("from_email") and not from_is_user:
                contacts.append({"name": item.get("from_name", ""), "email": item.get("from_email", ""), "role": ""})
            is_quote = bool(quote_request if quote_request is not None
                            else (item.get("is_quote_request") or is_self))
            wid = (workflow_id or item.get("workflow_id") or "").strip()
            if not wfm.get_workflow(doc, wid):
                wid = wfm.resolve_workflow_id(doc, work_type or item.get("work_type"), self.work_types())
            initial_status = (status or "").strip() or wfm.entry_stage(doc, wid)
            if is_quote and not status:
                qstage, _ = wfm.quote_task_for(doc, wid)
                if qstage:
                    initial_status = qstage
            new_id = self.create_project(
                name=title, workflow_id=wid, status=initial_status,
                work_type=work_type or item.get("work_type") or "", contacts=contacts,
                location=location, follow_up_date=follow_up,
                quote_amount=(item.get("dollar_amount") if money else None),
                emails_sent=1 if direction == "outbound" else 0, last_activity=self._now(),
                last_communication=email_day, source="triage",
                notes=[{"text": f"Created from triage: {item.get('subject') or ''}",
                        "time": self._note_time("triage")}])
            if item.get("conversation_id"):
                self.routing_set(item["conversation_id"], new_id, "approve")
            self.set_alt_thread_routing(item.get("thread_key_alt"), new_id)
            self.store_ids_from_email(item, new_id)
            armed = ladder.ensure_entry_stage_task(self, new_id)
            if armed and date_type == "deadline" and follow_up:
                self.update_subtask_fields(new_id, armed, date_type="deadline", follow_up_date=str(follow_up)[:10])
            if not is_self:
                ladder.spawn_response_subtask(self, item, new_id, from_is_user)
            result_project = self.get_project(new_id)
            final_pid = new_id

        self.update_triage_card(triage_id, status="approved", matched_project=final_pid,
                                resolved_at=self._now(), resolved_by=actor)
        if item.get("message_id"):
            self.message_mark(item["message_id"], "approved", pid=final_pid, triage_id=triage_id,
                              subject=item.get("subject"), received_at=item.get("received_at"))
        self.append_event("approve_email", {"triage_id": triage_id, "project_id": final_pid,
                                            "subject": item.get("subject", ""), "actor": actor})
        try:
            self.file_correspondence(final_pid, item, [], source="triage", triage_id=triage_id)
        except Exception:
            pass

        siblings = 0
        conv = item.get("conversation_id") or ""
        if conv:
            for sib in self._conn.execute(
                    "SELECT triage_id FROM triage_cards WHERE conversation_id = ? AND status = 'pending' "
                    "AND triage_id != ?", (conv, triage_id)).fetchall():
                sib_card = self.get_triage_card(sib["triage_id"])
                if not sib_card:
                    continue
                self.auto_commit_email(sib_card, final_pid, reason=f"thread_sibling of {triage_id}")
                self.update_triage_card(sib["triage_id"], status="auto_committed", matched_project=final_pid,
                                        resolved_at=self._now(), resolved_by=actor)
                if sib_card.get("message_id"):
                    self.message_mark(sib_card["message_id"], "auto_committed", pid=final_pid,
                                      triage_id=sib["triage_id"], subject=sib_card.get("subject"),
                                      received_at=sib_card.get("received_at"))
                try:
                    self.file_correspondence(final_pid, sib_card, [], source="auto_commit",
                                             triage_id=sib["triage_id"])
                except Exception:
                    pass
                siblings += 1
        return {"ok": True, "approved": triage_id, "project": result_project,
                "remaining": self.count_pending_triage(), "thread_siblings_committed": siblings}

    def file_before_dismiss(self, item, triage_id):
        """Dismiss must not delete history: a card on an already-routed thread
        is still filed (server.py:4985-5053). Returns the project id or None."""
        if (item.get("card_type") or "email") != "email" or item.get("is_self_note"):
            return None
        conv = item.get("conversation_id") or ""
        pid = self.routing_lookup(conv) if conv else None
        if not pid or not self.project_exists(pid):
            return None
        self.file_correspondence(pid, item, [], source="triage", triage_id=triage_id)
        self.update_project(pid, last_activity=self._now(),
                            last_communication=(item.get("received_at") or "")[:10] or self._today())
        if (item.get("direction") or "inbound") == "outbound":
            self.increment_emails_sent(pid)
            self.auto_complete_response_subtasks(pid, item)
        else:
            self.auto_complete_followup_subtasks(pid, item)
        self.append_note(pid, f"Dismissed in triage; {item.get('direction') or 'inbound'} correspondence "
                              f"still filed: {(item.get('subject') or '')[:60]}", self._note_time("triage"))
        return pid

    # ══════════════════════════════════════════════════════════════════════
    # CORRESPONDENCE (db.py:2969-3135)
    # ══════════════════════════════════════════════════════════════════════

    def save_correspondence(self, pid, email_data, source="triage", triage_id=None, attachment_files=None):
        msg_id = email_data.get("message_id")
        if msg_id:
            existing = self._conn.execute("SELECT id FROM project_correspondence WHERE project_id = ? "
                                          "AND message_id = ?", (pid, msg_id)).fetchone()
            if existing:
                return existing[0]
        att = email_data.get("attachment_data", [])
        if isinstance(att, str):
            att = self._parse_json(att, []) or []
        if source not in ("triage", "auto_commit", "manual", "assistant"):
            source = "manual"
        direction = email_data.get("direction") or "inbound"
        if direction not in ("inbound", "outbound"):
            direction = "inbound"
        try:
            cur = self._conn.execute(
                "INSERT INTO project_correspondence (project_id, message_id, conversation_id, subject, from_name, "
                "from_email, to_list, cc_list, direction, received_at, body_preview, body_html, source, triage_id, "
                "attachment_summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, msg_id, email_data.get("conversation_id", ""), email_data.get("subject", "") or "",
                 email_data.get("from_name", "") or "", email_data.get("from_email", "") or "",
                 email_data.get("to", "") or "", email_data.get("cc", "") or "", direction,
                 email_data.get("received_at", "") or "", (email_data.get("body_preview") or "")[:500],
                 email_data.get("body_raw") or email_data.get("body_html") or email_data.get("body") or "",
                 source, triage_id, json.dumps(att, default=str)))
            corr_id = cur.lastrowid
            for af in attachment_files or []:
                data = af.get("file_data") or b""
                self._conn.execute(
                    "INSERT INTO correspondence_files (correspondence_id, project_id, filename, file_type, "
                    "mime_type, file_size, file_data, extracted_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (corr_id, pid, af.get("filename", "unknown"), af.get("file_type", "other"),
                     af.get("mime_type", ""), len(data), data, json.dumps(af.get("extracted_data", {}), default=str)))
            self._conn.commit()
            return corr_id
        except Exception:
            self._conn.rollback()
            return None

    _FILE_TYPES = {".pdf": "pdf", ".xlsx": "xlsx", ".xlsm": "xlsx", ".xls": "xlsx", ".csv": "csv",
                   ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".docx": "docx"}

    def file_correspondence(self, pid, email_obj, attachment_paths, source="triage", triage_id=None):
        """Section 9: save an email + the files at attachment_paths (read into
        BLOBs). Returns the correspondence id as a string."""
        files = []
        for p in attachment_paths or []:
            try:
                path = Path(p)
                data = path.read_bytes()
            except Exception:
                continue
            ext = path.suffix.lower()
            files.append({"filename": path.name, "file_type": self._FILE_TYPES.get(ext, "other"),
                          "mime_type": "", "file_data": data})
        corr_id = self.save_correspondence(pid, email_obj, source=source, triage_id=triage_id,
                                           attachment_files=files)
        return str(corr_id) if corr_id is not None else ""

    def get_project_correspondence(self, pid, include_bodies=False):
        cols = ("id, project_id, message_id, conversation_id, subject, from_name, from_email, to_list, cc_list, "
                "direction, received_at, body_preview, source, triage_id, attachment_summary, created_at")
        if include_bodies:
            cols += ", body_html"
        out = []
        for row in self._conn.execute(f"SELECT {cols} FROM project_correspondence WHERE project_id = ? "
                                      "ORDER BY received_at DESC", (pid,)).fetchall():
            d = dict(row)
            d["attachment_summary"] = self._parse_json(d.get("attachment_summary"), []) or []
            d["file_count"] = self._conn.execute("SELECT COUNT(*) FROM correspondence_files "
                                                 "WHERE correspondence_id = ?", (d["id"],)).fetchone()[0]
            if include_bodies:
                html = d.get("body_html") or ""
                text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
                text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
                text = re.sub(r"<[^>]+>", " ", text)
                d["body"] = re.sub(r"[ \t]+", " ", text).strip() or (d.get("body_preview") or "")
            out.append(d)
        return out

    def get_correspondence(self, corr_id):
        return self._row(self._conn.execute("SELECT * FROM project_correspondence WHERE id = ?",
                                            (corr_id,)).fetchone())

    def get_correspondence_files(self, corr_id, include_data=False):
        cols = "id, correspondence_id, project_id, filename, file_type, mime_type, file_size, extracted_data, created_at"
        if include_data:
            cols += ", file_data"
        out = []
        for row in self._conn.execute(f"SELECT {cols} FROM correspondence_files WHERE correspondence_id = ? "
                                      "ORDER BY id", (corr_id,)).fetchall():
            d = dict(row)
            d["extracted_data"] = self._parse_json(d.get("extracted_data"), {}) or {}
            out.append(d)
        return out

    def get_correspondence_file_data(self, file_id):
        return self._row(self._conn.execute("SELECT id, filename, file_type, mime_type, file_size, file_data "
                                            "FROM correspondence_files WHERE id = ?", (file_id,)).fetchone())

    def delete_correspondence(self, pid, corr_id):
        self._conn.execute("DELETE FROM correspondence_files WHERE correspondence_id = ? AND project_id = ?",
                           (corr_id, pid))
        n = self._conn.execute("DELETE FROM project_correspondence WHERE id = ? AND project_id = ?",
                               (corr_id, pid)).rowcount
        self._conn.commit()
        return n

    def delete_project_correspondence(self, pid):
        self._conn.execute("DELETE FROM correspondence_files WHERE project_id = ?", (pid,))
        self._conn.execute("DELETE FROM project_correspondence WHERE project_id = ?", (pid,))
        self._conn.commit()

    def reassign_correspondence(self, source_pid, target_pid, corr_ids=None):
        if corr_ids:
            for cid in corr_ids:
                self._conn.execute("UPDATE project_correspondence SET project_id = ? WHERE id = ? AND project_id = ?",
                                   (target_pid, cid, source_pid))
                self._conn.execute("UPDATE correspondence_files SET project_id = ? WHERE correspondence_id = ? "
                                   "AND project_id = ?", (target_pid, cid, source_pid))
        else:
            self._conn.execute("UPDATE project_correspondence SET project_id = ? WHERE project_id = ?",
                               (target_pid, source_pid))
            self._conn.execute("UPDATE correspondence_files SET project_id = ? WHERE project_id = ?",
                               (target_pid, source_pid))
        self._conn.commit()

    def get_correspondence_stats(self, pid):
        emails = self._conn.execute("SELECT COUNT(*) FROM project_correspondence WHERE project_id = ?",
                                    (pid,)).fetchone()[0]
        fs = self._conn.execute("SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM correspondence_files "
                                "WHERE project_id = ?", (pid,)).fetchone()
        return {"email_count": emails, "file_count": fs[0], "total_file_size": fs[1]}

    # ══════════════════════════════════════════════════════════════════════
    # INTAKE (db.py:2820-2923, trimmed)
    # ══════════════════════════════════════════════════════════════════════

    def add_intake(self, pid, intake_type="text", **fields):
        cols, vals = ["project_id", "intake_type"], [pid, intake_type if intake_type in ("text", "file") else "text"]
        for k in ("content", "file_path", "file_name", "file_type", "file_size", "source"):
            if k in fields:
                cols.append(k)
                vals.append(fields[k])
        cur = self._conn.execute(
            f"INSERT INTO project_intake ({', '.join(chr(34) + c + chr(34) for c in cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})", vals)
        self._conn.commit()
        return cur.lastrowid

    def get_intake(self, pid):
        out = []
        for r in self._conn.execute("SELECT * FROM project_intake WHERE project_id = ? ORDER BY created_at DESC",
                                    (pid,)).fetchall():
            d = dict(r)
            d["extraction"] = self._parse_json(d.get("extraction"), {}) or {}
            out.append(d)
        return out

    def delete_intake(self, intake_id):
        self._conn.execute("DELETE FROM project_intake WHERE id = ?", (intake_id,))
        self._conn.commit()

    # ══════════════════════════════════════════════════════════════════════
    # EVENTS + HP HISTORY (db.py:2372-2480)
    # ══════════════════════════════════════════════════════════════════════

    def append_event(self, action, payload, machine=None):
        ts = self._now()
        machine = machine or self.machine()
        self._conn.execute("INSERT INTO events (ts, machine, action, payload) VALUES (?, ?, ?, ?)",
                           (ts, machine, action, self._json_col(payload or {})))
        self._conn.commit()
        return {"ts": ts, "machine": machine, "action": action, "payload": payload}

    def replay_events(self, action=None, project_id=None, since=None, limit=None):
        sql, params = "SELECT ts, machine, action, payload FROM events WHERE 1=1", []
        if action:
            if isinstance(action, str):
                sql += " AND action = ?"
                params.append(action)
            else:
                sql += f" AND action IN ({', '.join('?' * len(action))})"
                params.extend(action)
        if project_id:
            sql += " AND json_extract(payload, '$.project_id') = ?"
            params.append(project_id)
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY id ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        out = []
        for r in self._conn.execute(sql, params).fetchall():
            d = dict(r)
            d["payload"] = self._parse_json(d["payload"], {}) or {}
            out.append(d)
        return out

    def get_hp_history(self, days=8):
        out = {}
        for r in self._conn.execute("SELECT * FROM hp_history ORDER BY date DESC LIMIT ?", (days,)).fetchall():
            d = dict(r)
            d["snapshots"] = self._parse_json(d.get("snapshots"), []) or []
            d["damageEvents"] = self._parse_json(d.get("damage_events"), []) or []
            out[d["date"]] = d
        return out

    @_retry_on_locked
    def save_hp_snapshot(self, date_str, start_hp=None, start_time=None, peak_hp=None, daily_low=None,
                         snapshots=None, damage_events=None):
        """Damage events MERGE, never replace (dedup on time + src)."""
        if damage_events is not None:
            row = self._conn.execute("SELECT damage_events FROM hp_history WHERE date = ?", (date_str,)).fetchone()
            stored = self._parse_json(row[0], []) if row else []
            merged, seen = [], set()
            for e in list(stored or []) + list(damage_events):
                if not isinstance(e, dict):
                    continue
                key = (str(e.get("time")), str(e.get("src")))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(e)
            merged.sort(key=lambda e: str(e.get("time") or ""))
            damage_events = merged
        self._conn.execute(
            "INSERT INTO hp_history (date, start_hp, start_time, peak_hp, daily_low, snapshots, damage_events) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(date) DO UPDATE SET "
            "start_hp = COALESCE(excluded.start_hp, hp_history.start_hp), "
            "start_time = COALESCE(excluded.start_time, hp_history.start_time), "
            "peak_hp = COALESCE(excluded.peak_hp, hp_history.peak_hp), "
            "daily_low = COALESCE(excluded.daily_low, hp_history.daily_low), "
            "snapshots = COALESCE(excluded.snapshots, hp_history.snapshots), "
            "damage_events = COALESCE(excluded.damage_events, hp_history.damage_events)",
            (date_str, start_hp, start_time, peak_hp, daily_low, self._json_col(snapshots),
             self._json_col(damage_events)))
        self._conn.commit()

    # ══════════════════════════════════════════════════════════════════════
    # QUESTIONS (server.py:6666-6744)
    # ══════════════════════════════════════════════════════════════════════

    def add_question(self, pid, question, options, recommend="", evidence="", unblocks="", asked_by="assistant"):
        """Idempotent on (project, text) while open. Returns the question id."""
        text = (question or "").strip()
        if not pid or not text:
            raise ValueError("project_id and question are required")
        dupe = self._conn.execute("SELECT id FROM questions WHERE project_id = ? AND question = ? "
                                  "AND status = 'open'", (pid, text)).fetchone()
        if dupe:
            return dupe["id"]
        qid = "q_" + os.urandom(4).hex()
        self._conn.execute(
            "INSERT INTO questions (id, project_id, question, options, recommend, evidence, unblocks, status, "
            "answer, asked_by, created) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', '', ?, ?)",
            (qid, pid, text, json.dumps(options or []), recommend or "", evidence or "", unblocks or "",
             asked_by or "assistant", self.hours.now_local_naive().isoformat(timespec="seconds")))
        self._conn.commit()
        return qid

    def list_questions(self, status="open", pid=None):
        sql, args = "SELECT * FROM questions WHERE 1=1", []
        if status and status != "all":
            sql += " AND status = ?"
            args.append(status)
        if pid:
            sql += " AND project_id = ?"
            args.append(pid)
        sql += " ORDER BY created ASC"
        out = []
        for r in self._conn.execute(sql, args).fetchall():
            d = dict(r)
            d["options"] = self._parse_json(d.get("options"), []) or []
            out.append(d)
        return out

    def open_questions(self, pid=None):
        return self.list_questions("open", pid)

    def answer_question(self, qid, status="answered", answer=""):
        if status not in ("answered", "moot", "open"):
            raise ValueError("status must be answered, moot or open")
        row = self._conn.execute("SELECT id FROM questions WHERE id = ?", (qid,)).fetchone()
        if not row:
            return None
        answered_at = None if status == "open" else self.hours.now_local_naive().isoformat(timespec="seconds")
        self._conn.execute("UPDATE questions SET status = ?, answer = ?, answered_at = ? WHERE id = ?",
                           (status, answer or "", answered_at, qid))
        self._conn.commit()
        return {"question_id": qid, "status": status}

    def delete_question(self, qid):
        self._conn.execute("DELETE FROM questions WHERE id = ?", (qid,))
        self._conn.commit()

    # ══════════════════════════════════════════════════════════════════════
    # DRAFTS (new; nothing sends)
    # ══════════════════════════════════════════════════════════════════════

    def add_draft(self, pid, to, cc, subject, body, reply_to_corr_id=None, created_by="assistant"):
        did = "draft_" + uuid.uuid4().hex[:10]
        now = self._now()
        self._conn.execute(
            'INSERT INTO drafts (id, project_id, "to", cc, subject, body, reply_to_corr_id, status, created_by, '
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)",
            (did, pid or "", to or "", cc or "", subject or "", body or "", reply_to_corr_id,
             created_by or "assistant", now, now))
        self._conn.commit()
        return did

    def list_drafts(self, pid=None, status="draft"):
        sql, args = "SELECT * FROM drafts WHERE 1=1", []
        if status and status != "all":
            sql += " AND status = ?"
            args.append(status)
        if pid:
            sql += " AND project_id = ?"
            args.append(pid)
        sql += " ORDER BY created_at DESC"
        return self._rows(self._conn.execute(sql, args).fetchall())

    def get_draft(self, did):
        return self._row(self._conn.execute("SELECT * FROM drafts WHERE id = ?", (did,)).fetchone())

    def update_draft(self, did, **fields):
        allowed = {"to", "cc", "subject", "body", "status", "project_id", "provider_draft_id", "reply_to_corr_id"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if "status" in fields and fields["status"] not in ("draft", "sent", "discarded"):
            raise ValueError("status must be draft, sent or discarded")
        if not fields:
            return self.get_draft(did)
        fields["updated_at"] = self._now()
        set_clause = ", ".join(f'"{k}" = ?' for k in fields)
        self._conn.execute(f"UPDATE drafts SET {set_clause} WHERE id = ?", list(fields.values()) + [did])
        self._conn.commit()
        return self.get_draft(did)

    def delete_draft(self, did):
        n = self._conn.execute("DELETE FROM drafts WHERE id = ?", (did,)).rowcount
        self._conn.commit()
        return n

    # ══════════════════════════════════════════════════════════════════════
    # AI BOOKKEEPING (new)
    # ══════════════════════════════════════════════════════════════════════

    def record_usage(self, task, provider, model, in_tok, out_tok, cost_usd, latency_ms, ok):
        self._conn.execute(
            "INSERT INTO ai_usage (ts, task, provider, model, in_tok, out_tok, cost_usd, latency_ms, ok) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self._now(), task or "", provider or "", model or "", int(in_tok or 0), int(out_tok or 0),
             float(cost_usd or 0), int(latency_ms or 0), 1 if ok else 0))
        self._conn.commit()

    def usage_today(self):
        day = self._today()
        rows = self._conn.execute("SELECT task, cost_usd, ok FROM ai_usage WHERE substr(ts, 1, 10) = ?",
                                  (day,)).fetchall()
        by_task = {}
        cost = 0.0
        for r in rows:
            cost += float(r["cost_usd"] or 0)
            t = by_task.setdefault(r["task"], {"calls": 0, "cost_usd": 0.0, "failed": 0})
            t["calls"] += 1
            t["cost_usd"] = round(t["cost_usd"] + float(r["cost_usd"] or 0), 6)
            if not r["ok"]:
                t["failed"] += 1
        return {"date": day, "cost_usd": round(cost, 6), "calls": len(rows), "by_task": by_task}

    def log_assistant(self, run_id, pid, item_type, outcome, detail):
        self._conn.execute(
            "INSERT INTO assistant_log (ts, run_id, project_id, item_type, outcome, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (self._now(), run_id or "", pid or "", item_type or "", outcome or "", self._json_col(detail or {})))
        self._conn.commit()

    def assistant_log(self, limit=200, run_id=None):
        sql, args = "SELECT * FROM assistant_log", []
        if run_id:
            sql += " WHERE run_id = ?"
            args.append(run_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        out = []
        for r in self._conn.execute(sql, args).fetchall():
            d = dict(r)
            d["detail"] = self._parse_json(d.get("detail"), {}) or {}
            out.append(d)
        return out

    # ══════════════════════════════════════════════════════════════════════
    # MERGE (db.py:3141-3295)
    # ══════════════════════════════════════════════════════════════════════

    def merge_projects(self, target_id, source_id):
        target, source = self.get_project(target_id), self.get_project(source_id)
        if not target:
            raise ValueError(f"Target project {target_id} not found")
        if not source:
            raise ValueError(f"Source project {source_id} not found")
        if target_id == source_id:
            raise ValueError("Cannot merge a project into itself")
        now, today, stats = self._now(), self._today(), {}
        existing_emails = {c["email"].lower().strip() for c in target.get("contacts", []) if c.get("email")}
        moved = 0
        for c in self._conn.execute("SELECT * FROM contacts WHERE project_id = ?", (source_id,)).fetchall():
            if (c["email"] or "").lower().strip() in existing_emails and c["email"]:
                continue
            self._conn.execute("UPDATE contacts SET project_id = ? WHERE id = ?", (target_id, c["id"]))
            moved += 1
        self._conn.execute("DELETE FROM contacts WHERE project_id = ?", (source_id,))
        stats["contacts"] = moved
        existing_texts = {(s["text"] or "").lower().strip() for s in target.get("subtasks", [])}
        moved = 0
        for s in self._conn.execute("SELECT * FROM subtasks WHERE project_id = ?", (source_id,)).fetchall():
            if (s["text"] or "").lower().strip() in existing_texts:
                self._conn.execute("DELETE FROM subtasks WHERE id = ? AND project_id = ?", (s["id"], source_id))
            else:
                self._conn.execute("UPDATE subtasks SET project_id = ? WHERE id = ? AND project_id = ?",
                                   (target_id, s["id"], source_id))
                moved += 1
        stats["subtasks"] = moved
        for table, key in (("notes", "notes"), ("correspondence_files", "correspondence_files"),
                           ("project_correspondence", "correspondence"), ("conversation_routing", "conversations"),
                           ("project_intake", "intake"), ("questions", "questions"), ("drafts", "drafts")):
            stats[key] = self._conn.execute(f"UPDATE {table} SET project_id = ? WHERE project_id = ?",
                                            (target_id, source_id)).rowcount
        merged_kd = {**(source.get("key_details") or {}), **(target.get("key_details") or {})}
        self._conn.execute("UPDATE projects SET key_details = ? WHERE id = ?", (self._json_col(merged_kd), target_id))
        fin = {}
        for k in ("quote_amount", "approved_amount"):
            if not target.get(k) and source.get(k):
                fin[k] = source[k]
        if fin:
            set_clause = ", ".join(f'"{k}" = ?' for k in fin)
            self._conn.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", list(fin.values()) + [target_id])
        self._insert_note(target_id, f"Merged from {source['name']} ({source_id}). Absorbed {stats['subtasks']} "
                                     f"subtasks, {stats['contacts']} contacts, {stats['notes']} notes, "
                                     f"{stats['correspondence']} emails.", f"{today}, merge")
        self._insert_note(source_id, f"Merged into {target['name']} ({target_id}).", f"{today}, merge")
        doc = self.wf_doc()
        closed = wfm.stages_with_role(doc, "closed", source.get("workflow_id")) or wfm.stages_with_role(doc, "closed")
        self._conn.execute("UPDATE projects SET archived = 1, status = COALESCE(?, status), updated_at = ? WHERE id = ?",
                           (closed[0] if closed else None, now, source_id))
        self._conn.execute("UPDATE projects SET last_activity = ?, updated_at = ? WHERE id = ?",
                           (now, now, target_id))
        self._conn.commit()
        self.append_event("project_merged", {"target_id": target_id, "source_id": source_id, "stats": stats})
        return stats
