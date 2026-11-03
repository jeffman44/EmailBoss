#!/usr/bin/env python3
"""Write and sign the release manifest (update.json + update.json.sig).

    python make_update_manifest.py --version 1.4.0 --exe dist/EmailBoss-1.4.0-Setup.exe \
        --url https://github.com/jeffman44/emailboss/releases/download/v1.4.0/EmailBoss-1.4.0-Setup.exe \
        --key release_signing_key.pem --out dist

update.json holds the canonical JSON bytes that were signed (sorted keys, no
spaces); update.json.sig is the base64url Ed25519 signature over exactly those
bytes. The key is the production license private key (PEM, PKCS8 Ed25519), the
one whose public half is DEFAULT_PUBLIC_KEY_PEM in plannerboard/license/client.py,
so installed apps already trust the result. In CI it comes from the secret
EB_RELEASE_SIGNING_KEY. The key is read from --key and never printed.

--verify re-reads the written files and checks them with the public key the
app would use (add --dev-key to use the dev channel key in local trials).
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from plannerboard import updater  # noqa: E402

REPO_RELEASES = "https://github.com/jeffman44/emailboss/releases/tag/"


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("signing key is not an Ed25519 private key")
    return key


def build_manifest(version: str, exe: Path, url: str, notes_url: str = "", channel: str = "stable",
                   published: str = "") -> dict:
    updater.parse_semver(version)
    exe = Path(exe)
    if not exe.is_file():
        raise SystemExit(f"installer not found: {exe}")
    h = hashlib.sha256()
    with open(exe, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    doc = {
        "version": version,
        "published": published or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "channel": channel,
        "windows": {"url": url, "sha256": h.hexdigest(), "size": exe.stat().st_size},
        "notes_url": notes_url or (REPO_RELEASES + "v" + version),
    }
    return updater.validate_manifest(doc)


def write_signed(doc: dict, key: Ed25519PrivateKey, out_dir: Path) -> tuple:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = updater.canonical_manifest_bytes(doc)
    sig = updater.sign_manifest_bytes(raw, key)
    (out_dir / "update.json").write_bytes(raw)
    (out_dir / "update.json.sig").write_text(sig + "\n", encoding="ascii")
    return out_dir / "update.json", out_dir / "update.json.sig"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True)
    ap.add_argument("--exe", required=True, help="the built Setup.exe")
    ap.add_argument("--url", required=True, help="public https download URL of that Setup.exe")
    ap.add_argument("--key", required=True, help="Ed25519 private key PEM")
    ap.add_argument("--out", required=True, help="folder for update.json and update.json.sig")
    ap.add_argument("--notes-url", default="")
    ap.add_argument("--channel", default="stable")
    ap.add_argument("--published", default="", help="ISO UTC timestamp (default: now)")
    ap.add_argument("--verify", action="store_true", help="re-verify the written files")
    ap.add_argument("--dev-key", action="store_true", help="with --verify: use the dev channel public key")
    a = ap.parse_args(argv)

    key = load_private_key(Path(a.key))
    doc = build_manifest(a.version, Path(a.exe), a.url, a.notes_url, a.channel, a.published)
    mpath, spath = write_signed(doc, key, Path(a.out))
    print(f"wrote {mpath} and {spath}")
    print(f"  version {doc['version']}  sha256 {doc['windows']['sha256']}  size {doc['windows']['size']}")
    if a.verify:
        from plannerboard.license import client as lc
        pem = lc.DEV_PUBLIC_KEY_PEM if a.dev_key else lc.RELEASE_PUBLIC_KEY_PEM
        updater.verify_manifest(mpath.read_bytes(), spath.read_text(), lc.load_public_key(pem))
        print("  verify ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
