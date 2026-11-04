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
ENTRY = SPEC_DIR / "launch.py"

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

binaries = []

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
    "plannerboard.updater",
    "plannerboard.api.update_api",
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

if sys.platform == "win32":
    hiddenimports += [
        # Windows Credential Manager backend for keyring
        "win32ctypes.pywin32.pywintypes", "win32ctypes.pywin32.win32cred", "win32ctypes.core",
        # pywebview: Edge WebView2 through pythonnet (loaded by name from webview.guilib)
        "webview.platforms.edgechromium", "webview.platforms.winforms",
        "clr", "clr_loader", "pythonnet",
        # pystray picks its backend with importlib at runtime
        "pystray._win32", "pystray._base",
    ]
elif sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]
else:
    hiddenimports += ["webview.platforms.gtk"]
hiddenimports += ["webview", "webview.platforms"]

# Packages whose DLLs / data / .NET assemblies / dist-info are found by path or
# metadata, not by import: collect them whole. On Windows this is what ships
# Python.Runtime.dll, ClrLoader.dll, Microsoft.Web.WebView2.Core.dll,
# Microsoft.Web.WebView2.WinForms.dll and WebView2Loader.dll (webview/lib/**),
# webview/js/*, and the keyring / pystray entry-point metadata.
try:
    from PyInstaller.utils.hooks import collect_all, copy_metadata
    _collect = ["webview", "pystray", "keyring", "sqlcipher3"]
    if sys.platform == "win32":
        _collect += ["pythonnet", "clr_loader", "win32ctypes"]
    for _pkg in _collect:
        try:
            _d, _b, _h = collect_all(_pkg)
            datas += _d
            binaries += _b
            hiddenimports += _h
        except Exception as _e:  # package not installed in this build env
            print(f"spec: collect_all({_pkg}) skipped: {_e}")
    for _dist in ("keyring", "pywebview", "pystray"):
        try:
            datas += copy_metadata(_dist)
        except Exception:
            pass
except ImportError:
    pass  # py_compile / static check without PyInstaller

for optional in ("PIL", "pdfplumber", "openpyxl", "httpx", "plyer", "tzdata"):
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
    binaries=binaries,
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
