#!/usr/bin/env bash
# Run database migrations (the ONLY way tables are created in runtime envs).
set -euo pipefail
cd "$(dirname "$0")/.."
alembic upgrade head
echo "migrations applied"
