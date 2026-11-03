#!/usr/bin/env bash
# Install Email Boss for the current Linux user. No root.
#
#   venv       ~/.local/share/plannerboard/venv
#   data       ~/EmailBoss                       (PB_DATA_ROOT)
#   autostart  ~/.config/systemd/user/plannerboard.service (enabled + started)
#   launcher   ~/.local/share/applications/plannerboard.desktop
#
# Idempotent: re-running upgrades the package and restarts the unit.
#
#   ./install_linux.sh                 # install from this repo checkout
#   ./install_linux.sh /path/to/wheel  # install a built wheel
#   NO_AUTOSTART=1 ./install_linux.sh  # skip the systemd unit
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${1:-$(cd "$HERE/.." && pwd)}"
INSTALL_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/plannerboard"
VENV="$INSTALL_ROOT/venv"
DATA_ROOT="${PB_DATA_ROOT:-$HOME/EmailBoss}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/plannerboard.service"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
PORT="${PB_PORT:-8080}"

pick_python() {
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      echo "$c"; return
    fi
  done
  echo "Python 3.11+ not found (apt install python3 python3-venv, or dnf install python3)." >&2
  exit 1
}
PY="$(pick_python)"
echo "Python  : $PY ($($PY --version 2>&1))"
echo "Install : $INSTALL_ROOT"
echo "Data    : $DATA_ROOT"
echo "Source  : $SOURCE"

mkdir -p "$INSTALL_ROOT" "$DATA_ROOT" "$UNIT_DIR" "$APPS_DIR"

[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel --quiet
"$VENV/bin/python" -m pip install --upgrade "$SOURCE" --quiet
VERSION="$("$VENV/bin/python" -c 'import plannerboard; print(plannerboard.__version__)')"
echo "Installed plannerboard $VERSION"

cat > "$INSTALL_ROOT/start-plannerboard.sh" <<SH
#!/usr/bin/env bash
export PB_DATA_ROOT="$DATA_ROOT"
exec "$VENV/bin/python" -m plannerboard.start --no-browser --no-tray
SH
chmod +x "$INSTALL_ROOT/start-plannerboard.sh"

cat > "$APPS_DIR/plannerboard.desktop" <<DT
[Desktop Entry]
Type=Application
Name=Email Boss
Comment=Local project and inbox board
Exec=xdg-open http://127.0.0.1:$PORT/
Icon=view-list-details
Terminal=false
Categories=Office;ProjectManagement;
DT

if [ "${NO_AUTOSTART:-0}" = "1" ]; then
  echo "Autostart: skipped (NO_AUTOSTART=1)"
elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  cat > "$UNIT" <<UN
[Unit]
Description=Email Boss (local project and inbox board)
After=network-online.target

[Service]
Type=simple
Environment=PB_DATA_ROOT=$DATA_ROOT
ExecStart=$VENV/bin/python -m plannerboard.start --no-browser --no-tray
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UN
  systemctl --user daemon-reload
  systemctl --user enable plannerboard.service >/dev/null
  systemctl --user restart plannerboard.service
  echo "Autostart: systemd --user unit plannerboard.service enabled and started"
  echo "           (run 'loginctl enable-linger $USER' to keep it up without a login session)"
else
  echo "Autostart: systemd --user not available; start with $INSTALL_ROOT/start-plannerboard.sh"
fi

echo
echo "Done. Open http://127.0.0.1:$PORT/ (the setup wizard runs on first start)."
echo "Uninstall: systemctl --user disable --now plannerboard; rm \"$UNIT\" \"$APPS_DIR/plannerboard.desktop\"; rm -rf \"$INSTALL_ROOT\""
