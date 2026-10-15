#!/usr/bin/env bash
# ============================================================
# SHASHA_ACT — Stop Script
# Stops the backend and trading operations gracefully
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
PID_FILE="$BACKEND_DIR/app.pid"
PORT=8000

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║          SHASHA_ACT — STOPPING               ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

STOPPED=false

if [ -f "$PID_FILE" ]; then
  pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "► Stopping Shasha_Act process (PID $pid)..."
    kill -TERM "$pid" 2>/dev/null || true
    for _ in {1..15}; do
      if ! kill -0 "$pid" 2>/dev/null; then
        STOPPED=true
        break
      fi
      sleep 1
    done
    if [ "$STOPPED" != "true" ]; then
      echo "  Still running; sending SIGKILL..."
      kill -KILL "$pid" 2>/dev/null || true
      sleep 1
    fi
  fi
  rm -f "$PID_FILE"
fi

# Ensure port 8000 is cleared
echo "► Ensuring port $PORT is free..."
fuser -k "${PORT}/tcp" 2>/dev/null || true
pkill -f "ngrok http" 2>/dev/null || true

echo ""
echo "✅ Shasha_Act has been stopped."
echo ""
