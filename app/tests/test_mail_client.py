"""The mail client layer: compose helpers, SMTP submission, the IMAP / Gmail /
Graph client methods against fakes (no network), and the /api/mail/client
blueprint including the send gate and the board bridges."""
import base64
import email
import json
import re
from email import policy

import pytest

from plannerboard.mail import compose, secrets, smtp as smtpmod
from plannerboard.mail.base import guess_folder_kind, message_dict, summary_dict
from plannerboard.mail.gmail import GmailProvider
from plannerboard.mail.graph import GraphProvider
from plannerboard.mail.imap import ImapProvider, parse_list_line
from plannerboard.mail.message import Address, Attachment, Message
from tests.test_mail_providers import FakeHttp, _b64url


@pytest.fixture(autouse=True)
def _mem_secrets():
    secrets.use_memory_backend(True)
    yield
    secrets.use_memory_backend(False)


def _msg(**kw):
    from datetime import datetime, timezone
    base = dict(id="m1", received_at=datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc),
                subject="Re: Lobby project", body="<p>Hi there</p>", is_html=True,
                sender=Address("dana@northwind.example", "Dana"),
                to=[Address("sam@acme.example", "Sam"), Address("ops@northwind.example", "Ops")],
                cc=[Address("cc@northwind.example")], internet_message_id="<orig@mail.example>",
                headers=[{"name": "References", "value": "<root@mail.example>"}],
                conversation_id="CONV1", provider="imap", provider_ref="7:42:INBOX")
    base.update(kw)
    return Message(**base)


# ── compose ─────────────────────────────────────────────────────────────────

def test_compose_subjects_and_recipients():
    assert compose.reply_subject("Lobby") == "Re: Lobby"
    assert compose.reply_subject("RE: Lobby") == "Re: Lobby"
    assert compose.reply_subject("Fwd: Lobby") == "Re: Lobby"
    assert compose.forward_subject("Re: Lobby") == "Fw: Re: Lobby"
    m = _msg()
    to, cc = compose.reply_recipients(m, "sam@acme.example", reply_all=False)
    assert [a.email for a in to] == ["dana@northwind.example"] and cc == []
    to, cc = compose.reply_recipients(m, "sam@acme.example", reply_all=True)
    assert [a.email for a in cc] == ["ops@northwind.example", "cc@northwind.example"]
    m2 = _msg(reply_to=[Address("replies@northwind.example")])
    to, _ = compose.reply_recipients(m2, "sam@acme.example", reply_all=False)
    assert to[0].email == "replies@northwind.example"


def test_build_message_threads_and_parts():
    att = Attachment(name="q.pdf", content_type="application/pdf", content=b"%PDF")
    inline = Attachment(name="pic.png", content_type="image/png", content=b"PNG", is_inline=True, content_id="pic1")
    em = compose.build_message(Address("sam@acme.example", "Sam"), "dana@northwind.example", "ops@northwind.example",
                               "secret@acme.example", "Re: Lobby", "<p>Reply <img src='cid:pic1'></p>",
                               [att, inline], reply_to=_msg())
    assert em["In-Reply-To"] == "<orig@mail.example>"
    assert em["References"] == "<root@mail.example> <orig@mail.example>"
    assert em["From"] == "Sam <sam@acme.example>" and em["Message-ID"].endswith("@acme.example>")
    rcpts = compose.envelope_recipients(em)
    assert set(rcpts) == {"dana@northwind.example", "ops@northwind.example", "secret@acme.example"}
    raw = compose.strip_bcc(em)
    assert b"secret@acme.example" not in raw.split(b"\r\n\r\n")[0] and "Bcc" not in em
    parsed = email.message_from_bytes(raw, policy=policy.default)
    types = [p.get_content_type() for p in parsed.walk()]
    assert "text/plain" in types and "text/html" in types and "application/pdf" in types and "image/png" in types
    assert parsed.get_body(("plain",)).get_content().strip() == "Reply"
    assert "<b>From:</b>" in compose.quote_reply(_msg()) and "Forwarded message" in compose.quote_forward(_msg())


# ── SMTP ────────────────────────────────────────────────────────────────────

class FakeSMTP:
    def __init__(self):
        self.log = []
        self.refuse = {}
        self.auth_code = 235

    def ehlo(self):
        self.log.append("ehlo")

    def starttls(self, context=None):
        self.log.append("starttls")

    def login(self, user, pw):
        self.log.append(("login", user, pw))

    def docmd(self, cmd, arg=""):
        self.log.append((cmd, arg[:12]))
        return self.auth_code, b"ok"

    def sendmail(self, frm, rcpts, raw):
        self.log.append(("sendmail", frm, list(rcpts), len(raw)))
        return self.refuse

    def quit(self):
        self.log.append("quit")


def test_smtp_settings_presets_and_send():
    st = smtpmod.smtp_settings({"imap": {"preset": "gmail"}}, {"preset": "gmail", "user": "sam@gmail.com", "host": "imap.gmail.com"})
    assert st["host"] == "smtp.gmail.com" and st["port"] == 587 and st["starttls"] and st["user"] == "sam@gmail.com"
    st2 = smtpmod.smtp_settings({"smtp": {"port": 465, "ssl": True}}, {"preset": "custom", "host": "imap.example.com", "user": "u"})
    assert st2["host"] == "smtp.example.com" and st2["ssl"] and st2["port"] == 465
    fake = FakeSMTP()
    res = smtpmod.send(st, "sam@gmail.com", ["a@b.c", "d@e.f"], b"raw", password="pw",
                       smtp_factory=lambda *a: fake)
    assert res["accepted"] == ["a@b.c", "d@e.f"] and fake.log[1] == "starttls" and ("login", "sam@gmail.com", "pw") in fake.log
    fake2 = FakeSMTP()
    st3 = dict(st, auth_mode="xoauth2")
    smtpmod.send(st3, "sam@gmail.com", ["a@b.c"], b"raw", oauth_token="TOK", smtp_factory=lambda *a: fake2)
    assert ("AUTH", "XOAUTH2 dXNl") in fake2.log
    with pytest.raises(ValueError):
        smtpmod.send(dict(st, host=""), "x@y.z", ["a@b.c"], b"raw", password="pw", smtp_factory=lambda *a: fake)


# ── IMAP client against a fake imaplib connection ───────────────────────────

RAW1 = (b"From: Dana <dana@northwind.example>\r\nTo: sam@acme.example\r\nSubject: Lobby project\r\n"
        b"Date: Wed, 01 Oct 2026 10:00:00 -0600\r\nMessage-ID: <i1@mail.example>\r\n"
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\nhello\r\n"
        b"--B\r\nContent-Type: application/pdf; name=q.pdf\r\nContent-Disposition: attachment; filename=q.pdf\r\n\r\n%PDF\r\n--B--\r\n")
RAW2 = (b"From: ops@northwind.example\r\nTo: sam@acme.example\r\nSubject: Invoice\r\n"
        b"Date: Thu, 02 Oct 2026 10:00:00 -0600\r\nMessage-ID: <i2@mail.example>\r\n"
        b"Content-Type: text/plain\r\n\r\nplain body\r\n")


class FakeIMAP:
    """Just enough of imaplib.IMAP4_SSL for the client path."""
    capabilities = ("IMAP4REV1", "MOVE", "UIDPLUS")

    def __init__(self):
        self.folders = {
            "INBOX": {"uv": 7, "msgs": {42: [RAW1, ["\\Seen"]], 43: [RAW2, []]}},
            "Sent": {"uv": 8, "msgs": {}},
            "Drafts": {"uv": 9, "msgs": {}},
            "Trash": {"uv": 10, "msgs": {}},
        }
        self.selected = None
        self.readonly = True
        self.log = []
        self._resp = {}
        self.next_uid = 100

    # plumbing
    def noop(self):
        return "OK", [b"NOOP"]

    def close(self):
        pass

    def logout(self):
        self.log.append("logout")

    def response(self, code):
        return code, self._resp.get(code, [None])

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" INBOX', b'(\\HasNoChildren \\Sent) "/" Sent',
                      b'(\\HasNoChildren \\Drafts) "/" Drafts', b'(\\HasNoChildren \\Trash) "/" Trash',
                      b'(\\Noselect \\HasChildren) "/" "[Gmail]"']

    def status(self, folder, items):
        f = self.folders[folder.strip('"')]
        unseen = sum(1 for raw, fl in f["msgs"].values() if "\\Seen" not in fl)
        return "OK", [f'{folder} (MESSAGES {len(f["msgs"])} UNSEEN {unseen})'.encode()]

    def select(self, folder, readonly=False):
        name = folder.strip('"')
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.selected, self.readonly = name, readonly
        self._resp["UIDVALIDITY"] = [str(self.folders[name]["uv"]).encode()]
        return "OK", [str(len(self.folders[name]["msgs"])).encode()]

    def uid(self, cmd, *args):
        f = self.folders[self.selected]
        self.log.append((cmd,) + tuple(args))
        if cmd == "SEARCH":
            if "TEXT" in args:
                needle = args[-1].strip('"').lower().encode()
                hits = [u for u, (raw, fl) in f["msgs"].items() if needle in raw.lower()]
            else:
                hits = list(f["msgs"])
            return "OK", [" ".join(str(u) for u in sorted(hits)).encode()]
        if cmd == "FETCH":
            uids = [int(u) for u in args[0].split(",")]
            out = []
            for u in uids:
                if u not in f["msgs"]:
                    continue
                raw, fl = f["msgs"][u]
                flags = " ".join(fl).encode()
                if "HEADER.FIELDS" in args[1]:
                    head = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n"
                    out.append((f"1 (UID {u} FLAGS ({flags.decode()}) RFC822.SIZE {len(raw)} BODY[HEADER.FIELDS (X)] {{{len(head)}}}".encode(), head))
                else:
                    out.append((f"1 (UID {u} FLAGS ({flags.decode()}) BODY[] {{{len(raw)}}}".encode(), raw))
                out.append(b")")
            return "OK", out
        if cmd == "STORE":
            assert not self.readonly
            u, op, flag = int(args[0]), args[1], args[2].strip("()")
            fl = f["msgs"][u][1]
            if op.startswith("+") and flag not in fl:
                fl.append(flag)
            if op.startswith("-") and flag in fl:
                fl.remove(flag)
            return "OK", [b""]
        if cmd == "MOVE":
            u, dest = int(args[0]), args[1].strip('"')
            item = f["msgs"].pop(u)
            nu = self.next_uid = self.next_uid + 1
            self.folders[dest]["msgs"][nu] = item
            self._resp["COPYUID"] = [f"COPYUID {self.folders[dest]['uv']} {u} {nu}".encode()]
            return "OK", [b""]
        raise AssertionError(f"unexpected {cmd}")

    def expunge(self):
        return "OK", [b""]

    def append(self, folder, flags, date, raw):
        name = folder.strip('"')
        nu = self.next_uid = self.next_uid + 1
        self.folders[name]["msgs"][nu] = [raw, flags.strip("()").split()]
        self._resp["APPENDUID"] = [f"APPENDUID {self.folders[name]['uv']} {nu}".encode()]
        return "OK", [b"APPEND ok"]


@pytest.fixture
def imap_prov(tmp_path):
    secrets.set_secret("imap_password", "pw", account="sam@acme.example")
    prov = ImapProvider({"mail": {"imap": {"host": "imap.acme.example", "user": "sam@acme.example",
                                            "preset": "custom", "display_name": "Sam User"},
                                  "smtp": {"host": "smtp.acme.example"}}}, tmp_path)
    fake = FakeIMAP()
    prov._connect = lambda timeout=45: fake
    prov.fake = fake
    return prov


def test_imap_folders_list_read_flags(imap_prov):
    p = imap_prov
    assert parse_list_line(b'(\\HasNoChildren \\Trash) "/" "Deleted Items"')["name"] == "Deleted Items"
    assert guess_folder_kind("Deleted Items") == "trash" and guess_folder_kind("INBOX") == "inbox"
    folders = p.list_folders()
    kinds = {f["id"]: f["kind"] for f in folders}
    assert kinds == {"INBOX": "inbox", "Sent": "sent", "Drafts": "drafts", "Trash": "trash", "[Gmail]": "other"}
    inbox = [f for f in folders if f["id"] == "INBOX"][0]
    assert inbox["unread"] == 1 and inbox["total"] == 2 and folders[0]["kind"] == "inbox"

    page = p.list_messages("INBOX", limit=50)
    assert [r["subject"] for r in page["messages"]] == ["Invoice", "Lobby project"]
    row = page["messages"][1]
    assert row["ref"] == "7:42:INBOX" and row["is_read"] and row["has_attachments"] and row["from"] == "Dana <dana@northwind.example>"
    assert page["next_cursor"] is None
    page1 = p.list_messages("INBOX", limit=1)
    assert len(page1["messages"]) == 1 and page1["next_cursor"] == "43"
    page2 = p.list_messages("INBOX", limit=1, cursor=page1["next_cursor"])
    assert page2["messages"][0]["ref"] == "7:42:INBOX"
    hits = p.list_messages("INBOX", query="invoice")
    assert [r["subject"] for r in hits["messages"]] == ["Invoice"]

    m = p.get_message("7:42:INBOX")
    assert m.subject == "Lobby project" and m.attachments[0].name == "q.pdf" and m.attachments[0].content == b"%PDF"
    assert p.get_message("inbox:7:42").subject == "Lobby project"          # capture-path ref
    with pytest.raises(RuntimeError):
        p.get_message("6:42:INBOX")                                        # stale UIDVALIDITY

    assert p.set_flags("7:43:INBOX", read=True, flagged=True)
    assert p.fake.folders["INBOX"]["msgs"][43][1] == ["\\Seen", "\\Flagged"]
    assert p.get_message("7:43:INBOX").flagged is True
    d = message_dict(p.get_message("7:42:INBOX"))
    assert d["attachments"][0]["name"] == "q.pdf" and "content" not in d["attachments"][0]


def test_imap_move_delete_draft_send(imap_prov):
    p = imap_prov
    with pytest.raises(RuntimeError):
        p.move_message("7:43:INBOX", "archive")                # no archive folder on this server
    new_ref = p.move_message("7:43:INBOX", "Sent")
    assert new_ref and new_ref.endswith(":Sent") and 43 not in p.fake.folders["INBOX"]["msgs"]
    assert p.get_message(new_ref).subject == "Invoice"

    ref = p.create_draft(None, "dana@northwind.example", "", "Draft subj", "<p>draft</p>")
    assert ref and ref.endswith(":Drafts")
    raw, flags = list(p.fake.folders["Drafts"]["msgs"].values())[-1]
    assert b"Subject: Draft subj" in raw and "\\Draft" in flags

    fake_smtp = FakeSMTP()
    p.smtp_factory = lambda *a: fake_smtp
    res = p.send_message("dana@northwind.example", "", "hidden@acme.example", "Sent subj", "<p>go</p>",
                         reply_to=p.get_message("7:42:INBOX"))
    assert res["accepted"] == ["dana@northwind.example", "hidden@acme.example"]
    sm = [x for x in fake_smtp.log if x[0] == "sendmail"][0]
    assert sm[1] == "sam@acme.example"
    assert res["sent_ref"] and res["sent_ref"].endswith(":Sent")          # custom preset: appended to Sent
    raw, _ = list(p.fake.folders["Sent"]["msgs"].values())[-1]
    assert b"In-Reply-To: <i1@mail.example>" in raw and b"hidden@acme.example" not in raw.split(b"\r\n\r\n")[0]
    assert b"From: Sam User <sam@acme.example>" in raw

    assert p.delete_message("7:42:INBOX") is True
    assert 42 not in p.fake.folders["INBOX"]["msgs"] and len(p.fake.folders["Trash"]["msgs"]) == 1


# ── Gmail / Graph client methods ────────────────────────────────────────────

def _gmail_item(gid, labels, subject):
    return {"id": gid, "threadId": "t" + gid, "labelIds": labels, "internalDate": "1759330000000",
            "snippet": "snip", "payload": {"mimeType": "text/html", "body": {"data": _b64url(b"<p>b</p>")},
                                           "headers": [{"name": "From", "value": "Dana <dana@northwind.example>"},
                                                       {"name": "To", "value": "sam@acme.example"},
                                                       {"name": "Subject", "value": subject},
                                                       {"name": "Message-ID", "value": f"<{gid}@mail.example>"}]}}


def test_gmail_client(tmp_path):
    http = FakeHttp({
        "GET /labels": {"labels": [{"id": "INBOX", "name": "INBOX", "messagesUnread": 3, "messagesTotal": 10},
                                   {"id": "SENT", "name": "SENT"}, {"id": "TRASH", "name": "TRASH"},
                                   {"id": "CHAT", "name": "CHAT"},
                                   {"id": "Label_7", "name": "Clients/Northwind", "messagesUnread": 0}]},
        "GET /messages": {"messages": [{"id": "g1"}, {"id": "g2"}], "nextPageToken": "PT2", "resultSizeEstimate": 2},
        "GET /messages/g1": _gmail_item("g1", ["INBOX", "UNREAD", "STARRED"], "One"),
        "GET /messages/g2": _gmail_item("g2", ["Label_7"], "Two"),
        "POST /messages/g1/modify": {"id": "g1"},
        "POST /messages/g1/trash": {"id": "g1"},
        "GET /profile": {"emailAddress": "sam@acme.example"},
        "POST /messages/send": {"id": "s1", "threadId": "tg1"},
    })
    prov = GmailProvider({"mail": {"gmail": {"client_id": "cid"}}}, tmp_path, http=http)
    prov.tokens.save({"access_token": "AT", "expires_at": 9_999_999_999})
    assert "gmail.modify" in prov.scopes
    prov2 = GmailProvider({"mail": {"client_enabled": False, "gmail": {"client_id": "cid"}}}, tmp_path, http=http)
    assert "gmail.readonly" in prov2.scopes and "modify" not in prov2.scopes

    folders = prov.list_folders()
    ids = [f["id"] for f in folders]
    assert "CHAT" not in ids and ids[0] == "INBOX" and folders[0]["unread"] == 3
    lbl = [f for f in folders if f["id"] == "Label_7"][0]
    assert lbl["kind"] == "other" and lbl["name"] == "Northwind" and lbl["path"] == "Clients/Northwind"
    assert any(f["id"] == "__all__" for f in folders)

    page = prov.list_messages("INBOX", limit=10)
    assert [r["subject"] for r in page["messages"]] == ["One", "Two"] and page["next_cursor"] == "PT2"
    assert page["messages"][0]["flagged"] is True and page["messages"][0]["is_read"] is False
    assert "format=metadata" in [c[1] for c in http.calls if "/messages/g1" in c[1]][0]
    m = prov.get_message("g2|tg2")
    assert m is not None and m.subject == "Two"                      # any_folder: a label-only message reads
    assert prov.set_flags("g1|tg1", read=True, flagged=False)
    mod = [c for c in http.calls if c[1].endswith("/messages/g1/modify")][-1][2]
    assert mod == {"addLabelIds": [], "removeLabelIds": ["UNREAD", "STARRED"]}
    assert prov.delete_message("g1|tg1")
    res = prov.send_message("dana@northwind.example", "", "", "Hi", "<p>x</p>", reply_to=m)
    sent = [c for c in http.calls if c[1].endswith("/messages/send")][0][2]
    assert sent["threadId"] == "tg2" and res["id"] == "s1"
    raw = base64.urlsafe_b64decode(sent["raw"] + "==")
    assert b"In-Reply-To: <g2@mail.example>" in raw and b"From: sam@acme.example" in raw


def test_graph_client(tmp_path):
    def item(i, read=False, flagged=False):
        return {"id": i, "receivedDateTime": "2026-10-01T15:00:00Z", "hasAttachments": i == "G-2",
                "internetMessageId": f"<{i}@mail.example>", "subject": "S " + i, "bodyPreview": "p",
                "importance": "normal", "conversationId": "C", "isRead": read, "isDraft": False,
                "flag": {"flagStatus": "flagged" if flagged else "notFlagged"},
                "body": {"contentType": "html", "content": "<p>x</p>"},
                "from": {"emailAddress": {"address": "dana@northwind.example", "name": "Dana"}},
                "toRecipients": [{"emailAddress": {"address": "sam@acme.example"}}], "internetMessageHeaders": []}
    http = FakeHttp({
        "GET /me/mailFolders/inbox": {"id": "AAA-INBOX"},
        "GET /me/mailFolders/sentitems": {"id": "AAA-SENT"},
        "GET /me/mailFolders/drafts": {"id": "AAA-DRAFTS"},
        "GET /me/mailFolders/deleteditems": {"id": "AAA-DEL"},
        "GET /me/mailFolders/junkemail": {"id": "AAA-JUNK"},
        "GET /me/mailFolders/archive": {"id": "AAA-ARCH"},
        "GET /me/mailFolders": {"value": [
            {"id": "AAA-INBOX", "displayName": "Posteingang", "unreadItemCount": 2, "totalItemCount": 9, "childFolderCount": 1},
            {"id": "AAA-DEL", "displayName": "Gelöschte Elemente", "unreadItemCount": 0, "totalItemCount": 1},
            {"id": "AAA-X", "displayName": "Projects", "unreadItemCount": 0, "totalItemCount": 4}]},
        "GET /me/mailFolders/AAA-INBOX/childFolders": {"value": [{"id": "AAA-K", "displayName": "Kids"}]},
        "GET /me/mailFolders/inbox/messages": {"value": [item("G-1"), item("G-2", True, True)],
                                               "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"},
        "GET /me/messages/G-2": item("G-2", True, True),
        "GET /me/messages/G-2/attachments": {"value": []},
        "PATCH /me/messages/G-2": {"id": "G-2"},
        "POST /me/messages/G-2/move": {"id": "G-2-moved"},
        "POST /me/messages/G-2/createReply": {"id": "R-1", "internetMessageId": "<r1@mail.example>"},
        "PATCH /me/messages/R-1": {"id": "R-1"},
        "POST /me/messages/R-1/attachments": {"id": "att"},
        "POST /me/messages/R-1/send": {},
        "POST /me/sendMail": {},
    })
    prov = GraphProvider({"mail": {"send_enabled": True, "graph": {"client_id": "cid"}}}, tmp_path, http=http)
    prov.tokens.save({"access_token": "AT", "expires_at": 9_999_999_999})
    assert "Mail.Send" in prov.scopes
    assert "Mail.Send" not in GraphProvider({"mail": {"graph": {"client_id": "cid"}}}, tmp_path, http=http).scopes

    folders = prov.list_folders()
    kinds = {f["id"]: f["kind"] for f in folders}
    assert kinds["AAA-INBOX"] == "inbox" and kinds["AAA-DEL"] == "trash" and kinds["AAA-X"] == "other"
    assert [f for f in folders if f["id"] == "AAA-K"][0]["path"] == "Posteingang/Kids"
    page = prov.list_messages("inbox", limit=10)
    assert [r["ref"] for r in page["messages"]] == ["G-1", "G-2"] and page["next_cursor"].endswith("/next")
    assert page["messages"][1]["flagged"] and page["messages"][1]["has_attachments"]
    assert "orderby=receivedDateTime+desc" in [c[1] for c in http.calls if "/inbox/messages" in c[1]][0]
    m = prov.get_message("G-2")
    assert m.flagged is True
    prov.set_flags("G-2", read=False, flagged=False)
    patch = [c for c in http.calls if c[0] == "PATCH" and c[1].endswith("/G-2")][-1][2]
    assert patch == {"isRead": False, "flag": {"flagStatus": "notFlagged"}}
    assert prov.move_message("G-2", "archive") == "G-2-moved"
    mv = [c for c in http.calls if c[1].endswith("/G-2/move")][-1][2]
    assert mv == {"destinationId": "archive"}
    res = prov.send_message("dana@northwind.example", "", "", "Re: S G-2", "<p>r</p>",
                            attachments=[Attachment(name="a.txt", content_type="text/plain", content=b"hi")], reply_to=m)
    assert res["id"] == "R-1" and any(c[1].endswith("/R-1/send") for c in http.calls)
    res2 = prov.send_message("dana@northwind.example", "", "x@y.z", "New", "<p>n</p>")
    sm = [c for c in http.calls if c[1].endswith("/me/sendMail")][-1][2]
    assert sm["saveToSentItems"] is True and sm["message"]["bccRecipients"][0]["emailAddress"]["address"] == "x@y.z"
    assert res2["message_id"]


# ── the blueprint ───────────────────────────────────────────────────────────

@pytest.fixture
def client_app(tmp_path, imap_prov):
    flask = pytest.importorskip("flask")
    from tests.fakes import FakeStore
    from plannerboard.api.mail_client_api import bp
    app = flask.Flask(__name__)
    store = FakeStore()
    store.projects["P1"] = {"id": "P1", "name": "Lobby", "status": "action_required", "contacts": [],
                            "subtasks": [], "key_details": {}, "notes": []}
    store.project_exists = lambda pid: pid in store.projects
    cfg = {"mail": {"provider": "imap", "client_enabled": True, "send_enabled": False,
                    "imap": {"host": "imap.acme.example", "user": "sam@acme.example"}},
           "user": {"email": "sam@acme.example"},
           "paths": {"data_root": str(tmp_path), "mail_cache": str(tmp_path / "mail_cache")}}
    app.extensions["pb"] = {"store": store, "cfg": cfg, "profession": {}, "llm": None,
                            "mail_client_provider": imap_prov}
    app.register_blueprint(bp)
    return app, store, cfg, imap_prov


def test_client_api_roundtrip(client_app):
    app, store, cfg, prov = client_app
    c = app.test_client()
    caps = c.get("/api/mail/client/capabilities").get_json()
    assert caps["provider"] == "imap" and caps["capabilities"]["send"] and caps["send_enabled"] is False and caps["me"] == "sam@acme.example"
    f = c.get("/api/mail/client/folders").get_json()
    assert f["ok"] and f["folders"][0]["kind"] == "inbox"
    lst = c.get("/api/mail/client/messages?folder=INBOX&limit=10").get_json()
    assert lst["ok"] and len(lst["messages"]) == 2
    ref = "7:42:INBOX"
    m = c.get(f"/api/mail/client/message?ref={ref}").get_json()
    assert m["subject"] == "Lobby project" and m["attachments"][0]["url"].endswith("&index=0")
    a = c.get(f"/api/mail/client/attachment?ref={ref}&index=0")
    assert a.status_code == 200 and a.data == b"%PDF" and a.mimetype == "application/pdf"
    assert c.get("/api/mail/client/message?ref=7:999:INBOX").status_code == 404
    assert c.post("/api/mail/client/flags", json={"ref": "7:43:INBOX", "read": True}).get_json()["ok"]
    comp = c.post("/api/mail/client/compose", json={"ref": ref, "mode": "reply_all"}).get_json()
    assert comp["to"] == "Dana <dana@northwind.example>" and comp["subject"] == "Re: Lobby project" and comp["reply_ref"] == ref
    fw = c.post("/api/mail/client/compose", json={"ref": ref, "mode": "forward"}).get_json()
    assert fw["subject"] == "Fw: Lobby project" and fw["attachments"][0]["name"] == "q.pdf"

    # send gate: off by default -> 403, nothing touched the transport
    fake_smtp = FakeSMTP()
    prov.smtp_factory = lambda *a: fake_smtp
    r = c.post("/api/mail/client/send", json={"to": "dana@northwind.example", "subject": "x", "body_html": "<p>y</p>"})
    assert r.status_code == 403 and r.get_json()["send_enabled"] is False and fake_smtp.log == []
    # draft always works (provider Drafts folder)
    d = c.post("/api/mail/client/draft", json={"to": "dana@northwind.example", "subject": "Dr", "body_html": "<p>d</p>",
                                               "project_id": "P1"}).get_json()
    assert d.get("ok"), d
    assert d["where"] == "provider" and d["id"].endswith(":Drafts")
    # turn sending on
    cfg["mail"]["send_enabled"] = True
    r = c.post("/api/mail/client/send", json={"to": "dana@northwind.example", "subject": "Re: Lobby project",
                                              "body_html": "<p>y</p>", "reply_ref": ref, "project_id": "P1",
                                              "attachments": [{"name": "n.txt", "content_type": "text/plain",
                                                               "content_b64": base64.b64encode(b"note").decode()}]})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["accepted"] == ["dana@northwind.example"]
    sm = [x for x in fake_smtp.log if x[0] == "sendmail"][0]
    assert sm[1] == "sam@acme.example"
    assert store.correspondence and store.correspondence[-1]["pid"] == "P1"
    assert any("Sent: Re: Lobby project" in n[1] for n in store.notes)
    assert c.post("/api/mail/client/send", json={"subject": "no rcpt"}).status_code == 400

    # bridges
    fl = c.post("/api/mail/client/file", json={"ref": "7:43:INBOX", "project_id": "P1"}).get_json()
    assert fl["ok"] and fl["project_id"] == "P1"
    assert store.correspondence[-1]["subject"] == "Invoice"
    assert c.post("/api/mail/client/file", json={"ref": ref, "project_id": "nope"}).status_code == 404
    st = c.post("/api/mail/client/stage", json={"ref": ref}).get_json()
    assert st["ok"] and st["already"] is False and st["message_id"] == "i1@mail.example"
    st2 = c.post("/api/mail/client/stage", json={"ref": ref}).get_json()
    assert st2["already"] is True

    # client off -> everything 403
    cfg["mail"]["client_enabled"] = False
    assert c.get("/api/mail/client/folders").status_code == 403
    assert c.get("/api/mail/client/capabilities").get_json()["client_enabled"] is False


def test_client_api_no_provider(tmp_path):
    flask = pytest.importorskip("flask")
    from tests.fakes import FakeStore
    from plannerboard.api.mail_client_api import bp
    app = flask.Flask(__name__)
    app.extensions["pb"] = {"store": FakeStore(), "cfg": {"mail": {"provider": "none"}}, "profession": {}, "llm": None}
    app.register_blueprint(bp)
    c = app.test_client()
    assert c.get("/api/mail/client/capabilities").get_json()["capabilities"] == {}
    assert c.get("/api/mail/client/folders").status_code == 409
    # the drop-folder provider has no client capabilities -> 501
    app.extensions["pb"]["cfg"] = {"mail": {"provider": "pa_folder"}, "paths": {"data_root": str(tmp_path)}}
    assert c.get("/api/mail/client/folders").status_code == 501
