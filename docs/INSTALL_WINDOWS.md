# Installing on Windows

`EmailBoss-<version>-Setup.exe` (from the GitHub release) is a per-user
installer: no admin rights, installs to `%LOCALAPPDATA%\Programs\EmailBoss`,
adds a Start Menu entry and, if ticked, a desktop shortcut and start-at-sign-in.
Running it again over an older version upgrades in place.

The app is a native window (Edge WebView2). The WebView2 runtime ships with
Windows 10/11; if it is missing the app says so and links to Microsoft's
installer. No port to open and no browser is used.

Your data lives in `%USERPROFILE%\EmailBoss` (database, mail cache, backups);
uninstalling leaves it in place.

Developers: `app\scripts\install_windows.ps1` installs from a wheel into a venv
instead (`-Port` / `-TaskName` for server mode side by side with another copy).
