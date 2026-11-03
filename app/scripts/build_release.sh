#!/usr/bin/env bash
# Build a Email Boss release on macOS / Linux:
#   1. run the test suite
#   2. build the wheel (dist/*.whl)
#   3. build the PyInstaller onedir bundle (dist/EmailBoss/)
#   4. zip the bundle as dist/EmailBoss-<version>-<os>-<arch>.zip
#
#   ./scripts/build_release.sh            # everything
#   SKIP_TESTS=1 ./scripts/build_release.sh
#   SKIP_BUNDLE=1 ./scripts/build_release.sh   # wheel only
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$(cd "$HERE/.." && pwd)"
ROOT="$(cd "$APP/.." && pwd)"
cd "$APP"

PY="${PYTHON:-python3}"
VERSION="$(cat "$ROOT/VERSION" 2>/dev/null || "$PY" -c 'import plannerboard; print(plannerboard.__version__)')"
PKG_VERSION="$("$PY" -c 'import plannerboard; print(plannerboard.__version__)')"
if [ "$VERSION" != "$PKG_VERSION" ]; then
  echo "VERSION file ($VERSION) and plannerboard.__version__ ($PKG_VERSION) differ. Fix before releasing." >&2
  exit 1
fi
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
echo "Email Boss $VERSION ($OS-$ARCH)"

if [ "${SKIP_TESTS:-0}" != "1" ]; then
  "$PY" -m compileall -q plannerboard
  "$PY" -m pytest -q
fi

rm -rf build dist
# Release channel: PB_LICENSE_DEV / --dev-license are inert in what we ship.
CHANNEL_FILE="$APP/plannerboard/_channel.py"
cp "$CHANNEL_FILE" "$CHANNEL_FILE.devbak"
trap 'mv -f "$CHANNEL_FILE.devbak" "$CHANNEL_FILE"' EXIT
sed -i.tmp 's/^CHANNEL = "dev"/CHANNEL = "release"/' "$CHANNEL_FILE" && rm -f "$CHANNEL_FILE.tmp"
grep -q 'CHANNEL = "release"' "$CHANNEL_FILE" || { echo "could not set release channel" >&2; exit 1; }
"$PY" -m pip install --quiet --upgrade build pyinstaller >/dev/null
"$PY" -m build --wheel --outdir dist >/dev/null
echo "wheel : $(ls dist/*.whl)"

if [ "${SKIP_BUNDLE:-0}" != "1" ]; then
  "$PY" -m PyInstaller --noconfirm --clean --distpath dist --workpath build scripts/build_pyinstaller.spec
  for f in static professions ai/prompts schema.sql; do
    test -e "dist/EmailBoss/_internal/plannerboard/$f" || { echo "missing resource in bundle: $f" >&2; exit 1; }
  done
  ZIP="dist/EmailBoss-$VERSION-$OS-$ARCH.zip"
  (cd dist && zip -qr "$(basename "$ZIP")" EmailBoss)
  echo "bundle: $ZIP"
  echo "sha256: $(shasum -a 256 "$ZIP" | cut -d' ' -f1)"
fi
echo "done"
