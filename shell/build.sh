#!/bin/zsh
# Builds the shell ONCE. Do not run this for interface or engine changes: those live in ui/ and
# *.py and need no rebuild. Every rebuild changes the ad-hoc signature, and macOS then treats the
# app as a new one for the audio permission.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/build/Copilot.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -swift-version 5 -target arm64-apple-macos14.2 \
  -framework AppKit -framework CoreAudio -framework AudioToolbox -framework AVFoundation \
  -framework ScreenCaptureKit -framework Carbon -framework WebKit \
  "$HERE/main.swift" -o "$APP/Contents/MacOS/Copilot"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
codesign --force --sign "${COPILOT_SIGN_IDENTITY:--}" "$APP" 2>/dev/null
{ shasum -a 256 "$HERE/main.swift" "$APP/Contents/MacOS/Copilot"; swiftc --version 2>&1 | head -1;
  codesign -dv "$APP" 2>&1 | grep -E "Identifier|Signature|CDHash|TeamIdentifier"; } > "$HERE/build/build.sha256"
echo "built: $APP"
