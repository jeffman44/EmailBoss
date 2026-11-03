"""
/api/questions: the blocking-decision queue. Ported from server.py 6666-6744.
Shapes preserved: GET {questions, count}; POST {question_id, project_id} (201)
or {question_id, duplicate: true}; POST /<id> {question_id, status};
DELETE /<id> {deleted}.
"""

import threading

from flask import Blueprint, jsonify, request

from ._common import body, error_response, store, view

bp = Blueprint("questions", __name__)


@bp.route("/api/questions", methods=["GET"])
def api_list_questions():
    rows = store().list_questions(status=request.args.get("status", "open"),
                                  pid=request.args.get("project_id") or None)
    return jsonify({"questions": rows, "count": len(rows)})


@bp.route("/api/questions", methods=["POST"])
def api_create_question():
    db = store()
    data = body()
    pid = (data.get("project_id") or "").strip()
    text = (data.get("question") or "").strip()
    if not pid or not text:
        return jsonify({"error": "project_id and question are required"}), 400
    if not db.project_exists(pid):
        return jsonify({"error": f"unknown project {pid}"}), 404
    before = {q["id"] for q in db.open_questions(pid)}
    qid = db.add_question(pid, text, data.get("options") or [], recommend=data.get("recommend", ""),
                          evidence=data.get("evidence", ""), unblocks=data.get("unblocks", ""),
                          asked_by=data.get("asked_by", "assistant"),
                          inbound_id=data.get("inbound_id", "") or "", sender=data.get("sender", "") or "")
    if qid in before:
        return jsonify({"question_id": qid, "duplicate": True})
    return jsonify({"question_id": qid, "project_id": pid}), 201


@bp.route("/api/questions/<qid>", methods=["POST"])
def api_answer_question(qid):
    data = body()
    try:
        out = store().answer_question(qid, status=data.get("status", "answered"), answer=data.get("answer", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if out is None:
        return jsonify({"error": "question not found"}), 404
    return jsonify(out)


_ANSWER_LOCK = threading.Lock()


@bp.route("/api/questions/<qid>/answer", methods=["POST"])
def api_answer_and_draft(qid):
    """Body: {answer, option_id?}. Records the answer (status never changes),
    then, when AI is available, runs that ONE project's respond step now with
    the answer as the direction note. Returns {ok, question_id, status,
    drafted, draft_id, ai}. draft_id is "local:<id>" (board Drafts) or
    "provider:<ref>" (the mail provider's Drafts folder). Nothing is sent."""
    db = store()
    data = body()
    q = db.get_question(qid)
    if not q:
        return error_response(404, "question not found")
    answer = (data.get("answer") or "").strip()
    if not answer and data.get("option_id"):
        answer = next((str(o.get("label") or "") for o in q.get("options") or []
                       if isinstance(o, dict) and o.get("id") == data["option_id"]), "")
    if not answer:
        return error_response(400, "answer is required")
    out = db.answer_question(qid, status="answered", answer=answer)
    res = {"ok": True, "question_id": qid, "status": out["status"], "project_id": out["project_id"],
           "drafted": False, "draft_id": None, "ai": False}
    v = view()
    llm = v.get("llm")
    if llm is None or not getattr(llm, "available", False):
        return jsonify(res)   # the next assistant pass picks the answer up
    res["ai"] = True
    from ..ai.assistant import AssistantPass
    lock = v.get("assistant_lock")
    if not _ANSWER_LOCK.acquire(blocking=False):
        res["busy"] = True
        return jsonify(res)
    try:
        if lock is not None and not lock.acquire(blocking=False):
            res["busy"] = True
            return jsonify(res)
        try:
            ap = AssistantPass(db, v.get("cfg") or {}, v.get("profession") or {}, llm, v.get("mail"))
            r = ap.respond_for_project(out["project_id"])
        finally:
            if lock is not None:
                lock.release()
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:160]
        return jsonify(res)
    finally:
        _ANSWER_LOCK.release()
    res["drafted"] = bool(r.get("drafted"))
    res["draft_id"] = r.get("draft_id")
    if r.get("error"):
        res["error"] = r["error"]
    return jsonify(res)


@bp.route("/api/questions/<qid>", methods=["DELETE"])
def api_delete_question(qid):
    store().delete_question(qid)
    return jsonify({"deleted": qid})
