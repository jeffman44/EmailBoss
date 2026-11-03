"""Gmail / Graph providers against a fake HTTP function (no network), plus
the runner wiring and the /api/mail blueprint."""
import base64
import urllib.parse

import pytest

from plannerboard.mail import secrets
from plannerboard.mail.base import ProviderState
from plannerboard.mail.gmail import GmailProvider
from plannerboard.mail.graph import GraphProvider
from plannerboard.mail.oauth import pkce_pair
from plannerboard.mail.runner import (configured_provider_ids, provider_catalog, start_all,
                                      mail_cache_dir)


@pytest.fixture(autouse=True)
def _mem_secrets():
    secrets.use_memory_backend(True)
    yield
    secrets.use_memory_backend(False)


def _b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, data=None, headers=None, form=False, timeout=30, raw=False):
        self.calls.append((method, url, data))
        path = url.split("?")[0]
        for key, resp in self.routes.items():
            m, p = key.split(" ", 1)
            if m == method and (path == p or path.endswith(p)):
                return resp(url, data) if callable(resp) else (200, resp)
        return 404, {"error": f"no route {method} {url}"}


def test_pkce_pair_shape():
    v, c = pkce_pair()
    assert len(v) >= 43 and len(c) == 43


def test_gmail_oauth_and_fetch(tmp_path):
    gmail_msg = {
        "id": "g1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"], "internalDate": "1759330000000",
        "snippet": "hello there",
        "payload": {"mimeType": "multipart/mixed",
                    "headers": [{"name": "From", "value": "Dana <dana@northwind.example>"},
                                {"name": "To", "value": "Sam <sam@acme.example>"},
                                {"name": "Cc", "value": "ops@northwind.example"},
                                {"name": "Subject", "value": "Lobby project"},
                                {"name": "Message-ID", "value": "<g1@mail.example>"},
                                {"name": "Date", "value": "Wed, 01 Oct 2026 10:00:00 -0600"}],
                    "parts": [
                        {"mimeType": "text/html", "body": {"data": _b64url(b"<p>Hi</p>")}},
                        {"mimeType": "application/pdf", "filename": "quote.pdf",
                         "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                         "body": {"attachmentId": "att1", "size": 10}},
                    ]},
    }
    http = FakeHttp({
        "POST oauth2.googleapis.com/token": lambda u, d: (200, {"access_token": "AT", "refresh_token": "RT",
                                                                "expires_in": 3600, "scope": "x"}),
        "GET /profile": {"emailAddress": "sam@acme.example", "historyId": "500", "messagesTotal": 3},
        "GET /messages": {"messages": [{"id": "g1"}]},
        "GET /messages/g1": gmail_msg,
        "GET /messages/g1/attachments/att1": {"data": _b64url(b"%PDF-1.4 q")},
        "POST /drafts": {"id": "d1"},
    })
    prov = GmailProvider({"mail": {"gmail": {"client_id": "cid", "client_secret": "sec"}}}, tmp_path, http=http)
    info = prov.begin_auth("http://127.0.0.1:8080/api/mail/oauth/callback")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(info["url"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["state"] == [info["state"]]
    res = prov.finish_auth(info["state"], "the-code")
    assert res.ok, res.error
    assert prov.tokens.load()["refresh_token"] == "RT"
    assert prov.test_connection().ok

    st = ProviderState(tmp_path, "gmail")
    msgs = list(prov.fetch_new(st))
    assert len(msgs) == 1
    m = msgs[0]
    assert m.sender.name == "Dana" and m.folder == "inbox" and m.direction == "inbound"
    assert [a.email for a in m.cc] == ["ops@northwind.example"]
    assert m.attachments[0].name == "quote.pdf" and m.attachments[0].content == b"%PDF-1.4 q"
    assert m.is_html and m.body == "<p>Hi</p>"
    assert st.get("history_id") == "500"
    rec = m.to_pa_record()
    from plannerboard.mail.message import validate_record
    assert not validate_record(rec)

    did = prov.create_draft(m, "dana@northwind.example", "", "RE: Lobby project", "<p>Thanks</p>")
    assert did == "d1"
    draft_call = [c for c in http.calls if c[0] == "POST" and c[1].endswith("/drafts")][0]
    raw = draft_call[2]["message"]["raw"]
    assert draft_call[2]["message"]["threadId"] == "t1"
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "In-Reply-To: <g1@mail.example>" in decoded and "Thanks" in decoded
    assert not any(c[1].endswith("/send") for c in http.calls)


def test_gmail_history_fallback_on_lost_window(tmp_path):
    http = FakeHttp({
        "GET /profile": {"emailAddress": "sam@acme.example", "historyId": "900"},
        "GET /history": lambda u, d: (404, {"error": "not found"}),
        "GET /messages": {"messages": []},
    })
    prov = GmailProvider({"mail": {"gmail": {"client_id": "cid"}}}, tmp_path, http=http)
    prov.tokens.save({"access_token": "AT", "expires_at": 9_999_999_999})
    st = ProviderState(tmp_path, "gmail")
    st.set("history_id", "100")
    assert list(prov.fetch_new(st)) == []
    assert any("resyncing" in w for w in prov.health()["warnings"])


def test_graph_delta_fetch_and_reply_draft(tmp_path):
    item = {"id": "G-1", "receivedDateTime": "2026-10-01T15:00:00Z", "hasAttachments": False,
            "internetMessageId": "<graph1@mail.example>", "subject": "Lease renewal",
            "bodyPreview": "see attached", "importance": "normal", "conversationId": "CONV-G-1",
            "isRead": False, "isDraft": False, "body": {"contentType": "html", "content": "<p>x</p>"},
            "from": {"emailAddress": {"address": "dana@northwind.example", "name": "Dana Whitfield"}},
            "toRecipients": [{"emailAddress": {"address": "sam@acme.example", "name": "Sam User"}}],
            "ccRecipients": [{"emailAddress": {"address": "ops@northwind.example", "name": "Ops"}}],
            "internetMessageHeaders": []}
    http = FakeHttp({
        "GET /me": {"mail": "sam@acme.example", "displayName": "Sam User"},
        "GET /me/mailFolders/inbox/messages/delta": {"value": [item], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta-inbox-2"},
        "GET /me/mailFolders/sentitems/messages/delta": {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta-sent-2"},
        "GET /me/messages/G-1/attachments": {"value": [
            {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a1", "name": "photo.jpg",
             "contentType": "image/jpeg", "size": 5, "isInline": True, "contentId": "img1",
             "contentBytes": base64.b64encode(b"JPEG!").decode()}]},
        "POST /me/messages/G-1/createReply": {"id": "DRAFT-1"},
        "PATCH /me/messages/DRAFT-1": {"id": "DRAFT-1"},
    })
    prov = GraphProvider({"mail": {"graph": {"client_id": "cid"}}}, tmp_path, http=http)
    prov.tokens.save({"access_token": "AT", "expires_at": 9_999_999_999})
    assert prov.tenant == "common"
    assert prov.test_connection().ok
    st = ProviderState(tmp_path, "graph")
    msgs = list(prov.fetch_new(st))
    assert len(msgs) == 1
    m = msgs[0]
    assert m.sender.name == "Dana Whitfield" and m.cc[0].email == "ops@northwind.example"
    assert m.conversation_id == "CONV-G-1" and m.attachments[0].is_inline
    assert st.folder("inbox")["delta_link"].endswith("delta-inbox-2")
    assert prov.create_draft(m, "dana@northwind.example", "", "", "<p>reply</p>") == "DRAFT-1"
    patch = [c for c in http.calls if c[0] == "PATCH"][0]
    assert patch[2]["body"]["content"] == "<p>reply</p>"
    assert not any("/send" in c[1] for c in http.calls)


def test_graph_pkce_url_and_token_refresh(tmp_path):
    http = FakeHttp({
        "POST /token": lambda u, d: (200, {"access_token": "NEW", "refresh_token": "RT2", "expires_in": 3600}),
    })
    prov = GraphProvider({"mail": {"graph": {"client_id": "cid", "tenant": "contoso.example"}}}, tmp_path, http=http)
    info = prov.begin_auth("http://127.0.0.1:8080/api/mail/oauth/callback")
    assert "login.microsoftonline.com/contoso.example/" in info["url"]
    assert prov.finish_auth(info["state"], "code").ok
    prov.tokens.save({"access_token": "OLD", "refresh_token": "RT", "expires_at": 1})
    assert prov.access_token() == "NEW"
    assert prov.tokens.load()["refresh_token"] == "RT2"
    assert prov.finish_auth("bogus-state", "code").ok is False


def test_runner_catalog_and_start(cfg, mail_cache):
    from tests.fakes import FakeStore
    cat = {p["id"]: p for p in provider_catalog()}
    assert set(cat) == {"pa_folder", "imap", "gmail", "graph"}
    assert any(f["key"] == "password" and f["type"] == "secret" for f in cat["imap"]["fields"])
    assert configured_provider_ids(cfg) == ["pa_folder"]
    assert mail_cache_dir(cfg) == mail_cache
    runner = start_all(cfg, FakeStore(), {}, autostart=False)
    assert list(runner.threads) == ["pa_folder"]
    summary = runner.threads["pa_folder"].run_once()
    assert summary["fetched"] == 0 and summary["triage"]["scanned"] == 0
    hs = runner.health_summary()
    assert hs["provider"] == "pa_folder" and hs["ok"]


def test_mail_api_blueprint(cfg, mail_cache):
    flask = pytest.importorskip("flask")
    from tests.fakes import FakeStore
    try:
        from plannerboard.api.mail_api import bp
    except ImportError:
        # The api package registry imports every blueprint; while sibling
        # modules are still being written, load ours directly.
        import importlib.util, sys
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "plannerboard.api.mail_api", Path(__file__).resolve().parent.parent / "plannerboard" / "api" / "mail_api.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("plannerboard.api", type(sys)("plannerboard.api"))
        sys.modules["plannerboard.api"].__path__ = [str(Path(__file__).resolve().parent.parent / "plannerboard" / "api")]
        spec.loader.exec_module(mod)
        bp = mod.bp
    app = flask.Flask(__name__)
    app.extensions["pb"] = {"store": FakeStore(), "cfg": cfg, "profession": {}, "llm": None}
    app.register_blueprint(bp)
    c = app.test_client()
    r = c.get("/api/mail/providers")
    assert r.status_code == 200 and {p["id"] for p in r.get_json()["providers"]} >= {"imap", "gmail"}
    r = c.post("/api/mail/test", json={"provider": "pa_folder"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    r = c.post("/api/mail/oauth/start", json={"provider": "gmail", "settings": {"client_id": "cid"}})
    body = r.get_json()
    assert r.status_code == 200 and body["url"].startswith("https://accounts.google.com/") and body["state"]
    assert body["redirect_uri"] == "http://127.0.0.1:8080/api/mail/oauth/callback"
    r = c.get("/api/mail/oauth/callback?state=nope&code=x")
    assert r.status_code == 400
    r = c.get("/api/mail/status")
    assert r.status_code == 200 and r.get_json()["provider"] == "pa_folder"
    r = c.post("/api/mail/run", json={})
    assert r.status_code == 200 and "triage" in r.get_json()["results"]
