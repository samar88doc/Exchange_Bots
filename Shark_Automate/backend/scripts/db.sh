#!/usr/bin/env bash
# Create + restore documented PostgreSQL backups. Trading/audit logs preserved.
set -euo pipefail
ACTION="${1:-backup}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
DB_URL="${DATABASE_URL:-postgresql+asyncpg://trading:trading@localhost:5432/trading}"
# convert asyncpg URL -> libpq URL
PGURL="$(echo "$DB_URL" | sed 's/+asyncpg//; s/^postgresql:/postgresql:/')"
mkdir -p "$BACKUP_DIR"

case "$ACTION" in
  backup)
    FILE="$BACKUP_DIR/trading_$STAMP.sql.gz"
    pg_dump "$PGURL" | gzip > "$FILE"
    echo "backup written: $FILE"
    ;;
  restore)
    FILE="$2"
    if [[ -z "${FILE:-}" ]]; then echo "usage: db.sh restore <file.sql.gz>"; exit 1; fi
    gunzip -c "$FILE" | psql "$PGURL"
    echo "restored: $FILE"
    ;;
  *)
    echo "usage: db.sh [backup|restore <file>]"; exit 1 ;;
esac
