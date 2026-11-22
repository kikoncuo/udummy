#!/bin/zsh
# Copilot. One command:
#   ./run.sh                 starts the engine, then opens the panel. Ctrl+C here (or "Salir") stops both.
#   ./run.sh --check [wav]   silent check: no window, no capture, no sound. A recorded Spanish question goes
#                            through the app's audio path, the engine and the model; prints the answer.
#                            Extra arguments go to the app's self-test (--instruction "..", --shot f.jpg, --model k).
#   ./run.sh --render out.png   draws the panel with sample content into a PNG; nothing appears on screen.
#   ./run.sh --restart       closes the running copy (app and engine) and starts a new one (the panel's restart button uses it).
source "$(dirname "$0")/env.sh"
# the installed app (shell/install.sh) is THE app: one name and one identity for macOS's permissions; else the build folder's
APP="${COPILOT_APP:-$HOME/Applications/Copilot.app}"
[[ -x "$APP/Contents/MacOS/Copilot" ]] || APP="$COPILOT_DIR/shell/build/Copilot.app"
BIN="$APP/Contents/MacOS/Copilot"
mkdir -p "$COPILOT_HOME/logs"
LOG="$COPILOT_HOME/logs/engine.log"
[[ "$1" == "--check" || "$1" == "--render" ]] && LOG="$COPILOT_HOME/logs/engine-check.log"
[[ -x "$PY" ]] || { echo "No Python environment at $PY: run install.sh first."; exit 1; }
if [[ ! -x "$BIN" ]]; then
  echo "The app is missing: $APP"; exit 1
fi
if [[ "$1" == "--restart" ]]; then        # the panel's restart button (via the engine): close the running copy, then start as usual
  shift
  pkill -f "$BIN --url" 2>/dev/null
  for i in {1..40}; do pgrep -f "$BIN --url" >/dev/null || break; sleep 0.25; done
  sleep 0.5; pkill -f "$COPILOT_DIR/engine.py" 2>/dev/null; sleep 0.5
fi
if [[ -z "$1" ]] && pgrep -f "$BIN --url" >/dev/null; then
  PORT=$(grep -o 'COPILOT READY http://127.0.0.1:[0-9]*' "$COPILOT_HOME/logs/engine.log" | tail -1 | grep -o '[0-9]*$')
  [[ -n "$PORT" ]] && /usr/bin/curl -s -m 3 -X POST "http://127.0.0.1:$PORT/show" >/dev/null && { echo "Already open: panel shown."; exit 0; }
  echo "The copilot is already open. Close it first (its Salir button, or Ctrl+C in its terminal)."; exit 1
fi

cleanup() {
  trap - INT TERM EXIT
  [[ -n "$APP_STARTED" ]] && pkill -f "$BIN --url $URL" 2>/dev/null
  [[ -n "$ENGINE" ]] && kill "$ENGINE" 2>/dev/null
}
trap 'cleanup; exit 130' INT TERM
trap cleanup EXIT

# A fresh secret for this run: the panel and the app need it to talk to the engine. Memory only.
export COPILOT_TOKEN="$(/usr/bin/openssl rand -hex 24)"
echo "1/2 Engine: loading Phonon-2.5 and opening the model connection..."
: > "$LOG"
"$PY" "$COPILOT_DIR/engine.py" >> "$LOG" 2>&1 &
ENGINE=$!
i=0
until grep -q "COPILOT READY" "$LOG" 2>/dev/null; do
  if ! kill -0 $ENGINE 2>/dev/null || (( i++ > 120 )); then
    echo "Engine did not start. Last lines of $LOG:"; grep -v transformers "$LOG" | tail -8; exit 1
  fi
  sleep 0.5
done
URL="$(grep -o 'http://127.0.0.1:[0-9]*' "$LOG" | head -1)"

if [[ "$1" == "--check" ]]; then
  WAV="$COPILOT_DIR/test/pregunta_madrid.wav"
  shift
  [[ -n "$1" && "$1" != --* ]] && { WAV="$1"; shift; }
  echo "2/2 Silent check (no window, no capture): ${WAV:t}"
  "$BIN" --url "$URL/?t=$COPILOT_TOKEN" --selftest "$WAV" "$@"
  rc=$?
  [[ $rc == 0 ]] && echo "CHECK OK" || echo "CHECK FAILED ($rc)"
  exit $rc
fi
if [[ "$1" == "--render" ]]; then
  OUT="${2:?usage: run.sh --render out.png}"
  SHOT=$(/usr/bin/curl -s -X POST -H "X-Copilot-Token: $COPILOT_TOKEN" -H "Content-Type: image/jpeg" \
         --data-binary @"$COPILOT_DIR/test/pantalla_sintetica.jpg" "$URL/shot" | sed -E 's/.*"id": "([a-f0-9]+)".*/\1/')
  "$BIN" --render-test "$URL/?t=$COPILOT_TOKEN&demo=${3:-1}&shot=$SHOT${4:+&lightbox=1}" "$OUT"
  exit $?
fi

echo "2/2 Panel: opening Copilot (top right). Press the Preguntar button, or Ctrl+Option+Space."
echo "    If macOS asks to let 'Copilot' record the screen or system audio, allow it."
echo "    Engine log (sizes and timings only, never content): $LOG"
APP_STARTED=1
open -W -n "$APP" --args --url "$URL/?t=$COPILOT_TOKEN"
