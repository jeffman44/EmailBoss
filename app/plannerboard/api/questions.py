"""
/api/questions: the blocking-decision queue. Ported from server.py 6666-6744.
Shapes preserved: GET {questions, count}; POST {question_id, project_id} (201)
or {question_id, duplicate: true}; POST /<id> {question_id, status};
DELETE /<id> {deleted}.
"""

from flask import Blueprint, jsonify, request

from ._common import body, store

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
                          asked_by=data.get("asked_by", "assistant"))
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


@bp.route("/api/questions/<qid>", methods=["DELETE"])
def api_delete_question(qid):
    store().delete_question(qid)
    return jsonify({"deleted": qid})
