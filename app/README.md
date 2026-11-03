# Email Boss

A local, single-user project and inbox board. Domain vocabulary, stages, fields
and prompts come from a profession template plus the user's own config.

## Run

```
pip install -e .
python -m plannerboard --port 8080          # same as python -m plannerboard.start
```

Open `http://127.0.0.1:8080`. On first run the setup wizard is served until
`<DATA_ROOT>/plannerboard.json` exists.

## Layout

- `plannerboard/config.py` - config load / merge / write, keyring secrets
- `plannerboard/paths.py` - resource dir vs. writable data root
- `plannerboard/db.py` + `schema.sql` - SQLite store (WAL, per-thread connections)
- `plannerboard/workflows.py` - stage roster with roles, ladder derivation
- `plannerboard/ladder.py` - task arming, ladder advance, SLA tasks
- `plannerboard/profession.py` + `professions/*.json` - shipped templates
- `plannerboard/api/` - Flask blueprints
- `plannerboard/mail/` - mail providers (drop folder, IMAP, Gmail, Graph) writing PA-shaped records
- `plannerboard/triage/` - email -> card / auto-commit pipeline
- `plannerboard/ai/` - bring-your-own-model layer, prompt registry, assistant pass (drafts only)
- `plannerboard/license/` - signed license + activation client, offline grace
- `plannerboard/start.py` + `__main__.py` - entry point: lock, migrate, threads, serve
- `scripts/` - installers (Windows / macOS / Linux), PyInstaller spec, release scripts

`create_app()` builds the `Runtime` (`app.extensions["pb"]`) and wires every layer
onto it: `license.middleware`, `ai.wiring.install`, `mail.runner.wire_runtime`,
then the blueprints. Under pytest (or `PB_TESTING=1`) no background thread starts.

## CLI

```
plannerboard --port 8080 --data-root ~/EmailBoss
plannerboard --dev-license          # skip license checks while developing
plannerboard --no-threads --no-tray # serve only
```

## Environment

`PB_DATA_ROOT`, `PB_CONFIG`, `PB_PORT`, `PB_DB_PATH`, `PB_FORCE_SETUP=1`,
`PB_IMAP_PASSWORD`, `PB_AI_API_KEY`, `PB_LICENSE_DEV=1`, `PB_RESOURCE_DIR` (override the
read-only resource folder; packagers and tests only).

## Packaging

```
scripts/install_windows.ps1 | install_macos.sh | install_linux.sh   # venv + autostart, no admin
scripts/build_release.ps1 | build_release.sh                        # tests, wheel, PyInstaller onedir, zip
scripts/build_pyinstaller.spec                                      # resources land in _internal/plannerboard/
```

`paths.RESOURCE_DIR` resolves to the package directory from source, and to
`<bundle>/_internal/plannerboard/` when `sys.frozen` is set. The data root is
never inside the bundle. See `../README.md` and `../docs/RELEASE_CHECKLIST.md`.

## Tests

```
python -m pytest -q
python -m compileall -q plannerboard
```

`tests/SMOKE_LOG.md` records a scripted run against a live server (setup through
triage, approve, assistant pass, license and restart).
