"""The MAIL view's store-backed reads, its context pane and undo (2.0 Phase 2).

GET  /api/mail/store/list?folder_id=&folder_kind=&offset=&limit=   rows from the store
GET  /api/mail/store/message?ref=                                  reading pane (store, provider fallback)
GET  /api/mail/store/context?ref=                                  project, people, money, thread
POST /api/mail/undo/<action_id>                                    reverse one recorded list action

Writes stay on /api/mail/client/* (flags, move, delete, send); those routes
now update the store and return an ``action_id`` for undo.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..mail import terminal
from ..mail.base import message_dict
from ..mail.store import open_store
from ._common import error_response, int_arg, store as board_store
from .mail_client_api import _fail, _guard, _mail_store_for

bp = Blueprint("mail_terminal_api", __name__)


@bp.route("/api/mail/store/list", methods=["GET"])
def api_store_list():
    prov, err = _guard("list")
    if err:
        return err
    st = _mail_store_for(prov)
    if st is None:
        return jsonify({"ok": True, "covered": False})
    folder = {"id": request.args.get("folder_id") or "", "kind": request.args.get("folder_kind") or "other",
              "provider": prov.id, "path": request.args.get("folder_id") if prov.id == "imap" else ""}
    page = terminal.list_rows(st, getattr(prov, "account_id", prov.id), folder,
                              offset=int_arg("offset", 0), limit=int_arg("limit", 200))
    if page is None:
        return jsonify({"ok": True, "covered": False})
    page.update(ok=True, covered=True, provider=prov.id)
    return jsonify(page)


@bp.route("/api/mail/store/message", methods=["GET"])
def api_store_message():
    prov, err = _guard("read")
    if err:
        return err
    ref = request.args.get("ref") or ""
    if not ref:
        return error_response(400, "Pick a message first.")
    st = _mail_store_for(prov)
    d = terminal.message(st, getattr(prov, "account_id", prov.id), ref) if st is not None else None
    if d is None:
        # Outside the body window (or not stored yet): the provider has it.
        try:
            msg = prov.get_message(ref)
        except Exception as e:  # noqa: BLE001
            return _fail(e)
        if msg is None:
            return error_response(404, "That message is no longer in the mailbox. Reload the list.")
        d = message_dict(msg, flagged=bool(getattr(msg, "flagged", False)))
        d["source"] = "server"
    from urllib.parse import quote
    for a in d.get("attachments") or []:
        a["url"] = f"/api/mail/client/attachment?ref={quote(ref, safe='')}&index={a['index']}"
    d.update(ok=True, provider=prov.id)
    return jsonify(d)


@bp.route("/api/mail/store/context", methods=["GET"])
def api_store_context():
    prov, err = _guard()
    if err:
        return err
    st = _mail_store_for(prov)
    if st is None:
        return jsonify({"ok": True, "thread": [], "people": [], "project": None})
    out = terminal.context(st, getattr(prov, "account_id", prov.id), request.args.get("ref") or "",
                           board=board_store())
    out["ok"] = True
    return jsonify(out)


@bp.route("/api/mail/undo/<action_id>", methods=["POST"])
def api_undo(action_id):
    prov, err = _guard("move")
    if err:
        prov, err = _guard("flags")
        if err:
            return err
    st = _mail_store_for(prov)
    if st is None:
        return error_response(409, "Undo needs the mail store on this computer.")
    try:
        return jsonify(terminal.undo(st, prov, getattr(prov, "account_id", prov.id), action_id, board=board_store()))
    except LookupError as e:
        return error_response(404, str(e))
    except Exception as e:  # noqa: BLE001
        return _fail(e)
