#!/bin/zsh
# Removes the app and everything it keeps (settings, logs, downloaded models, its Python environment). Your OpenRouter key stays in the
# keychain unless you add --key. This folder (the source) is not touched.
DIR="$(cd "$(dirname "$0")" && pwd)"; source "$DIR/env.sh"
pkill -f "Copilot.app/Contents/MacOS/Copilot --url" 2>/dev/null; sleep 1; pkill -f "$DIR/engine.py" 2>/dev/null
rm -rf "${COPILOT_APP_DIR:-$HOME/Applications}/Copilot.app" "$COPILOT_HOME"
[[ "$1" == "--key" ]] && security delete-generic-password -a openrouter -s copilot-openrouter-key >/dev/null 2>&1
echo "Copilot removed."
