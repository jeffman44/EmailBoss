"""Out of office (non-deadline work does not age while you are out).

GET    /api/ooo            ranges and the days they cover
POST   /api/ooo            {start, end (both inclusive, YYYY-MM-DD), note}
DELETE /api/ooo/<start>    remove the range that starts that day
"""
from __future__ import annotations

from flask import Blueprint, jsonify

from .. import ooo
from ._common import body, error_response, rt

bp = Blueprint("ooo_api", __name__)


def _out(rs):
    ds = ooo.apply(rt())
    return jsonify({"ok": True, "ranges": rs, "days": ds})


@bp.route("/api/ooo", methods=["GET"])
def api_ooo():
    return _out(ooo.ranges(rt().store))


@bp.route("/api/ooo", methods=["POST"])
def api_ooo_set():
    b = body({}) or {}
    try:
        rs = ooo.set_range(rt().store, b.get("start") or "", b.get("end") or "", b.get("note") or "")
    except ValueError as e:
        return error_response(400, str(e) if "date" in str(e).lower() or "year" in str(e).lower()
                              else "Dates are YYYY-MM-DD.")
    return _out(rs)


@bp.route("/api/ooo/<start>", methods=["DELETE"])
def api_ooo_clear(start):
    return _out(ooo.clear_range(rt().store, start))
