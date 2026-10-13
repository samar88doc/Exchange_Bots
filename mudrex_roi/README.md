# Mudrex Signal Trader

Listens to a private Telegram signal channel (via Telethon) and turns explicit
signals into **LIMIT** perpetual-futures orders on Mudrex, with Target 1 as the
only take-profit.

> **NO STOP-LOSS ENABLED — liquidation and substantial loss of allocated margin are possible.**
>
> This bot never submits a stop-loss. Positions are protected only by leverage limits and
> the margin cap. A position can be liquidated, and losses can exceed the margin you expect
> to lose. The per-trade margin cap (`MAX_MARGIN_USDT_PER_TRADE`, default 50 USDT) limits
> *allocated margin*; it is **not** a maximum loss.

## Status

- **PAPER mode is the default.** It simulates fills and P&L locally and never calls a mutating
  Mudrex endpoint.
- **REAL mode is implemented but has not been exercised against the live Mudrex API.** Mudrex
  order-detail response schemas are not yet verified against a live account. Run `check` and a
  small PAPER trial before you consider REAL.

## What it does

1. Connects to Telegram as your user account (MTProto) and reads **new** messages from the
   channel only. Edited messages are ignored. History present at first start is not executed.
2. Parses explicit signals: `#SYMBOL/USDT`, `Long/Buy` or `Short/Sell`, `Entry`, `Target 1`
   (required), optional `Stop Loss` (recorded as reference only, never sent), `Leverage: xN`.
   Direction comes only from those words, never from emoji.
3. Rejects signals with missing or invalid Target 1, a LONG target at or below entry (or SHORT
   at or above), conflicting duplicate values, ranges, non-USDT quotes, or non-positive leverage.
4. Rejects stale signals (`MAX_SIGNAL_AGE_MINUTES`) and entries deviating from market by more
   than `MAX_ENTRY_DEVIATION_PCT`. Stale signals are never queued.
5. Sets leverage to `min(telegram_leverage, asset_max)`, snapped down onto the asset's
   `min_leverage + k × leverage_step` grid, and never above the Telegram level.
6. Sizes the position from `MAX_MARGIN_USDT_PER_TRADE − MARGIN_RESERVE_USDT` (default 48 USDT of
   margin), rounded down to the asset's quantity step. It never raises size to meet a minimum; it
   rejects instead.
7. Places a **LIMIT** order at the signal Entry with Target 1 as `takeprofit_price`. It never sends
   `is_stoploss` or `stoploss_price`.
8. Monitors open trades, cancels unfilled entries after `LIMIT_ORDER_TTL_MINUTES` (never a filled
   one), and records outcomes in SQLite.

Every order uses a deterministic `client_order_id` (`TG-{message_id}-{hash}`). Each Telegram
message is processed at most once (unique `(channel_id, message_id)`). Mutating requests are never
blindly retried. After a timeout, the bot looks the order up by `client_order_id` first. If the
outcome stays unknown, the trade goes to `NEEDS_REVIEW` and is not resubmitted.

The bot has **no** withdrawal, deposit, or transfer code, and never calls transfer endpoints.

## Project layout

```
app/        main (CLI), config, logging_config, telegram_listener, signal_parser,
            symbol_mapper, mudrex_client, leverage_manager, risk_manager,
            execution_engine, paper_broker, position_monitor, storage, notifications,
            models, engine_factory, numeric
tests/      unit + safety tests (run offline; Mudrex is faked)
data/       local database, Telegram session (gitignored)
scripts/    setup.sh, run_paper.sh
deploy/     optional systemd user unit (PAPER only)
```

## Setup

Requires Python 3.12+ (developed and tested on 3.13).

```bash
bash scripts/setup.sh          # venv, dependencies, DB schema, test suite
cp .env.example .env           # if setup did not already do it
```

Edit `.env` **on your own machine**. Never paste secrets into chat or commit them.
`.gitignore` excludes `.env`, `data/*` (except `.gitkeep`), `*.session`, and `*.db`.

### 1. Telegram API credentials
Create an application at https://my.telegram.org → *API development tools*. Put `TELEGRAM_API_ID`,
`TELEGRAM_API_HASH`, and `TELEGRAM_PHONE` in `.env`. Your account must already be a member of
the signal channel (the invite link in `.env.example`).

Then log in once, interactively:

```bash
source .venv/bin/activate
python -m app.main auth
```

This asks for the login code locally and writes the session to `data/telegram_session.session`.
Keep that file private.

### 2. Mudrex API key
Create a key in your Mudrex account (API keys page). Mudrex shows two values:

- **API Key** (short ID): not used by this bot. Ignore it.
- **Secret Key**: copy it into `MUDREX_API_SECRET` in `.env`. The bot sends it as the
  `X-Authentication` header. Mudrex shows it only once. If you lose it, revoke the key and create a new one.

Do not paste either value into chat, screenshots, issues, or commits.
- PAPER mode needs only **read-only** scope (metadata and prices).
- REAL mode also needs trading scope. Do **not** grant withdrawal or transfer scope.
- Restrict the key by IP where the platform allows it.

## Run in PAPER mode (default)

```bash
source .venv/bin/activate
python -m app.main check --telegram   # config, Mudrex read-only check, Telegram channel
scripts/start.sh                      # start in the background (forces PAPER)
scripts/status.sh                     # is it running? + simulated results
tail -f data/bot.log                  # live log (Ctrl+C here does NOT stop the bot)
scripts/stop.sh                       # stop the bot cleanly
```

Foreground alternative (stops when the terminal closes): `bash scripts/run_paper.sh`.

The log banner reads `PAPER MODE — NO LIVE ORDERS WILL BE PLACED`, followed by the no-stop-loss
warning. PAPER fills a LIMIT order when price crosses the entry, and TP1 is checked on ticks
after the fill. The ledger is approximate: liquidation uses an isolated-margin estimate.

### Script reference

| Script | What it does |
|---|---|
| `scripts/setup.sh` | one-time: virtualenv, dependencies, database, tests |
| `scripts/start.sh` | start the PAPER bot in the background; PID in `data/bot.pid`, log in `data/bot.log` |
| `scripts/stop.sh` | stop it (SIGTERM, then SIGKILL after 30 s); refuses to signal any other process |
| `scripts/status.sh` | running state and database summary |
| `scripts/reset_trades.sh` | delete **PAPER** trades (dry run by default; `--yes` to delete). Needs the bot stopped. |
| `scripts/run_paper.sh` | run in the foreground (PAPER forced) |
| `scripts/dashboard.sh` | start the lightweight web status dashboard (default: http://localhost:8500) |

### Resetting PAPER trades

```bash
scripts/stop.sh
scripts/reset_trades.sh          # dry run: shows what would be deleted
scripts/reset_trades.sh --yes    # deletes PAPER trades only
```

Before deleting, a backup is written to `data/backups/trading-<timestamp>.db`. Reset keeps:
- **signals**, so an old Telegram message can never execute again after a reset;
- **REAL trades**, if any exist;
- the Telegram session and your `.env`.

After a reset, the simulated balance returns to `PAPER_INITIAL_BALANCE_USDT`.

## REAL trading (read carefully)

REAL needs **all** of the following, or the process refuses to start (exit code 2):

| Gate | Value |
|---|---|
| `TRADING_MODE` | `REAL` |
| `ENABLE_REAL_TRADING` | `true` |
| `LIVE_TRADING_ACKNOWLEDGEMENT` | `I ACCEPT NO-STOP-LOSS LIVE TRADING RISK` (exact) |
| `MUDREX_API_SECRET` | valid key with trading scope |
| Telegram session | completed `auth` |
| Read-only Mudrex checks | balance and asset metadata succeed |
| Interactive confirmation | you type `START LIVE TRADING` in a **terminal** (TTY) |

Non-interactive or service starts in REAL are refused. Do not use `deploy/` for REAL.

Before REAL: run PAPER for a while, check `status`, and set `MAX_MARGIN_USDT_PER_TRADE`
deliberately. Keep `MARGIN_RESERVE_USDT` (default 2) above zero.

## Operations

| Command | Purpose |
|---|---|
| `scripts/status.sh` / `python -m app.main status` | running state, signals, trades, PAPER results, events |
| `python -m app.main kill` / `resume` | stop / allow new trades (kill switch file). Monitoring continues. |
| `TRADING_ENABLED=false` in `.env` | same kill switch, persistent across restarts |
| `python -m app.main resolve-trade ID --status S --note "..."` | close out a `NEEDS_REVIEW` trade manually |
| `scripts/reset_trades.sh --yes` | delete PAPER trades (bot must be stopped; backup is taken first) |

Exit codes: `2` gate or confirmation refused, `3` read-only check failed, `4` fatal listener error.
Logs go to stdout and the event log in SQLite.

Optional: `deploy/mudrex-signal-trader.service` runs PAPER as a systemd user service.

## Configuration

See `.env.example` for every variable with comments. Key values:

| Variable | Default | Meaning |
|---|---|---|
| `TRADING_MODE` | `PAPER` | `PAPER` or `REAL` |
| `MAX_MARGIN_USDT_PER_TRADE` | `50` | initial margin cap per trade (not max loss) |
| `MARGIN_RESERVE_USDT` | `2` | reserve kept out of the margin budget |
| `MAX_SIGNAL_AGE_MINUTES` | `5` | older signals are rejected |
| `MAX_ENTRY_DEVIATION_PCT` | `3` | max distance of entry from market |
| `LIMIT_ORDER_TTL_MINUTES` | `120` | unfilled entries are cancelled after this |
| `MAX_CONCURRENT_POSITIONS` | `3` | open trades + pending entries cap |

## Tests

```bash
python -m pytest -q
ruff check app tests --select E,F,W,B,I --ignore E501
```

Tests run offline using a fake Mudrex transport. They cover the parser fixtures (CRV and EIGEN,
which must **never** be executed as historical signals), symbol mapping, leverage and sizing,
PAPER broker, Mudrex client safety (read-only default, no stop-loss fields, no retry of
mutations), the execution engine, REAL monitor reconciliation, Telegram listener behaviour, and
REAL gate and confirmation logic.

## Known limitations

- Not validated against the live Mudrex API. The order-detail schema
  (`/fapi/v1/futures/orders/detail`) is still unverified. The client surfaces an error rather than
  guessing if fields differ.
- Take-profit confirmation takes effect on the monitor cycle after a fill, not the same cycle.
- The PAPER liquidation model is an approximation, not Mudrex's engine.
- No stop-loss is ever placed. Losses are bounded only by the margin you allocate and by Mudrex's
  own liquidation behaviour.
- Single channel, single account, single process. Do not run two instances against one database.

## Safety summary

- PAPER by default. There is no silent fallback from PAPER to REAL, and having credentials does
  not activate REAL.
- No stop-loss, ever. No MARKET entries. Only Target 1 is used. Prices are never adjusted silently.
- Direction only from explicit words. Edited, stale, and historical messages are ignored.
- No withdrawal, deposit, or transfer code.
