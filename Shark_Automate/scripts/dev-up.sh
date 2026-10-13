#!/usr/bin/env bash
# One-command local startup (without Docker): postgres + redis must be running.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"

echo "[1/4] applying database migrations…"
(cd "$ROOT/backend" && source .venv/bin/activate && alembic upgrade head)

echo "[2/4] starting backend on :8000…"
(cd "$ROOT/backend" && "$ROOT/backend/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port 8000) &
BACKEND_PID=$!

echo "[3/4] starting frontend on :3000…"
(cd "$ROOT/frontend" && npm run dev) &
FRONTEND_PID=$!

echo "[4/4] up."
echo "  Dashboard:      http://localhost:3000"
echo "  API:            http://localhost:8000/api/health"
echo "  Webhook URL:    http://<your-host>:8000/api/webhooks/tradingview"
trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null" EXIT
wait
