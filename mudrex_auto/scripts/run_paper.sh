#!/usr/bin/env bash
# Starts the app in PAPER mode. TRADING_MODE is forced to PAPER for this process,
# so a REAL setting in .env cannot be picked up by this launcher.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
  echo "No .env found. Run scripts/setup.sh or: cp .env.example .env" >&2
  exit 1
fi
PYTHON_BIN="python"
if [ -d .venv ]; then
  . .venv/bin/activate
  if [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
  fi
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
fi
export TRADING_MODE=PAPER
export ENABLE_REAL_TRADING=false
exec "$PYTHON_BIN" -m app.main run
