# Shasha_Act

**PAPER-first TradingView SHASHA signal executor for Shark Exchange** — an isolated project inside the existing `Exchange_Bots` repository. It does not replace or modify `Shark_Automate/`, `mudrex_auto/`, `mudrex_roi/`, or `pi42_roi/`.

TradingView (Binance prices) is only a **trigger**. The traded price is the Shark Exchange price:
the close of the last **completed 5-minute candle** for that contract, quantized to the contract's
exact price fraction (for example `0.00239`).

> **Safety status:** PAPER is the only mode that can currently execute the strategy. LIVE activation is intentionally blocked. Current official Shark material does not provide enough verified detail for exact contract tick/quantity-step rules and split TP/SL replacement/OCO behavior. Do not set either `SHARK_CONTRACT_RULES_VERIFIED` or `SHARK_SPLIT_PROTECTION_VERIFIED` to `true` until those rules are independently verified against current official documentation and a non-production account. This project never submits real orders in tests or its default configuration.

## What is included

- Tokenized TradingView webhook (`/api/webhooks/tradingview/{token}`), bounded request size, per-process rate limit, JSON plus constrained legacy-text parsing, stable deduplication, and durable event persistence.
- Strict `.P`-suffix normalization only; no automatic `USDT` suffixing. Shark exchange metadata must match an active USDT-quoted perpetual contract. TradingView and Shark symbols are stored separately.
- **LONG (BUY) and SHORT (SELL) signals.** Both sides use the same closed-candle entry, percentage levels, break-even step and trailing ratchet, mirrored for the short side. See [Strategy](#strategy-the-exact-rules).
- Server-side risk gates: reserved position slots, one active exposure per contract, symbol allow/deny filters, notional limits, market-local day-loss check, emergency pause, and stale-entry handling.
- Exact-Decimal order levels: stop at **2% below** the Shark entry price, **Target 1 at +2% closing 50%** of the position, then the stop moves to **break-even** and **trails 1% behind every new 1% high**. Optional fixed Target 2/3 ladder remains available but is off by default.
- Entry provenance is stored per trade (`entry_price_source`, `entry_candle_time`, `price_tick`) so the dashboard can always show where the executed price came from.
- Dashboard sign-in with **username and password** (PBKDF2-hashed, opaque `X-Session-Token` sessions), a **subscription** record with plan/fee/expiry, and an operator **risk disclaimer** gate.
- PAPER simulator whose code has no Shark private-order client. PAPER fills are explicitly simulated from Shark public last-price data and are not real execution.
- Durable SQL outbox with leases, idempotent work keys and retry states for trade processing, Telegram, reconciliation and cancellation.
- Authenticated dashboard/API, operator actions with typed confirmations, event/order/audit history, CSV export, health status, and explicit stale/unavailable market and P&L states.
- PostgreSQL migrations with tables scoped to the `shasha_act_*` namespace.

## Strategy (the exact rules)

| Step | Rule |
|---|---|
| 1. Trigger | A TradingView alert arrives (Binance-based price). The alert is only a trigger; its price is recorded as `signal_close`, never traded. |
| 2. Entry | The bot immediately reads the last **completed** 5-minute candle for that contract on Shark and uses its close as the entry price, quantized to the contract's exact price fraction (e.g. `0.00239`). A LONG limit buy is rounded down and a SHORT limit sell is rounded up, so neither is placed on the wrong side of the candle close. A stale or unavailable candle rejects the signal instead of guessing a price (`ENTRY_ALLOW_LIVE_TICKER_FALLBACK=false` by default). |
| 3. Stop loss | **LONG:** 2% below entry. **SHORT:** 2% above entry. Submitted reduce-only together with the entry and confirmed before the position is reported as protected. |
| 4. Target 1 | **LONG:** 2% above entry. **SHORT:** 2% below entry. Closes **50%** of the position. |
| 5. Break-even | As soon as Target 1 is filled, the stop moves to the entry price, so the runner cannot lose money on the trade. |
| 6. Trailing stop | Thereafter the stop trails **1% behind every new favourable extreme**: 1% behind the **highest** price for a LONG, 1% above the **lowest** price for a SHORT (extreme stored per trade). The stop only ever moves toward profit and never back. |

**SHORT mirror:** a SHORT signal is a sell entry. It profits when price falls, so the stop sits above the entry, Target 1 sits below it, and the trailing stop ratchets **down** as price makes new lows. Stop-outs on a short are triggered by a price rise, and gap fills take the worse observed price for that side. Position P&L is `(entry − price) × quantity` for a SHORT and `(price − entry) × quantity` for a LONG.

Secondary fixed Target 2 / Target 3 exits exist but are disabled by default, so the runner follows
the trailing stop instead. Percentages apply to the underlying price, not to leveraged account ROI.

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

For PAPER, an accepted signal is queued as a limit entry at the **Shark 5-minute closed-candle close** (never the TradingView price). A simulated LONG entry is created only when a fresh public last-price update is at or below that limit; a simulated SHORT entry only when it is at or above that limit. This is a simulation rule, not proof that a real order would fill. After the fill, the 2% stop, the 50% Target 1 slice, the break-even step and the 1% trailing ratchet are all simulated from fresh public prices and recorded with audit events.

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

Edit only your local `backend/.env` file. Never commit it. Use a PostgreSQL database dedicated to this application and a least-privilege database role. Generate a high-entropy `TRADINGVIEW_WEBHOOK_TOKEN` locally, for example with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`. Set `DASHBOARD_USERNAME` and a strong `DASHBOARD_PASSWORD`: the first account is bootstrapped with the `OWNER` role. Operators always use username/password; the browser is never asked to paste a server token. For a secret-manager-free first deployment only, set `INITIAL_OWNER_SIGNUP_ENABLED=true`, create the sole first owner through the sign-up screen, then set it back to `false` and restart. An optional `DASHBOARD_API_TOKEN` is strictly a server-to-server automation credential, not a dashboard login method. A browser session is stored only for the browser session unless **Keep me signed in** is selected; that option uses local storage and still has a finite server-side expiry. Owners can revoke sessions by resetting a password or disabling an account.

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

Open `http://localhost:8000/` and sign in with `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`. The first sign-in asks for the risk disclaimer, then use **Playbook** and **Settings → Risk parameters** to review the server-enforced rules. `/api/health` and `/api/ready` are safe health endpoints; dashboard data/actions require an authenticated operator session. `/api/auth/signup` is available only for the one-time initial-owner setup described above.

For production, set `ENVIRONMENT=production`, use PostgreSQL, configure a dashboard username/password and the webhook token, terminate HTTPS at a trusted proxy, and ensure the proxy/ASGI server correctly sets the external HTTPS scheme. Do not trust arbitrary forwarded headers from the public internet. Keep access logging disabled or configure the reverse proxy to redact the tokenized webhook path; the webhook token is part of the URL. Do not enable a broad CORS policy. Framing is denied by default; set an explicit trusted frame policy only if embedding the console is required.

## TradingView alert setup

See [`docs/tradingview.md`](docs/tradingview.md) and the minimal Pine block in [`docs/tradingview-alert-template.pine`](docs/tradingview-alert-template.pine).

The alert block references the existing `shashaAlertConditionCurrent` variable and uses exactly `alert.freq_once_per_bar_close`. It does not redefine the SHASHA condition. Send JSON fields:

- `strategy`: `SHASHA`
- `event_id`: stable for the same signal candle; the template derives it from ticker, timeframe and candle time
- `symbol`: `syminfo.ticker`, such as `BTCUSDT.P` (not `syminfo.tickerid` with an exchange prefix)
- `direction`: `LONG` (buy signal) or `SHORT` (sell signal). `action` may be used instead: `BUY`/`LONG` or `SELL`/`SHORT`. An alert whose `direction` and `action` disagree is rejected, and so is any exit-style action such as `CLOSE`, which is never turned into a new SHORT entry.
- `timeframe`, `bar_time`
- `signal_close`: current signal candle close
- `entry_price`: previous candle close (`close[1]`)

Set the TradingView webhook URL to `https://<your-host>/api/webhooks/tradingview/<your-webhook-token>`. Keep the token private. Do not send a guessed symbol or price; missing/invalid fields are durably recorded as rejected events and never become an order.

The receiver strips only one case-insensitive trailing `.P`, then requires exact contract metadata matching. It does not infer a quote asset, remove an exchange prefix, or append `USDT`.

## Dashboard and controls

The console is a single-page **neural-schema dashboard**: an animated overview with a live execution-path schema, KPI cards, a performance chart, a system-status matrix, positions with a visual protection rail (stop / entry / target / last price), pending entries, public markets with market-day change, the signal/outbox log, history with CSV export, the strategy playbook with a worked example, account/subscription, settings and operator access control. It displays prices as public **last price**, not mark price. Day P&L is based on the configured market-local calendar day (default `Asia/Kolkata`), uses persisted fills/fees, and labels unavailable funding separately. It does not use the exchange's rolling 24-hour percentage change as day P&L.

Operator controls include:

- **Pause/resume new signals** with typed phrases. A pause does not close trades or cancel existing orders.
- **Cancel pending entries** with typed confirmation. A cancellation is reported only after exchange confirmation; ambiguous results remain visible for reconciliation.
- **Close position** with typed confirmation. PAPER closes are simulated at a fresh last price; any future LIVE close remains pending until Shark confirms its final state.
- **Edit PAPER risk levels** with typed confirmation and validation against a fresh last price. LIVE position-level risk edits are disabled while exchange protection rules remain unverified.
- **Retry failed Telegram delivery** without creating a new trade.
- **Strategy rules** (entry candle interval, stop %, Target 1 %, Target 1 close %, trailing %) with server-side validation and an audit entry for every change.
- **Manage the subscription** (plan, fee, currency, cycle) and **operator accounts** (create, role, enable/disable, per-account plan and expiry) when the signed-in role allows it.

A status such as `ENTRY_PENDING` is not an open position. `UNKNOWN`, `PROTECTION_ERROR`, stale prices, missing fee/funding data and failed outbox work remain visible rather than being replaced with fabricated values.

### Navigation, live values and session behaviour

- **Every page has its own URL.** The open view is mirrored into the URL fragment (`/`, `#positions`,
  `#markets`, `#signals`, `#history`, `#strategy`, `#account`, `#settings`, `#users`). A refresh, a
  bookmark or a shared link reopens exactly that page instead of dropping the operator on the
  Overview, and the fragment is updated as the operator navigates.
- **Live values move on their own.** Last price, unrealized P/L, the position KPIs and the pending
  entries are refreshed about once per second, next to the slower full refresh. The Positions page
  shows a `LIVE PRICES 1s` marker and a timestamp of the last update.
- **A refresh never signs the operator out.** A saved session survives a browser reload, and a
  rendering, data or network failure is reported in place. The credential is dropped only when the
  server itself rejects the presented session, and the console warns shortly before a session
  expires so an expiry is never a surprise.
- **Every control answers its click.** Pause, cancel-pending, cancel-entry, close, mode switches and
  the refresh buttons show progress while the request is in flight and then report the server's
  answer.

## Risk defaults

Initial database settings are PAPER, 100 USDT margin/trade, 10x leverage, 3 open slots, a **2% stop**, **Target 1 at +2% closing 50%**, break-even then a **1% trailing stop**, secondary Target 2/3 exits disabled, 1,000 USDT notional/trade cap, 200 USDT daily loss cap, and a 15-minute pending-entry expiry. These are editable from the authenticated dashboard. They are guardrails, not a promise of execution or P&L. Percentages apply to the underlying price, not leveraged account ROI; fees, funding and slippage are excluded from strategy illustrations.

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

- [`docs/tradingview.md`](docs/tradingview.md) — Pine alert template and receiver contract (TradingView triggers; Shark prices execute)
- [`docs/api.md`](docs/api.md) — endpoints, auth and response semantics
- [`docs/shark-api-verification.md`](docs/shark-api-verification.md) — official documentation inspected and fail-closed gaps
- [`docs/operations.md`](docs/operations.md) — runbook, state model, troubleshooting and security
- [`backend/migrations/`](backend/migrations/) — Alembic schema history
- [`backend/.env.example`](backend/.env.example) — placeholder-only local configuration template
