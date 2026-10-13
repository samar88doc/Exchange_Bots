# Deployment

## Docker (recommended)

```bash
cp .env.example .env    # set WEBHOOK_SECRET; keep TRADING_MODE=PAPER
docker compose up --build
```

Services: `postgres` (16), `redis` (7), `backend` (FastAPI :8000, runs
`alembic upgrade head` at start), `frontend` (Next.js :3000, proxies `/api` to
the backend).

## Bare metal

```bash
# postgres + redis running, database `trading` created
cd backend
python3 -m venv .venv && source .venv/bin/activate     # recommended
python3 -m pip install -U pip setuptools wheel          # REQUIRED if pip < 23 / setuptools < 68
pip install -e ".[dev]"
alembic upgrade head                       # tables are created ONLY by migrations
uvicorn app.main:app --host 0.0.0.0 --port 8000

cd frontend && npm install && npm run dev  # or npm run build && npm run start
```

`scripts/dev-up.sh` runs both.

## Environment matrix

| File | Purpose |
| --- | --- |
| `.env.example` | documented template |
| `.env.paper` | PAPER-safe defaults (copy to `.env`) |
| `.env.live` | LIVE config skeleton (fill secrets; copy to `.env` only deliberately) |

LIVE secrets must never load accidentally: `.env.live` is only used when copied
deliberately; `LIVE_TRADING_ENABLED` defaults to `false` everywhere else.

## PostgreSQL backup & restore

```bash
cd backend
./scripts/db.sh backup                    # -> backups/trading_<UTC>.sql.gz
./scripts/db.sh restore backups/trading_20260921T101500Z.sql.gz
```

Underlying commands (documented for manual use):

```bash
pg_dump "$DATABASE_URL" | gzip > backups/trading_$(date -u +%Y%m%dT%H%M%SZ).sql.gz
gunzip -c backups/<file>.sql.gz | psql "$DATABASE_URL"
```

⚠ **Trading logs and audit logs must not be accidentally deleted during
maintenance.** `trades`, `orders`, `fills`, `position_events`, `audit_logs`,
`system_events`, `webhook_events`, `reconciliation_events` are append-only
business records — exclude them from any ad-hoc cleanup; use the backup/restore
flow above for retention management.

## Migrations

```bash
cd backend
alembic upgrade head       # apply
alembic revision --autogenerate -m "change"   # authoring (review the diff!)
```

Never run runtime DDL — the application does not create tables at runtime.

## Workers / processes

A single `uvicorn app.main:app` process hosts API + asyncio workers (ROI monitor,
reconciler, maintenance). Scale to multiple API processes safely: the reservation
path is protected by `SELECT … FOR UPDATE` + UNIQUE constraints across processes.
