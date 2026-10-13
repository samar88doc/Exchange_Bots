#!/usr/bin/env bash
# Start the lightweight web dashboard.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON_BIN="python"
if [ -d .venv ]; then
  . .venv/bin/activate
  if [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
  fi
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
fi
PORT="${DASHBOARD_PORT:-8500}"
HOST="${DASHBOARD_HOST:-0.0.0.0}"
echo "Starting Mudrex Signal Trader Dashboard on http://localhost:$PORT ..."
exec "$PYTHON_BIN" -m app.main dashboard --host "$HOST" --port "$PORT"
