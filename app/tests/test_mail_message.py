import json
from datetime import datetime, timezone

from plannerboard.mail import imap, secrets
from plannerboard.mail.base import ProviderState, ConnResult
from plannerboard.mail.message import (Address, Attachment, Message, PA_OPTIONAL_KEYS,
                                       PA_REQUIRED_KEYS, validate_record, write_record)
from plannerboard.mail.pa_folder import PAFolderProvider


def _msg():
    return Message(
        id="", received_at=datetime(2026, 10, 1, 9, 5, 7, tzinfo=timezone.utc),
        subject="Lobby project update", body="<p>Hello <b>there</b></p>", is_html=True,
        sender=Address("dana@northwind.example", "Dana Whitfield"),
        to=[Address("sam@acme.example", "Sam User")],
        cc=[Address("ops@northwind.example", "Ops Desk")],
        attachments=[Attachment("plan.pdf", "application/pdf", content=b"%PDF-1.4 x"),
                     Attachment("logo.png", "image/png", is_inline=True, content_id="logo1", content=b"\x89PNG")],
        internet_message_id="<round@trip.example>", importance="high",
    )


def test_round_trip_keeps_names_and_cc():
    m = _msg()
    rec = m.to_pa_record()
    assert not validate_record(rec), validate_record(rec)
    assert set(rec) - PA_OPTIONAL_KEYS == PA_REQUIRED_KEYS
    assert rec["from"] == "dana@northwind.example"
    assert rec["ccRecipients"] == "ops@northwind.example"
    assert rec["receivedDateTime"] == "2026-10-01T09:05:07+00:00"
    assert rec["hasAttachments"] is True
    assert rec["id"] == "round@trip.example"
    back = Message.from_pa_record(json.loads(json.dumps(rec)), "inbox")
    assert back.sender.name == "Dana Whitfield"
    assert back.to[0].name == "Sam User"
    assert [a.email for a in back.cc] == ["ops@northwind.example"]
    assert back.cc[0].name == "Ops Desk"
    assert back.attachments[0].content == b"%PDF-1.4 x"
    assert back.attachments[1].is_inline and back.attachments[1].content_id == "logo1"
    assert back.importance == "high"
    assert back.conversation_id == rec["conversationId"]
    assert back.direction == "inbound"


def test_write_record_attachments_first_then_json(tmp_path):
    out, written = write_record(tmp_path, _msg())
    assert out.parent.name == "inbox" and out.suffix == ".json"
    assert out.name.endswith("-inbox.json")
    att_dir = tmp_path / "attachments" / "round@trip.example"
    assert (att_dir / "plan.pdf").read_bytes() == b"%PDF-1.4 x"
    assert (att_dir / "logo.png").exists()
    assert len(written) == 2
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert not validate_record(rec)
    assert not list(tmp_path.glob("inbox/*.tmp"))


def test_sent_folder_marks_read_and_outbound(tmp_path):
    m = _msg()
    m.folder = "sent"
    rec = m.to_pa_record()
    assert rec["isRead"] is True
    out, _ = write_record(tmp_path, m)
    assert out.parent.name == "sent"
    assert Message.from_pa_record(rec, "sent").direction == "outbound"


def test_legacy_display_name_key_is_read():
    rec = _msg().to_pa_record()
    rec["internetMessageHeaders"] = []
    rec["from_display_name"] = "Legacy Name"
    m = Message.from_pa_record(rec)
    assert m.sender.name == "Legacy Name"


def test_imap_selftest_record_shape():
    assert imap.selftest(strict=True) == []


def test_imap_settings_presets():
    s = imap.settings_from_config({"imap": {"preset": "gmail", "user": "a@b.example"}})
    assert s["host"] == "imap.gmail.com" and s["sent_folder"] == "[Gmail]/Sent Mail"
    assert s["auth_mode"] == "password"
    ids = {p["id"] for p in imap.preset_list()}
    assert ids == {"gmail", "outlook", "icloud", "custom"}


def test_provider_state_roundtrip(tmp_path):
    st = ProviderState(tmp_path, "imap")
    st.folder("inbox")["last_uid"] = 42
    st.set("history_id", "99")
    assert st.save()
    st2 = ProviderState(tmp_path, "imap")
    assert st2.folder("inbox")["last_uid"] == 42 and st2.get("history_id") == "99"
    assert (tmp_path / "state_imap.json").exists()
    (tmp_path / "state_imap.json").write_text("{not json")
    st3 = ProviderState(tmp_path, "imap")
    assert st3.data.get("folders") is None and st3.warnings


def test_secrets_memory_backend_and_env(monkeypatch):
    secrets.use_memory_backend(True)
    try:
        ok, where, err = secrets.set_secret("imap_password", "pw1", account="a@b.example")
        assert ok and where == "memory" and err is None
        assert secrets.get_secret("imap_password", account="a@b.example") == ("pw1", "memory")
        assert secrets.get_secret("imap_password") == (None, None)
        monkeypatch.setenv("PB_IMAP_PASSWORD", "envpw")
        assert secrets.get_secret("imap_password") == ("envpw", "env")
        assert secrets.delete_secret("imap_password", account="a@b.example")
    finally:
        secrets.use_memory_backend(False)


def test_pa_folder_health(mail_cache):
    prov = PAFolderProvider({}, mail_cache)
    h = prov.health()
    assert h["folder_exists"] and h["pending"] == 0 and h["ok"]
    (mail_cache / "inbox" / "bad.json").write_text("{nope")
    h = prov.health()
    assert h["unreadable"] == 1 and not h["ok"]
    assert isinstance(prov.test_connection(), ConnResult)
    assert list(prov.fetch_new(ProviderState(mail_cache, "pa_folder"))) == []
