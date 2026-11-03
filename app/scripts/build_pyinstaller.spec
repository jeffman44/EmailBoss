# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Email Boss (onedir bundle).

    pyinstaller --noconfirm --clean scripts/build_pyinstaller.spec

Output: dist/EmailBoss/EmailBoss(.exe) with the read-only resources
copied under _internal/plannerboard/ (PyInstaller 6 layout), which is what
plannerboard.paths.RESOURCE_DIR resolves to when sys.frozen is set:

    _internal/plannerboard/static/*.html, *.js, *.css
    _internal/plannerboard/professions/*.json
    _internal/plannerboard/ai/prompts/*.yaml
    _internal/plannerboard/schema.sql

The user's data never lives inside the bundle (paths.default_data_root()).
This file is plain Python so it can be syntax-checked with py_compile even
where PyInstaller is not installed; the PyInstaller names (Analysis, PYZ,
EXE, COLLECT) are injected by PyInstaller at build time.
"""

import os
import sys
from pathlib import Path

try:  # SPECPATH is injected by PyInstaller; fall back for py_compile / static checks
    SPEC_DIR = Path(SPECPATH)  # noqa: F821
except NameError:
    SPEC_DIR = Path(os.path.abspath(os.path.dirname(__file__))) if "__file__" in globals() else Path.cwd()

APP_DIR = (SPEC_DIR / "..").resolve()
PKG = APP_DIR / "plannerboard"
ENTRY = PKG / "start.py"

APP_NAME = "EmailBoss"
# keep in step with plannerboard/__init__.py and /VERSION
VERSION = (APP_DIR.parent / "VERSION").read_text().strip() if (APP_DIR.parent / "VERSION").is_file() else "1.0.0"


def _tree(src: Path, dest: str):
    """Every file under src -> (abs file, bundle folder) tuples, recursively."""
    out = []
    for p in src.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            rel = p.parent.relative_to(src)
            out.append((str(p), str(Path(dest) / rel)))
    return out


datas = []
datas += _tree(PKG / "static", "plannerboard/static")
datas += _tree(PKG / "professions", "plannerboard/professions")
datas += _tree(PKG / "ai" / "prompts", "plannerboard/ai/prompts")
datas += [(str(PKG / "schema.sql"), "plannerboard")]

hiddenimports = [
    # keyring backends are loaded by entry point, not by import
    "keyring.backends.Windows",
    "keyring.backends.macOS",
    "keyring.backends.SecretService",
    "keyring.backends.kwallet",
    "keyring.backends.chainer",
    "keyring.backends.fail",
    "keyring.backends.null",
    # stdlib pieces imported lazily
    "sqlite3",
    "imaplib",
    "email.mime.text",
    "email.mime.multipart",
    # package modules imported by name at runtime
    "plannerboard.tray",
    "plannerboard.desktop",
    # pywebview backends (loaded by name)
    "webview", "webview.platforms", "webview.platforms.edgechromium", "webview.platforms.winforms",
    "webview.platforms.cocoa", "webview.platforms.gtk",
    "plannerboard.mail.pa_folder",
    "plannerboard.mail.imap",
    "plannerboard.mail.gmail",
    "plannerboard.mail.graph",
    "plannerboard.ai.anthropic_provider",
    "plannerboard.ai.openai_provider",
    "plannerboard.ai.ollama_provider",
    "plannerboard.triage.pipeline",
    "plannerboard.license.client",
    "yaml",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
]
for optional in ("pystray", "PIL", "pdfplumber", "openpyxl", "httpx", "plyer", "clr", "clr_loader", "tzdata"):
    try:
        __import__(optional)
        hiddenimports.append(optional)
    except Exception:
        pass

excludes = ["tkinter", "unittest", "pytest", "test", "tests", "setuptools", "pip", "wheel"]

block_cipher = None

a = Analysis(  # noqa: F821
    [str(ENTRY)],
    pathex=[str(APP_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False if sys.platform == "win32" else True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
