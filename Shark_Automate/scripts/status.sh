#!/bin/bash
# ============================================================
# SHARK AUTOMATE — Status Checker
# Check the operational health of all system components
# ============================================================

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║          SHARK AUTOMATE — STATUS             ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# 1. PostgreSQL
if pg_isready >/dev/null 2>&1; then
    echo "  [DATABASE]   PostgreSQL       :  🟢 Online"
else
    echo "  [DATABASE]   PostgreSQL       :  🔴 Offline"
fi

# 2. Redis
if redis-cli ping >/dev/null 2>&1; then
    echo "  [CACHE]      Redis            :  🟢 Online"
else
    echo "  [CACHE]      Redis            :  🔴 Offline"
fi

# 3. Backend (FastAPI)
BACKEND_HEALTH=$(curl -s -m 2 http://localhost:8000/api/health 2>/dev/null)
if echo "$BACKEND_HEALTH" | grep -q '"status":"ok"'; then
    echo "  [BACKEND]    FastAPI (:8000)  :  🟢 Running & Healthy"
else
    if fuser 8000/tcp >/dev/null 2>&1; then
        echo "  [BACKEND]    FastAPI (:8000)  :  🟡 Port busy (not responding to health)"
    else
        echo "  [BACKEND]    FastAPI (:8000)  :  🔴 Stopped"
    fi
fi

# 4. Frontend (Next.js)
if curl -s -m 2 http://localhost:3000 >/dev/null 2>&1; then
    echo "  [FRONTEND]   Next.js (:3000)  :  🟢 Running"
else
    if fuser 3000/tcp >/dev/null 2>&1; then
        echo "  [FRONTEND]   Next.js (:3000)  :  🟡 Starting up / Port active"
    else
        echo "  [FRONTEND]   Next.js (:3000)  :  🔴 Stopped"
    fi
fi

# 5. Telegram Listener
if pgrep -f "telegram_listener.py" >/dev/null 2>&1; then
    echo "  [TELEGRAM]   Signal Listener  :  🟢 Active"
else
    echo "  [TELEGRAM]   Signal Listener  :  🔴 Inactive"
fi

# 6. ngrok tunnel
NGROK_TUNNEL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | \
    python3 -c "import sys,json; d=json.load(sys.stdin); \
urls=[t['public_url'] for t in d.get('tunnels',[]) if t['public_url'].startswith('https')]; \
print(urls[0] if urls else '')" 2>/dev/null)

if [ -n "$NGROK_TUNNEL" ]; then
    echo "  [NGROK]      Public Tunnel    :  🟢 $NGROK_TUNNEL"
    echo "               Webhook URL      :  $NGROK_TUNNEL/api/webhooks/tradingview"
else
    echo "  [NGROK]      Public Tunnel    :  ⚪ Inactive"
fi

echo ""
