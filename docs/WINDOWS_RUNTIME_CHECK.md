# Windows runtime check

Written from a read-through of the code on Linux (no Windows box was available),
backed by unit tests where the logic is platform independent. Part 1 lists what
was found and what was changed. Part 2 is the checklist to run on a real Windows
10/11 laptop with the Setup.exe from CI. Tick each line; anything that fails
goes back as a bug with `%USERPROFILE%\EmailBoss\logs\server.log` attached.

## Part 1. Findings from the code review

### Fixed in this change

1. **The desktop window never started (any platform).** `desktop.wait_for_http`
   probed `/api/health` without the session cookie; the API gate answers 401, the
   probe treated that as "no answer" and `run()` gave up after 20 s with
   "engine did not answer". The same bug made `DesktopState.running()` always
   false, so a second launch tried to start another engine, lost the lock file
   race and exited silently. Now a 4xx from a live server counts as up
   (`test_wait_for_http_counts_auth_refusal_as_alive`).
2. **Settings lost on every launch.** The window used a new ephemeral port each
   time and the board keeps its settings in `localStorage`, which is per origin
   including the port. The shell now reuses the last port (`<data_root>\desktop_port`)
   when it is free and only falls back to a new one. Note werkzeug's `make_server`
   calls `sys.exit(1)` on a taken port, so the fallback catches `SystemExit`.
3. **WebView2 missing.** With `gui="edgechromium"` and no runtime, pywebview raises;
   a windowed exe (`console=False`) then exits with no visible message. `desktop.ensure_webview2()`
   reads the Evergreen runtime's `pv` value from the three EdgeUpdate registry
   locations before the engine starts and shows a native message box with a Yes/No
   to open https://developer.microsoft.com/microsoft-edge/webview2/ . Any other
   failure to create the window is caught in `open_window_safe` and shown the same way.
   `PB_SKIP_WEBVIEW2_CHECK=1` bypasses the probe.
4. **Restart from inside the app (desktop mode).** `/api/restart` on Windows starts
   a child and `os._exit(0)`s. The child attached to the dying engine through
   `desktop.json` (it was checked before `PB_RESTART_WAIT` was handled), or lost the
   `board.lock` race and quit. `start.py` now handles `PB_RESTART_WAIT` first: it waits
   for the old engine to stop answering, then retries the lock for up to 15 s. The old
   tray helper is terminated before the exit so the new process's icon is the only one.
5. **PyInstaller spec.** `hiddenimports` now includes the pywebview Edge backend,
   `clr`, `clr_loader`, `pythonnet`, `pystray._win32` (pystray picks its backend with
   `importlib`), the keyring Windows backend's `win32ctypes` modules, and the new
   `plannerboard.updater` / `api.update_api`. `collect_all` ships the non-Python files
   that are found by path: `Python.Runtime.dll`, `ClrLoader.dll`,
   `Microsoft.Web.WebView2.Core.dll`, `Microsoft.Web.WebView2.WinForms.dll`,
   `WebView2Loader.dll` (under `webview/lib`), `webview/js`, and the keyring and
   pystray dist-info (entry points). cocoa and gtk backends are only listed on their
   own platforms. If a DLL is still missing at runtime the symptom is the
   "could not open its window" message (see check 3 below).
6. **Installer.** `PrivilegesRequiredOverridesAllowed=dialog` removed: an admin-mode
   install would make every later silent update raise a UAC prompt (Inno reuses the
   previous install's privilege level). Added a `/RELAUNCH=1` run entry so the in-app
   update reopens the app (`skipifsilent` on the normal launch entry meant a silent
   update left nothing running), and a `taskkill` of leftover `EmailBoss.exe` at the
   install step (Restart Manager closes the window but the tray helper is the same exe
   with no window).
7. **Update install.** `Setup.exe /SILENT /CLOSEAPPLICATIONS /RELAUNCH=1` is started
   detached, then the app closes its window (engine, mail threads and tray follow in
   `desktop.run`'s cleanup); a watchdog thread `os._exit(0)`s after 8 s so a stuck window
   cannot block the installer.

### Reviewed, left as is (decide or verify on Windows)

- **`os.chmod(..., 0o600)` is a no-op on Windows** (only the read-only bit is
  honoured). `desktop.json` (holds the per-launch session token) and
  `plannerboard.json` rely on the default ACL of `%USERPROFILE%`, which is private to
  the user. If the data root is moved to a shared folder or a synced drive, the token
  is readable by others. The token is per launch and only valid against a loopback
  server, so the exposure is small, but it is not "0600".
- **Paths with spaces.** Every `subprocess.Popen` here takes a list, so spaces in
  `C:\Users\First Last\...` or the install dir are safe; the tray, restart and
  installer launches were checked for this. `Paths.ensure()` uses `pathlib`. The Inno
  `[Run]` / `[Icons]` entries are quoted. Verify with a user name that has a space
  (check 9).
- **Autostart.** The `autostart` task writes `{userstartup}\Email Boss.lnk` pointing at
  `EmailBoss.exe` with no arguments, which is correct (the frozen exe forces `--desktop`).
  Consequence: at sign-in the full window opens. There is no "start minimised to the
  tray" mode; if that is wanted it is a new flag (`--background`: engine + tray, no
  window) and a `Parameters:` on that shortcut.
- **Second launch** goes through `desktop.json` (url + token, health-probed) and opens
  a second window on the running engine. A stale file left by a crash is harmless
  (the probe fails, a fresh engine starts and overwrites it). With the fix above this
  now works; it needs a real test (check 6).
- **Tray "Open board"** opens the system browser on `/auth/<token>`, i.e. a second
  UI in the default browser next to the native window. Works, but it is not "focus the
  window". The tray polls `/api/health` with the session token in `PB_SESSION_TOKEN`
  (environment of the helper process, visible to same-user processes only).
- **Single instance lock** uses `msvcrt.locking` on byte 0 of `board.lock`; OK for a
  per-user file. A second `EmailBoss.exe --tray-only` helper is a separate process and
  does not take the lock (intended).
- **keyring on Windows** stores secrets in Credential Manager, which limits one
  secret to 2560 bytes (about 1280 characters of UTF-16). Short IMAP app passwords and
  API keys fit. A Gmail or Microsoft OAuth token blob that includes a long id token or
  several scopes might not; `set_secret` reports the failure instead of writing a file.
  Test a real Gmail and Microsoft sign-in (check 10).
- **pywebview constants.** `webview.FOLDER_DIALOG`, `OPEN_DIALOG`, `SAVE_DIALOG` are
  deprecated in pywebview 6 in favour of `webview.FileDialog.*`; they still work and
  print a DeprecationWarning to the log. Pin or migrate when pywebview drops them.
- **The Setup.exe is not code signed.** SmartScreen will show "Windows protected your
  PC" for first installs. The in-app update is not affected by that (the app verifies the
  Ed25519 signature and sha256 itself, and the installer is started by an already-trusted
  process), but a first-time download will be. Buying an Authenticode certificate (or
  Azure Trusted Signing) and adding a `signtool` step is the fix.
- **pythonnet / .NET.** pywebview's WinForms host runs on the .NET Framework 4.x that ships with
  Windows 10/11; no extra install. Build with Python 3.12 (pythonnet 3.0.x wheels); a newer
  Python needs a pythonnet release with a wheel for it, or the bundle builds and then fails at
  `import clr`.
- **Antivirus.** Unsigned PyInstaller bundles are sometimes quarantined by Defender.
  If `EmailBoss.exe` disappears after install, restore it and add the install folder as an
  exclusion; code signing reduces this.

## Part 2. Checklist for a real Windows box

Use a clean Windows 10 or 11 user profile (a VM is fine) with the CI `EmailBoss-<version>-Setup.exe`.

### Install
- [ ] 1. Run Setup.exe as a normal user: no UAC prompt, installs to `%LOCALAPPDATA%\Programs\EmailBoss`, wizard offers the desktop icon and "Start Email Boss when I sign in" tasks.
- [ ] 2. SmartScreen: note the wording shown (expected, unsigned). "More info > Run anyway" works.

### First launch
- [ ] 3. **No WebView2:** on a VM without the runtime (or temporarily set `PB_SKIP_WEBVIEW2_CHECK` unset and rename the registry key `HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}` and the HKCU twin), launch: a message box titled "Email Boss needs Microsoft Edge WebView2" appears; Yes opens the Microsoft page; no process is left running in Task Manager.
- [ ] 4. With WebView2 present: a native window opens within about 5 s, shows the setup wizard (first run) and the title bar says Email Boss. No console window flashes. `logs\server.log` shows `[desktop] engine on http://127.0.0.1:<port>/ (window only)` and no tracebacks.
- [ ] 5. If the window does not open: the message box "Email Boss could not open its window" shows the exception name. Record it. Typical causes: a missing DLL from `_internal\webview\lib` (spec `collect_all`), `pythonnet` not loading, antivirus.
- [ ] 6. **Second launch:** with the window open, start `EmailBoss.exe` again (and the desktop shortcut): a second window opens on the same engine; there is still exactly one `EmailBoss.exe` engine process plus the tray helper; closing one window leaves the other working.
- [ ] 7. Close the last window: all `EmailBoss.exe` processes (engine and tray) exit within a few seconds; `desktop.json` is gone from `%USERPROFILE%\EmailBoss`.
- [ ] 8. **Settings persist across launches:** change a Display or Interface setting, close, relaunch: the setting is still there, and `desktop_port` in the data folder holds the same port as the log line.

### Environment
- [ ] 9. **User name or install path with a space** (for example a profile `C:\Users\Test User`, or install to `C:\Program Files\Email Boss` via `/DIR=`): app starts, tray appears, restart (check 12) works, Setup in-app update works.
- [ ] 10. **Mail sign-in:** connect Gmail (built-in sign-in) and Microsoft; both finish and the credential survives a relaunch (Credential Manager entry "EmailBoss"). Connect IMAP with an app password. If a token is rejected by CredWrite for size, record which provider.
- [ ] 11. File dialogs from the page work: pick a folder (mail cache), pick files (attachment), save a file (export). External links open in the default browser, not inside the window.

### Tray and restart
- [ ] 12. The tray dot is green; hover shows "Email Boss: OK". Settings > restart (or tray "Restart server"): the window closes and a new one opens within about 15 s, exactly one tray icon, no "another instance holds the lock" in `server.log`, the data is intact.
- [ ] 13. Tray "Open board" opens the default browser signed in (expected second UI). "Quit (icon only)" removes the icon and leaves the window working.

### Autostart
- [ ] 14. Reinstall with the autostart task ticked; sign out and in: `Email Boss.lnk` is in `shell:startup`, the app opens once (not twice), and the tray icon appears. Decide whether a full window at sign-in is acceptable (see Part 1).
- [ ] 15. Uninstall: shortcut, install folder and tray process are gone; `%USERPROFILE%\EmailBoss` (your data) is kept.

### Update path (needs two published builds, N and N+1, with `update.json` and `update.json.sig` on the latest release)
- [ ] 16. On build N: Settings > General > Check for updates now says "Email Boss N+1 is available." and the quiet banner appears at the top of the board with Update now and Later. Later hides it until the next version.
- [ ] 17. Update now downloads (banner shows progress text), then asks "Restart and install". Nothing installs before that click. `%USERPROFILE%\EmailBoss\updates\EmailBoss-<N+1>-Setup.exe` exists.
- [ ] 18. Restart and install: the window closes, a silent installer runs (small progress window is normal), then Email Boss N+1 opens by itself; Settings > General shows N+1; no UAC prompt; the data and settings are intact.
- [ ] 19. Negative: edit the downloaded Setup.exe (append a byte) before clicking Restart and install: the banner reports the installer no longer matches and nothing runs. Block the update host in the hosts file: Check for updates reports "could not reach the update server" and the app keeps working.
- [ ] 20. `GET /api/health` (from the app window's devtools or the tray token) contains `update.available`.

### Antivirus and SmartScreen
- [ ] 21. Note any Defender quarantine of `EmailBoss.exe` or the installer during checks 1 to 18 (and which definition version).
