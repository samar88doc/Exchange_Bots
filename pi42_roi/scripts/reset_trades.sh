#!/usr/bin/env bash
# Delete PAPER trades only. Signals, REAL trades, Telegram session and config are kept.
# A backup of the database is written to data/backups/ before anything is deleted.
#   scripts/reset_trades.sh          -> dry run (shows what would be deleted)
#   scripts/reset_trades.sh --yes    -> actually delete
set -euo pipefail
cd "$(dirname "$0")/.."

PID_FILE="data/bot.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "The bot is running. Stop it first: scripts/stop.sh" >&2
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
if [ "${1:-}" = "--yes" ]; then
  "$PYTHON_BIN" -m app.main reset-paper --yes
else
  "$PYTHON_BIN" -m app.main reset-paper || true
fi
