# Shared environment for the copilot scripts (sourced). No key is read or set here.
COPILOT_DIR="$(cd "$(dirname "${(%):-%x}")" && pwd)"
# everything the app keeps: settings, logs, spend ledger, downloaded models, and its Python environment (made by install.sh)
export COPILOT_HOME="${COPILOT_HOME:-$HOME/Library/Application Support/Copilot}"
[[ -f "$COPILOT_DIR/env.local.sh" ]] && source "$COPILOT_DIR/env.local.sh"   # optional local overrides (never published)
PY="${COPILOT_PY:-$COPILOT_HOME/venv/bin/python}"
export TOKENIZERS_PARALLELISM=false
