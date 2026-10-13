#!/bin/bash
# ============================================================
# SHARK AUTOMATE — Stop Script
# Run this to easily stop all background processes
# ============================================================

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║         SHARK AUTOMATE — STOPPING            ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

echo "► Stopping Backend (Port 8000)..."
fuser -k 8000/tcp 2>/dev/null || echo "  Backend was not running."

echo "► Stopping Frontend (Port 3000)..."
fuser -k 3000/tcp 2>/dev/null || echo "  Frontend was not running."

echo "► Stopping ngrok (Port 4040)..."
fuser -k 4040/tcp 2>/dev/null || echo "  ngrok was not running."

echo "► Stopping Telegram listener..."
pkill -f "telegram_listener.py" 2>/dev/null || echo "  Telegram listener was not running."

echo ""
echo "✅ All Shark Automate processes (Backend, Frontend, Telegram) stopped!"
echo ""
