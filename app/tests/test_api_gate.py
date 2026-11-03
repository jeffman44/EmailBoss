"""The loopback engine must only answer its own window: session cookie,
Host and Origin checks, JSON-only writes (security review C2 / H1)."""
from plannerboard import SESSION_COOKIE, create_app
from tests.conftest import make_config


def _app(data_root):
    app = create_app(config=make_config(data_root), testing=True)
    app.config["PB_ENFORCE_AUTH"] = True
    return app


def test_api_needs_session_cookie_then_bootstrap_sets_it(data_root):
    app = _app(data_root)
    c = app.test_client()
    assert c.get("/api/health").status_code == 401
    assert b'content="' + app.extensions['pb'].session_token.encode() not in c.get('/').data  # no token in the page
    assert c.get("/auth/not-the-token").status_code == 403
    r = c.get(f"/auth/{app.extensions['pb'].session_token}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    cookie = r.headers.get("Set-Cookie", "")
    assert SESSION_COOKIE in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert c.get("/api/health").status_code == 200


def test_bearer_session_token_works_for_tray(data_root):
    app = _app(data_root)
    tok = app.extensions["pb"].session_token
    c = app.test_client()
    assert c.get("/api/health", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_host_and_origin_and_content_type_gates(data_root):
    app = _app(data_root)
    tok = app.extensions["pb"].session_token
    c = app.test_client()
    c.get(f"/auth/{tok}")
    # DNS rebinding: a foreign Host is refused even with the cookie
    assert c.get("/api/health", headers={"Host": "evil.example:8080"}).status_code == 400
    # CSRF: a cross-site Origin is refused
    r = c.post("/api/restart", headers={"Origin": "https://evil.example"}, json={})
    assert r.status_code == 403
    # Same-origin fetch is fine
    assert c.get("/api/health", headers={"Origin": "http://localhost"}).status_code == 200
    # A "simple" text/plain write (no preflight) is refused
    r = c.post("/api/projects", data='{"name":"x"}', content_type="text/plain")
    assert r.status_code == 415


def test_bootstrap_next_is_in_site_only(data_root):
    app = _app(data_root)
    tok = app.extensions["pb"].session_token
    c = app.test_client()
    for bad in ("//evil.example", "/\\evil.example", "https://evil.example"):
        r = c.get(f"/auth/{tok}?next={bad}")
        assert r.status_code == 302 and r.headers["Location"].endswith("/") and "evil" not in r.headers["Location"]
