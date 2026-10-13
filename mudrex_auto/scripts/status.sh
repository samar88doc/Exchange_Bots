#!/usr/bin/env bash
# Show whether the bot is running, plus the database summary.
cd "$(dirname "$0")/.."
PID_FILE="data/bot.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Bot: RUNNING (pid $(cat "$PID_FILE"))"
else
  echo "Bot: NOT running"
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
echo "Dashboard: http://localhost:8500"
"$PYTHON_BIN" -m app.main status
