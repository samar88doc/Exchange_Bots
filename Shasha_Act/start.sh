#!/usr/bin/env bash
# ============================================================
# SHASHA_ACT — Startup Script
# Starts the backend API, WebSocket market feed, and UI console
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
LOGS_DIR="$BACKEND_DIR/logs"
PID_FILE="$BACKEND_DIR/app.pid"
ENV_FILE="$BACKEND_DIR/.env"
PORT=8000

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║          SHASHA_ACT — STARTING UP            ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ Error: $ENV_FILE not found."
  echo "   Copy $BACKEND_DIR/.env.example to $ENV_FILE and configure your credentials."
  exit 1
fi

if [ ! -d "$BACKEND_DIR/.venv" ]; then
  echo "❌ Error: Virtual environment not found at $BACKEND_DIR/.venv"
  echo "   Create it first: cd $BACKEND_DIR && python3.11 -m venv .venv && pip install -e '.[dev]'"
  exit 1
fi

mkdir -p "$LOGS_DIR"

# Clean up any old process on port 8000
echo "► Checking port $PORT..."
fuser -k "${PORT}/tcp" 2>/dev/null || true
if [ -f "$PID_FILE" ]; then
  old_pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    kill -TERM "$old_pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi
sleep 1

# Apply schema migrations
echo "► Applying database migrations..."
cd "$BACKEND_DIR"
"$BACKEND_DIR/.venv/bin/alembic" upgrade head

# Launch Uvicorn in the background
echo "► Starting server on port $PORT..."
nohup setsid "$BACKEND_DIR/.venv/bin/uvicorn" app.main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --no-access-log \
  >> "$LOGS_DIR/app.log" 2>&1 &

APP_PID=$!
disown "$APP_PID" 2>/dev/null || true
echo "$APP_PID" > "$PID_FILE"

# Wait for readiness
echo "► Waiting for service readiness..."
READY=false
for i in {1..20}; do
  if curl -s "http://127.0.0.1:${PORT}/api/ready" 2>/dev/null | grep -q '"ready":true'; then
    READY=true
    break
  fi
  sleep 1
done

if [ "$READY" = "true" ]; then
  DASHBOARD_TOKEN=$(grep -E '^DASHBOARD_API_TOKEN=' "$ENV_FILE" | cut -d '=' -f2- | tr -d ' "' || echo "")
  WEBHOOK_TOKEN=$(grep -E '^TRADINGVIEW_WEBHOOK_TOKEN=' "$ENV_FILE" | cut -d '=' -f2- | tr -d ' "' || echo "")

  echo ""
  echo "══════════════════════════════════════════════════════════"
  echo "  ✅ SHASHA_ACT IS RUNNING (PID $APP_PID)"
  echo "══════════════════════════════════════════════════════════"
  echo "  • Dashboard UI:     http://localhost:${PORT}/"
  echo "  • Markets Feed:     http://localhost:${PORT}/#markets"
  echo "  • Dashboard Token:  $DASHBOARD_TOKEN"
  echo "  • Webhook URL:      http://localhost:${PORT}/api/webhooks/tradingview/${WEBHOOK_TOKEN}"
  echo "  • Logs:             tail -f $LOGS_DIR/app.log"
  echo "══════════════════════════════════════════════════════════"
  echo ""
else
  echo "⚠️ Server started (PID $APP_PID), but readiness check timed out."
  echo "   Check logs: tail -n 50 $LOGS_DIR/app.log"
fi
