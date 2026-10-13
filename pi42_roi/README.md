# Pi42 Signal Trader

Listens to a private Telegram signal channel (via Telethon) and turns explicit
signals into **LIMIT** perpetual-futures orders on **Pi42**, with an **ROI-based
take-profit** as the only exit — the same design as the `mudrex_roi` project, on
Pi42's API.

> **NO STOP-LOSS ENABLED — liquidation and substantial loss of allocated margin are possible.**
>
> This bot never submits a stop-loss (there is no stop-loss parameter anywhere in its
> Pi42 client). Positions are protected only by leverage limits, sizing, and the margin cap.
> A position can be liquidated and losses can exceed the margin you expected to lose.
> `MAX_MARGIN_PER_TRADE` (default 50) caps *allocated margin*; it is **not** a maximum loss.

## Status

- **PAPER mode is the default.** It simulates fills and P&L locally and never calls a
  mutating Pi42 endpoint (the client is constructed read-only and raises otherwise).
- **REAL mode is implemented but has not been exercised against a live Pi42 account.**
  Response schemas beyond the public documentation are unverified. Run `check`, a
  `contracts` review, and a long PAPER trial before considering REAL.

## What it does

1. Connects to Telegram as your user account (MTProto) and reads **new** messages from the
   channel only. Edited messages are ignored. History present at first start is never executed.
2. Parses explicit signals: `#SYMBOL/USDT` (or `/INR`), `Long/Buy` or `Short/Sell`, `Entry`,
   `Target 1` (required), optional `Stop Loss` (recorded as reference only, never sent), and
   `Leverage: xN`. Direction comes only from explicit words, never from emoji.
3. Enforces the configured market: with `PI42_MARKET=USDT` a `#CRV/INR` signal is rejected;
   prices are **never converted** between currencies. The Pi42 contract must match exactly
   (`CRVUSDT`), never a similar name.
4. Rejects stale signals (`MAX_SIGNAL_AGE_MINUTES`), future-dated signals, entries further
   than `MAX_ENTRY_DEVIATION_PCT` from the live price, and duplicate content within 24 h.
5. Sets leverage with `POST /v1/exchange/update/preference` to
   `min(telegram_leverage, contract_maxLeverage)` (integer, always rounded **down**) together
   with `PI42_MARGIN_MODE`, and requires Pi42's response to confirm both. If it does not,
   no order is placed.
6. Sizes the position from `MAX_MARGIN_PER_TRADE − MARGIN_RESERVE` (default 48 of the margin
   asset), rounded **down** to Pi42's quantity precision. It never raises size to meet a
   minimum; it rejects instead.
7. Places a **LIMIT** entry (insufficient balance/size is rejected, never resized) carrying a
   take-profit at `entry ± TARGET_ROI_PCT` (default ±8 %), snapped to Pi42's price precision,
   plus a deterministic `refId`. It never sends `stopLossPrice`.
8. Monitors open trades: cancels unfilled entries after `LIMIT_ORDER_TTL_MINUTES` (never a
   filled one), confirms the take-profit from the order echo, and — if the market reaches the
   ROI target before Pi42 closes the position itself — squares the position off with a
   **reduce-only MARKET** order placed from the position (`placeType=POSITION`).

Every order carries a deterministic `refId` (`TG-{message_id}-{hash}`). Each Telegram message
is processed at most once (unique `(channel_id, message_id)`). Mutating requests are never
blindly retried; a lost response is reconciled by `refId`, then by the Pi42-returned
`clientOrderId`, then by a strict order-history fingerprint. If the outcome stays unknown the
trade is parked as `NEEDS_REVIEW` and is **not** resubmitted.

The bot has **no** withdrawal, deposit, or transfer code: funding the Pi42 futures wallet is
something you do yourself in the Pi42 app.

## Project layout

```
app/        main (CLI), config, logging_config, telegram_listener, signal_parser,
            symbol_mapper, pi42_client, leverage_manager, risk_manager,
            execution_engine, paper_broker, position_monitor, storage, notifications,
            models, engine_factory, numeric, dashboard
tests/      unit + safety tests (run offline; Pi42 is faked, signatures verified)
data/       local database, Telegram session (gitignored)
scripts/    setup.sh, run_paper.sh, start.sh, stop.sh, status.sh, dashboard.sh, reset_trades.sh
deploy/     optional systemd user unit (PAPER only)
```

## Setup

Requires Python 3.11+ (developed and tested on 3.11).

```bash
bash scripts/setup.sh          # venv, dependencies, DB schema, test suite
cp .env.example .env           # if setup did not already do it
```

Edit `.env` **on your own machine**. Never paste secrets into chat or commit them.
`.gitignore` excludes `.env`, `data/*` (except `.gitkeep`), `*.session`, and `*.db`.

### 1. Telegram API credentials
Create an application at https://my.telegram.org → *API development tools*. Put `TELEGRAM_API_ID`,
`TELEGRAM_API_HASH`, and `TELEGRAM_PHONE` in `.env`. Your account must already be a member of
the signal channel (the invite link in `.env.example`). Then log in once, interactively:

```bash
source .venv/bin/activate
python -m app.main auth
```

The login code is asked locally and the session is written to
`data/telegram_session.session`. Keep that file private.

### 2. Pi42 API key
Create a key in the Pi42 API-management screen, then:

- **Whitelist your server's public IP.** Pi42 rejects signed requests from other addresses.
- Copy the key id into `PI42_API_KEY` and the secret into `PI42_API_SECRET`.
- PAPER needs only **read** access (contract metadata, wallet read, public prices).
- REAL needs trading permission. Never grant withdrawal or transfer permission.
- Requests are signed with HMAC-SHA256 over the exact query string (GET) or body (POST/DELETE);
  the bot handles this for you, including the millisecond `timestamp`.

### 3. Fund the futures wallet
Pi42 keeps a **funding wallet** and a **futures wallet**. Positions are margined from the
futures wallet, so transfer the margin you intend to use there in the Pi42 app. The bot only
ever *reads* wallet balances; `check` and `status` tell you what the futures wallet holds.

## Run in PAPER mode (default)

```bash
source .venv/bin/activate
python -m app.main check --telegram   # config, Pi42 read-only check, Telegram channel
python -m app.main contracts          # review the contracts and their leverage caps
scripts/start.sh                      # start in the background (forces PAPER)
scripts/status.sh                     # is it running? + simulated results
tail -f data/bot.log                  # live log (Ctrl+C here does NOT stop the bot)
scripts/stop.sh                       # stop the bot cleanly
```

Foreground alternative (stops when the terminal closes): `bash scripts/run_paper.sh`.

The log banner reads `PAPER MODE — NO LIVE ORDERS WILL BE PLACED`, followed by the
no-stop-loss warning. PAPER fills a LIMIT when the price crosses the entry (at the limit
price), triggers the ROI take-profit only on ticks after the fill, expires unfilled entries
after the TTL, and approximates liquidation on isolated margin. Fees are a flat rate; funding,
order-book depth, and slippage are not modelled.

### Script reference

| Script | What it does |
|---|---|
| `scripts/setup.sh` | one-time: virtualenv, dependencies, database, tests |
| `scripts/start.sh` | start the bot in the background; PID in `data/bot.pid`, log in `data/bot.log` |
| `scripts/stop.sh` | stop it (SIGTERM, then SIGKILL after 30 s); refuses to signal any other process |
| `scripts/status.sh` | running state and database summary |
| `scripts/reset_trades.sh` | delete **PAPER** trades (dry run by default; `--yes` to delete). Needs the bot stopped. |
| `scripts/run_paper.sh` | run in the foreground (PAPER forced, even if `.env` says REAL) |
| `scripts/dashboard.sh` | start the lightweight web dashboard (default: http://localhost:8500) |

### Resetting PAPER trades

```bash
scripts/stop.sh
scripts/reset_trades.sh          # dry run: shows what would be deleted
scripts/reset_trades.sh --yes    # deletes PAPER trades only
```

A backup is written to `data/backups/trading-<timestamp>.db` first. Reset keeps **signals**
(so an old Telegram message can never execute again after a reset), **REAL trades**, the
Telegram session, and your `.env`. The simulated balance returns to `PAPER_INITIAL_BALANCE`.

## REAL trading (read carefully)

REAL needs **all** of the following, or the process refuses to start (exit code 2):

| Gate | Value |
|---|---|
| `TRADING_MODE` | `REAL` |
| `ENABLE_REAL_TRADING` | `true` |
| `LIVE_TRADING_ACKNOWLEDGEMENT` | `I ACCEPT NO-STOP-LOSS LIVE TRADING RISK` (exact) |
| `PI42_API_KEY` / `PI42_API_SECRET` | valid key with trading permission, IP-whitelisted |
| Telegram session | completed `auth` |
| Read-only Pi42 checks | futures wallet and contract metadata succeed |
| Interactive confirmation | you type `START LIVE TRADING` in a **terminal** (TTY) |

Non-interactive or service starts in REAL are refused (`scripts/start.sh` only ever starts
`run --yes` after its own TTY confirmation; `deploy/` is PAPER-only).

Before REAL: keep PAPER running long enough to see real signals flow, check `status`, then set
`MAX_MARGIN_PER_TRADE` deliberately. Keep `MARGIN_RESERVE` above zero.

## Operations

| Command | Purpose |
|---|---|
| `python -m app.main status` | signals, trades, PAPER results, open positions, events |
| `python -m app.main dashboard` | web dashboard at http://localhost:8500 |
| `python -m app.main kill` / `resume` | stop / allow new trades (kill-switch file). Monitoring continues. |
| `TRADING_ENABLED=false` in `.env` | same kill switch, persistent across restarts |
| `python -m app.main resolve-trade ID --status CANCELLED --note "..."` | close out a `NEEDS_REVIEW` trade manually |
| `python -m app.main parse-file message.txt` | parse a saved message offline (no settings needed) |
| `scripts/reset_trades.sh --yes` | delete PAPER trades (bot stopped; backup first) |

Exit codes: `2` gate or confirmation refused, `3` read-only check failed, `4` fatal listener error.
Logs go to stdout and to the event log in SQLite.

Optional: `deploy/pi42-signal-trader.service` runs PAPER as a systemd user service.

### Dashboard

`python -m app.main dashboard` serves a zero-dependency (standard-library) dark-mode terminal:

- live positions with the ROI target pipeline, unrealized PnL and margin loss percentage;
- open/working Pi42 orders plus the bot's scheduled ROI exit legs;
- recent Telegram messages with the full raw text and the decision reason;
- ledger (simulated in PAPER, real Pi42 futures wallet in REAL) and closed-trade history;
- activity log, and manual actions: edit the ROI target, close a trade, cancel an order.

Manual actions call Pi42 directly and are written to the local database. Editing a target
amends the working order (`PATCH /v1/order/edit-order`) or attaches a split take-profit to the
open position (`POST /v2/order/split-tp-sl`); no stop-loss is ever attached.

## Configuration

See `.env.example` for every variable with comments. Key values:

| Variable | Default | Meaning |
|---|---|---|
| `TRADING_MODE` | `PAPER` | `PAPER` or `REAL` |
| `PI42_MARKET` | `USDT` | quote currency traded; other quotes are rejected |
| `PI42_MARGIN_ASSET` | `USDT` | margin currency (BTCINR can be margined in `USDT`) |
| `PI42_MARGIN_MODE` | `ISOLATED` | `ISOLATED` or `CROSS` |
| `PI42_SEND_REF_ID` | `true` | send the deterministic `refId` (idempotent submissions) |
| `MAX_MARGIN_PER_TRADE` | `50` | initial margin cap per trade (not max loss) |
| `MARGIN_RESERVE` | `2` | reserve kept out of the margin budget |
| `TARGET_ROI_PCT` | `8.0` | ROI take-profit attached to the entry order |
| `MAX_SIGNAL_AGE_MINUTES` | `5` | older signals are rejected |
| `MAX_ENTRY_DEVIATION_PCT` | `3` | max distance of the signal entry from the live price |
| `LIMIT_ORDER_TTL_MINUTES` | `120` | unfilled entries are cancelled after this |
| `MAX_CONCURRENT_POSITIONS` | `50` | open trades + pending entries cap |
| `MONITOR_POLL_SECONDS` | `15` | reconciliation/tick interval |
| `PI42_REQUESTS_PER_MINUTE` | `50` | client-side rate limit (Pi42 allows 60/min) |

## Tests

```bash
python -m pytest -q
python -m ruff check .
```

Tests run fully offline against a fake Pi42 HTTP transport (`tests/helpers.py`) that **verifies
the HMAC signature of every request** against the exact bytes sent, so a re-serialised body
fails the suite instead of the live account. The fake also mimics Pi42's error codes
(`3018` insufficient margin, `3019` duplicate refId, `2305` leverage locked by an open
position, `3005`, …). Coverage includes the parser fixtures (CRV and EIGEN, which must never be
executed as historical signals), exact symbol mapping, leverage and sizing, the PAPER broker,
client safety (read-only default, no stop-loss fields, no automatic retry of mutations), the
execution engine's PAPER and REAL flows, REAL reconciliation (`refId` → `clientOrderId` →
fingerprint → `NEEDS_REVIEW`), TTL cancel safety, ROI square-off, gate and TTY confirmation
logic, the dashboard routes and the CLI helpers.

## Known limitations

- **Not validated against the live Pi42 API.** Endpoint shapes follow the published docs; the
  client raises a clear error rather than guessing when a payload differs.
- Pi42 generates the order `clientOrderId`; the bot stores the returned id. Client-supplied ids
  are only sent if Pi42 starts honouring them — that switch (`client_order_id`) is not exposed
  in `.env` on purpose.
- Take-profit confirmation is seen on the monitor cycle after a fill, not the same cycle.
- Reconciliation between cycles relies on polling, not the Pi42 websocket stream; the stream
  would be the next improvement for sub-second exits.
- The PAPER liquidation model is an approximation, not Pi42's engine.
- Exits use the **single ROI target** (`TARGET_ROI_PCT`). Extra Telegram targets (`Target 2..5`)
  are parsed and stored for reference, but do not create partial exits — exactly like
  `mudrex_roi`. Pi42's `split-tp-sl` endpoint makes a ROI ladder possible later.
- No stop-loss is ever placed. Losses are bounded only by the margin you allocate and by Pi42's
  own liquidation behaviour.
- Single channel, single account, single process. Do not run two instances against one database.

## Safety summary

- PAPER by default, and PAPER cannot obtain a mutating client (enforced in `engine_factory`).
- No stop-loss, ever. No MARKET entries; only the ROI square-off uses MARKET (reduce-only).
- Prices, quantities and leverage are snapped **down**/**to grid**, never silently moved.
- Direction only from explicit words. Edited, stale, future-dated and historical messages are ignored.
- Mutations are never retried; unknown outcomes are reconciled or parked for a human.
- No withdrawal, deposit, or transfer code exists anywhere in the project.
