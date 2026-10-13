#!/bin/bash
# ============================================================
# SHARK AUTOMATE — Reset Trades Script
# Run this to clear all test trades, positions, and signals,
# and reset the paper trading balance.
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║      SHARK AUTOMATE — RESETTING TRADES       ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

cd "$BACKEND_DIR"
source .venv/bin/activate
"$BACKEND_DIR/.venv/bin/python" scripts/reset_trades.py

echo ""
echo "✅ Done! You can now start the bot fresh using ./start.sh"
echo ""
