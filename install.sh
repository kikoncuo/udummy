#!/bin/zsh
# Copilot installer: one command, run from this folder.  ./install.sh
# Checks the Mac, makes a Python environment inside the app's data folder, installs the packages in requirements.txt,
# builds the small macOS app and puts it in ~/Applications. Nothing is installed system-wide. Uninstall: ./uninstall.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/env.sh"
say() { print -P "%B$1%b"; }
say "1/4 Checking this Mac"
[[ "$(uname -m)" == "arm64" ]] || { echo "Copilot needs a Mac with Apple silicon (M1 or newer)."; exit 1; }
v=$(sw_vers -productVersion); [[ "$(printf '%s\n14.2\n' "$v" | sort -V | head -1)" == "14.2" ]] || { echo "Copilot needs macOS 14.2 or newer (this Mac: $v)."; exit 1; }
xcrun --find swiftc >/dev/null 2>&1 || { echo "Apple's command line tools are missing. Run:  xcode-select --install   then run ./install.sh again."; exit 1; }
PYBIN=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v $c >/dev/null 2>&1 && $c -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then PYBIN=$(command -v $c); break; fi
done
if [[ -z "$PYBIN" ]] && command -v uv >/dev/null 2>&1; then uv python install 3.12 >/dev/null && PYBIN=$(uv python find 3.12); fi
[[ -n "$PYBIN" ]] || { echo "Python 3.11 or newer is needed: install it from https://www.python.org/downloads/ (or 'brew install python@3.12') and run ./install.sh again."; exit 1; }
say "2/4 Python environment in $COPILOT_HOME/venv"
mkdir -p "$COPILOT_HOME/logs" "$COPILOT_HOME/models"
[[ -x "$COPILOT_HOME/venv/bin/python" ]] || "$PYBIN" -m venv "$COPILOT_HOME/venv"
"$COPILOT_HOME/venv/bin/python" -m pip install -q --upgrade pip
"$COPILOT_HOME/venv/bin/python" -m pip install -q -r "$DIR/requirements.txt"
say "3/4 Building the app"
zsh "$DIR/shell/install.sh" >/dev/null
say "4/4 Done"
echo "Open Copilot from your Applications folder (~/Applications/Copilot.app), or drag it to the Dock."
echo "First steps: Ajustes (the gear) > paste your OpenRouter key, or download a local model. macOS will ask to let Copilot record the screen and system audio: allow both."
