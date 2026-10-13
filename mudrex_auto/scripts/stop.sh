#!/usr/bin/env bash
# Stop the bot started by scripts/start.sh. Sends SIGTERM and waits; SIGKILL only as a last resort.
set -uo pipefail
cd "$(dirname "$0")/.."

PID_FILE="data/bot.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "Not running (no pid file)."
  exit 0
fi

pid="$(cat "$PID_FILE")"
if ! kill -0 "$pid" 2>/dev/null; then
  echo "Process $pid is not running. Removing stale pid file."
  rm -f "$PID_FILE"
  exit 0
fi

# Safety: only signal a process that is really this app, never an unrelated pid reused by the OS.
if ! tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "app.main run"; then
  echo "pid $pid is not the trader process. Refusing to signal it. Delete $PID_FILE if this is wrong." >&2
  exit 1
fi

echo "Stopping bot (pid $pid)..."
kill -TERM "$pid"
for _ in $(seq 1 30); do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    break
  fi
  sleep 1
done

if kill -0 "$pid" 2>/dev/null; then
  echo "Still running after 30s; sending SIGKILL." >&2
  kill -KILL "$pid" 2>/dev/null || true
  sleep 1
  rm -f "$PID_FILE"
  echo "Killed."
else
  echo "Stopped bot."
fi

DASH_PID_FILE="data/dashboard.pid"
if [ -f "$DASH_PID_FILE" ]; then
  dash_pid="$(cat "$DASH_PID_FILE")"
  if kill -0 "$dash_pid" 2>/dev/null; then
    echo "Stopping dashboard (pid $dash_pid)..."
    kill -TERM "$dash_pid" 2>/dev/null || true
  fi
  rm -f "$DASH_PID_FILE"
fi

