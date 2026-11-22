#!/bin/zsh
# Builds the shell and installs it as THE app: ~/Applications/Copilot.app (name "Copilot", its icon, id app.copilot.panel).
# One app, one name, one identity: what macOS lists in Privacy is exactly what is run. Each install is a new build: macOS asks
# again for screen and system audio (ad-hoc signature) unless COPILOT_SIGN_IDENTITY names a certificate.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"; DIR="$(cd "$HERE/.." && pwd)"
source "$DIR/env.sh"                                  # COPILOT_HOME (and local overrides, if any)
zsh "$HERE/build.sh"
APPS="${COPILOT_APP_DIR:-$HOME/Applications}"; DEST="$APPS/Copilot.app"
mkdir -p "$APPS"; rm -rf "$DEST"
cp -R "$HERE/build/Copilot.app" "$DEST"
P=/usr/libexec/PlistBuddy; PL="$DEST/Contents/Info.plist"
$P -c "Set :CFBundleIdentifier app.copilot.panel" "$PL"
$P -c "Set :CFBundleName Copilot" "$PL"; $P -c "Set :CFBundleDisplayName Copilot" "$PL"
$P -c "Delete :CFBundleIconFile" "$PL" 2>/dev/null || true; $P -c "Add :CFBundleIconFile string Copilot" "$PL"
$P -c "Delete :CopilotDir" "$PL" 2>/dev/null || true; $P -c "Add :CopilotDir string $DIR" "$PL"
HOMEDIR="$COPILOT_HOME"
$P -c "Delete :CopilotHome" "$PL" 2>/dev/null || true; $P -c "Add :CopilotHome string $HOMEDIR" "$PL"
$P -c "Set :NSAudioCaptureUsageDescription 'Copilot escucha el audio que reproduce este Mac para responder a lo último que se ha dicho cuando tú lo pidas. El audio se queda en memoria.'" "$PL"
mkdir -p "$DEST/Contents/Resources"; cp "$HERE/Copilot.icns" "$DEST/Contents/Resources/Copilot.icns"
codesign --force --sign "${COPILOT_SIGN_IDENTITY:--}" "$DEST" 2>/dev/null
touch "$DEST"
echo "installed: $DEST"
