#!/usr/bin/env bash
# Install Email Boss for the current macOS user. No sudo.
#
#   venv       ~/Library/Application Support/EmailBoss/venv
#   data       ~/EmailBoss                        (PB_DATA_ROOT)
#   autostart  ~/Library/LaunchAgents/com.plannerboard.app.plist (RunAtLoad + KeepAlive)
#
# Idempotent: re-running upgrades the package and reloads the agent.
#
#   ./install_macos.sh                 # install from this repo checkout
#   ./install_macos.sh /path/to/wheel  # install a built wheel
#   NO_AUTOSTART=1 ./install_macos.sh  # skip the LaunchAgent
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${1:-$(cd "$HERE/.." && pwd)}"
INSTALL_ROOT="$HOME/Library/Application Support/EmailBoss"
VENV="$INSTALL_ROOT/venv"
DATA_ROOT="${PB_DATA_ROOT:-$HOME/EmailBoss}"
LABEL="com.plannerboard.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${PB_PORT:-8080}"

pick_python() {
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      echo "$c"; return
    fi
  done
  echo "Python 3.11+ not found. Install from python.org or 'brew install python@3.12'." >&2
  exit 1
}
PY="$(pick_python)"
echo "Python  : $PY ($($PY --version 2>&1))"
echo "Install : $INSTALL_ROOT"
echo "Data    : $DATA_ROOT"
echo "Source  : $SOURCE"

mkdir -p "$INSTALL_ROOT" "$DATA_ROOT" "$HOME/Library/LaunchAgents"

[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel --quiet
"$VENV/bin/python" -m pip install --upgrade "$SOURCE" --quiet
"$VENV/bin/python" -m pip install --upgrade "pystray>=0.19" "pillow>=10" --quiet 2>/dev/null || true
VERSION="$("$VENV/bin/python" -c 'import plannerboard; print(plannerboard.__version__)')"
echo "Installed plannerboard $VERSION"

# launcher used by the Dock / Spotlight friendly command
cat > "$INSTALL_ROOT/start-plannerboard.sh" <<SH
#!/usr/bin/env bash
export PB_DATA_ROOT="$DATA_ROOT"
exec "$VENV/bin/python" -m plannerboard.start --no-browser
SH
chmod +x "$INSTALL_ROOT/start-plannerboard.sh"

if [ "${NO_AUTOSTART:-0}" = "1" ]; then
  echo "Autostart: skipped (NO_AUTOSTART=1)"
else
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string>
    <string>-m</string>
    <string>plannerboard.start</string>
    <string>--no-browser</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PB_DATA_ROOT</key><string>$DATA_ROOT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$DATA_ROOT/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$DATA_ROOT/logs/launchd.err.log</string>
</dict>
</plist>
PL
  mkdir -p "$DATA_ROOT/logs"
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  echo "Autostart: LaunchAgent $LABEL loaded"
fi

echo
echo "Done. Open http://127.0.0.1:$PORT/ (the setup wizard runs on first start)."
echo "Uninstall: launchctl bootout gui/\$(id -u)/$LABEL; rm \"$PLIST\"; rm -rf \"$INSTALL_ROOT\""
