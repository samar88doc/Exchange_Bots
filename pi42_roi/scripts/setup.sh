#!/usr/bin/env bash
# One-time local setup: virtualenv, dependencies, database schema, tests.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON_BIN=""
if [ -d .venv ]; then
  PYTHON_BIN=".venv/bin/python"
elif command -v python3.13 >/dev/null 2>&1; then
  PYTHON_BIN="python3.13"
elif [ -x "$HOME/miniconda3/bin/python3.13" ]; then
  PYTHON_BIN="$HOME/miniconda3/bin/python3.13"
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON_BIN="python3.12"
else
  PYTHON_BIN="python3"
fi

if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv
fi
. .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements-dev.txt
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example (edit it locally; never commit it)."
fi
python -m app.main init-db
python -m pytest -q
echo "Setup complete. Next: edit .env, run: python -m app.main auth, then scripts/start.sh"
