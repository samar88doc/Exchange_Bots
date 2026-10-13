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
if [ -f .env ]; then
  ENV_PORT=$("$PYTHON_BIN" -c 'from app.config import load_env_file; print(load_env_file(".env").get("DASHBOARD_PORT", "8501"))' 2>/dev/null || echo "8501")
fi
PORT="${DASHBOARD_PORT:-${ENV_PORT:-8501}}"
HOST="${DASHBOARD_HOST:-0.0.0.0}"
echo "Starting Mudrex Signal Trader Dashboard on http://localhost:$PORT ..."
exec "$PYTHON_BIN" -m app.main dashboard --host "$HOST" --port "$PORT"
