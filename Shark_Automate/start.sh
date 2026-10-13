#!/bin/bash
# ============================================================
# SHARK AUTOMATE — One-click startup (Backend + Frontend + Telegram)
# Run this after every laptop restart
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
NGROK_BIN="$HOME/.local/bin/ngrok"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║         SHARK AUTOMATE — STARTING UP         ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Kill any old processes on our ports & scripts
echo "► Cleaning up old processes..."
fuser -k 8000/tcp 2>/dev/null || true
fuser -k 3000/tcp 2>/dev/null || true
fuser -k 4040/tcp 2>/dev/null || true
pkill -f "telegram_listener.py" 2>/dev/null || true
sleep 1

# Start Backend
echo "► Starting Backend (FastAPI)..."
cd "$BACKEND_DIR"
source .venv/bin/activate
mkdir -p "$BACKEND_DIR/logs"
nohup setsid "$BACKEND_DIR/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port 8000 \
    > "$BACKEND_DIR/logs/startup.log" 2>&1 &
BACKEND_PID=$!
disown $BACKEND_PID
echo "  Backend PID: $BACKEND_PID"

echo "  Waiting for backend..."
for i in {1..15}; do
    if curl -s http://localhost:8000/api/health > /dev/null 2>&1; then
        echo "  ✅ Backend ready!"
        break
    fi
    sleep 1
done

# Start Telegram Listener
echo ""
echo "► Starting Telegram Listener (VIP Crypto Signal Jemal)..."
cd "$BACKEND_DIR"
source .venv/bin/activate
nohup setsid "$BACKEND_DIR/.venv/bin/python" scripts/telegram_listener.py \
    > "$BACKEND_DIR/logs/telegram_listener.log" 2>&1 &
TELEGRAM_PID=$!
disown $TELEGRAM_PID
echo "  Telegram Listener PID: $TELEGRAM_PID"
echo "  Logs: tail -f backend/logs/telegram_listener.log"

# Start Frontend
echo ""
echo "► Starting Frontend (Next.js)..."
cd "$FRONTEND_DIR"
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
nohup setsid npm run dev > /tmp/frontend.log 2>&1 &
FRONTEND_PID=$!
disown $FRONTEND_PID
echo "  Frontend PID: $FRONTEND_PID"

# Start ngrok
echo ""
echo "► Starting ngrok tunnel..."
if [ -x "$NGROK_BIN" ]; then
    nohup setsid "$NGROK_BIN" http 8000 --log=stdout > /tmp/ngrok.log 2>&1 &
    NGROK_PID=$!
    disown $NGROK_PID

    echo "  Waiting for ngrok URL..."
    WEBHOOK_URL=""
    for i in {1..15}; do
        sleep 1
        NGROK_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | \
            python3 -c "import sys,json; d=json.load(sys.stdin); \
urls=[t['public_url'] for t in d.get('tunnels',[]) if t['public_url'].startswith('https')]; \
print(urls[0] if urls else '')" 2>/dev/null)
        if [ -n "$NGROK_URL" ]; then
            WEBHOOK_URL="${NGROK_URL}/api/webhooks/tradingview"
            break
        fi
    done
else
    echo "  ngrok not found, skipping tunnel."
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════════════╗"
echo "║                    ✅  ALL SYSTEMS STARTED                           ║"
echo "╠══════════════════════════════════════════════════════════════════════╣"
echo "║  Dashboard  →  http://localhost:3000                                 ║"
echo "║  Backend    →  http://localhost:8000                                 ║"
echo "║  Telegram   →  Listening to 'Vip Crypto Signal Jemal' (Auto-Trading) ║"
echo "╠══════════════════════════════════════════════════════════════════════╣"
echo "║  🔗 TRADINGVIEW WEBHOOK URL:                                         ║"
if [ -n "$WEBHOOK_URL" ]; then
    echo "║  $WEBHOOK_URL"
else
    echo "║  (Local mode / ngrok tunnel inactive)                                ║"
fi
echo "╚══════════════════════════════════════════════════════════════════════╝"
echo ""
