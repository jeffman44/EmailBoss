import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# The desktop app's license client lives in the sibling app/ tree.
sys.path.insert(0, str(ROOT.parent / "app"))

from licserver.config import Settings  # noqa: E402
from licserver.main import create_app  # noqa: E402

KEY_PATH = ROOT / "keys" / "DEV_ONLY_private.pem"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "lic.db"),
        private_key_path=str(KEY_PATH),
        base_url="http://testserver",
        trial_enabled=True,
        trial_days=14,
        admin_emails=("admin@example.test",),
        stripe_webhook_secret="whsec_test",
        download_urls={"windows": "https://dl.example.test/pb.exe"},
        login_rate_limit=5,
        login_rate_window=60,
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


class Portal:
    """Form helpers that handle the CSRF cookie."""

    def __init__(self, client: TestClient):
        self.c = client

    def csrf(self) -> str:
        tok = self.c.cookies.get("lic_csrf")
        if tok:
            return tok
        r = self.c.get("/forgot")  # renders for everyone and sets the csrf cookie
        assert r.status_code == 200, r.text
        return self.c.cookies.get("lic_csrf")

    def post(self, path, **data):
        data.setdefault("csrf_token", self.csrf())
        return self.c.post(path, data=data)

    def signup(self, email, password="hunter2hunter2"):
        return self.post("/signup", email=email, password=password, password2=password)

    def login(self, email, password="hunter2hunter2"):
        return self.post("/login", email=email, password=password)

    def logout(self):
        return self.post("/logout")


@pytest.fixture
def portal(client):
    return Portal(client)


def sign_body(secret: str, lid: str, fingerprint: str, path: str, body: dict, *, ts=None, nonce=None) -> dict:
    """Return ``body`` plus ts, nonce and a device proof bound to path and body."""
    import secrets as _secrets
    import time as _time
    from licserver.licenses import body_sha256, compute_proof, secret_hash
    ts = int(_time.time()) if ts is None else ts
    nonce = nonce or _secrets.token_hex(8)
    out = {k: v for k, v in body.items() if k not in ("ts", "nonce", "proof")}
    out.update(ts=ts, nonce=nonce,
               proof=compute_proof(secret_hash(secret), lid, fingerprint, ts, path, nonce, body_sha256(out)))
    return out
