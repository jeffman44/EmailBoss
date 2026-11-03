"""
Where things live: RESOURCE_DIR (ships with the program) vs DATA_ROOT (the
user's writable tree).

Ported from the reference install's config.py lines 36-62 (the frozen-build
split). Conflating the two roots is the bug that breaks a packaged build: a
onedir install is replaced wholesale on upgrade, which would take the database
with it if it lived inside the bundle.

Resolution (DESIGN.md section 2):
    config  : $PB_CONFIG  ->  <data_root>/plannerboard.json
    data    : $PB_DATA_ROOT  ->  ~/EmailBoss
"""

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
_MEIPASS = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "_MEIPASS", "") else None

PACKAGE_DIR = Path(__file__).resolve().parent


def _resolve_resource_dir():
    """Where the read-only resources (static/, professions/, ai/prompts/,
    schema.sql) live.

    Source / pip install : the package directory itself.
    PyInstaller onedir   : build_pyinstaller.spec copies the resources to
                           <bundle>/_internal/plannerboard/ (sys._MEIPASS is
                           the _internal folder in PyInstaller 6, the bundle
                           root in older releases). Fall back to the folder
                           next to the executable so a hand-assembled bundle
                           still runs, then to _MEIPASS itself.
    PB_RESOURCE_DIR      : explicit override for packagers and tests.
    """
    env = os.environ.get("PB_RESOURCE_DIR")
    if env:
        return Path(env).expanduser()
    if not FROZEN:
        return PACKAGE_DIR
    candidates = []
    if _MEIPASS:
        candidates.append(_MEIPASS / "plannerboard")
    exe_dir = Path(sys.executable).resolve().parent
    candidates += [exe_dir / "_internal" / "plannerboard", exe_dir / "plannerboard"]
    for cand in candidates:
        if (cand / "schema.sql").is_file():
            return cand
    return _MEIPASS if _MEIPASS else exe_dir


RESOURCE_DIR = _resolve_resource_dir()

CONFIG_FILENAME = "plannerboard.json"
DB_FILENAME = "plannerboard.db"
WORKFLOWS_FILENAME = "workflows.json"

STATIC_DIR = RESOURCE_DIR / "static"
PROFESSIONS_DIR = RESOURCE_DIR / "professions"
PROMPTS_DIR = RESOURCE_DIR / "ai" / "prompts"
SCHEMA_PATH = RESOURCE_DIR / "schema.sql"


def default_data_root():
    """$PB_DATA_ROOT, else ~/EmailBoss. Never inside the bundle."""
    env = os.environ.get("PB_DATA_ROOT")
    if env:
        return Path(env).expanduser()
    return Path.home() / "EmailBoss"


def config_path(data_root=None):
    """$PB_CONFIG wins; otherwise <data_root>/plannerboard.json."""
    env = os.environ.get("PB_CONFIG")
    if env:
        return Path(env).expanduser()
    root = Path(data_root) if data_root else default_data_root()
    return root / CONFIG_FILENAME


class Paths:
    """Resolved writable paths for one data root. Built by config.Config."""

    def __init__(self, data_root, mail_cache=None, backups=None, db_file=None):
        self.data_root = Path(data_root).expanduser()
        self.mail_cache = Path(mail_cache).expanduser() if mail_cache else self.data_root / "mail_cache"
        self.backups = Path(backups).expanduser() if backups else self.data_root / "backups"
        env_db = os.environ.get("PB_DB_PATH")
        if env_db:
            self.db_file = Path(env_db).expanduser()
        elif db_file:
            self.db_file = Path(db_file).expanduser()
        else:
            self.db_file = self.data_root / DB_FILENAME
        self.workflows_file = self.data_root / WORKFLOWS_FILENAME
        self.workflow_backups = self.data_root / "backups" / "workflows"
        self.logs = self.data_root / "logs"
        self.project_files = self.data_root / "project_files"
        self.lock_file = self.data_root / "board.lock"

    def ensure(self):
        """Create every writable folder. Returns the list created."""
        made = []
        for d in (self.data_root, self.mail_cache, self.mail_cache / "inbox",
                  self.mail_cache / "sent", self.mail_cache / "attachments",
                  self.backups, self.workflow_backups, self.logs, self.project_files):
            try:
                d.mkdir(parents=True, exist_ok=True)
                made.append(str(d))
            except Exception:
                pass
        return made

    def as_dict(self):
        return {
            "data_root": str(self.data_root),
            "mail_cache": str(self.mail_cache),
            "backups": str(self.backups),
            "db_file": str(self.db_file),
            "workflows_file": str(self.workflows_file),
            "logs": str(self.logs),
            "project_files": str(self.project_files),
        }
