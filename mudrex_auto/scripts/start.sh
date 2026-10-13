#!/usr/bin/env bash
# Start the trading bot in the background (REAL or PAPER based on .env). Logs: data/bot.log   PID: data/bot.pid
set -euo pipefail
cd "$(dirname "$0")/.."

PID_FILE="data/bot.pid"
LOG_FILE="data/bot.log"

if [ ! -f .env ]; then
  echo "No .env found. Copy .env.example to .env and fill it in first." >&2
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

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Already running (pid $(cat "$PID_FILE")). Use scripts/stop.sh first, or scripts/status.sh to inspect."
  exit 0
fi
rm -f "$PID_FILE"

mkdir -p data
touch "$LOG_FILE"
start_line=$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)

CURRENT_MODE=$("$PYTHON_BIN" -c 'from app.config import load_env_file; print(load_env_file(".env").get("TRADING_MODE", "PAPER").upper())' 2>/dev/null || echo "PAPER")

if [ "$CURRENT_MODE" = "REAL" ]; then
  if [ -t 0 ]; then
    echo "========================================================================"
    echo "REAL MODE — LIVE MUDREX FUTURES ORDERS WILL BE PLACED"
    echo "NO STOP-LOSS ENABLED — liquidation and substantial loss are possible."
    echo "The margin budget is an allocation, not a maximum loss."
    echo "========================================================================"
    echo "Type exactly 'START LIVE TRADING' to start REAL trading in background:"
    read -r confirmation
    if [ "$confirmation" != "START LIVE TRADING" ]; then
      echo "Confirmation not given. Aborting startup."
      exit 1
    fi
  fi
  setsid "$PYTHON_BIN" -m app.main run --yes </dev/null >>"$LOG_FILE" 2>&1 &
else
  export TRADING_MODE=PAPER
  export ENABLE_REAL_TRADING=false
  setsid "$PYTHON_BIN" -m app.main run </dev/null >>"$LOG_FILE" 2>&1 &
fi
pid=$!
disown "$pid" 2>/dev/null || true
echo "$pid" > "$PID_FILE"

# Give it time to start; fail loudly if it dies during startup.
for _ in $(seq 1 20); do
  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "The bot exited during startup. Last log lines:" >&2
    tail -n 25 "$LOG_FILE" >&2 || true
    rm -f "$PID_FILE"
    exit 1
  fi
  if tail -n +$((start_line + 1)) "$LOG_FILE" 2>/dev/null | grep -q "telegram.connected"; then
    break
  fi
done

DASH_PID_FILE="data/dashboard.pid"
if ! curl -s "http://localhost:8500/api/health" >/dev/null 2>&1; then
  touch data/dashboard.log
  setsid "$PYTHON_BIN" -m app.main dashboard --port 8500 </dev/null >>data/dashboard.log 2>&1 &
  dash_pid=$!
  disown "$dash_pid" 2>/dev/null || true
  echo "$dash_pid" > "$DASH_PID_FILE"
  sleep 1
fi

if [ "$CURRENT_MODE" = "REAL" ]; then
  echo "Mudrex REAL bot started (pid $pid)."
else
  echo "Mudrex PAPER bot started (pid $pid)."
fi
echo "  Log:       tail -f data/bot.log"
echo "  Status:    ./scripts/status.sh"
echo "  Stop:      scripts/stop.sh"
echo "  Dashboard: http://localhost:8500"
