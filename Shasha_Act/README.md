# Shasha_Act

**PAPER-first TradingView SHASHA signal receiver for Shark Exchange** — an isolated project inside the existing `Exchange_Bots` repository. It does not replace or modify `Shark_Automate/`, `mudrex_auto/`, `mudrex_roi/`, or `pi42_roi/`.

> **Safety status:** PAPER is the only mode that can currently execute the strategy. LIVE activation is intentionally blocked. Current official Shark material does not provide enough verified detail for exact contract tick/quantity-step rules and split TP/SL replacement/OCO behavior. Do not set either `SHARK_CONTRACT_RULES_VERIFIED` or `SHARK_SPLIT_PROTECTION_VERIFIED` to `true` until those rules are independently verified against current official documentation and a non-production account. This project never submits real orders in tests or its default configuration.

## What is included

- Tokenized TradingView webhook (`/api/webhooks/tradingview/{token}`), bounded request size, per-process rate limit, JSON plus constrained legacy-text parsing, stable deduplication, and durable event persistence.
- Strict `.P`-suffix normalization only; no automatic `USDT` suffixing. Shark exchange metadata must match an active USDT-quoted perpetual contract. TradingView and Shark symbols are stored separately.
- Server-side risk gates: reserved position slots, one active exposure per contract, symbol allow/deny filters, notional limits, market-local day-loss check, emergency pause, and stale-entry handling.
- Decimal-based order levels and `Q1 + Q2 + Q3 = Q` sizing. Previous-candle close is stored as planned limit entry; signal-candle close remains separate.
- PAPER simulator whose code has no Shark private-order client. PAPER fills are explicitly simulated from Shark public last-price data and are not real execution.
- Durable SQL outbox with leases, idempotent work keys and retry states for trade processing, Telegram, reconciliation and cancellation.
- Authenticated dashboard/API, operator actions with typed confirmations, event/order/audit history, CSV export, health status, and explicit stale/unavailable market and P&L states.
- PostgreSQL migrations with tables scoped to the `shasha_act_*` namespace.

## Architecture and state flow

```text
TradingView HTTPS webhook
        │ authenticate → parse → dedupe → reserve risk slot (single DB transaction)
        ▼
PostgreSQL: signal event + trade reservation + outbox jobs
        │
        ├── worker: PAPER simulator (no private order API) / guarded Shark executor
        ├── worker: Telegram notification with durable retries
        └── reconciler: paper ticker processing / Shark order & position state
                         │
                         ▼
                 dashboard + health API
```

For PAPER, an accepted signal is queued as a limit entry at the supplied previous-candle close. A simulated entry is created only when a fresh public last-price update is at or below that limit; this is a simulation rule, not proof that a real order would fill. Targets/stop changes and simulated fills are stored with audit records.

LIVE controls require all of the following: the server-side `LIVE_TRADING_ENABLED` gate, configured Shark private credentials, deliberate dashboard confirmation, verified Shark contract rules, verified split-protection semantics, and an exchange metadata match. **The current implementation fails those verification gates closed, and the dashboard LIVE action returns HTTP 409.** Do not bypass this by editing the database.

## Requirements

- Python 3.11+
- PostgreSQL for production; SQLite is supported for isolated tests only
- A TLS-terminating reverse proxy for production webhook/dashboard traffic
- Optional Telegram bot and chat configuration

## Local setup

From the project directory:

```bash
cd Shasha_Act/backend
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Edit only your local `backend/.env` file. Never commit it. Use a PostgreSQL database dedicated to this application and a least-privilege database role. Generate two unrelated random tokens locally, for example with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`, and set them as `DASHBOARD_API_TOKEN` and `TRADINGVIEW_WEBHOOK_TOKEN`. The dashboard token is entered into the browser at sign-in and held in memory only; it is not written to browser storage.

Set up the PostgreSQL database/role using your normal secrets manager, then set `DATABASE_URL` to a SQLAlchemy async URL, for example:

```text
postgresql+asyncpg://<db-role>:<db-password>@<db-host>:5432/<db-name>
```

Apply schema migrations:

```bash
alembic upgrade head
```

Run the application locally (PAPER remains the default):

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
```

Open `http://localhost:8000/`, enter the dashboard token, and use **Settings → Risk parameters** to review the server-enforced limits. `/api/health` and `/api/ready` are safe health endpoints; all dashboard data/actions require `X-Dashboard-Token`.

For production, set `ENVIRONMENT=production`, use PostgreSQL, configure the dashboard and webhook tokens, terminate HTTPS at a trusted proxy, and ensure the proxy/ASGI server correctly sets the external HTTPS scheme. Do not trust arbitrary forwarded headers from the public internet. Keep access logging disabled or configure the reverse proxy to redact the tokenized webhook path; the webhook token is part of the URL. Do not enable a broad CORS policy.

## TradingView alert setup

See [`docs/tradingview.md`](docs/tradingview.md) and the minimal Pine block in [`docs/tradingview-alert-template.pine`](docs/tradingview-alert-template.pine).

The alert block references the existing `shashaAlertConditionCurrent` variable and uses exactly `alert.freq_once_per_bar_close`. It does not redefine the SHASHA condition. Send JSON fields:

- `strategy`: `SHASHA`
- `event_id`: stable for the same signal candle; the template derives it from ticker, timeframe and candle time
- `symbol`: `syminfo.ticker`, such as `BTCUSDT.P` (not `syminfo.tickerid` with an exchange prefix)
- `direction`: `LONG`
- `timeframe`, `bar_time`
- `signal_close`: current signal candle close
- `entry_price`: previous candle close (`close[1]`)

Set the TradingView webhook URL to `https://<your-host>/api/webhooks/tradingview/<your-webhook-token>`. Keep the token private. Do not send a guessed symbol or price; missing/invalid fields are durably recorded as rejected events and never become an order.

The receiver strips only one case-insensitive trailing `.P`, then requires exact contract metadata matching. It does not infer a quote asset, remove an exchange prefix, or append `USDT`.

## Dashboard and controls

The dashboard provides overview, positions, public markets, signal/outbox log, history/CSV, risk settings and service health. It displays prices as public **last price**, not mark price. Day P&L is based on the configured market-local calendar day (default `Asia/Kolkata`), uses persisted fills/fees, and labels unavailable funding separately. It does not use the exchange's rolling 24-hour percentage change as day P&L.

Operator controls include:

- **Pause/resume new signals** with typed phrases. A pause does not close trades or cancel existing orders.
- **Cancel pending entries** with typed confirmation. A cancellation is reported only after exchange confirmation; ambiguous results remain visible for reconciliation.
- **Close position** with typed confirmation. PAPER closes are simulated at a fresh last price; any future LIVE close remains pending until Shark confirms its final state.
- **Edit PAPER risk levels** with typed confirmation and validation against a fresh last price. LIVE position-level risk edits are disabled while exchange protection rules remain unverified.
- **Retry failed Telegram delivery** without creating a new trade.

A status such as `ENTRY_PENDING` is not an open position. `UNKNOWN`, `PROTECTION_ERROR`, stale prices, missing fee/funding data and failed outbox work remain visible rather than being replaced with fabricated values.

## Risk defaults

Initial database settings are PAPER, 100 USDT margin/trade, 10x leverage, 3 open slots, 1%/2%/3% underlying price targets, 2% stop, 1,000 USDT notional/trade cap, 200 USDT daily loss cap, and 15-minute pending-entry expiry. These are editable from the authenticated dashboard. They are guardrails, not a promise of execution or P&L. Target percentages apply to the underlying price, not leveraged account ROI.

## Checks

From `Shasha_Act/backend`:

```bash
ruff check app tests migrations
mypy --explicit-package-bases app
pytest -q
DATABASE_URL='sqlite+aiosqlite:////tmp/shasha-act-migrations.db' alembic upgrade head
DATABASE_URL='sqlite+aiosqlite:////tmp/shasha-act-migrations.db' alembic check
```

The test suite uses fake tokens, SQLite and mocked HTTP. It does not call Telegram or Shark private order endpoints. SQLite verifies test behavior only; deploy on PostgreSQL.

## Documentation

- [`docs/tradingview.md`](docs/tradingview.md) — Pine alert template and receiver contract
- [`docs/api.md`](docs/api.md) — endpoints, auth and response semantics
- [`docs/shark-api-verification.md`](docs/shark-api-verification.md) — official documentation inspected and fail-closed gaps
- [`docs/operations.md`](docs/operations.md) — runbook, state model, troubleshooting and security
- [`backend/migrations/`](backend/migrations/) — Alembic schema history
- [`backend/.env.example`](backend/.env.example) — placeholder-only local configuration template
