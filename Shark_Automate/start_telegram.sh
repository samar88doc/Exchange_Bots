#!/bin/bash
# ============================================================
# SHARK AUTOMATE — Telegram Listener Startup
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

echo "╔══════════════════════════════════════════════╗"
echo "║      SHARK AUTOMATE — TELEGRAM LISTENER      ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

cd "$BACKEND_DIR"
source .venv/bin/activate

# Check if API_ID is configured
if ! grep -q "TELEGRAM_API_ID=[a-zA-Z0-9]" "$SCRIPT_DIR/.env"; then
    echo "⚠️ TELEGRAM_API_ID is missing in .env file."
    echo "Please get it from https://my.telegram.org and add it to .env"
    exit 1
fi

echo "► Starting Telegram listener..."
# We don't use nohup here because on the first run, Telethon needs interactive input
# (Phone number + Telegram login code).
"$BACKEND_DIR/.venv/bin/python" scripts/telegram_listener.py
