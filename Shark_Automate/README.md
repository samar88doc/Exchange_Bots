# TV → Shark Exchange Automated Trading Platform

Professional-grade automated trading platform: **TradingView watchlist alerts → secure webhook → validation → symbol normalization → idempotency → risk control → paper / live engine → Shark Exchange → position monitoring → +10% ROI take-profit**.

> **Default mode is PAPER.** LIVE requires a manual, multi-gate activation and never happens automatically.

---

## Quick start (Docker — one command)

```bash
cp .env.example .env          # edit WEBHOOK_SECRET at minimum
docker compose up --build
```

| Service   | URL                                        |
| --------- | ------------------------------------------ |
| Dashboard | http://localhost:3000                       |
| API       | http://localhost:8000                       |
| Health    | http://localhost:8000/api/health            |
| Webhook   | `POST http://<host>:8000/api/webhooks/tradingview` |

## Quick start (local, no Docker)

Requirements: Python 3.12+, Node 20+, PostgreSQL 14+, (optional) Redis.

```bash
# 1. database
createdb trading            # or use docker compose up -d postgres redis

# 2. backend
cd backend
python3 -m venv .venv && source .venv/bin/activate  # recommended
python3 -m pip install -U pip setuptools wheel      # REQUIRED if pip < 23 / setuptools < 68
pip install -e ".[dev]"
alembic upgrade head        # the ONLY way tables are created (no runtime DDL)
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 3. frontend (second terminal)
cd frontend
npm install
npm run dev                 # http://localhost:3000
```

Or simply: `./scripts/dev-up.sh`

## Run the tests

```bash
cd backend
python -m pytest -q                       # 68 tests: unit, integration, security, concurrency, acceptance TEST 1–18
python -m pytest tests/acceptance -q      # the 18 specification acceptance tests only
```

## TradingView configuration

1. In TradingView: create your alert from each Pine strategy (2 watchlist alerts).
2. Alert dialog → **Notifications → Webhook** → URL: `http://<your-host>:8000/api/webhooks/tradingview`
3. Message template (must match your Pine payload):

```json
{
  "event_id": "{{timenow}}-{{ticker}}",
  "strategy_id": "strategy_1",
  "strategy_name": "Strategy 1",
  "symbol": "{{ticker}}",
  "action": "BUY",
  "timestamp": "{{timenow}}",
  "timeframe": "{{interval}}",
  "price": {{close}},
  "source": "TRADINGVIEW",
  "secret": "<your WEBHOOK_SECRET>"
}
```

`{{ticker}}` on perpetual charts delivers values like `BASUSDT.P` — the raw value is
preserved and normalized (`.P` stripped) **before** any risk/duplicate/order work.
See [docs/TRADINGVIEW_INTEGRATION.md](docs/TRADINGVIEW_INTEGRATION.md).

## Verify PAPER mode

1. Dashboard shows a blue **● PAPER MODE** banner.
2. `GET /api/status` → `"mode": "PAPER"`.
3. Send a test alert (below) → signal appears in *Signals* with raw/canonical/shark triple, a paper position opens in *Positions*.
4. No network call to `POST /v1/order/place-order` is ever made (Shark adapter not used in PAPER).
5. Paper report: *Settings → Generate Paper Test Report*.

```bash
curl -X POST http://localhost:8000/api/webhooks/tradingview \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"manual-1","strategy_id":"strategy_1","symbol":"BASUSDT.P","action":"BUY","timestamp":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'","secret":"<WEBHOOK_SECRET>"}'
```

## Enable LIVE (manual, multi-gate — never automatic)

1. Trade PAPER for ~2 trading days; review the paper report.
2. `cp .env.live .env` with `LIVE_TRADING_ENABLED=true`, `LIVE_TRADING_CONFIRMATION=<secret>`, Shark API key/secret, `SHARK_DRY_RUN=true` first.
3. Restart; dashboard → **Settings / Go Live** → *Generate Paper Test Report* → fill the GO LIVE panel:
   the exact phrase `ENABLE LIVE TRADING`, the secret confirmation, and acknowledgements of
   margin 120 / 10x / max 3 / ROI 10% / **STOP LOSS DISABLED**.
4. With `SHARK_DRY_RUN=true` the exact Shark payloads are logged + displayed but **not submitted**.
5. Set `SHARK_DRY_RUN=false` only after verifying the payloads.

Full procedure: [docs/LIVE_TRADING.md](docs/LIVE_TRADING.md).

## Repository layout

```
backend/    FastAPI + SQLAlchemy 2.x + Alembic (app/, tests/, alembic/)
frontend/   Next.js trading terminal (app/, components/, lib/)
docs/       Architecture & operations documentation
deploy/     Extra deployment assets
scripts/    dev-up.sh
docker-compose.yml  .env.example .env.paper .env.live
```

## Documentation index

| Doc | Content |
| --- | --- |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, diagrams, state machines |
| [API.md](docs/API.md) | REST + webhook + SSE reference |
| [TRADING_LOGIC.md](docs/TRADING_LOGIC.md) | Entry/exit flow, ROI math, sizing |
| [RISK_ENGINE.md](docs/RISK_ENGINE.md) | 3-position limit, dupes, reservations, kill switch |
| [PAPER_TRADING.md](docs/PAPER_TRADING.md) | Paper engine, slippage/fees/funding |
| [LIVE_TRADING.md](docs/LIVE_TRADING.md) | Safety gates, dry run, go-live workflow |
| [SHARK_INTEGRATION.md](docs/SHARK_INTEGRATION.md) | Verified Shark API surface + limitations |
| [TRADINGVIEW_INTEGRATION.md](docs/TRADINGVIEW_INTEGRATION.md) | Alert setup, payload templates |
| [SYMBOL_MAPPING.md](docs/SYMBOL_MAPPING.md) | Normalization rules + mapping table |
| [SECURITY.md](docs/SECURITY.md) | Secrets, webhook auth, rate limits |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Docker, backups, restore |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common issues and fixes |

## Example: symbol normalization workflow

```
TradingView alert symbol:   BASUSDT.P
   ↓ store raw (never discarded)
   ↓ strip ONLY terminal ".P"
canonical symbol:           BASUSDT
   ↓ resolve symbol_mappings (verified against Shark exchangeInfo)
shark symbol:               BASUSDT

Shark order payload:        {"symbol": "BASUSDT", ...}   ← NEVER "BASUSDT.P"
```

## Example: paper-trading workflow

Alert `BUY BTCUSDT.P` → webhook 202 → signal stored (triple preserved) → slot reserved
(OPEN+RESERVED ≤ 3) → qty = 1200/price on contract precision → simulated market fill
(with slippage/fees) → position OPEN/MONITORING → ROI on **margin** tracked against
real market prices → at **+10% ROI** (= +1% price at 10x) → reduce-only close → trade
recorded (gross/net P&L, ROI **and** price Δ) → slot freed.

## Financial-safety guarantees

* PAPER by default; LIVE requires env gate + secret + typed phrase + config acknowledgements.
* **OPEN + RESERVED ≤ 3** enforced under a database lock with UNIQUE constraints — 4 simultaneous signals can never all open.
* Duplicate events (retries, replays) are suppressed by UNIQUE `event_id` + deterministic fingerprint.
* `BASUSDT` and `BASUSDT.P` are the **same position identity** (canonical symbol).
* Shark orders always use `shark_symbol`; submission is never blindly retried (uncertain outcomes go to `UNKNOWN` + reconciliation).
* Stop loss is disabled by design; risk sizing is never silently changed to force an order through.
