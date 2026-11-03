# Release checklist

Work top to bottom. Nothing ships until every box is ticked.

## 1. Keys

- [x] 2026-11-03: production signing pair generated; public half embedded as `DEFAULT_PUBLIC_KEY_PEM`, dev key only trusted on the dev channel. Private half is NOT in the repo (see license_server/keys/README.md).
- [ ] GitHub repository secret `EB_RELEASE_SIGNING_KEY` holds the production private key PEM
  (the one matching `DEFAULT_PUBLIC_KEY_PEM`). The windows job fails a tag release without it.
  After the first release, download `update.json` + `update.json.sig` from the release page and
  confirm `python app/scripts/make_update_manifest.py` output verifies (the workflow does this too).
- [ ] Decide whether this release rotates the license signing key. Rotate
      when the previous private key may have been exposed, when the shipped
      public key is still `DEV_ONLY_public.pem`, or on the yearly schedule.
- [ ] If rotating: `cd license_server && python scripts/gen_keys.py --out
      /etc/licserver --name prod-<yyyy>`; paste the public PEM into
      `DEFAULT_PUBLIC_KEY_PEM` in `app/plannerboard/license/client.py`;
      follow "Key rotation" in `license_server/README.md` (point
      `LIC_PRIVATE_KEY_PATH` at the new key, restart, re-sign live licenses).
- [ ] Confirm `license_server/keys/DEV_ONLY_private.pem` is not what
      production runs on (`GET /healthz` reports `key_id`; compare).
- [ ] Rotate any provider keys used in CI or demo configs (`PB_AI_API_KEY`,
      test IMAP app passwords).

## 2. Version

- [ ] Bump `VERSION`, `app/plannerboard/__init__.py:__version__` and
      `app/pyproject.toml:version` to the same value. `build_release.*`
      refuses to run when `VERSION` and `__version__` differ.
- [ ] Update `docs/LICENSING.md` if any `/v1/*` or `/api/license*` endpoint
      or payload changed. Update `PRIVACY.md` "Changes" if what the app sends
      or stores changed.
- [ ] Write release notes in `docs/RELEASE_NOTES.md`: user-facing changes,
      migration notes (scope re-consent when mail scopes changed), known
      issues.

## 3. Tests and hygiene

- [ ] `cd app && python -m compileall -q plannerboard`
- [ ] `cd app && python -m pytest -q` all green (includes the UI smoke test
      and the end-to-end server test).
- [ ] `cd license_server && python -m pytest -q` all green.
- [ ] `make check` (or `.\tasks.ps1 check`): no banned literals from the
      reference install and no em dashes in `.md .py .html .yaml .json .ps1
      .sh` outside `tests/` and `screens/`.
- [ ] Shipped profession templates validate:
      `python -c "import json,glob,plannerboard.profession as p; [print(f, p.validate_template(json.load(open(f)))) for f in glob.glob('plannerboard/professions/*.json')]"`
      prints `[]` for each.
- [ ] Fresh clone, fresh venv, `pip install .` (not `-e`): `python -m
      plannerboard --no-threads --no-tray --no-browser` serves the wizard
      and `/manifest.webmanifest` returns 200. Catches missing
      `package-data` entries.

## 4. Build

- [ ] Windows: `.\tasks.ps1 build` on a Windows machine with Python 3.11+.
      Produces `app/dist/EmailBoss-<version>-windows-x64.zip` and prints
      its SHA256.
- [ ] macOS: `make build` on an Apple-silicon machine (and Intel if
      supported). Code-sign and notarize the `EmailBoss.app` or the
      onedir binary if distributing outside a tester group; set
      `codesign_identity` in the spec.
- [ ] Linux: `make build` on the oldest glibc you support (build in a
      container of that distribution).
- [ ] Each bundle contains `_internal/plannerboard/{static,professions,
      ai/prompts,schema.sql}` (the build script asserts this).
- [ ] Wheel built (`app/dist/plannerboard-<version>-py3-none-any.whl`) for
      the venv installers.
- [ ] Record the SHA256 of every artifact in the release notes.
- [ ] Release assets include `update.json` and `update.json.sig` (signed update manifest).
  On a machine running the previous version: Settings > General > Check for updates now shows
  the new version; Update now, then Restart and install, ends with the new version running.
  Work through `docs/WINDOWS_RUNTIME_CHECK.md` on a real Windows box before the first public release.

## 5. Smoke on a clean VM (per platform)

Use a VM or a fresh user account with no Python installed for the bundle,
and one with Python installed for the venv installer.

- [ ] Bundle: unzip, run `EmailBoss`. Wizard opens in the browser. Create
      a config with the `general` profession, drop-folder mail provider,
      no AI. Board renders; `/api/health` is `ok`; `license.status` is
      `unlicensed` (not `dev`).
- [ ] Installer: `install_windows.ps1` / `install_macos.sh` /
      `install_linux.sh`. Confirm: venv created, `PB_DATA_ROOT` set, Start
      Menu shortcut or desktop entry present, autostart registered (Task
      Scheduler task "Email Boss", LaunchAgent, systemd user unit),
      board reachable after a logout/login or reboot.
- [ ] Re-run the installer: no errors, same version, no duplicate task or
      shortcut.
- [ ] Activate a real license from the production server; `license.status`
      becomes `ok`. Deactivate from the portal; next heartbeat flips the app
      to read-only (`PUT /api/projects/<id>` answers 402).
- [ ] Mail: connect one real provider (IMAP app password is the fastest).
      Send yourself an email; a triage card appears within one poll.
      Approve it as a new project; correspondence shows the message.
- [ ] Mail client: open the MAIL view; folders, unread counts and the list
      load; open a message (inline image renders, attachment downloads);
      Reply opens the composer with the quoted original and no Send button;
      Save draft lands in the mailbox's Drafts folder; File puts it on a
      project; Triage stages it. Turn on "Allow sending", send yourself a
      reply, and confirm it arrives threaded under the original and appears
      in Sent. Turn sending back off and confirm `/api/mail/client/send`
      answers 403.
- [ ] AI (if shipping AI changes): paste a key, Test succeeds, one card
      classifies, Draft reply produces text and nothing is sent.
- [ ] Uninstall (`uninstall_windows.ps1`, or the printed commands on macOS /
      Linux): task, shortcut and venv gone; data folder kept.
- [ ] Upgrade path: install the previous release, create data, install this
      release over it. Database migrates, projects intact, no duplicate
      triage cards.

## 6. Publish

- [ ] Upload artifacts; set `DOWNLOAD_URL_WINDOWS`, `DOWNLOAD_URL_MACOS`,
      `DOWNLOAD_URL_LINUX` on the license server and restart it.
- [ ] Tag the commit `v<version>`.
- [ ] Publish release notes with SHA256 values.
- [ ] Verify the portal Download page links resolve and the hashes match.
- [ ] Rotate `DEV_ONLY` keys in the repo if they were ever used outside
      tests.

## Rollback

Keep the previous release's artifacts and download URLs. To roll back: point
the `DOWNLOAD_URL_*` variables at the previous files and restart the license
server. Databases created by the new release are forward-migrated only;
restore `<data_root>/backups/<latest before upgrade>.db` on affected
machines.
