"""Local mail search and store housekeeping (2.0 plan section 3.4, Phase 1).

GET  /api/mail/search?q=&limit=&account=      local store search, grouped, with coverage
GET  /api/mail/search/server?q=&folder=&limit=  the fallback row: the provider's own search
GET  /api/mail/store/usage                    bytes on disk per place, per-account counts
POST /api/mail/store/reindex                  rebuild the search indexes
POST /api/mail/store/purge                    apply the body retention window now

Every route sits behind the same API gate as the rest of /api.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..mail import index as mail_index
from ..mail import lifecycle
from ..mail import runner as mail_runner
from ..mail.store import open_error, open_store
from ._common import cfg_dict, error_response, int_arg, rt, store

bp = Blueprint("search_api", __name__)


def _cache_dir():
    return mail_runner.mail_cache_dir(cfg_dict())


def _mail_store():
    return open_store(_cache_dir())


def _unavailable():
    why = open_error(_cache_dir())
    return error_response(503, why or "Mail search is not ready: no mail folder is set up yet.")


def _backups_dir():
    ext = rt()
    paths = getattr(ext, "paths", None)
    if paths is not None and getattr(paths, "backups", None):
        return paths.backups
    p = (cfg_dict().get("paths") or {}).get("backups")
    return p or None


def _stage_ids(kind: str, value: str) -> set[str]:
    """Stage ids whose id, label (or, for stage:, role) match ``value``."""
    v = value.strip().lower()
    out = {v}
    ext = rt()
    wf = getattr(ext, "workflows", None)
    try:
        doc = wf.doc() if callable(getattr(wf, "doc", None)) else (getattr(wf, "data", None) or {})
    except Exception:
        doc = {}
    for w in (doc or {}).get("workflows") or []:
        for st in w.get("stages") or []:
            sid = str(st.get("id") or "").lower()
            label = str(st.get("label") or "").lower()
            role = str(st.get("role") or "").lower()
            if v in (sid, label) or (kind == "stage" and (v == role or (len(v) >= 3 and v in label))):
                out.add(sid)
    return out


def project_resolver(op: str, value: str):
    """Message-ID headers and conversation ids filed to the matching projects.
    None when no project matches (the search answers with a hint)."""
    db = store()
    conn = getattr(db, "conn", None)
    if conn is None:
        return None
    v = value.strip()
    if op == "project":
        rows = conn.execute("SELECT id FROM projects WHERE lower(id) = lower(?) OR name LIKE ?",
                            (v, f"%{v}%")).fetchall()
    else:
        ids = sorted(_stage_ids(op, v))
        marks = ",".join("?" * len(ids))
        rows = conn.execute(f"SELECT id FROM projects WHERE lower(status) IN ({marks})", ids).fetchall()
    pids = [r[0] for r in rows]
    if not pids:
        return None
    marks = ",".join("?" * len(pids))
    found = set()
    for r in conn.execute(f"SELECT message_id, conversation_id FROM project_correspondence"
                          f" WHERE project_id IN ({marks})", pids).fetchall():
        for x in (r[0], r[1]):
            if x:
                found.add(str(x))
    for r in conn.execute(f"SELECT conversation_id FROM conversation_routing WHERE project_id IN ({marks})",
                          pids).fetchall():
        if r[0]:
            found.add(str(r[0]))
    return found


@bp.route("/api/mail/search", methods=["GET"])
def api_search():
    raw = request.args.get("q") or ""
    q = raw.strip()
    st = _mail_store()
    if st is None:
        return _unavailable()
    limit = max(1, min(int_arg("limit", 50), 200))
    # A trailing space means the last word is finished: no prefix expansion.
    res = mail_index.search(st, q, limit=limit, account=((request.args.get("account") or "").lower() or None),
                            project_resolver=project_resolver, typing=(raw == raw.rstrip()))
    res["ok"] = True
    return jsonify(res)


@bp.route("/api/mail/search/server", methods=["GET"])
def api_search_server():
    """The always-present last row: ask the mail server directly."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return error_response(400, "Type something to search for.")
    from .mail_client_api import _provider
    try:
        prov = _provider()
    except LookupError as e:
        return error_response(409, str(e))
    if not prov.capabilities().get("search"):
        return error_response(501, f"The {prov.id} mail server cannot be searched from Email Boss.")
    folder = request.args.get("folder") or ("__all__" if prov.id == "gmail" else "inbox")
    try:
        page = prov.list_messages(folder, limit=max(1, min(int_arg("limit", 50), 100)), query=q)
    except Exception as e:  # noqa: BLE001
        return error_response(502, f"The mail server did not answer the search ({type(e).__name__}).")
    page.update(ok=True, provider=prov.id, source="server")
    return jsonify(page)


@bp.route("/api/mail/store/usage", methods=["GET"])
def api_store_usage():
    st = _mail_store()
    if st is None:
        return _unavailable()
    out = lifecycle.usage(st, _cache_dir(), _backups_dir())
    out["coverage"] = mail_index.coverage_for(st)
    out["ok"] = True
    return jsonify(out)


@bp.route("/api/mail/store/reindex", methods=["POST"])
def api_store_reindex():
    st = _mail_store()
    if st is None:
        return _unavailable()
    n = st.reindex()
    return jsonify({"ok": True, "messages": n})


@bp.route("/api/mail/store/purge", methods=["POST"])
def api_store_purge():
    st = _mail_store()
    if st is None:
        return _unavailable()
    out = lifecycle.purge(st, cfg_dict(), _cache_dir(), _backups_dir())
    out["ok"] = True
    return jsonify(out)
