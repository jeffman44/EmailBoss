"""
/api/projects: list, create, read, update (with the sanctioned cascades),
delete, merge.

Ported from the reference server.py 1015-1547. Every literal status check is
now a role or a workflow-document lookup:

  * quote-task cascade: checking the quote task on a todo-role project moves it
    to the stage its quote stage advances to (an out-role stage); un-checking
    reverts (1210-1241).
  * money: writing `quote_amount` on a todo-role project flips it to the
    out-role stage when the profession has money.
  * won cascade: `won_date` empty -> set moves the project to
    won_transition.to and arms that rung's task; clearing it reverts to
    won_transition.from and drops the ladder tasks (1243-1312).
  * ladder advance / revert on a rung task check / un-check (1314-1336).
  * explicit status pick re-arms the rung (1338-1387).
  * reactivation on a new subtask lives in Store.add_subtask.

Status never changes automatically except through these cascades, each of
which is triggered by the user's own click.
"""

import csv
import io
import re
import sqlite3
from datetime import date

from flask import Blueprint, jsonify, request

from .. import ladder
from .. import workflows as wfm
from ._common import body, error_response, rt, store

bp = Blueprint("projects", __name__)


@bp.route("/api/projects", methods=["GET"])
def api_get_projects():
    """{meta, projects[], archived[]} in DATABASE order."""
    return jsonify(store().get_projects_for_board())


@bp.route("/api/projects/<proj_id>", methods=["GET"])
def api_get_project(proj_id):
    proj = store().get_project(proj_id)
    if not proj:
        return jsonify({"ok": False, "error": "not_found", "project_id": proj_id}), 404
    return jsonify(proj)


@bp.route("/api/projects", methods=["POST"])
def api_create_project():
    """Create a project. A caller may name a workflow instead of a status; the
    workflow's entry_stage becomes the status. An explicit status wins."""
    db = store()
    data = dict(body())
    doc = db.wf_doc()
    wid = (data.pop("workflow_id", "") or "").strip()
    status = (data.pop("status", "") or "").strip()
    work_type = data.pop("work_type", "") or ""
    if not wid:
        wid = wfm.resolve_workflow_id(doc, work_type, db.work_types())
    if not wfm.get_workflow(doc, wid):
        return error_response(400, f"unknown workflow_id '{wid}'")
    if not status:
        status = wfm.entry_stage(doc, wid)
    name = (data.pop("name", "") or "").strip()
    if not name:
        return error_response(400, "name is required")
    kwargs = {
        "contacts": data.pop("contacts", None), "key_details": data.pop("key_details", None),
        "scope": data.pop("scope", "") or "", "tags": data.pop("tags", None),
        "source": data.pop("source", "") or "manual", "subtasks": data.pop("subtasks", None),
        "notes": data.pop("notes", None), "proj_id": data.pop("proj_id", None) or data.pop("id", None),
    }
    if "site" in data and "location" not in data:
        data["location"] = data.pop("site")
    data.pop("site_slug", None)
    data.pop("internal_handoffs", None)
    if not rt().config.feature("money"):
        for k in ("quote_amount", "approved_amount"):
            data.pop(k, None)
    try:
        pid = db.create_project(name, workflow_id=wid, status=status, work_type=work_type, **kwargs, **data)
    except sqlite3.IntegrityError as ie:
        if "UNIQUE" in str(ie):
            return jsonify({"ok": False, "error": "duplicate",
                            "detail": f"Project {kwargs.get('proj_id')} already exists"}), 409
        raise
    except ValueError as ve:
        return error_response(400, str(ve))
    ladder.ensure_entry_stage_task(db, pid)
    proj = db.get_project(pid)
    db.append_event("project_created", {"project_id": pid, "name": proj.get("name", "")})
    return jsonify({"ok": True, "project": proj}), 201


_CSV_ALIASES = {
    "name": ("name", "project", "title", "job", "matter", "deal", "ticket", "account", "subject"),
    "client": ("client", "customer", "company", "organization", "organisation", "tenant", "insured", "person"),
    "email": ("contactemail", "email", "emailaddress", "contact", "clientemail", "customeremail"),
    "stage": ("stage", "status", "state"),
    "due": ("duedate", "due", "deadline", "date", "followupdate", "followup"),
    "notes": ("notes", "note", "comments", "comment", "description", "details"),
}
MAX_IMPORT_ROWS = 2000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _csv_key(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _canon_row(raw):
    """Map one input row onto name/client/email/stage/due/notes."""
    by_key = {_csv_key(k): (v if v is not None else "") for k, v in (raw or {}).items() if k}
    out = {}
    for field, aliases in _CSV_ALIASES.items():
        for a in aliases:
            if by_key.get(a) not in (None, ""):
                out[field] = str(by_key[a]).strip()
                break
    return out


def _match_stage(doc, text):
    """(workflow_id, stage_id) for a stage typed by a person, else (None, None)."""
    want = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    if not want:
        return None, None
    for wf in doc.get("workflows") or []:
        for st in wf.get("stages") or []:
            if want in (re.sub(r"[^a-z0-9]", "", str(st.get("id", "")).lower()),
                        re.sub(r"[^a-z0-9]", "", str(st.get("label", "")).lower())):
                return wf.get("id"), st.get("id")
    return None, None


@bp.route("/api/projects/import-csv", methods=["POST"])
def api_import_projects_csv():
    """"Add many from a spreadsheet". Accepts multipart (`file` = a .csv) or
    JSON {rows: [{...}]} or {csv: "text"}. Columns (any common spelling):
    name, client, contact email, stage, due date, notes. Existing project with
    the same name and client is skipped. Returns {ok, total, created, skipped,
    errors: [{row, error}], projects: [ids]}. Money is never imported."""
    from ..triage.deadlines import parse_date_text
    db = store()
    rows = None
    if request.files.get("file"):
        raw = request.files["file"].read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        rows = list(csv.DictReader(io.StringIO(text)))
    else:
        data = body()
        if isinstance(data, list):
            rows = data
        elif isinstance(data.get("rows"), list):
            rows = data["rows"]
        elif isinstance(data.get("csv"), str):
            rows = list(csv.DictReader(io.StringIO(data["csv"])))
    if rows is None:
        return error_response(400, "send a CSV file, or JSON with rows")
    if len(rows) > MAX_IMPORT_ROWS:
        return error_response(400, f"too many rows (max {MAX_IMPORT_ROWS})")
    doc = db.wf_doc()
    default_wid = wfm.default_workflow_id(doc)
    existing = {((p.get("name") or "").strip().lower(), (p.get("client") or "").strip().lower())
                for p in db.all_projects(include_archived=True)} if hasattr(db, "all_projects") else set()
    created, skipped, errors, ids = 0, 0, [], []
    today = date.fromisoformat(db.hours.today())
    for i, raw in enumerate(rows, start=2):   # row 1 is the header
        try:
            r = _canon_row(raw if isinstance(raw, dict) else {})
            name = r.get("name") or ""
            if not name:
                errors.append({"row": i, "error": "no name"})
                continue
            client = r.get("client", "")
            if (name.lower(), client.lower()) in existing:
                skipped += 1
                continue
            wid, status = _match_stage(doc, r.get("stage"))
            if not wid:
                wid = default_wid
                status = wfm.entry_stage(doc, wid)
            email = r.get("email", "")
            contacts = [{"name": "", "email": email, "role": ""}] if _EMAIL_RE.match(email) else []
            due = parse_date_text(r.get("due", ""), today) if r.get("due") else ""
            kd = {"client": client} if client else {}
            notes = [{"text": r["notes"], "time": db._note_time("import")}] if r.get("notes") else None
            pid = db.create_project(name, workflow_id=wid, status=status, contacts=contacts or None,
                                    key_details=kd, source="import", notes=notes,
                                    follow_up_date=due or None)
            if due:
                db.add_subtask(pid, "Due", date_type="deadline", follow_up_date=due, source="manual")
            if status == wfm.entry_stage(doc, wid):
                try:
                    ladder.ensure_entry_stage_task(db, pid)
                except Exception:
                    pass
            existing.add((name.lower(), client.lower()))
            created += 1
            ids.append(pid)
        except Exception as e:  # one bad row never sinks the batch
            errors.append({"row": i, "error": f"{type(e).__name__}: {e}"[:200]})
    db.append_event("import_csv", {"created": created, "skipped": skipped, "errors": len(errors)})
    return jsonify({"ok": True, "total": len(rows), "created": created, "skipped": skipped,
                    "errors": errors, "projects": ids}), 200


@bp.route("/api/projects/<proj_id>", methods=["PUT"])
def api_update_project(proj_id):
    """Partial merge of provided fields plus the cascades described above.
    `contacts` and `subtasks` are FULL REPLACE; `notes` inserts new ones."""
    db = store()
    data = dict(body())
    contacts = data.pop("contacts", None)
    subtasks = data.pop("subtasks", None)
    notes = data.pop("notes", None)
    data.pop("internal_handoffs", None)
    if "site" in data and "location" not in data:
        data["location"] = data.pop("site")
    if "sold_date" in data and "won_date" not in data:
        data["won_date"] = data.pop("sold_date")
    data.pop("sold_date", None)
    money = rt().config.feature("money") and db.money_enabled()
    if not money:
        for k in ("quote_amount", "approved_amount", "quote_sent_date"):
            data.pop(k, None)

    try:
        before = db.get_project(proj_id)
        if not before:
            return error_response(404, f"Project {proj_id} not found")
        doc = db.wf_doc()
        wid = ladder.wf_id_for(db, before, doc)
        today = db.hours.today()
        prior_won = bool(before.get("won_date"))
        new_status = data.get("status")
        prior_status = before.get("status")
        role_before = wfm.role_of(doc, prior_status, wid)
        qstage, _qtask = wfm.quote_task_for(doc, wid)
        q_next = (wfm.get_stage(wfm.get_workflow(doc, wid), qstage) or {}).get("advance_on_task_complete") \
            if qstage else None

        # Auto-stamp dates on explicit status transitions (1069-1081), by role.
        if new_status and new_status != prior_status:
            if new_status not in wfm.all_stage_ids(doc):
                return error_response(400, f"unknown status '{new_status}'")
            new_role = wfm.role_of(doc, new_status, wid)
            if new_role == "out" and money and not before.get("quote_sent_date") and "quote_sent_date" not in data:
                data["quote_sent_date"] = today
            if new_role in ("closed", "won") and not before.get("closed_date") and "closed_date" not in data \
                    and new_role == "closed":
                data["closed_date"] = today
            if new_role == "won" and not before.get("won_date") and "won_date" not in data:
                data["won_date"] = today

        # Money cascade: quote_amount written on a todo-role project -> out stage.
        if (money and data.get("quote_amount") and not new_status and role_before == "todo"
                and not before.get("quote_amount")):
            outs = wfm.stages_with_role(doc, "out", wid)
            target = q_next if (q_next and wfm.role_of(doc, q_next, wid) == "out") else (outs[0] if outs else None)
            if target:
                data["status"] = target
                new_status = target
                if not before.get("quote_sent_date"):
                    data.setdefault("quote_sent_date", today)
                db.append_note(proj_id, f"Quote amount recorded -> status auto-set to {target}", f"{today}, auto")

        if data:
            db.update_project(proj_id, **data)

        if contacts is not None:
            db.replace_contacts(proj_id, contacts if isinstance(contacts, list) else [])

        # Subtask reconciliation (board sends the full array) (1119-1208).
        quote_completed = quote_reopened = False
        ladder_completed = ladder_reopened = None
        if subtasks is not None:
            if not isinstance(subtasks, list):
                subtasks = []
            existing_rows = {s["id"]: s for s in db.get_subtasks(proj_id)}
            incoming_ids = {st.get("id", "") for st in subtasks if isinstance(st, dict)}
            for rm_id in set(existing_rows) - incoming_ids:
                db.delete_subtask(proj_id, rm_id)
            for st in subtasks:
                if not isinstance(st, dict):
                    continue
                st_id = st.get("id", "")
                if st_id in existing_rows:
                    prior_row = existing_rows[st_id]
                    if st.get("status") == "complete":
                        done = db.complete_subtask(proj_id, st_id, completed_by=st.get("completed_by") or "board_sync")
                        if done and prior_row.get("status") == "pending":
                            if ladder.is_quote_task(db, prior_row, before, doc):
                                quote_completed = True
                            rung = ladder.rung_for_text(db, prior_row.get("text"), before, doc)
                            if rung:
                                ladder_completed = rung
                    elif st.get("status") == "pending":
                        if prior_row.get("status") == "complete":
                            if ladder.is_quote_task(db, prior_row, before, doc):
                                quote_reopened = True
                            rung = ladder.rung_for_text(db, prior_row.get("text"), before, doc)
                            if rung:
                                ladder_reopened = rung
                        db.reopen_subtask(proj_id, st_id)
                    fields = {}
                    if "follow_up_date" in st:
                        fields["follow_up_date"] = st.get("follow_up_date") or None
                        fields["date_type"] = st.get("date_type") or prior_row.get("date_type") or "follow_up"
                    if "sla_type" in st:
                        fields["sla_type"] = st.get("sla_type") or None
                        fields["sla_created"] = st.get("sla_created") or None
                        fields["sla_hours"] = st.get("sla_hours")
                    if "text" in st and st.get("text") and st["text"] != prior_row.get("text"):
                        fields["text"] = st["text"]
                    if fields:
                        db.update_subtask_fields(proj_id, st_id, **fields)
                else:
                    db.add_subtask(proj_id, st.get("text", ""), date_type=st.get("date_type") or "follow_up",
                                   follow_up_date=st.get("follow_up_date"), sla_hours=st.get("sla_hours"),
                                   source=st.get("source", "manual"), id=st_id or None,
                                   created=st.get("created"), sla_type=st.get("sla_type"),
                                   sla_created=st.get("sla_created"), category=st.get("category"))

        # Quote-task cascade (1210-1241): the check IS the send signal.
        if (quote_completed and not new_status and not data.get("won_date") and qstage and q_next
                and prior_status == qstage):
            upd = {"status": q_next}
            if money and not before.get("quote_sent_date"):
                upd["quote_sent_date"] = today
            db.update_project(proj_id, **upd)
            db.append_note(proj_id, f"Quote task checked -> status auto-set to {q_next}", f"{today}, auto")
        if (quote_reopened and not quote_completed and not new_status and not prior_won
                and not data.get("won_date") and qstage and q_next and prior_status == q_next):
            db.update_project(proj_id, status=qstage)
            db.append_note(proj_id, f"Quote task reopened -> status reverted to {qstage}", f"{today}, auto")
            ladder.ensure_quote_task(db, proj_id)

        # Won cascade (1250-1289): won_date empty -> set.
        if (not prior_won) and bool(data.get("won_date")):
            eff_status = new_status or prior_status
            won_to = wfm.won_target(doc, wid)
            eff_role = wfm.role_of(doc, eff_status, wid)
            if (not new_status and won_to and eff_role not in ("closed", "in_progress", "won")
                    and eff_status not in ladder.ladder_statuses(db, before, doc)):
                db.update_project(proj_id, status=won_to)
                db.append_note(proj_id, f"Won -> status auto-set to {won_to} (was {eff_status})", f"{today}, auto")
                ladder.ensure_ladder_task(db, proj_id, won_to)

        # Un-won revert (1291-1312).
        if prior_won and ("won_date" in data) and not data.get("won_date"):
            revert_to = wfm.won_revert_target(doc, wid)
            on_ladder = prior_status in ladder.ladder_statuses(db, before, doc) + \
                wfm.stages_with_role(doc, "won", wid) + wfm.stages_with_role(doc, "waiting", wid)
            if not new_status and revert_to and on_ladder:
                db.update_project(proj_id, status=revert_to)
                for s in db.get_subtasks(proj_id, status="pending"):
                    if ladder.rung_for_text(db, s.get("text"), before, doc):
                        db.delete_subtask(proj_id, s["id"])
                db.append_note(proj_id, f"Won cleared -> status reverted to {revert_to}, ladder tasks removed",
                               f"{today}, auto")

        # Ladder advance / revert (1314-1336).
        if ladder_completed and not new_status and not data.get("won_date"):
            if prior_status == ladder_completed:
                ladder.advance_job_ladder(db, proj_id, ladder_completed)
        if ladder_reopened and not ladder_completed and not new_status:
            row = ladder.ladder_by_status(db, before, doc).get(ladder_reopened)
            if row and prior_status == row[2]:
                ladder.revert_job_ladder(db, proj_id, ladder_reopened)

        # Explicit status pick re-arms the rung / quote task (1338-1387).
        if new_status and new_status != prior_status:
            ladder.rearm_on_status_change(db, proj_id, before, new_status)

        # Notes: insert new ones; never iterate a bare string (1389-1413).
        if notes is not None:
            if isinstance(notes, (str, dict)):
                notes = [notes]
            if isinstance(notes, list):
                existing_texts = {r["text"] for r in db.conn.execute(
                    "SELECT text FROM notes WHERE project_id = ?", (proj_id,)).fetchall()}
                for n in notes:
                    if isinstance(n, dict):
                        n_text, n_time = (n.get("text") or "").strip(), n.get("time", "")
                    elif isinstance(n, str):
                        n_text, n_time = n.strip(), ""
                    else:
                        continue
                    if n_text and n_text not in existing_texts:
                        db.append_note(proj_id, n_text, time_str=n_time or None)

        proj = db.get_project(proj_id)
        if not proj:
            return error_response(404, f"Project {proj_id} not found")
        reminders = _sync_date_field_reminders(db, before, proj)
        auto = []
        if data.get("status") and data["status"] != prior_status and not status_in_request(request):
            st_ = wfm.get_stage(wfm.get_workflow(doc, wid), data["status"]) or {}
            auto.append(f"status set to {st_.get('label') or data['status']}")
        return jsonify({"ok": True, "project": proj, "reminders": reminders, "auto": auto})
    except ValueError as ve:
        return error_response(400, str(ve))
    except Exception as e:  # pragma: no cover - defensive
        import traceback
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()}), 500


def status_in_request(req) -> bool:
    try:
        return "status" in (req.get_json(silent=True) or {})
    except Exception:
        return False


# profession field keys of type date that carry a reminder; the subtask text
# is the field label so the card reads "Court date" / "Renewal date".
def _reminder_fields(db) -> list:
    out = []
    try:
        prof = rt().profession() or {}
    except Exception:
        return out
    for f in prof.get("fields") or []:
        if f.get("type") == "date" and (f.get("reminder") or f.get("key") in
                                        ("hearing_date", "court_date", "renewal_date", "bid_due", "closing_date", "showing_date")):
            out.append((f.get("key"), f.get("label") or f.get("key"), int(f.get("reminder_days_before") or 0)))
    return out


def _sync_date_field_reminders(db, before, after) -> list:
    """A dated field on the project (court date, renewal date, bid due) keeps
    ONE deadline subtask in step with it: created when set, moved when
    changed, completed when cleared. Returns the changes made."""
    changes = []
    kd_b = (before or {}).get("key_details") or {}
    kd_a = (after or {}).get("key_details") or {}
    for key, label, days_before in _reminder_fields(db):
        old, new = (kd_b.get(key) or "").strip(), (kd_a.get(key) or "").strip()
        if old == new:
            continue
        pid = after["id"]
        subs = db.get_subtasks(pid)
        mine = [s for s in subs if ((s.get("metadata") or {}).get("field") == key) if isinstance(s.get("metadata"), dict)] \
            or [s for s in subs if (s.get("text") or "").strip().lower() == label.lower()]
        if not new:
            for s_ in mine:
                if s_.get("status") == "pending":
                    db.complete_subtask(pid, s_["id"], completed_by="field_cleared")
                    changes.append({"field": key, "action": "cleared"})
            continue
        due = new
        if days_before:
            try:
                from datetime import date, timedelta
                due = (date.fromisoformat(new[:10]) - timedelta(days=days_before)).isoformat()
            except Exception:
                due = new
        if mine:
            db.update_subtask_fields(pid, mine[0]["id"], follow_up_date=due, date_type="deadline", status="pending",
                                     completed_date=None, completed_by=None)
            changes.append({"field": key, "action": "moved", "due": due})
        else:
            db.add_subtask(pid, label, date_type="deadline", follow_up_date=due, source="system",
                           metadata={"auto": "date_field", "field": key})
            changes.append({"field": key, "action": "created", "due": due})
    return changes


@bp.route("/api/projects/<proj_id>", methods=["DELETE"])
def api_delete_project(proj_id):
    db = store()
    proj = db.get_project(proj_id)
    if not proj:
        return error_response(404, f"Project {proj_id} not found")
    name = proj.get("name", proj_id)
    try:
        import shutil
        pf = rt().paths.project_files / proj_id
        if pf.is_dir():
            shutil.rmtree(pf, ignore_errors=True)
        db.delete_project(proj_id)
        return jsonify({"ok": True, "deleted": proj_id, "name": name})
    except Exception as e:
        try:
            db.conn.rollback()
        except Exception:
            pass
        return error_response(500, str(e))


@bp.route("/api/projects/<proj_id>/merge", methods=["POST"])
def api_merge_project(proj_id):
    """Merge another project INTO this one. Body: {"source_id": "proj_XXX"}."""
    db = store()
    source_id = (body().get("source_id") or "").strip()
    if not source_id:
        return error_response(400, "source_id is required")
    source = db.get_project(source_id)
    if not source:
        return error_response(404, f"Source project {source_id} not found")
    was_archived = bool(source.get("archived"))
    try:
        if was_archived:
            db.conn.execute("UPDATE projects SET archived = 0 WHERE id = ?", (source_id,))
            db.conn.commit()
        stats = db.merge_projects(target_id=proj_id, source_id=source_id)
        stats["disk_files_moved"] = _move_project_files(source_id, proj_id)
        if was_archived:
            stats["source_was_archived"] = True
    except ValueError as e:
        return error_response(400, str(e))
    except Exception as e:
        return error_response(500, f"Merge failed: {e}")
    return jsonify({"ok": True, "project": db.get_project(proj_id), "stats": stats})


def _move_project_files(source_id, target_id):
    """Move project_files/<source>/{attachments,emails}/* under the target."""
    import shutil
    root = rt().paths.project_files
    src = root / source_id
    if not src.is_dir():
        return 0
    moved = 0
    for sub in ("attachments", "emails"):
        s = src / sub
        if not s.is_dir():
            continue
        t = root / target_id / sub
        t.mkdir(parents=True, exist_ok=True)
        for f in s.iterdir():
            if not f.is_file():
                continue
            dest = t / f.name
            n = 1
            while dest.exists():
                dest = t / f"{f.stem}_{n}{f.suffix}"
                n += 1
            shutil.move(str(f), str(dest))
            moved += 1
    shutil.rmtree(src, ignore_errors=True)
    return moved
