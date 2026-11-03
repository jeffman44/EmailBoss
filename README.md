# Email Boss

A local, single-user project and inbox board. Email comes in through the
provider you already use (Power Automate drop folder, IMAP, Gmail, Microsoft
365), gets triaged onto cards, and files itself onto projects. Stages, fields,
vocabulary and prompts come from a profession template, so the same app runs a
mechanical contractor's quote ladder, a law firm's matters or a recruiter's
searches.

Optional AI (your own Anthropic, OpenAI-compatible or local Ollama key) handles
classification, summaries, project matching tie-breaks and draft replies.
The AI only ever writes drafts. The built-in mail client (folders, reading pane,
composer) can send a message a person writes, but only after "Allow sending"
is switched on; it ships off.

Licensed per user against a small hosted license server, with 14 days of
offline grace.

## Repository layout

```
product/
  VERSION                 release version (1.1.0); must match app/plannerboard/__init__.py
  Makefile / tasks.ps1    test / run / build / serve-license targets (Unix / Windows)
  DESIGN.md               design contract every module follows
  app/                    the desktop app (package: plannerboard)
    plannerboard/         Flask app, SQLite store, mail / triage / ai / license layers
    scripts/              installers, PyInstaller spec, release scripts
    tests/                pytest suite (python -m pytest -q)
  license_server/         hosted portal + device API (FastAPI, SQLite, Ed25519)
  docs/                   operator and user documentation (see below)
```

Docs: [RELEASE_NOTES](docs/RELEASE_NOTES.md), [ARCHITECTURE](docs/ARCHITECTURE.md), [EMAIL_PROVIDERS](docs/EMAIL_PROVIDERS.md),
[POWER_AUTOMATE](docs/POWER_AUTOMATE.md), [AI_SETUP](docs/AI_SETUP.md),
[PROFESSIONS](docs/PROFESSIONS.md), [LICENSING](docs/LICENSING.md),
[PRIVACY](docs/PRIVACY.md), [EULA](docs/EULA.md), [RELEASE_CHECKLIST](docs/RELEASE_CHECKLIST.md).

## Quickstart (development)

Python 3.11 or newer.

```bash
cd app
python -m pip install -e ".[dev,attachments,http]"
python -m pytest -q                      # all green before you start
PB_LICENSE_DEV=1 python -m plannerboard --port 8080 --no-tray
```

Open `http://127.0.0.1:8080`. The three-screen setup wizard (you and your email, your work, done) runs until
`<data root>/plannerboard.json` exists. The data root is `$PB_DATA_ROOT` or
`~/EmailBoss`; the database, config, mail cache, backups and logs all live
there and never inside the install.

`make run`, `make test`, `make check` (or `.\tasks.ps1 run|test|check` on
Windows) wrap the same commands. `make check` also runs the banned-literal and
em-dash scan that the release checklist requires.

Useful environment variables: `PB_DATA_ROOT`, `PB_CONFIG`, `PB_PORT`,
`PB_DB_PATH`, `PB_FORCE_SETUP=1`, `PB_LICENSE_DEV=1`, `PB_AI_API_KEY`,
`PB_IMAP_PASSWORD`, `PB_RESOURCE_DIR` (packagers only).

## Installing for a user

The installers create a venv, install the package, set `PB_DATA_ROOT` and
register an autostart. No admin rights.

| platform | command | venv | autostart |
|---|---|---|---|
| Windows | `powershell -ExecutionPolicy Bypass -File app\scripts\install_windows.ps1` | `%LOCALAPPDATA%\EmailBoss\venv` | Task Scheduler task "Email Boss" at logon (`pythonw -m plannerboard.start`) + Start Menu shortcut |
| macOS | `app/scripts/install_macos.sh` | `~/Library/Application Support/EmailBoss/venv` | LaunchAgent `com.plannerboard.app` |
| Linux | `app/scripts/install_linux.sh` | `~/.local/share/plannerboard/venv` | `systemd --user` unit `plannerboard.service` |

Each script is idempotent (re-run to upgrade) and takes an optional wheel path
as its first argument. `uninstall_windows.ps1` reverses the Windows install and
keeps the data folder unless `-PurgeData` is passed.

## Running the license server

```bash
cd license_server
python -m pip install -e ".[test]"
TRIAL_ENABLED=1 uvicorn licserver.main:app --reload --port 8600
python -m pytest -q
```

The first account created becomes admin when `LIC_ADMIN_EMAILS` is empty.
Point the app at it in Settings > License (`server_url`), or set
`license.server_url` in `plannerboard.json`. Production deployment (systemd,
reverse proxy, Stripe webhook, key rotation) is in `license_server/README.md`;
the protocol and threat model are in `docs/LICENSING.md`.

The checked-in `license_server/keys/DEV_ONLY_*.pem` pair is for development
and tests. Rotate it before anything ships.

## Cutting a release

Short form; the full list is `docs/RELEASE_CHECKLIST.md`.

1. Rotate the license signing key if this release ships a new public key
   (`license_server/scripts/gen_keys.py`, then update `DEFAULT_PUBLIC_KEY_PEM`
   in `app/plannerboard/license/client.py`).
2. Bump `VERSION`, `app/plannerboard/__init__.py:__version__` and
   `app/pyproject.toml:version` together.
3. `make test` and `make check` green.
4. `make build` (Unix) or `.\tasks.ps1 build` (Windows). This runs the tests,
   builds the wheel, runs PyInstaller with `app/scripts/build_pyinstaller.spec`
   and zips `app/dist/EmailBoss/` as `EmailBoss-<version>-<os>-<arch>.zip`
   with a SHA256.
5. Smoke the installer and the bundle on a clean VM; set the
   `DOWNLOAD_URL_*` variables on the license server to the new files.

## License

Proprietary. All rights reserved. See `LICENSE` and `docs/EULA.md`.
