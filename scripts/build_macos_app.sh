#!/usr/bin/env bash
# Build Polymarket Weather.app and a local DMG for beginner distribution.
# Does not embed .env, DBs, logs, or credentials. Signing credentials are
# optional via environment variables and are never required to be in-repo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${APP_VERSION:-0.1.4}"
DIST_DIR="${DIST_DIR:-$ROOT/dist}"
BUILD_DIR="${BUILD_DIR:-$ROOT/build}"
SPEC="$ROOT/packaging/macos/polymarket_weather.spec"
RAW_ARCH="$(uname -m)"
if [[ "$RAW_ARCH" == "arm64" || "$RAW_ARCH" == "aarch64" ]]; then
  ARCH="arm64"
elif [[ "$RAW_ARCH" == "x86_64" || "$RAW_ARCH" == "amd64" ]]; then
  ARCH="x86_64"
else
  echo "ERROR: Unsupported build host architecture: $RAW_ARCH" >&2
  exit 1
fi

APP_NAME="Polymarket Weather"
APP_PATH="${APP_PATH:-$DIST_DIR/${APP_NAME}.app}"
DMG_PATH="${DMG_PATH:-$DIST_DIR/${APP_NAME}-${VERSION}-macos-${ARCH}.dmg}"

echo "==> Building ${APP_NAME} v${VERSION} for arch=${ARCH}"
echo "    root=${ROOT}"

# Ensure packaging deps without dropping developer tooling (ruff/pytest).
echo "==> Syncing dev + packaging extras"
uv sync --extra dev --extra packaging
if ! uv run python -c "import PyInstaller" 2>/dev/null; then
  echo "ERROR: PyInstaller missing after uv sync --extra dev --extra packaging" >&2
  exit 1
fi
if [[ "$(uname -s)" == "Darwin" ]]; then
  if ! uv run python -c "import AppKit" 2>/dev/null; then
    echo "ERROR: AppKit (pyobjc-framework-Cocoa) missing — status menu would be broken" >&2
    exit 1
  fi
  echo "==> AppKit import OK"
fi

rm -rf \
  "$BUILD_DIR/polymarket_weather" \
  "$DIST_DIR/${APP_NAME}" \
  "$APP_PATH" \
  "$DMG_PATH" \
  "${DMG_PATH}.sha256"
mkdir -p "$DIST_DIR" "$BUILD_DIR"

echo "==> Running PyInstaller"
uv run pyinstaller \
  --noconfirm \
  --clean \
  --distpath "$DIST_DIR" \
  --workpath "$BUILD_DIR" \
  "$SPEC"

if [[ ! -d "$APP_PATH" ]]; then
  # Some PyInstaller layouts nest the .app differently.
  if [[ -d "$DIST_DIR/${APP_NAME}/${APP_NAME}.app" ]]; then
    mv "$DIST_DIR/${APP_NAME}/${APP_NAME}.app" "$APP_PATH"
  fi
fi

if [[ ! -d "$APP_PATH" ]]; then
  echo "ERROR: expected app bundle at $APP_PATH" >&2
  find "$DIST_DIR" -maxdepth 3 -type d -name "*.app" -print >&2 || true
  exit 1
fi

BINARY_PATH="$APP_PATH/Contents/MacOS/${APP_NAME}"
ACTUAL_ARCHES="$(lipo -archs "$BINARY_PATH")"
if [[ "$ACTUAL_ARCHES" != "$ARCH" ]]; then
  echo "ERROR: packaged executable architecture mismatch: expected=$ARCH actual=$ACTUAL_ARCHES" >&2
  exit 1
fi
echo "==> Packaged executable architecture verified: $ACTUAL_ARCHES"

echo "==> Scanning bundle for forbidden materials"
FORBIDDEN_MATCHES="$(
  find "$APP_PATH" \( \
    -name '.env' -o -name 'config.env' -o -name '*.db' -o -name '*.sqlite3' \
    -o -name '*private*key*' -o -name '*.pem' \
  \) -print 2>/dev/null || true
)"
if [[ -n "${FORBIDDEN_MATCHES}" ]]; then
  echo "ERROR: forbidden files found in app bundle:" >&2
  echo "$FORBIDDEN_MATCHES" >&2
  exit 1
fi
# Grep for obvious secret assignments in bundled text (best-effort).
if grep -R --binary-files=without-match -E 'POLYMARKET_PRIVATE_KEY=0x|TELEGRAM_BOT_TOKEN=[0-9]+:' "$APP_PATH" >/dev/null 2>&1; then
  echo "ERROR: secret-like material detected in app bundle" >&2
  exit 1
fi

# Optional Developer ID signing (never required; never embeds credentials).
if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  echo "==> Codesigning with identity from CODESIGN_IDENTITY"
  codesign --force --deep --options runtime --sign "$CODESIGN_IDENTITY" "$APP_PATH"
  codesign --verify --deep --strict "$APP_PATH"
else
  echo "==> Ad-hoc codesign for local use"
  codesign --force --deep --sign - "$APP_PATH" || true
fi

if [[ -n "${NOTARIZE_PROFILE:-}" ]]; then
  echo "==> Notarization profile detected (NOTARIZE_PROFILE); zip + submit"
  ZIP_PATH="$DIST_DIR/${APP_NAME}-${VERSION}-signed.zip"
  ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"
  xcrun notarytool submit "$ZIP_PATH" --keychain-profile "$NOTARIZE_PROFILE" --wait
  xcrun stapler staple "$APP_PATH"
else
  echo "==> Skipping notarization (set NOTARIZE_PROFILE to enable)"
fi

echo "==> Creating DMG"
STAGE="$BUILD_DIR/dmg-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP_PATH" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$STAGE" \
  -ov \
  -format UDZO \
  "$DMG_PATH"

echo "==> Generating SHA-256 checksum"
(cd "$(dirname "$DMG_PATH")" && shasum -a 256 "$(basename "$DMG_PATH")" > "${DMG_PATH}.sha256")

echo "==> Artifact summary"
echo "APP:  $APP_PATH"
echo "DMG:  $DMG_PATH"
echo "ARCH: $ARCH"
du -sh "$APP_PATH" "$DMG_PATH" || true
codesign -dv --verbose=2 "$APP_PATH" 2>&1 | head -20 || true
file "$BINARY_PATH" || true
lipo -archs "$BINARY_PATH"

echo "==> Build complete"
echo "Install: open the DMG and drag ${APP_NAME}.app to Applications."
echo "Gatekeeper (unsigned/ad-hoc): right-click → Open on first launch, or"
echo "  xattr -dr com.apple.quarantine \"/Applications/${APP_NAME}.app\""
