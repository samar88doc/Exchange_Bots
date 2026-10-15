#!/usr/bin/env bash
# ============================================================
# SHASHA_ACT — Reset Trades Script
# Clears simulated paper trades, orders, fills, and events
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║      SHASHA_ACT — RESETTING TRADES           ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

CONFIRM=false
if [ "${1:-}" = "--yes" ] || [ "${1:-}" = "-y" ]; then
  CONFIRM=true
else
  read -r -p "⚠️  Are you sure you want to clear all Shasha_Act trades, orders, fills, and events? (y/N): " response
  if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
    CONFIRM=true
  fi
fi

if [ "$CONFIRM" != "true" ]; then
  echo "Cancelled. No trades were removed."
  exit 0
fi

cd "$BACKEND_DIR"
"$BACKEND_DIR/.venv/bin/python" scripts/reset_trades.py --yes

echo ""
echo "Done! You can start clean using: ./start.sh"
echo ""
