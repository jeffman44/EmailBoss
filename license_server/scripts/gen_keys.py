#!/usr/bin/env python3
"""Generate an Ed25519 signing pair for the license server.

    python scripts/gen_keys.py [--out keys/] [--name prod]

Writes <out>/<name>_private.pem (mode 0600) and <out>/<name>_public.pem,
then prints the public PEM so it can be pasted into
app/plannerboard/license/client.py as DEFAULT_PUBLIC_KEY_PEM.
Refuses to overwrite an existing private key unless --force is given.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from licserver.keys import generate_keypair  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "keys"))
    ap.add_argument("--name", default="license")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    priv_path, pub_path = out / f"{a.name}_private.pem", out / f"{a.name}_public.pem"
    if priv_path.exists() and not a.force:
        print(f"refusing to overwrite {priv_path} (use --force)", file=sys.stderr)
        return 2
    priv, pub = generate_keypair()
    priv_path.write_bytes(priv)
    os.chmod(priv_path, 0o600)
    pub_path.write_bytes(pub)
    print(f"private key: {priv_path}\npublic key:  {pub_path}\n")
    print(pub.decode())
    print("Set LIC_PRIVATE_KEY_PATH to the private key and embed the public PEM in the app client.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
